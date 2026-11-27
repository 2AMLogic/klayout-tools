"""Select fresh xschem outputs and publish a raw split export."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .output_lock import OutputLockError, hold_output_dir

#: Slack allowed when comparing the produced netlist's mtime against the
#: moment xschem was launched. Not cosmetic: HFS+ and some network/container
#: filesystems carry whole-second mtime granularity, so a file genuinely
#: written 10ms after launch can stat as having been written up to a second
#: *before* it. One second is the coarsest granularity we need to tolerate.
FRESHNESS_TOLERANCE_S = 1.0

# xschem pipes disk undo through gzip. On a full disk gzip names only stdout
# on write, but names the snapshot when it fails to read it back.
_UNDO_FAILURE_RE = re.compile(
    r"^(?:gzip: stdout: No space left on device"
    r"|gzip: .*/xschem_undo_[^/\n]+/undo\d+(?:\.gz)?: .+"
    r"|init_undo\(\): problems creating tmp undo dir.*"
    r"|push_undo\(\): failed to open write pipe.*"
    r"|pop_undo\(\): failed to open read pipe.*)$",
    re.MULTILINE,
)


class NetlistError(Exception):
    """A netlist export could not be completed (bad input, xschem missing,
    xschem hung, or no fresh output file produced).

    ``code`` is an optional machine-readable classification the CLI emits as
    the error envelope's ``error.code`` (``docs/json-contract.md``). It is
    ``None`` for every local failure; the ``batch`` backend sets it so a
    caller can tell a retryable capacity refusal or a runner-version mismatch
    from a bad schematic (see :mod:`klayout_tools.netlist_batch`).
    ``job_id`` names the batch job, when one was allocated. ``details`` holds
    extra structured fields the CLI merges into the error envelope's
    ``error`` object (e.g. a failed batch job's ``remote_stderr_tail``).
    """

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.code = code
        self.job_id = job_id
        self.details = details


def _fresh_files(work_dir: str, started_at: float) -> list[str]:
    """Names of files in ``work_dir`` whose mtime is at or after
    ``started_at`` (within :data:`FRESHNESS_TOLERANCE_S`), sorted."""
    fresh = []
    for name in sorted(os.listdir(work_dir)):
        path = os.path.join(work_dir, name)
        if not os.path.isfile(path):
            continue
        if os.stat(path).st_mtime >= started_at - FRESHNESS_TOLERANCE_S:
            fresh.append(name)
    return fresh


def _resolve_produced_netlist(
    work_dir: str,
    stem: str,
    started_at: float,
    *,
    exit_status: int,
    stdout: str,
    stderr: str,
    fresh: list[str] | None = None,
) -> str:
    """Decide whether the run succeeded, **without consulting the exit status**.

    xschem returns non-zero after a successful batch netlist (issue #55's
    first sharp edge). Require a fresh file in the private output directory
    and reject disk-undo errors, which can leave a partial hierarchy.
    Prefer xschem's usual name (``foo.sch`` -> ``foo.spice``). Accept a single
    fresh file under any other name and report it via ``netlist.path``.

    The freshness half matters as much as the existence half: without it, a
    stale netlist left behind by an earlier run would be read as this run's
    output -- the exact drift failure ``--check`` exists to catch.

    Raises :class:`NetlistError` if disk undo failed or no fresh file can be selected.
    """
    for stream in (stderr, stdout):
        failure = _UNDO_FAILURE_RE.search(stream)
        if failure:
            raise NetlistError(f"xschem disk undo failed: {failure.group()}")

    if fresh is None:
        fresh = _fresh_files(work_dir, started_at)
    expected = f"{stem}.spice"
    if expected in fresh:
        return os.path.join(work_dir, expected)
    if len(fresh) == 1:
        return os.path.join(work_dir, fresh[0])

    detail = (stdout or stderr or "").strip()
    if fresh:
        produced = (
            f"It produced {len(fresh)} files, none named {expected!r}: "
            f"{', '.join(fresh)}."
        )
    else:
        produced = f"Expected {expected!r}; the output directory is empty."
    raise NetlistError(
        "xschem produced no fresh netlist file. "
        + produced
        + " (xschem's exit status is deliberately not used as the verdict "
        "here -- it is non-zero even on success -- so this means the export "
        f"really did not happen. xschem exited {exit_status}."
        + (f" Its own output:\n{detail}" if detail else "")
        + ")"
    )


def export_destination(
    output: str | None,
    output_dir: str | None,
    check: bool,
    block: bool,
    relative_paths: bool,
    canonical_wrap: bool,
) -> str:
    """Validate the two destination contracts before xschem runs."""
    if (output is None) == (output_dir is None):
        raise NetlistError("choose exactly one of --output or --output-dir")
    if output is not None:
        return output
    assert output_dir is not None
    for active, option in (
        (check, "--check"),
        (block, "--block"),
        (relative_paths, "--relative-paths"),
        (canonical_wrap, "--canonical-wrap"),
    ):
        if active:
            raise NetlistError(f"--output-dir is incompatible with {option}")
    return output_dir


def split_warning(siblings: list[str]) -> dict[str, Any]:
    return {
        "code": "split_netlists_omitted",
        "files": siblings,
        "message": f"Omitted split netlists: {', '.join(siblings)}. "
        "Use --output-dir to export them.",
    }


def _stage_sibling(
    source: Path, incoming: Path, check_content: Callable[[bytes], list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    data = source.read_bytes()
    try:
        warnings = check_content(data)
    except NetlistError as exc:
        raise NetlistError(f"{source.name}: {exc}", code=exc.code) from exc
    (incoming / source.name).write_bytes(data)
    return [{**warning, "file": source.name} for warning in warnings]


def publish_split_netlists(
    work_dir: str,
    output_dir: str,
    top: str,
    top_data: bytes,
    siblings: list[str],
    check_content: Callable[[bytes], list[dict[str, Any]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Stage every file before publishing and restore prior outputs on write failure."""
    names = sorted([top, *siblings])
    warnings: list[dict[str, Any]] = []
    destination = Path(output_dir)
    try:
        with hold_output_dir(destination):
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination) as scratch:
                stage = Path(scratch)
                incoming = stage / "incoming"
                incoming.mkdir()
                backup = stage / "previous"
                backup.mkdir()
                (incoming / top).write_bytes(top_data)
                for name in siblings:
                    warnings.extend(
                        _stage_sibling(Path(work_dir) / name, incoming, check_content)
                    )
                for name in names:
                    target = destination / name
                    if target.is_symlink() or target.exists() and not target.is_file():
                        raise NetlistError(
                            f"cannot replace non-regular output: {target}"
                        )
                    if target.exists():
                        shutil.copy2(target, backup / name)
                _replace_staged(incoming, backup, destination, names)
    except OutputLockError as exc:
        raise NetlistError(
            f"could not publish netlists to {output_dir}: {exc}", code=exc.code
        ) from exc
    except OSError as exc:
        raise NetlistError(
            f"could not publish netlists to {output_dir}: {exc}"
        ) from exc
    return names, warnings


def _replace_staged(
    stage: Path, backup: Path, destination: Path, names: list[str]
) -> None:
    replaced = []
    try:
        for name in names:
            os.replace(stage / name, destination / name)
            replaced.append(name)
    except OSError:
        for name in reversed(replaced):
            if (backup / name).exists():
                os.replace(backup / name, destination / name)
            else:
                (destination / name).unlink()
        raise
