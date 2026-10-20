"""``klt netlist --backend batch``: run the xschem export on the batch runner
(issue #3182).

A fleet host that must not run xschem itself sets ``KLT_NETLIST_BACKEND=batch``
(or a caller passes ``--backend batch``), and the export is shipped to the
same S3 job contract ``klt sim --backend batch`` already uses. **This module
owns no transport of its own**: submit, launch, poll and collect, the
runner-version preflight, and the bucket/profile/provision-script resolution
are all :mod:`klayout_tools.sim_batch`'s, called as they are. What is
netlist-specific is small:

1. **What is uploaded.** The job instance holds no forge credential, so it
   cannot clone the repo. The submitter ships one ``tree.tar.gz`` built from
   the **git-tracked** files under a root (the schematic's repository by
   default, ``--batch-root`` to narrow it), refused above
   :data:`MAX_BATCH_TREE_BYTES` *before* the archive is built and before any
   S3 write. The schematic and a project ``--rcfile`` must be tracked files
   under that root. A PDK-relative ``--rcfile`` (``$PDK_ROOT/...``) is never
   uploaded: it is forwarded unexpanded and resolved on the runner, against
   the runner's own PDK -- a host PDK path is never shipped.
2. **What runs.** The job command extracts the tree, ``cd``\\ s into it and
   runs ``klt netlist ... --backend local`` there -- *explicitly* local, so a
   runner whose environment sets ``KLT_NETLIST_BACKEND`` cannot recurse. The
   forced xschem flags and the process-group kill therefore apply on the
   runner exactly as they do on a workstation; this module never builds an
   xschem argv. ``timeout_seconds`` is ``--timeout-s`` plus the batch
   overhead, so the inner ``klt`` timeout (and its actionable hang message)
   always fires before the harness's outer ``timeout``.
3. **What comes back.** The netlist is collected and written to ``-o`` --
   or, under ``--check``, compared on *this* host against the committed file
   with the same :func:`klayout_tools.netlist._check_result` a local run
   uses, which ignores the checkout-specific directories in xschem's
   ``** sch_path:`` / ``** sym_path:`` comments (issue #3010). Those comment
   lines are also rewritten from the runner's scratch directory to the
   caller's root, so a batch export reads as if it were made in this
   checkout.
4. **No fallback, ever.** Every batch failure -- unresolvable config, upload,
   launch, capacity refusal, poll timeout, a non-``done`` job, a preflight
   rejection, a missing report -- is a :class:`~klayout_tools.netlist.NetlistError`
   (exit 1) naming the job id and, when the runner reported one, its
   ``error.code``. Nothing here can launch xschem on this host; keeping
   xschem off the host is the reason the backend exists.

Unlike ``klt sim``'s host default, ``KLT_NETLIST_BACKEND=batch`` is never
stepped back to ``local`` for a "single unit": a netlist export is always one
unit, so that rule would make the setting a no-op.
"""

from __future__ import annotations

import fnmatch
import math
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import _provenance, remote_transport, sim_batch
from .netlist import (
    _PATH_COMMENT_RE,
    DEFAULT_XSCHEM_BINARY,
    SCHEMA_VERSION,
    STATUS_GENERATED,
    NetlistError,
    _check_no_unevaluated_expr,
    _check_no_unresolved_symbol,
    _check_result,
    _netlist_block,
    _sha256_file_hash,
    _validate_schematic,
    _write_output,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Host-default environment variable: ``--backend`` > ``$KLT_NETLIST_BACKEND``
#: > ``local``. Never yields back to ``local`` (see the module docstring).
BACKEND_ENV = "KLT_NETLIST_BACKEND"

#: Values ``--backend`` / ``$KLT_NETLIST_BACKEND`` accept.
SUPPORTED_BACKENDS = ("local", "batch")

#: Cap on the **uncompressed** total size of the git-tracked files uploaded
#: as the job's input tree (64 MiB). Schematics, symbols and an ``xschemrc``
#: are small; a tree above this almost always means the root also covers
#: layouts or other large tracked artifacts, so the fix is a narrower
#: ``--batch-root``, not a bigger upload.
MAX_BATCH_TREE_BYTES = 64 * 1024 * 1024

#: The single input the job receives under ``inputs/``.
BATCH_TREE_FILENAME = "tree.tar.gz"

#: Where the job command writes the netlist, under ``$EDA_OUTPUT_DIR``.
BATCH_NETLIST_FILENAME = "netlist.spice"

#: Where the job command records the absolute path(s) of the extracted tree
#: on the runner, under ``$EDA_OUTPUT_DIR`` -- what the client rewrites
#: xschem's path comments from.
BATCH_TREE_ROOT_FILENAME = "tree_root.txt"

#: ``job.json``'s ``tool`` label (``klt sim`` uses ``"ngspice"``).
BATCH_JOB_TOOL = "xschem"

#: ``--rcfile`` prefixes that mark a PDK-relative rcfile, resolved on the
#: runner against its own ``$PDK_ROOT``. ``$PDK`` / ``${PDK}`` in the rest of
#: the path is substituted with ``--pdk`` before submission.
PDK_ROOT_PREFIXES = ("$PDK_ROOT/", "${PDK_ROOT}/")

#: ``error.code`` values this backend sets itself (the transport's own
#: ``batch_no_capacity`` / ``batch_concurrency_cap`` and the runner's
#: ``batch_runner_version_*`` codes pass through unchanged).
BATCH_POLL_TIMEOUT_CODE = "batch_poll_timeout"
BATCH_JOB_FAILED_CODE = "batch_job_failed"
BATCH_JOB_TIMEOUT_CODE = "batch_job_timeout"
BATCH_INPUT_TOO_LARGE_CODE = "batch_input_too_large"

#: Appended to every batch failure message: the reason the backend exists.
_NO_FALLBACK = "(no local fallback: xschem was not run on this host)"


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def resolve_backend(flag: str | None) -> str:
    """``--backend`` > ``$KLT_NETLIST_BACKEND`` > ``"local"``.

    A hard routing choice: unlike ``klt sim``'s ``$KLT_SIM_BACKEND``, an
    env-sourced ``batch`` is never stepped back to ``local``. An unknown
    value from the environment is an error naming the variable (argparse
    already validates the flag).
    """
    if flag is not None:
        value, source = flag, "--backend"
    else:
        value = os.environ.get(BACKEND_ENV, "").strip() or "local"
        source = f"${BACKEND_ENV}"
    if value not in SUPPORTED_BACKENDS:
        raise NetlistError(
            f"{source} must be one of {', '.join(SUPPORTED_BACKENDS)} (got {value!r})"
        )
    return value


# --------------------------------------------------------------------------- #
# Input tree
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchTree:
    """The git-tracked input tree a batch export ships, fully validated.

    ``files`` are root-relative POSIX paths. ``rcfile_arg`` is the shell
    fragment the job command passes as ``--rcfile`` (a quoted root-relative
    path, or ``"$PDK_ROOT"/<rest>`` for a PDK-relative rcfile), ``None``
    without one.
    """

    root: str
    files: tuple[str, ...]
    total_bytes: int
    schematic: str
    rcfile_arg: str | None
    rcfile_pdk_relative: bool


def _git(cwd: str, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetlistError(f"could not run git in {cwd}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise NetlistError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout


def _resolve_root(schematic: str, batch_root: str | None) -> str:
    """``--batch-root`` (which must be inside a git checkout), else the
    top level of the schematic's own checkout."""
    if batch_root is not None:
        root = os.path.realpath(batch_root)
        if not os.path.isdir(root):
            raise NetlistError(f"--batch-root is not a directory: {batch_root}")
        _git(root, "rev-parse", "--show-toplevel")
        return root
    start = os.path.dirname(os.path.realpath(schematic))
    try:
        top = _git(start, "rev-parse", "--show-toplevel")
    except NetlistError as exc:
        raise NetlistError(
            f"--backend batch uploads the git-tracked files around the "
            f"schematic, but {schematic} is not in a git checkout ({exc}); "
            "pass --batch-root, or use --backend local"
        ) from exc
    return os.path.realpath(top.decode("utf-8", errors="surrogateescape").strip())


def _tracked_files(root: str) -> list[str]:
    raw = _git(root, "ls-files", "-z")
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in raw.split(b"\0")
        if item
    )


def _relative_tracked(
    path: str, root: str, tracked: set[str], *, what: str, flag: str
) -> str:
    """``path`` as a root-relative POSIX path, refusing one outside the root
    or not tracked by git (it would not be in the upload)."""
    rel = os.path.relpath(os.path.realpath(path), root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise NetlistError(
            f"{what} {path} is outside the batch root {root}; only files under "
            f"the root are uploaded -- pass a wider --batch-root{flag}"
        )
    rel = rel.replace(os.sep, "/")
    if rel not in tracked:
        raise NetlistError(
            f"{what} {path} is not tracked by git under {root}; --backend "
            "batch uploads only git-tracked files, so the runner would not "
            "have it -- commit (or `git add`) it first"
        )
    return rel


def _require_selected(rel: str, selected: set[str], *, what: str) -> None:
    if rel not in selected:
        raise NetlistError(
            f"{what} {rel} is left out of the upload by --batch-include / "
            f"--batch-exclude; the {what} must be in the selected file set"
        )


def _pdk_relative_rcfile(rcfile: str, pdk: str) -> str | None:
    """The runner-side ``--rcfile`` fragment for a PDK-relative rcfile, or
    ``None`` when ``rcfile`` is not in that form.

    ``$PDK``/``${PDK}`` in the rest of the path becomes ``--pdk`` here; any
    other ``$``, an absolute rest, or a ``..`` segment is refused, so the
    fragment can only name a file under the runner's ``$PDK_ROOT``.
    """
    for prefix in PDK_ROOT_PREFIXES:
        if rcfile.startswith(prefix):
            rest = rcfile[len(prefix) :]
            break
    else:
        return None
    rest = rest.replace("${PDK}", pdk).replace("$PDK", pdk)
    if (
        not rest
        or "$" in rest
        or rest.startswith("/")
        or ".." in rest.split("/")
        or "\0" in rest
    ):
        raise NetlistError(
            f"PDK-relative --rcfile {rcfile!r} must be $PDK_ROOT/<path under the "
            "PDK> with no other variables and no '..' segments (e.g. "
            "'$PDK_ROOT/$PDK/libs.tech/xschem/xschemrc')"
        )
    return '"$PDK_ROOT"/' + shlex.quote(rest)


_GLOB_CHARS = frozenset("*?[")


def _matches_glob(rel: str, pattern: str) -> bool:
    """``rel`` (a root-relative POSIX path) against one ``--batch-include`` /
    ``--batch-exclude`` pattern. ``fnmatch`` semantics, so ``*`` also crosses
    ``/``; a pattern with no glob character names a file or a directory and
    matches that file or everything under that directory."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not any(char in _GLOB_CHARS for char in pattern):
        bare = pattern.rstrip("/")
        return rel == bare or rel.startswith(bare + "/")
    return fnmatch.fnmatchcase(rel, pattern)


def _select_files(
    files: list[str], include: Sequence[str], exclude: Sequence[str]
) -> list[str]:
    """``files`` kept by ``include`` (every file when empty) and not dropped
    by ``exclude``."""
    return [
        rel
        for rel in files
        if (not include or any(_matches_glob(rel, pat) for pat in include))
        and not any(_matches_glob(rel, pat) for pat in exclude)
    ]


def package_tree(
    schematic: str,
    *,
    rcfile: str | None,
    pdk: str,
    batch_root: str | None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    max_bytes: int | None = None,
) -> BatchTree:
    """Select and validate the input tree, **without** building the archive.

    Raises :class:`NetlistError` -- before any archive is built or any S3
    write -- for a root outside git, a schematic or project rcfile outside
    the root or untracked, a schematic or project rcfile that ``include`` /
    ``exclude`` leaves out of the selected set, or an uncompressed total of
    the selected set above ``max_bytes`` (default
    :data:`MAX_BATCH_TREE_BYTES`).
    """
    if max_bytes is None:
        max_bytes = MAX_BATCH_TREE_BYTES
    root = _resolve_root(schematic, batch_root)
    files = _tracked_files(root)
    tracked = set(files)
    selected = set(_select_files(files, include, exclude))
    rel_schematic = _relative_tracked(
        schematic, root, tracked, what="schematic", flag=""
    )
    _require_selected(rel_schematic, selected, what="schematic")

    rcfile_arg: str | None = None
    pdk_relative = False
    if rcfile is not None:
        rcfile_arg = _pdk_relative_rcfile(rcfile, pdk)
        if rcfile_arg is not None:
            pdk_relative = True
        else:
            if not os.path.isfile(rcfile):
                raise NetlistError(f"xschemrc file not found: {rcfile}")
            rel_rcfile = _relative_tracked(
                rcfile,
                root,
                tracked,
                what="--rcfile",
                flag=(
                    ", or give a PDK rcfile in its PDK-relative form "
                    "'$PDK_ROOT/$PDK/libs.tech/xschem/xschemrc'"
                ),
            )
            _require_selected(rel_rcfile, selected, what="--rcfile")
            rcfile_arg = shlex.quote(rel_rcfile)

    present: list[str] = []
    total = 0
    for rel in files:
        if rel not in selected:
            continue
        path = os.path.join(root, rel)
        try:
            info = os.lstat(path)
        except OSError:
            continue  # tracked but deleted in the working tree
        if os.path.isdir(path) and not os.path.islink(path):
            continue  # a submodule's gitlink
        present.append(rel)
        total += info.st_size
    if total > max_bytes:
        raise NetlistError(
            f"--backend batch input tree under {root} is {total} bytes "
            f"uncompressed across {len(present)} git-tracked files, above the "
            f"{max_bytes}-byte cap; pass a narrower --batch-root (e.g. the "
            "directory holding the schematics, symbols and xschemrc), or "
            "select just the files the schematic needs with --batch-include "
            "<glob> / --batch-exclude <glob>",
            code=BATCH_INPUT_TOO_LARGE_CODE,
        )
    return BatchTree(
        root=root,
        files=tuple(present),
        total_bytes=total,
        schematic=rel_schematic,
        rcfile_arg=rcfile_arg,
        rcfile_pdk_relative=pdk_relative,
    )


def _scrub_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No submitting-host user or group names in the archive."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_tarball(tree: BatchTree, path: str) -> None:
    """Write ``tree``'s files into a gzip tarball at ``path``, with
    root-relative member names. Symlinks are stored as symlinks."""
    with tarfile.open(path, "w:gz") as archive:
        for rel in tree.files:
            archive.add(
                os.path.join(tree.root, rel),
                arcname=rel,
                recursive=False,
                filter=_scrub_tarinfo,
            )


# --------------------------------------------------------------------------- #
# job.json
# --------------------------------------------------------------------------- #


def _job_timeout_seconds(timeout_s: float) -> int:
    """``--timeout-s`` plus the same per-job overhead ``klt sim`` adds, so
    the harness's outer bound is always looser than the inner ``klt`` one."""
    return int(math.ceil(sim_batch._default_batch_job_timeout_s(1, timeout_s)))


def build_job_spec(
    tree: BatchTree,
    *,
    tarball_path: str,
    pdk: str,
    timeout_s: float,
    block: bool,
    client_version: str | None = None,
    version_check: str = "enforce",
) -> sim_batch.BatchJobSpec:
    """The netlist job as a :class:`~klayout_tools.sim_batch.BatchJobSpec`.

    ``cmd`` is the runner-version preflight, then: extract the tree into a
    fresh directory, record that directory's path, and run ``klt netlist``
    there with ``--backend local`` and ``--format json``, its report
    redirected to ``report.json`` and its stderr teed to ``stderr.log``
    (where klt writes an error envelope), the same channels ``klt sim``'s
    batch job uses. ``pdk_root`` is never set: the runner resolves its own.
    """
    version = (
        client_version if client_version is not None else _provenance._klt_version()
    )
    try:
        preflight = sim_batch._runner_preflight_script(
            version, enforce=version_check == "enforce"
        )
    except Exception as exc:  # SimError: a client version unsafe to embed
        raise NetlistError(str(exc)) from exc

    out = '"$EDA_OUTPUT_DIR/{}"'
    setup = " && ".join(
        [
            'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
            'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
            "w=$(mktemp -d)",
            f'tar -xzf "$EDA_INPUT_DIR/{BATCH_TREE_FILENAME}" -C "$w"',
            'cd "$w"',
            'printf \'%s\\n%s\\n\' "$w" "$(pwd -P)" >'
            + out.format(BATCH_TREE_ROOT_FILENAME),
        ]
    )
    argv = [
        "klt netlist",
        shlex.quote(tree.schematic),
        "-o",
        out.format(BATCH_NETLIST_FILENAME),
    ]
    if block:
        argv.append("--block")
    if tree.rcfile_arg is not None:
        argv += ["--rcfile", tree.rcfile_arg]
    argv += ["--pdk", shlex.quote(pdk), "--timeout-s", repr(float(timeout_s))]
    argv += ["--backend local --format json"]
    command = (
        preflight
        + setup
        + " && "
        + " ".join(argv)
        + f' > "$EDA_OUTPUT_DIR/{sim_batch.BATCH_REPORT_FILENAME}"'
        + f' 2> >(tee "$EDA_OUTPUT_DIR/{sim_batch.BATCH_STDERR_FILENAME}" >&2)'
        + "; rc=$?; wait $! 2>/dev/null; exit $rc"
    )
    return sim_batch.BatchJobSpec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk,
        timeout_seconds=_job_timeout_seconds(timeout_s),
        inputs=(
            sim_batch.BatchJobInput(
                name=BATCH_TREE_FILENAME,
                label="schematic tree",
                local_path=tarball_path,
            ),
        ),
    )


def new_job_id() -> str:
    """A fresh job id in the ``[A-Za-z0-9._-]+`` alphabet the launch script
    accepts, prefixed so a netlist job is recognisable in the bucket."""
    return f"klt-netlist-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Config and failure translation
# --------------------------------------------------------------------------- #


def _resolve_config(pdk: str, timeout_s: float) -> sim_batch.BatchConfig:
    """:func:`sim_batch._resolve_batch_config` itself, fed a synthetic
    request, so the fleet config resolves exactly as for ``klt sim``
    (``$KLT_BATCH_*``, then ``batch-fleet.env`` beside the provision script).
    Its ``SimError``/``BatchError`` becomes a :class:`NetlistError`."""
    from .sim import SimError

    try:
        return sim_batch._resolve_batch_config(
            {"models": {"pdk": pdk}}, corner_count=1, timeout_s=timeout_s
        )
    except (SimError, sim_batch.BatchError) as exc:
        raise NetlistError(
            f"batch backend: {exc} {_NO_FALLBACK}", code=getattr(exc, "code", None)
        ) from exc


def _runner_error(collected: str) -> tuple[dict[str, Any] | None, str]:
    """The runner klt's own error envelope (``message`` and maybe ``code``):
    from ``report.json`` (where the preflight writes its rejection), else
    from the tail of ``stderr.log`` (where ``klt netlist`` writes one)."""
    note = ""
    try:
        kind, error = sim_batch._classify_collected_report(
            sim_batch.read_collected_report(collected)
        )
        if kind == "error":
            return error, ""
    except sim_batch.BatchError as exc:
        note = str(exc)
    error, stderr_note = sim_batch._read_stderr_envelope(collected)
    if error is not None:
        return error, ""
    return None, "; ".join(item for item in (note, stderr_note) if item)


#: Bytes of ``stderr.log`` quoted back when the job left no klt envelope.
REMOTE_STDERR_TAIL_BYTES = 2048

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _remote_stderr_tail(collected: str) -> str:
    """The last :data:`REMOTE_STDERR_TAIL_BYTES` of the collected
    ``stderr.log`` as sanitized text: invalid UTF-8 replaced, ANSI escapes and
    control characters (other than newline and tab) dropped. ``""`` when the
    file is missing or empty. Only used when no klt envelope was recovered."""
    path = os.path.join(collected, sim_batch.BATCH_STDERR_FILENAME)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - REMOTE_STDERR_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return _CONTROL_CHARS.sub("", _ANSI_ESCAPE.sub("", text)).strip()


def _job_failure(
    *,
    job_id: str,
    status: dict[str, Any],
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str = "",
    harness_log: str | None = None,
) -> NetlistError:
    state = str(status.get("state") or "")
    exit_code = sim_batch._status_exit_code(status)
    detail = str(status.get("detail") or "").strip()
    head = f"batch job {job_id} finished {state!r}" + (
        f" (exit {exit_code})" if exit_code is not None else ""
    )
    if detail:
        head += f" ({detail})"
    if runner_error is not None:
        message = f"{head}: the runner's klt reported: {runner_error['message']}"
        code = runner_error.get("code")
    else:
        message = f"{head} without a usable report"
        if note:
            message += f" [output recovery: {note}]"
        message += (
            f" -- full log: {harness_log}"
            if harness_log
            else " -- see harness.log under the job's S3 prefix"
        )
        if stderr_tail:
            message += f"\nlast of the runner's stderr.log:\n{stderr_tail}"
        code = None
    if code is None:
        code = BATCH_JOB_TIMEOUT_CODE if state == "timeout" else BATCH_JOB_FAILED_CODE
    message += f" {_NO_FALLBACK}"
    details: dict[str, Any] = {}
    if runner_error is None:
        details["remote_exit_code"] = exit_code
        details["remote_state"] = state
        if harness_log:
            details["harness_log"] = harness_log
        if stderr_tail:
            details["remote_stderr_tail"] = stderr_tail
    return NetlistError(message, code=code, job_id=job_id, details=details or None)


# --------------------------------------------------------------------------- #
# Collected output
# --------------------------------------------------------------------------- #


def _read_tree_roots(collected: str) -> list[str]:
    try:
        with open(
            os.path.join(collected, BATCH_TREE_ROOT_FILENAME), encoding="utf-8"
        ) as handle:
            lines = [line.strip() for line in handle.read().splitlines()]
    except OSError:
        return []
    roots = [line.rstrip("/") for line in lines if line.startswith("/")]
    # Longest first, so a resolved path is rewritten before a prefix of it.
    return sorted(set(roots), key=len, reverse=True)


def _rewrite_runner_paths(
    data: bytes, runner_roots: list[str], local_root: str
) -> bytes:
    """Rewrite xschem's ``** sch_path:`` / ``** sym_path:`` comments that
    point into the runner's scratch tree so they point into ``local_root``
    -- the paths a local export from this checkout would have written.
    Paths outside the tree (the runner's PDK symbols) are left alone."""
    if not runner_roots:
        return data
    targets = [root.encode("utf-8", errors="surrogateescape") for root in runner_roots]
    local = local_root.rstrip("/").encode("utf-8", errors="surrogateescape")

    def _rewrite(match: Any) -> bytes:
        path = match.group(2)
        for target in targets:
            if path == target or path.startswith(target + b"/"):
                return match.group(1) + local + path[len(target) :]
        return match.group(0)

    return _PATH_COMMENT_RE.sub(_rewrite, data)


def _read_runner_report(collected: str, job_id: str) -> dict[str, Any]:
    try:
        report = sim_batch.read_collected_report(collected)
    except sim_batch.BatchError as exc:
        raise NetlistError(
            f"batch job {job_id}: {exc} {_NO_FALLBACK}",
            code=BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        ) from exc
    kind, error = sim_batch._classify_collected_report(report)
    if kind == "error" and error is not None:
        raise NetlistError(
            f"batch job {job_id}: the runner's klt reported: "
            f"{error['message']} {_NO_FALLBACK}",
            code=error.get("code") or BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    if (
        report.get("status") != STATUS_GENERATED
        or not isinstance(report.get("xschem"), dict)
        or not isinstance(report.get("netlist"), dict)
    ):
        raise NetlistError(
            f"batch job {job_id}: the collected {sim_batch.BATCH_REPORT_FILENAME} "
            f"is not a `klt netlist` generate report {_NO_FALLBACK}",
            code=BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    return report


def _read_collected_netlist(
    collected: str, report: dict[str, Any], job_id: str
) -> bytes:
    path = os.path.join(collected, BATCH_NETLIST_FILENAME)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise NetlistError(
            f"batch job {job_id}: collected outputs hold no "
            f"{BATCH_NETLIST_FILENAME} although the runner reported one "
            f"{_NO_FALLBACK}",
            code=BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        ) from exc
    expected = report["netlist"].get("content_hash")
    if expected is not None and expected != _sha256_file_hash(data):
        raise NetlistError(
            f"batch job {job_id}: the collected {BATCH_NETLIST_FILENAME} does "
            f"not match the content_hash the runner reported ({expected}) "
            f"{_NO_FALLBACK}",
            code=BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    return data


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _validate_batch_args(
    schematic: str,
    *,
    pdk: str | None,
    timeout_s: float,
    xschem_binary: str,
) -> str:
    """The local refusals, all before any packaging or upload. Returns the
    (required) PDK variant."""
    if xschem_binary != DEFAULT_XSCHEM_BINARY:
        raise NetlistError(
            "--xschem-binary names an executable on this host and cannot apply "
            "to --backend batch, which runs the runner image's own xschem; "
            "drop it, or use --backend local"
        )
    if not pdk:
        raise NetlistError(
            "--backend batch requires --pdk (it selects the runner image and the "
            "runner's PDK, e.g. --pdk sky130A)"
        )
    if timeout_s <= 0:
        raise NetlistError(f"--timeout-s must be positive (got {timeout_s})")
    _validate_schematic(schematic)
    return pdk


def _transport_error(job_id: str, exc: sim_batch.BatchError) -> NetlistError:
    if isinstance(exc, sim_batch.BatchPollTimeout):
        return NetlistError(
            f"batch job {job_id}: {exc} -- it may still finish on the fleet, but "
            f"no netlist was collected {_NO_FALLBACK}",
            code=exc.code or BATCH_POLL_TIMEOUT_CODE,
            job_id=job_id,
        )
    return NetlistError(
        f"batch job {job_id}: {exc} {_NO_FALLBACK}", code=exc.code, job_id=job_id
    )


def _submit_and_wait(
    config: sim_batch.BatchConfig,
    job_id: str,
    spec: sim_batch.BatchJobSpec,
    *,
    runner: remote_transport.CommandRunner | None,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Upload, launch and poll to a terminal ``status.json``. Every transport
    failure, the poll timeout included, becomes a :class:`NetlistError`.
    No progress lines: `klt netlist` carries its error envelope on stderr."""
    try:
        sim_batch.submit_job(config, job_id, spec, runner=runner, progress=False)
        sim_batch.launch_job(config, job_id, runner=runner, sleep=sleep)
        return sim_batch.poll_status(
            config, job_id, runner=runner, sleep=sleep, progress=False
        )
    except sim_batch.BatchError as exc:
        raise _transport_error(job_id, exc) from exc


def _collect(
    config: sim_batch.BatchConfig,
    job_id: str,
    status: dict[str, Any],
    collected: str,
    *,
    runner: remote_transport.CommandRunner | None,
) -> None:
    """Collect ``outputs/``. A job that did not finish ``done`` is turned
    into a :class:`NetlistError` carrying the runner's own error, when one
    can be recovered."""
    done = status.get("state") == "done" and (
        sim_batch._status_exit_code(status) in (0, None)
    )
    if done:
        try:
            sim_batch.collect_outputs(config, job_id, collected, runner=runner)
        except sim_batch.BatchError as exc:
            raise _transport_error(job_id, exc) from exc
        return
    runner_error: dict[str, Any] | None = None
    stderr_tail = ""
    try:
        sim_batch.collect_outputs(config, job_id, collected, runner=runner)
        runner_error, note = _runner_error(collected)
        if runner_error is None:
            stderr_tail = _remote_stderr_tail(collected)
    except sim_batch.BatchError as exc:
        note = str(exc)
    raise _job_failure(
        job_id=job_id,
        status=status,
        runner_error=runner_error,
        note=note,
        stderr_tail=stderr_tail,
        harness_log=sim_batch.report_job_uri(config, job_id).rstrip("/")
        + "/harness.log",
    )


def _payload(
    *,
    schematic: str,
    output: str,
    check: bool,
    report: dict[str, Any],
    data: bytes,
    remote: dict[str, Any],
) -> dict[str, Any]:
    """The ``schema_version: 1`` payload a local run returns, from the
    runner's report, plus ``environment.remote``; writes ``output`` unless
    ``check``."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "schematic": schematic,
        "output": output,
        "mode": "check" if check else "generate",
        "xschem": report["xschem"],
    }
    for key in ("pdk", "block"):
        if isinstance(report.get(key), dict):
            payload[key] = report[key]
    payload["environment"] = {"remote": remote}

    if check:
        status_value, drift = _check_result(output, data)
        payload["status"] = status_value
        payload["netlist"] = _netlist_block(None, data)
        payload["drift"] = drift
        return payload

    _write_output(output, data)
    payload["status"] = STATUS_GENERATED
    payload["netlist"] = _netlist_block(output, data)
    return payload


def run_netlist_batch(
    schematic: str,
    output: str,
    *,
    check: bool = False,
    timeout_s: float,
    rcfile: str | None = None,
    pdk: str | None = None,
    pdk_root: str | None = None,
    block: bool = False,
    batch_root: str | None = None,
    batch_include: Sequence[str] = (),
    batch_exclude: Sequence[str] = (),
    xschem_binary: str = DEFAULT_XSCHEM_BINARY,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Export ``schematic`` on the batch runner; same payload as
    :func:`klayout_tools.netlist.run_netlist`, plus ``environment.remote``.

    ``runner`` is the injectable command runner every ``aws`` / launch-script
    call goes through (tests pass a fake; ``None`` is a real subprocess).
    Raises :class:`NetlistError` for every failure, before any upload when
    the cause is local, and never runs xschem on this host.
    """
    variant = _validate_batch_args(
        schematic, pdk=pdk, timeout_s=timeout_s, xschem_binary=xschem_binary
    )
    if pdk_root is not None and warn is not None:
        warn(
            "--pdk-root names a path on this host and is not forwarded to the "
            "batch runner, which resolves its own PDK"
        )

    tree = package_tree(
        schematic,
        rcfile=rcfile,
        pdk=variant,
        batch_root=batch_root,
        include=batch_include,
        exclude=batch_exclude,
    )
    config = _resolve_config(variant, timeout_s)
    job_id = new_job_id()

    with tempfile.TemporaryDirectory(prefix="klt-netlist-batch-") as scratch:
        tarball = os.path.join(scratch, BATCH_TREE_FILENAME)
        build_tarball(tree, tarball)
        spec = build_job_spec(
            tree,
            tarball_path=tarball,
            pdk=variant,
            timeout_s=timeout_s,
            block=block,
            version_check=config.runner_version_check,
        )
        status = _submit_and_wait(config, job_id, spec, runner=runner, sleep=sleep)
        collected = os.path.join(scratch, "outputs")
        _collect(config, job_id, status, collected, runner=runner)
        report = _read_runner_report(collected, job_id)
        data = _read_collected_netlist(collected, report, job_id)
        data = _rewrite_runner_paths(data, _read_tree_roots(collected), tree.root)
        # The runner's klt gates too; this keeps the guarantee client-side.
        _check_no_unevaluated_expr(data, report["xschem"].get("version"))
        _check_no_unresolved_symbol(data)
        remote = {
            **sim_batch._batch_environment(config, job_id, status),
            **sim_batch._read_runner_identity(collected),
            "batch_root": tree.root,
            "input_files": len(tree.files),
            "input_bytes": tree.total_bytes,
        }

    return _payload(
        schematic=schematic,
        output=output,
        check=check,
        report=report,
        data=data,
        remote=remote,
    )


__all__ = [
    "BACKEND_ENV",
    "BATCH_JOB_TOOL",
    "BATCH_NETLIST_FILENAME",
    "BATCH_TREE_FILENAME",
    "MAX_BATCH_TREE_BYTES",
    "SUPPORTED_BACKENDS",
    "BatchTree",
    "build_job_spec",
    "build_tarball",
    "package_tree",
    "resolve_backend",
    "run_netlist_batch",
]
