"""``klt extract --backend batch``: run a long extraction on the batch runner
(issue #4613).

A host that must not run a long whole-layout extraction itself passes
``--backend batch`` (or sets ``KLT_EXTRACT_BACKEND=batch``), and the run is
shipped to the same S3 job contract ``klt sim`` and ``klt netlist`` already
use. Like :mod:`klayout_tools.netlist_batch`, **this module owns no transport
of its own**: submit, launch, poll, collect, the runner-version preflight and
the fleet-config / job-ceiling resolution are all :mod:`klayout_tools.sim_batch`'s,
and the git-tracked input tree is :mod:`klayout_tools.netlist_batch`'s. What is
extract-specific:

1. **What is uploaded.** One ``tree.tar.gz`` of the git-tracked files under
   the layout's repository (``--batch-root`` to narrow), plus a generated
   ``request.json`` -- a ``klt.extract.request/1`` document the runner executes
   verbatim. The layout and every input-file field (``def_pins``,
   ``def_net_connections``, ``abstract_cell_lef``) must be tracked files under
   the root; host-path outputs (``pdk_root``, ``spef``, ``subcircuit_output``)
   are refused, since nothing but the netlist and report come back.
2. **What runs.** ``klt extract request.json`` on the runner, wrapped in
   ``timeout`` for the request's extract timeout (``--timeout-s``), with
   ``progress`` forced on and ``KLT_EXTRACT_BACKEND=local`` so the runner
   cannot recurse. stderr (the ``--progress`` stage lines and heartbeat) is
   teed to ``stderr.log`` and returned.
3. **What comes back.** The netlist is written to ``-o`` and the runner's
   report becomes this run's report, with runner scratch paths rewritten to
   the caller's root and ``environment.remote`` added.
4. **The ceiling is honest.** ``timeout_seconds`` is the extract timeout plus
   the batch overhead, never capped client-side; one above the fleet's
   ``max_job_seconds`` goes through ``sim_batch._assess_job_ceiling`` and is
   refused (or warned about) before any S3 write, naming the setting to raise.
5. **No fallback, ever.** Every batch failure is an :class:`ExtractError`
   (exit 1) naming the job id; nothing here runs the extraction on this host.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import Any

from . import _batch_common, _provenance, remote_transport, sim_batch
from . import netlist_batch as _nb
from .extract_spef import ExtractError
from .netlist import NetlistError

#: ``--backend`` > ``$KLT_EXTRACT_BACKEND`` > ``local``. A dedicated variable,
#: not ``$KLT_SIM_BACKEND``: a dispatch host's blanket sim default would
#: otherwise reroute every short extraction (including ones with no ``--pdk``
#: or an untracked layout) to the fleet.
BACKEND_ENV = "KLT_EXTRACT_BACKEND"
SUPPORTED_BACKENDS = ("local", "batch")

#: Default ``--timeout-s``: 50 minutes, so the default job (timeout plus the
#: batch overhead) fits the fleet's default 3600 s whole-job ceiling.
DEFAULT_TIMEOUT_S = 3000.0

BATCH_TREE_FILENAME = "tree.tar.gz"
BATCH_REQUEST_FILENAME = "request.json"
#: The runner-side output name (relative to the extracted tree).
BATCH_NETLIST_FILENAME = "netlist.spice"
BATCH_JOB_TOOL = "klayout"

#: Request fields that name an input file: must be git-tracked under the root.
_INPUT_FILE_FIELDS = ("def_net_connections", "def_pins")
_INPUT_FILE_LIST_FIELDS = ("abstract_cell_lef",)
#: Request fields that name a host path whose content never comes back.
_REFUSED_PATH_FIELDS = ("pdk_root", "spef", "subcircuit_output")
#: Argv-form flags the batch backend forwards. Anything else set on the argv
#: form is refused: put it in a request document, which is forwarded whole.
ARGV_FORWARDED = ("file", "deck", "top", "pdk", "parasitics", "no_netlist")

_NO_FALLBACK = "(no local fallback: the extraction was not run on this host)"


def resolve_backend(flag: str | None) -> str:
    """``--backend`` > ``$KLT_EXTRACT_BACKEND`` > ``"local"``."""
    if flag is not None:
        value, source = flag, "--backend"
    else:
        value = os.environ.get(BACKEND_ENV, "").strip() or "local"
        source = f"${BACKEND_ENV}"
    if value not in SUPPORTED_BACKENDS:
        raise ExtractError(
            f"{source} must be one of {', '.join(SUPPORTED_BACKENDS)} (got {value!r})"
        )
    return value


def _translate(exc: NetlistError) -> ExtractError:
    return ExtractError(str(exc), code=exc.code, job_id=exc.job_id, details=exc.details)


# --------------------------------------------------------------------------- #
# Request and input tree
# --------------------------------------------------------------------------- #


def argv_request(args_values: dict[str, Any]) -> dict[str, Any]:
    """The runner request for an argv-form invocation (``file`` plus the
    :data:`ARGV_FORWARDED` flags and ``--deck-option`` pairs)."""
    from .extract import REQUEST_SCHEMA

    request: dict[str, Any] = {"schema": REQUEST_SCHEMA}
    for key in ARGV_FORWARDED:
        value = args_values.get(key)
        if value not in (None, False):
            request[key] = value
    if args_values.get("deck_options"):
        request["deck_options"] = args_values["deck_options"]
    return request


def _abs(path: str, base_dir: str) -> str:
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


def package(
    request: dict[str, Any],
    base_dir: str,
    *,
    batch_root: str | None,
    include: Sequence[str],
    exclude: Sequence[str],
    max_bytes: int | None = None,
) -> tuple[_nb.BatchTree, dict[str, Any]]:
    """Validate ``request`` and select the input tree, **without** building
    the archive. Returns the tree and the runner request: ``request`` with
    every input path made root-relative, the output pinned to the runner's
    ``netlist.spice`` (or absent under ``no_netlist``) and ``progress`` on.
    """
    refused = [key for key in _REFUSED_PATH_FIELDS if request.get(key) is not None]
    if refused:
        raise ExtractError(
            f"{', '.join(refused)} cannot apply to --backend batch: it names a "
            "path on this host, and only the netlist and report come back "
            "from the runner; drop it, or use --backend local"
        )
    layout = request.get("file")
    if not isinstance(layout, str) or not layout:
        raise ExtractError("--backend batch requires an input layout file")
    layout = _abs(layout, base_dir)
    if not os.path.isfile(layout):
        raise ExtractError(f"layout file not found: {layout}")
    try:
        root = _nb._resolve_root(layout, batch_root)
        files = _nb._tracked_files(root)
        tracked = set(files)
        selected = set(_nb._select_files(files, include, exclude))
        runner_request = dict(request)

        def _input(path: str, what: str) -> str:
            rel = _nb._relative_tracked(
                _abs(path, base_dir), root, tracked, what=what, flag=""
            )
            _nb._require_selected(rel, selected, what=what)
            return rel

        rel_layout = _input(layout, "layout")
        runner_request["file"] = rel_layout
        for key in _INPUT_FILE_FIELDS:
            if request.get(key) is not None:
                runner_request[key] = _input(request[key], key)
        for key in _INPUT_FILE_LIST_FIELDS:
            if request.get(key):
                runner_request[key] = [_input(item, key) for item in request[key]]
    except NetlistError as exc:
        raise _translate(exc) from exc

    runner_request.pop("output", None)
    if not request.get("no_netlist"):
        runner_request["output"] = BATCH_NETLIST_FILENAME
    runner_request["progress"] = True

    present: list[str] = []
    total = 0
    for rel in files:
        if rel not in selected:
            continue
        path = os.path.join(root, rel)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            continue
        present.append(rel)
        total += info.st_size
    cap = max_bytes if max_bytes is not None else _nb.MAX_BATCH_TREE_BYTES
    if total > cap:
        raise ExtractError(
            f"--backend batch input tree under {root} is {total} bytes "
            f"uncompressed across {len(present)} git-tracked files, above the "
            f"{cap}-byte cap; pass a narrower --batch-root, or select the "
            "needed files with --batch-include <glob> / --batch-exclude <glob>",
            code=_nb.BATCH_INPUT_TOO_LARGE_CODE,
        )
    tree = _nb.BatchTree(
        root=root,
        files=tuple(present),
        total_bytes=total,
        schematic=rel_layout,
        rcfile_arg=None,
        rcfile_pdk_relative=False,
    )
    return tree, runner_request


def build_tarball(
    tree: _nb.BatchTree, runner_request: dict[str, Any], path: str
) -> None:
    """The tracked files plus the generated ``request.json`` member."""
    body = json.dumps(runner_request, indent=2, sort_keys=True).encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        for rel in tree.files:
            archive.add(
                os.path.join(tree.root, rel),
                arcname=rel,
                recursive=False,
                filter=_batch_common.scrub_tarinfo,
            )
        info = tarfile.TarInfo(BATCH_REQUEST_FILENAME)
        info.size = len(body)
        info.mode = 0o644
        _batch_common.scrub_tarinfo(info)
        archive.addfile(info, io.BytesIO(body))


# --------------------------------------------------------------------------- #
# job.json
# --------------------------------------------------------------------------- #


def job_timeout_seconds(timeout_s: float) -> int:
    """``--timeout-s`` plus the per-job overhead ``klt sim`` adds, so the
    inner ``timeout`` always fires before the harness's outer one. Never
    capped here; the fleet ceiling is :func:`_check_ceiling`'s concern."""
    return int(math.ceil(sim_batch._default_batch_job_timeout_s(1, timeout_s)))


def build_job_spec(
    *,
    tarball_path: str,
    pdk: str,
    timeout_s: float,
    no_netlist: bool,
    client_version: str | None = None,
    version_check: str = "enforce",
) -> sim_batch.BatchJobSpec:
    """The extract job: runner-version preflight, extract the tree, run
    ``klt extract request.json`` there under ``timeout`` (report to
    ``report.json``, stderr teed to ``stderr.log``), then copy the netlist to
    ``$EDA_OUTPUT_DIR``."""
    version = (
        client_version if client_version is not None else _provenance._klt_version()
    )
    try:
        preflight = sim_batch._runner_preflight_script(
            version, enforce=version_check == "enforce"
        )
    except Exception as exc:  # SimError: a client version unsafe to embed
        raise ExtractError(str(exc)) from exc
    out = '"$EDA_OUTPUT_DIR/{}"'
    setup = " && ".join(
        [
            'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
            'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
            "w=$(mktemp -d)",
            f'tar -xzf "$EDA_INPUT_DIR/{BATCH_TREE_FILENAME}" -C "$w"',
            'cd "$w"',
            'printf \'%s\\n%s\\n\' "$w" "$(pwd -P)" >'
            + out.format(_batch_common.BATCH_TREE_ROOT_FILENAME),
        ]
    )
    run = (
        f"{BACKEND_ENV}=local timeout --kill-after=30 {int(math.ceil(timeout_s))} "
        f"klt extract {BATCH_REQUEST_FILENAME} --format json"
        f' > "$EDA_OUTPUT_DIR/{sim_batch.BATCH_REPORT_FILENAME}"'
        f' 2> >(tee "$EDA_OUTPUT_DIR/{sim_batch.BATCH_STDERR_FILENAME}" >&2)'
    )
    copy = (
        ""
        if no_netlist
        else (
            f"; if [ -f {BATCH_NETLIST_FILENAME} ]; then cp {BATCH_NETLIST_FILENAME} "
            f"{out.format(BATCH_NETLIST_FILENAME)}; fi"
        )
    )
    command = (
        preflight
        + setup
        + " && "
        + run
        + "; rc=$?; wait $! 2>/dev/null"
        + copy
        + "; exit $rc"
    )
    return sim_batch.BatchJobSpec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk,
        timeout_seconds=job_timeout_seconds(timeout_s),
        inputs=(
            sim_batch.BatchJobInput(
                name=BATCH_TREE_FILENAME,
                label="layout tree",
                local_path=tarball_path,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Config, ceiling and failures
# --------------------------------------------------------------------------- #


def _resolve_config(pdk: str, timeout_s: float) -> sim_batch.BatchConfig:
    from .sim import SimError

    try:
        return sim_batch._resolve_batch_config(
            {"models": {"pdk": pdk}}, corner_count=1, timeout_s=timeout_s
        )
    except (SimError, sim_batch.BatchError) as exc:
        raise ExtractError(
            f"batch backend: {exc} {_NO_FALLBACK}", code=getattr(exc, "code", None)
        ) from exc


def _check_ceiling(config: sim_batch.BatchConfig, timeout_s: float) -> dict[str, Any]:
    """The pre-submit whole-job ceiling assessment (``sim_batch``'s own, one
    unit), acted on before any S3 write: refused under
    ``job_ceiling_check: enforce``, else a stderr warning, as for ``klt sim``.
    The message names what to raise, not ``klt sim``'s ``remote.hosts``."""
    assessment = sim_batch._assess_job_ceiling(
        unit_count=1, per_unit_timeout_s=timeout_s, config=config
    )
    if not assessment["exceeds_ceiling"]:
        return assessment
    ceiling = assessment["max_job_seconds"]
    message = (
        f"extract job bound {assessment['job_bound_s']:g}s (--timeout-s "
        f"{timeout_s:g} + {assessment['overhead_s']:g}s overhead) exceeds the "
        f"fleet's whole-job ceiling of {ceiling:g}s (max_job_seconds from "
        f"{assessment['max_job_seconds_source']}); a job still running at the "
        f"ceiling is killed. Raise the fleet's max_job_seconds "
        f"(${sim_batch.MAX_JOB_SECONDS_ENV}), or lower --timeout-s to at most "
        f"{max(0.0, ceiling - assessment['overhead_s']):g}"
    )
    if assessment["check"] == "enforce":
        raise ExtractError(
            f"backend 'batch' refused before submission: {message} {_NO_FALLBACK}",
            code=sim_batch.BATCH_JOB_CEILING_CODE,
        )
    print(f"klt extract: warning: {message}", file=sys.stderr)
    return assessment


def _transport_error(job_id: str, exc: sim_batch.BatchError) -> ExtractError:
    if isinstance(exc, sim_batch.BatchPollTimeout):
        return ExtractError(
            f"batch job {job_id}: {exc} -- it may still finish on the fleet, but "
            f"no netlist was collected {_NO_FALLBACK}",
            code=exc.code or _nb.BATCH_POLL_TIMEOUT_CODE,
            job_id=job_id,
        )
    return ExtractError(
        f"batch job {job_id}: {exc} {_NO_FALLBACK}", code=exc.code, job_id=job_id
    )


def _job_failure(
    *,
    job_id: str,
    status: dict[str, Any],
    runner_error: dict[str, Any] | None,
    note: str,
    stderr_tail: str,
    harness_log: str,
) -> ExtractError:
    message, code, details = _batch_common.job_failure_parts(
        job_id=job_id,
        state=str(status.get("state") or ""),
        exit_code=sim_batch._status_exit_code(status),
        detail=str(status.get("detail") or "").strip(),
        runner_error=runner_error,
        note=note,
        stderr_tail=stderr_tail,
        harness_log=harness_log,
    )
    return ExtractError(
        f"{message} {_NO_FALLBACK}", code=code, job_id=job_id, details=details or None
    )


def _collect(
    config: sim_batch.BatchConfig,
    job_id: str,
    status: dict[str, Any],
    collected: str,
    *,
    runner: remote_transport.CommandRunner | None,
) -> None:
    """Collect ``outputs/``. A job that did not finish ``done`` becomes an
    :class:`ExtractError` carrying the runner's own error, else the tail of
    its ``stderr.log`` (the ``--progress`` lines show how far it got)."""
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
        runner_error, note = _nb._runner_error(collected)
        if runner_error is None:
            stderr_tail = _nb._remote_stderr_tail(collected)
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


# --------------------------------------------------------------------------- #
# Collected output
# --------------------------------------------------------------------------- #


def _rewrite(value: Any, runner_roots: list[str], local_root: str) -> Any:
    """Rewrite every string under a runner scratch root to the caller's root."""
    if isinstance(value, str):
        for root in runner_roots:
            if value == root or value.startswith(root + "/"):
                return local_root.rstrip("/") + value[len(root) :]
        return value
    if isinstance(value, list):
        return [_rewrite(item, runner_roots, local_root) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite(item, runner_roots, local_root) for key, item in value.items()
        }
    return value


def _read_report(collected: str, job_id: str) -> dict[str, Any]:
    try:
        report = sim_batch.read_collected_report(collected)
    except sim_batch.BatchError as exc:
        raise ExtractError(
            f"batch job {job_id}: {exc} {_NO_FALLBACK}",
            code=_batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        ) from exc
    kind, error = sim_batch._classify_collected_report(report)
    if kind == "error" and error is not None:
        raise ExtractError(
            f"batch job {job_id}: the runner's klt reported: "
            f"{error['message']} {_NO_FALLBACK}",
            code=error.get("code") or _batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    if "netlist_path" not in report:
        raise ExtractError(
            f"batch job {job_id}: the collected {sim_batch.BATCH_REPORT_FILENAME} "
            f"is not a `klt extract` report {_NO_FALLBACK}",
            code=_batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    return report


def _write_collected_netlist(
    collected: str, report: dict[str, Any], output: str, job_id: str
) -> None:
    from ._provenance import sha256_file

    source = os.path.join(collected, BATCH_NETLIST_FILENAME)
    expected = report.get("netlist_sha256")
    if not os.path.isfile(source) or (
        expected is not None and sha256_file(source) != expected
    ):
        raise ExtractError(
            f"batch job {job_id}: the collected {BATCH_NETLIST_FILENAME} is "
            "missing or does not match the netlist_sha256 the runner reported "
            f"{_NO_FALLBACK}",
            code=_batch_common.BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(source, "rb") as src, open(output, "wb") as dst:
            dst.write(src.read())
    except OSError as exc:
        raise ExtractError(f"could not write netlist '{output}': {exc}") from exc


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_extract_batch(
    request: dict[str, Any],
    base_dir: str,
    *,
    output: str | None,
    timeout_s: float,
    batch_root: str | None = None,
    batch_include: Sequence[str] = (),
    batch_exclude: Sequence[str] = (),
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the extraction ``request`` describes on the batch runner; same
    report as :func:`klayout_tools.extract.run_extract`, plus
    ``environment.remote``. ``output`` is the caller's netlist path (the
    request's own ``output`` / ``-o``), defaulting to next to the layout.
    """
    if timeout_s <= 0:
        raise ExtractError(f"--timeout-s must be positive (got {timeout_s})")
    pdk = request.get("pdk")
    if not pdk:
        raise ExtractError(
            "--backend batch requires --pdk (it selects the runner image and the "
            "runner's PDK, e.g. --pdk sky130A)"
        )
    tree, runner_request = package(
        request,
        base_dir,
        batch_root=batch_root,
        include=batch_include,
        exclude=batch_exclude,
    )
    no_netlist = bool(request.get("no_netlist"))
    if output is None and not no_netlist:
        from .extract import _default_output_path

        output = _default_output_path(_abs(request["file"], base_dir))
    config = _resolve_config(pdk, timeout_s)
    ceiling = _check_ceiling(config, timeout_s)
    job_id = _batch_common.make_job_id("extract")

    with tempfile.TemporaryDirectory(prefix="klt-extract-batch-") as scratch:
        tarball = os.path.join(scratch, BATCH_TREE_FILENAME)
        build_tarball(tree, runner_request, tarball)
        spec = build_job_spec(
            tarball_path=tarball,
            pdk=pdk,
            timeout_s=timeout_s,
            no_netlist=no_netlist,
            version_check=config.runner_version_check,
        )
        try:
            sim_batch.submit_job(config, job_id, spec, runner=runner, progress=False)
            sim_batch.launch_job(config, job_id, runner=runner, sleep=sleep)
            status = sim_batch.poll_status(
                config, job_id, runner=runner, sleep=sleep, progress=False
            )
        except sim_batch.BatchError as exc:
            raise _transport_error(job_id, exc) from exc
        collected = os.path.join(scratch, "outputs")
        _collect(config, job_id, status, collected, runner=runner)
        report = _read_report(collected, job_id)
        if not no_netlist:
            _write_collected_netlist(collected, report, output, job_id)
        runner_roots = _batch_common.read_runner_roots(collected)
        report = _rewrite(report, runner_roots, tree.root)
        report["netlist_path"] = None if no_netlist else output
        remote = {
            **sim_batch._batch_environment(config, job_id, status),
            **sim_batch._read_runner_identity(collected),
            "batch_root": tree.root,
            "input_files": len(tree.files),
            "input_bytes": tree.total_bytes,
            "timeout_s": timeout_s,
            "job_ceiling": ceiling,
            "stderr_tail": _nb._remote_stderr_tail(collected),
        }
    environment = report.get("environment")
    report["environment"] = {
        **(environment if isinstance(environment, dict) else {}),
        "remote": remote,
    }
    tail = remote["stderr_tail"]
    if tail:
        print(f"klt extract: runner stderr (tail):\n{tail}", file=sys.stderr)
    return report


__all__ = [
    "BACKEND_ENV",
    "DEFAULT_TIMEOUT_S",
    "SUPPORTED_BACKENDS",
    "argv_request",
    "build_job_spec",
    "build_tarball",
    "package",
    "resolve_backend",
    "run_extract_batch",
]
