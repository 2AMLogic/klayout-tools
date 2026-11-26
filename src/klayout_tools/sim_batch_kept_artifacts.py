"""Make a kept ``batch`` artifact tree usable on the submitting host (#7905).

``options.keep_artifacts`` on ``--backend batch`` brings the job's
``artifacts/`` tree home (``sim_batch._install_batch_artifacts``). Two things
in it still describe the job instance rather than the host that asked:

* A corner deck can include the staged input netlist or a copy generated
  for that corner. Local replay needs the submitted netlist or the collected
  copy, with staged includes in the copy pointing to their original files.
* A worker can report a log path even when collection omits the log.
  :func:`note_unretained_logs` checks each corner against the files copied
  from this job.

Neither changes an existing JSON field: the rewrite touches file contents
only, and the note is one additive ``info`` diagnostic (``info`` never changes
a corner's ``status``).
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .sim_batch_contract import (
    BATCH_ARTIFACTS_DIRNAME,
    BATCH_NETLIST_FILENAME,
    BatchError,
)
from .sim_batch_hints import engine_log_not_retained
from .sim_remote import _rewrite_remote_artifact_paths

_GENERATED_NETLISTS = ("netlist.expanded.cir", "netlist.supply_waveforms.cir")

# Match only the directive and its argument, preserving trailing SPICE comments.
_INCLUDE = re.compile(
    r"""^(?P<lead>[ \t]*\.inc(?:lude)?[ \t]+)"""
    r"""(?P<arg>"[^"\r\n]*"|'[^'\r\n]*'|[^\s;]+)""",
    re.IGNORECASE | re.MULTILINE,
)


def recover_remote_root(corners: list[dict[str, Any]], marker: str) -> str | None:
    """``<$EDA_OUTPUT_DIR>/artifacts`` as the job instance spelled it, read
    off the first path-valued ``artifacts.*`` entry that contains ``marker``
    (``"/artifacts/"``). The harness picks ``$EDA_OUTPUT_DIR`` at boot and
    never reports it, but every reported path lies under it."""
    for corner in corners:
        for value in (corner.get("artifacts") or {}).values():
            if isinstance(value, str) and marker in value:
                return value[: value.rindex(marker) + len(marker) - 1]
    return None


def copy_collected_tree(source: str, local_root: str) -> dict[Path, Path]:
    """Map collected relative paths to their files under the resolved artifact root."""
    source_root = Path(source)
    installed: dict[Path, Path] = {}
    root = Path(local_root)
    root.mkdir(parents=True, exist_ok=True)

    def copy_entry(src: Path, dest: Path) -> None:
        if src.is_symlink() or dest.is_symlink():
            raise BatchError(
                f"cannot install a batch artifact through a symlink: {src} -> {dest}"
            )
        if src.is_dir():
            dest.mkdir(exist_ok=True)
            for child in src.iterdir():
                copy_entry(child, dest / child.name)
            shutil.copystat(src, dest)
        else:
            if dest.exists() and (not dest.is_file() or dest.stat().st_nlink > 1):
                raise BatchError(
                    f"cannot overwrite a special or multiply linked artifact: {dest}"
                )
            shutil.copy2(src, dest)
            installed[src.relative_to(source_root)] = dest

    # The caller may choose a symlink as the root. Pin its target once, then
    # reject links below it, including directory links encountered during copying.
    copy_entry(source_root, root.resolve())
    return installed


def _read(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
            return fh.read()
    except OSError:
        return None


def _include_path(match: re.Match[str]) -> str:
    arg = match["arg"]
    return arg[1:-1] if arg[0] in "\"'" else arg


def _job_netlist_path(deck: str, generated: dict[str, Path]) -> str | None:
    for match in _INCLUDE.finditer(deck):
        path = _include_path(match)
        if path in generated or (
            posixpath.isabs(path) and posixpath.basename(path) == BATCH_NETLIST_FILENAME
        ):
            return path
    return None


def _quote_target(target: str, original_quote: str) -> str:
    """Preserve the source delimiter when possible and protect spaces and comments."""
    if original_quote and original_quote not in target:
        return f"{original_quote}{target}{original_quote}"
    if not any(c.isspace() or c == ";" for c in target):
        return target
    quote = "'" if '"' in target else '"'
    return f"{quote}{target}{quote}"


def _localize_staged_includes(
    text: str, remote_inputs: str, sources: Callable[[], dict[str, str]]
) -> str:
    def replace(match: re.Match[str]) -> str:
        arg = match["arg"]
        directory, name = posixpath.split(_include_path(match))
        if directory != remote_inputs:
            return match[0]
        original = sources().get(name)
        if original is None:
            return match[0]
        quote = arg[0] if arg[0] in "\"'" else ""
        return match["lead"] + _quote_target(original, quote)

    return _INCLUDE.sub(replace, text)


def _rewrite_corner(
    deck: Path,
    reported: str,
    companions: dict[str, Path],
    client_netlist: str,
    remote_inputs: str,
    include_sources: Callable[[], dict[str, str]] | None,
) -> int:
    deck_text = _read(deck)
    if deck_text is None:
        return 0
    generated = {
        posixpath.join(posixpath.dirname(reported), name): companions[name]
        for name in _GENERATED_NETLISTS
        if name in companions
    }
    job_path = _job_netlist_path(deck_text, generated)
    if job_path is None:
        return 0  # Includes portable token decks and absent generated siblings.
    target = str(generated[job_path]) if job_path in generated else client_netlist
    exact = re.compile(
        r"""(?P<quote>["']?)""" + re.escape(job_path) + r"(?P=quote)(?![\w./-])"
    )

    rewritten = 0
    for path in (deck, *companions.values()):
        text = deck_text if path == deck else _read(path)
        if text is None:
            continue
        updated = exact.sub(lambda m: _quote_target(target, m["quote"]), text)
        if path.name in _GENERATED_NETLISTS and include_sources is not None:
            updated = _localize_staged_includes(updated, remote_inputs, include_sources)
        if updated != text:
            with open(
                path, "w", encoding="utf-8", errors="surrogateescape", newline=""
            ) as fh:
                fh.write(updated)
            rewritten += 1
    return rewritten


def rewrite_job_input_includes(
    corners: list[dict[str, Any]],
    client_netlist: str,
    installed: dict[Path, Path],
    *,
    remote_root: str,
    include_sources: Callable[[], dict[str, str]] | None = None,
) -> int:
    """Localize kept inputs using only files copied from this collection."""
    # The batch harness stages flat inputs beside outputs/artifacts.
    remote_inputs = posixpath.join(
        posixpath.dirname(posixpath.dirname(remote_root)), "inputs"
    )
    rewritten = 0
    for corner in corners:
        reported = (corner.get("artifacts") or {}).get("deck")
        if not isinstance(reported, str) or not posixpath.isabs(reported):
            continue
        relative = Path(posixpath.relpath(reported, remote_root))
        deck = installed.get(relative)
        if deck is None:
            continue
        companions = {
            name: path
            for name in (*_GENERATED_NETLISTS, ".spiceinit")
            if (path := installed.get(relative.with_name(name))) is not None
        }
        rewritten += _rewrite_corner(
            deck,
            reported,
            companions,
            os.path.abspath(client_netlist),
            remote_inputs,
            include_sources,
        )
    return rewritten


def _never_ran(corner: dict[str, Any]) -> bool:
    """A unit that never started an engine (budget, orphan, lost shard...)
    has no log to retain, so "not retained" would mislead."""
    from .sim_batch_resume import NEVER_RAN_CODES  # sim_batch_resume imports sim_batch

    return any(
        isinstance(d, dict) and d.get("code") in NEVER_RAN_CODES
        for d in corner.get("diagnostics") or []
    )


def note_unretained_logs(
    corners: list[dict[str, Any]],
    installed: dict[Path, Path],
    *,
    remote_root: str | None,
    job_id: str | None = None,
) -> int:
    """Flag and count corners whose reported log is absent from this collection.

    Match worker paths before the installer localizes the report. An earlier
    job may already have placed a log at the same destination.
    """
    noted = 0
    for corner in corners:
        if _never_ran(corner):
            continue
        log = (corner.get("artifacts") or {}).get("log")
        if (
            remote_root is not None
            and isinstance(log, str)
            and posixpath.isabs(log)
            and Path(posixpath.relpath(log, remote_root)) in installed
        ):
            continue
        diagnostics = corner.get("diagnostics")
        if not isinstance(diagnostics, list):
            diagnostics = corner["diagnostics"] = []
        diagnostics.append(engine_log_not_retained(job_id))
        noted += 1
    return noted


def install_batch_artifacts(
    corners: list[dict[str, Any]],
    *,
    collected_dir: str,
    artifacts_dir: str,
    client_netlist: str | None = None,
    include_sources: Callable[[], dict[str, str]] | None = None,
    job_id: str | None = None,
) -> None:
    """Install kept worker files and localize their report and deck paths (#7905)."""
    source = os.path.join(collected_dir, BATCH_ARTIFACTS_DIRNAME)
    installed = {}
    root = None
    if os.path.isdir(source):
        installed = copy_collected_tree(source, artifacts_dir)
        root = recover_remote_root(corners, f"/{BATCH_ARTIFACTS_DIRNAME}/")
    note_unretained_logs(corners, installed, remote_root=root, job_id=job_id)
    if root is not None:
        if client_netlist is not None:
            rewrite_job_input_includes(
                corners,
                client_netlist,
                installed,
                remote_root=root,
                include_sources=include_sources,
            )
        _rewrite_remote_artifact_paths(
            corners, remote_root=root, local_root=artifacts_dir
        )
