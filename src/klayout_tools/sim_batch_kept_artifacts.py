"""Make a kept ``batch`` artifact tree usable on the submitting host (#7905).

``options.keep_artifacts`` on ``--backend batch`` brings the job's
``artifacts/`` tree home (``sim_batch._install_batch_artifacts``). Two things
in it still describe the job instance rather than the host that asked:

* Every corner deck ``.include``s the job's own copy of the netlist
  (``/var/tmp/eda-job-<id>/inputs/netlist.cir``). Only ``outputs/`` is
  collected, so that path never exists locally and a kept deck cannot be
  re-run. :func:`rewrite_job_input_includes` points it at the netlist the run
  was submitted from -- the same file a ``--backend local`` deck includes.
* A corner whose engine log did not come back reports ``artifacts.log: null``,
  indistinguishable from a run that never kept logs.
  :func:`note_unretained_logs` says so explicitly, per corner.

Neither changes an existing JSON field: the rewrite touches file contents
only, and the note is one additive ``info`` diagnostic (``info`` never changes
a corner's ``status``).
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
from functools import partial
from pathlib import Path
from typing import Any

from .sim_batch_contract import BATCH_NETLIST_FILENAME, BatchError
from .sim_batch_hints import engine_log_not_retained

#: Files beside ``corner.cir`` that can name the job's netlist path.
_SIBLING_FILES = ("netlist.expanded.cir", ".spiceinit")

#: An absolute ``.include``/``.inc`` line, bare or quoted, as the deck writers
#: emit it (``.include {netlist_path}``). Job-side paths are POSIX.
_INCLUDE_LINE = re.compile(
    r"^[ \t]*\.(?:include|inc)[ \t]+(?P<quote>[\"']?)(?P<path>/[^\s\"']+)"
    r"(?P=quote)[ \t]*\r?$",
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


def _job_netlist_path(deck_text: str, expanded_path: str | None) -> str | None:
    """The job-side netlist path ``deck_text`` includes: the first absolute
    ``.include`` whose file is the staged netlist (``netlist.cir``). A token
    path (``$REQUEST_DIR/...`` from ``options.portable_artifacts``) is not
    absolute, so a portable record is left exactly as written."""
    for match in _INCLUDE_LINE.finditer(deck_text):
        if (
            match["path"] == expanded_path
            or posixpath.basename(match["path"]) == BATCH_NETLIST_FILENAME
        ):
            return match["path"]
    return None


def _quote_target(target: str, match: re.Match[str]) -> str:
    """Preserve the source delimiter when possible and protect spaces and comments."""
    original_quote = match["quote"]
    if original_quote and original_quote not in target:
        return f"{original_quote}{target}{original_quote}"
    if not any(c.isspace() or c == ";" for c in target):
        return target
    quote = "'" if '"' in target else '"'
    return f"{quote}{target}{quote}"


def rewrite_job_input_includes(
    corners: list[dict[str, Any]],
    client_netlist: str,
    installed: dict[Path, Path],
    *,
    remote_root: str,
) -> int:
    """Point each installed corner deck's job-side netlist ``.include`` at
    ``client_netlist``; return how many files were rewritten.

    The job-side path is learned from the corner's own ``corner.cir`` (the
    one generated, ``klt``-written deck), then replaced -- as an exact path,
    never a pattern -- in that deck and in the ``netlist.expanded.cir`` /
    ``.spiceinit`` beside it when they carry the same path. Decks are located
    through ``artifacts.deck``. Each deck and sibling must be a file copied
    from this collection. A corner with no deck, an unreadable deck, or no job-side
    include is skipped. A worker-side reference to the retained expanded deck
    is localized to that corner’s installed sibling. Model-library directives still
    name the worker's PDK root.
    """
    client = os.path.abspath(client_netlist)
    rewritten = 0
    for corner in corners:
        reported = (corner.get("artifacts") or {}).get("deck")
        if not isinstance(reported, str) or not posixpath.isabs(reported):
            continue
        # The report supplies only a lookup key. All filesystem paths come
        # from the current copy, including optional files beside the deck.
        relative = Path(posixpath.relpath(reported, remote_root))
        deck = installed.get(relative)
        if deck is None:
            continue
        deck_text = _read(deck)
        if deck_text is None:
            continue
        expanded = installed.get(relative.with_name("netlist.expanded.cir"))
        remote_expanded = posixpath.join(
            posixpath.dirname(reported), "netlist.expanded.cir"
        )
        job_path = _job_netlist_path(deck_text, remote_expanded if expanded else None)
        if job_path is None:
            continue
        target = str(expanded) if expanded and job_path == remote_expanded else client
        exact = re.compile(
            r"""(?P<quote>["']?)""" + re.escape(job_path) + r"(?P=quote)(?![\w./-])"
        )
        quote_target = partial(_quote_target, target)
        siblings = (installed.get(relative.with_name(name)) for name in _SIBLING_FILES)
        for path in (deck, *siblings):
            if path is None:
                continue
            text = deck_text if path == deck else _read(path)
            if text is None:
                continue
            updated = exact.sub(quote_target, text)
            if updated == text:
                continue
            with open(
                path, "w", encoding="utf-8", errors="surrogateescape", newline=""
            ) as fh:
                fh.write(updated)
            rewritten += 1
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
    corners: list[dict[str, Any]], *, job_id: str | None = None
) -> int:
    """Append one ``engine_log_not_retained`` ``info`` diagnostic to every
    corner of a kept batch run whose engine log is not on disk after the
    install; return how many were noted.

    "Not on disk" covers a report that names no log (``artifacts.log: null``)
    and one whose path is not a file locally (the ``artifacts/`` tree did not
    come back, so the reported path is still the job's own). ``job_id`` names
    the job (one per shard on a split run) whose ``outputs/`` prefix to
    inspect; the report never carries the bucket.
    """
    noted = 0
    for corner in corners:
        if _never_ran(corner):
            continue
        log = (corner.get("artifacts") or {}).get("log")
        if isinstance(log, str) and os.path.isfile(log):
            continue
        diagnostics = corner.get("diagnostics")
        if not isinstance(diagnostics, list):
            diagnostics = corner["diagnostics"] = []
        diagnostics.append(engine_log_not_retained(job_id))
        noted += 1
    return noted
