"""``klt netlist --backend batch``: run the xschem export on the batch runner
(issue #3182).

A fleet host that must not run xschem itself sets ``KLT_NETLIST_BACKEND=batch``
(or a caller passes ``--backend batch``), and the export is shipped to the
same S3 job contract ``klt sim --backend batch`` already uses. **This module
owns no transport of its own**: submit, launch, poll and collect, the
runner-version preflight, and the bucket/profile/provision-script resolution
are all :mod:`klayout_tools.sim_batch`'s, called as they are, and the
git-tracked input tree is :mod:`klayout_tools._batch_tree`'s, shared with
``klt extract`` and ``klt signoff``. What is netlist-specific is small:

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

import functools
import os
import shlex
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import (
    _batch_collect,
    _batch_common,
    _batch_tree,
    _provenance,
    remote_transport,
    sim_batch,
)
from .netlist import (
    DEFAULT_XSCHEM_BINARY,
    SCHEMA_VERSION,
    STATUS_GENERATED,
    NetlistError,
    _check_no_unevaluated_expr,
    _check_no_unresolved_symbol,
    _check_result,
    _netlist_block,
    _validate_schematic,
    _write_output,
)
from .netlist_batch_select import _relative_symbol_dirs
from .netlist_instances import instances_payload
from .netlist_paths import (
    _rewrite_runner_paths,
    fold_continuations,
    relativize_schematic_paths,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Host-default environment variable: ``--backend`` > ``$KLT_NETLIST_BACKEND``
#: > ``local``. Never yields back to ``local`` (see the module docstring).
BACKEND_ENV = "KLT_NETLIST_BACKEND"

#: Values ``--backend`` / ``$KLT_NETLIST_BACKEND`` accept.
SUPPORTED_BACKENDS = ("local", "batch")

#: Cap on the **uncompressed** total size of the git-tracked files uploaded as
#: the job's input tree. It is :mod:`klayout_tools._batch_tree`'s, re-exported
#: for ``klt netlist``'s callers and tests: ``package_tree`` reads this
#: module's binding at call time, so patching ``netlist_batch``'s moves its cap.
MAX_BATCH_TREE_BYTES = _batch_tree.MAX_BATCH_TREE_BYTES

#: The single input the job receives under ``inputs/``.
BATCH_TREE_FILENAME = "tree.tar.gz"

#: Where the job command writes the netlist, under ``$EDA_OUTPUT_DIR``.
BATCH_NETLIST_FILENAME = "netlist.spice"

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
BATCH_INPUT_TOO_LARGE_CODE = _batch_tree.BATCH_INPUT_TOO_LARGE_CODE

#: Appended to every batch failure message: the reason the backend exists.
_NO_FALLBACK = "(no local fallback: xschem was not run on this host)"


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def resolve_backend(flag: str | None) -> str:
    """``--backend`` > ``$KLT_NETLIST_BACKEND`` > ``"local"``. A hard routing
    choice: an env-sourced ``batch`` is never stepped back to ``local``; an
    unknown env value is an error naming the variable (argparse validates
    the flag).
    """
    return _batch_common.resolve_backend_value(
        flag, BACKEND_ENV, SUPPORTED_BACKENDS, NetlistError
    )


# --------------------------------------------------------------------------- #
# Input tree
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchTree(_batch_tree.TrackedTree):
    """The git-tracked input tree a batch export ships, fully validated: the
    measured :class:`~klayout_tools._batch_tree.TrackedTree` plus what the
    job command passes to ``klt netlist`` on the runner.

    ``rcfile_arg`` is the shell fragment the job command passes as
    ``--rcfile`` (a quoted root-relative path, or ``"$PDK_ROOT"/<rest>`` for
    a PDK-relative rcfile), else ``None``; ``symbol_paths`` are
    root-relative."""

    schematic: str
    rcfile_arg: str | None
    rcfile_pdk_relative: bool
    symbol_paths: tuple[str, ...] = ()


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


def package_tree(
    schematic: str,
    *,
    rcfile: str | None,
    pdk: str,
    batch_root: str | None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    max_bytes: int | None = None,
    symbol_path: Sequence[str] = (),
) -> BatchTree:
    """Select and validate the input tree, **without** building the archive.

    Raises :class:`NetlistError` -- before any archive is built or any S3
    write -- for a root outside git, a schematic or project rcfile outside
    the root or untracked or left out of the selected set, or an uncompressed
    total of the selected set above ``max_bytes`` (default
    :data:`MAX_BATCH_TREE_BYTES`).
    """
    if max_bytes is None:
        max_bytes = MAX_BATCH_TREE_BYTES
    root = _batch_tree.resolve_root(
        schematic, batch_root, what="schematic", err_cls=NetlistError
    )
    files = _batch_tree.tracked_files(root, err_cls=NetlistError)
    tracked = set(files)
    selected = set(_batch_tree.select_files(files, include, exclude))
    rel_schematic = _batch_tree.relative_tracked(
        schematic, root, tracked, what="schematic", flag="", err_cls=NetlistError
    )
    _batch_tree.require_selected(
        rel_schematic, selected, what="schematic", err_cls=NetlistError
    )

    rcfile_arg: str | None = None
    pdk_relative = False
    if rcfile is not None:
        rcfile_arg = _pdk_relative_rcfile(rcfile, pdk)
        if rcfile_arg is not None:
            pdk_relative = True
        else:
            if not os.path.isfile(rcfile):
                raise NetlistError(f"xschemrc file not found: {rcfile}")
            rel_rcfile = _batch_tree.relative_tracked(
                rcfile,
                root,
                tracked,
                what="--rcfile",
                flag=(
                    ", or give a PDK rcfile in its PDK-relative form "
                    "'$PDK_ROOT/$PDK/libs.tech/xschem/xschemrc'"
                ),
                err_cls=NetlistError,
            )
            _batch_tree.require_selected(
                rel_rcfile, selected, what="--rcfile", err_cls=NetlistError
            )
            rcfile_arg = shlex.quote(rel_rcfile)

    symbol_rel = _relative_symbol_dirs(symbol_path, root, selected)

    tree = _batch_tree.measure_tree(root, selected)
    if tree.total_bytes > max_bytes:
        raise NetlistError(
            f"--backend batch input tree under {root} is {tree.total_bytes} bytes "
            f"uncompressed across {len(tree.files)} git-tracked files, above the "
            f"{max_bytes}-byte cap; pass a narrower --batch-root (e.g. the "
            "directory holding the schematics, symbols and xschemrc), or "
            "select just the files the schematic needs with --batch-include "
            "<glob> / --batch-exclude <glob>",
            code=BATCH_INPUT_TOO_LARGE_CODE,
        )
    return BatchTree(
        root=root,
        files=tree.files,
        total_bytes=tree.total_bytes,
        schematic=rel_schematic,
        rcfile_arg=rcfile_arg,
        rcfile_pdk_relative=pdk_relative,
        symbol_paths=tuple(symbol_rel),
    )


def build_tarball(tree: BatchTree, path: str) -> None:
    """Write ``tree``'s files into a gzip tarball at ``path``, with
    root-relative member names. Symlinks are stored as symlinks."""
    _batch_tree.write_tarball(tree.root, tree.files, path)


# --------------------------------------------------------------------------- #
# job.json
# --------------------------------------------------------------------------- #


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

    ``cmd`` is the runner-version preflight, then: extract the tree, record
    the directory's path, and run ``klt netlist`` there with ``--backend
    local`` and ``--format json``, report to ``report.json``, stderr teed to
    ``stderr.log`` (where klt writes an error envelope) -- the same channels
    ``klt sim``'s batch job uses. ``pdk_root`` is never set: the runner
    resolves its own.
    """
    argv = [
        "klt netlist",
        shlex.quote(tree.schematic),
        "-o",
        f'"$EDA_OUTPUT_DIR/{BATCH_NETLIST_FILENAME}"',
    ]
    if block:
        argv.append("--block")
    if tree.rcfile_arg is not None:
        argv += ["--rcfile", tree.rcfile_arg]
    for rel_dir in tree.symbol_paths:
        argv += ["--symbol-path", shlex.quote(rel_dir)]
    argv += ["--pdk", shlex.quote(pdk), "--timeout-s", repr(float(timeout_s))]
    argv += ["--backend local --format json"]
    command = _batch_common.build_job_command(
        tree_member=BATCH_TREE_FILENAME,
        run=" ".join(argv),
        err_cls=NetlistError,
        stdout_target=sim_batch.BATCH_REPORT_FILENAME,
        client_version=client_version,
        version_check=version_check,
        tree_root_subdir="",
    )
    return _batch_common.build_job_spec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk,
        tree_member=BATCH_TREE_FILENAME,
        tree_label="schematic tree",
        tarball_path=tarball_path,
        timeout_s=timeout_s,
    )


def new_job_id() -> str:
    """A fresh job id in the ``[A-Za-z0-9._-]+`` alphabet the launch script
    accepts, prefixed so a netlist job is recognisable in the bucket."""
    return _batch_common.make_job_id("netlist")


# --------------------------------------------------------------------------- #
# Config and failure translation
# --------------------------------------------------------------------------- #


def _resolve_config(pdk: str, timeout_s: float) -> sim_batch.BatchConfig:
    """:func:`sim_batch._resolve_batch_config` itself, fed a synthetic
    request, so the fleet config resolves exactly as for ``klt sim``
    (``$KLT_BATCH_*``, then ``batch-fleet.env`` beside the provision script).
    Its ``SimError``/``BatchError`` becomes a :class:`NetlistError`."""
    return _batch_common.resolve_batch_config(
        pdk, timeout_s, NetlistError, _NO_FALLBACK
    )


# --------------------------------------------------------------------------- #
# Collected output
# --------------------------------------------------------------------------- #


def _read_runner_report(collected: str, job_id: str) -> dict[str, Any]:
    return _batch_collect.read_collected_unit_report(
        collected,
        job_id,
        err_cls=NetlistError,
        no_fallback=_NO_FALLBACK,
        validate=lambda report: (
            report.get("status") == STATUS_GENERATED
            and isinstance(report.get("xschem"), dict)
            and isinstance(report.get("netlist"), dict)
        ),
        what="a `klt netlist` generate report",
    )


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
    if expected is not None and expected != _provenance.content_hash_bytes(data):
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
    return _batch_common.transport_error(job_id, exc, NetlistError, _NO_FALLBACK)


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
    """Collect ``outputs/``; a job that did not finish ``done`` becomes a
    :class:`NetlistError` carrying the runner's own error, if recoverable."""
    _batch_collect.collect(
        config,
        job_id,
        status,
        collected,
        runner=runner,
        err_cls=NetlistError,
        no_fallback=_NO_FALLBACK,
        job_failure=functools.partial(
            _batch_common.job_failure,
            err_cls=NetlistError,
            no_fallback=_NO_FALLBACK,
        ),
    )


def _payload(
    *,
    schematic: str,
    output: str,
    check: bool,
    report: dict[str, Any],
    data: bytes,
    remote: dict[str, Any],
    ignore_comments: bool = False,
    symbol_path: Sequence[str] = (),
    canonical_wrap: bool = False,
) -> dict[str, Any]:
    """The ``schema_version: 1`` payload from the runner's report; writes ``output``."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "schematic": schematic,
        "output": output,
        "mode": "check" if check else "generate",
        # ``symbol_path``: the root-relative list the client forwarded.
        "xschem": {**report["xschem"], "symbol_path": list(symbol_path)},
        **instances_payload(data),
    }
    for key in ("pdk", "block"):
        if isinstance(report.get(key), dict):
            payload[key] = report[key]
    payload["environment"] = {"remote": remote}

    if check:
        status_value, drift = _check_result(output, data, ignore_comments)
        payload["status"] = status_value
        payload["netlist"] = _netlist_block(None, data)
        payload["drift"] = drift
        return payload

    _write_output(output, data)
    payload["status"] = STATUS_GENERATED
    payload["canonical_wrap"] = canonical_wrap
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
    ignore_comments: bool = False,
    symbol_path: Sequence[str] = (),
    relative_paths: bool = False,
    canonical_wrap: bool = False,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Export ``schematic`` on the batch runner; same payload as
    :func:`klayout_tools.netlist.run_netlist`, plus ``environment.remote``.

    ``runner`` is the injectable command runner every ``aws`` / launch-script
    call goes through (tests pass a fake; ``None`` is a real subprocess).
    Raises :class:`NetlistError` for every failure, before any upload when
    the cause is local; never runs xschem on this host.
    """
    if ignore_comments and not check:
        raise NetlistError("--ignore-comments only applies with --check")
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
        symbol_path=symbol_path,
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
        data = _rewrite_runner_paths(
            data, _batch_common.read_runner_roots(collected), tree.root
        )
        if relative_paths:
            data = relativize_schematic_paths(data, schematic)
        if canonical_wrap:
            data = fold_continuations(data)
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
        ignore_comments=ignore_comments,
        symbol_path=list(tree.symbol_paths),
        canonical_wrap=canonical_wrap,
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
