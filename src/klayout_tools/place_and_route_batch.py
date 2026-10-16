"""``klt place-and-route --backend batch``: run each ``pdk.rebuild_corners``
unit on the Spot batch fleet (issue #3518).

A fleet dispatch host that must not run hand-launched grids sets
``KLT_SIM_BACKEND=batch`` (or a caller passes ``--backend batch`` / sets the
request's ``backend``), and every corner of a multi-corner request becomes
one job on the same S3 job contract ``klt sim --backend batch`` and ``klt
netlist --backend batch`` use. **This module owns no transport of its own**:
submit, launch, poll, collect, the runner-version preflight and the
bucket/profile/provision-script resolution are :mod:`klayout_tools.sim_batch`'s,
called as they are. What is place-and-route-specific is small:

1. **Backend selection** (:func:`resolve_backend`) reuses
   :func:`klayout_tools.sim.resolve_backend`'s precedence (flag > request >
   ``$KLT_SIM_BACKEND`` > ``local``). An env-sourced ``batch`` steps back to
   ``local`` for a single unit (a request without ``pdk.rebuild_corners``, or
   a one-element list) -- the same rule ``klt sim`` applies. An *explicit*
   ``batch`` on a single unit is refused rather than silently run locally.
2. **What is uploaded.** The job instance holds no forge credential, so the
   submitter ships one ``tree.tar.gz`` of the request's directory (minus
   ``.klt/`` and ``.git/``), refused above :data:`MAX_BATCH_TREE_BYTES`
   *before* anything is written to S3, plus the unit's own ``request.json``:
   the request with ``pdk.rebuild_corners`` replaced by ``pdk.corner`` and
   ``backend`` dropped. The netlist and macro LEF/GDS must live under the
   request directory; one outside it is refused, not silently left behind.
3. **What runs.** The job command extracts the tree, drops the unit request
   over the original, and runs ``klt place-and-route <request> --backend
   local --format json`` there -- explicitly local, so a runner whose
   environment sets ``KLT_SIM_BACKEND`` cannot recurse. The runner's
   ``.klt/place-and-route/`` is copied to ``$EDA_OUTPUT_DIR/artifacts/``.
4. **What comes back.** Each unit's artifacts land in the same
   ``.klt/place-and-route/<corner>/`` directory a local run would write, and
   every runner path in its report is rewritten to that directory, so
   ``--check``/``--rerun`` see the layout they expect. The aggregated report
   keeps the local shape and adds ``environment.remote.fleet[]`` (one block
   per job, ``job_id`` included) and a ``job_id`` per ``corner_runs`` entry.
5. **No fallback, ever.** A unit whose upload, launch, poll or run fails is a
   ``failed`` ``corner_runs`` entry with the error, the job id and, when the
   runner reported one, its ``error.code``; the command exits ``1``. Nothing
   here can launch OpenROAD on this host. An unresolvable fleet config is a
   :class:`~klayout_tools.place_and_route.PlaceAndRouteError` before any
   upload.

``deck_content_hash`` is the hash of the liberty deck the *runner* used (its
report's ``provenance.deck.content_hash``). When this host can resolve the
same corner's deck too, the two are compared and a disagreement fails the
unit: a fleet image whose PDK differs from this host's would otherwise pass
with a fake runner and silently diverge on the fleet.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import _provenance, output_lock, remote_transport, sim, sim_batch
from ._provenance import _content_hash
from .netlist_batch import _remote_stderr_tail, _runner_error
from .place_and_route import (
    SCHEMA_VERSION,
    PlaceAndRouteError,
    _resolve_liberty,
    _resolve_netlist,
)

#: The host-default variable, shared with ``klt sim`` (2am#1004).
BACKEND_ENV = sim.BACKEND_ENV

#: Values ``--backend`` / ``request.backend`` / ``$KLT_SIM_BACKEND`` accept
#: for this verb; ``klt sim``'s other backends make no sense for a flow.
SUPPORTED_BACKENDS = ("local", "batch")

#: Cap on the **uncompressed** total size of the uploaded request tree
#: (256 MiB: a netlist plus macro LEF/GDS, never a whole checkout).
MAX_BATCH_TREE_BYTES = 256 * 1024 * 1024

#: The tree input and the per-unit request under ``inputs/``.
BATCH_TREE_FILENAME = "tree.tar.gz"
BATCH_REQUEST_FILENAME = "request.json"

#: Under ``$EDA_OUTPUT_DIR``: the runner's absolute stage-output path(s), and
#: the copied ``.klt/place-and-route/`` tree.
BATCH_TREE_ROOT_FILENAME = "tree_root.txt"

#: ``job.json``'s ``tool`` label.
BATCH_JOB_TOOL = "openroad"

#: Per-unit job timeout when ``request.batch.job_timeout_s`` is not set. A
#: full floorplan-to-route flow has no timeout of its own to derive one from.
DEFAULT_JOB_TIMEOUT_S = 4 * 3600.0

#: ``error.code`` values this backend sets itself.
BATCH_POLL_TIMEOUT_CODE = "batch_poll_timeout"
BATCH_JOB_FAILED_CODE = "batch_job_failed"
BATCH_JOB_TIMEOUT_CODE = "batch_job_timeout"
BATCH_INPUT_TOO_LARGE_CODE = "batch_input_too_large"
BATCH_INPUT_OUTSIDE_TREE_CODE = "batch_input_outside_tree"
BATCH_DECK_MISMATCH_CODE = "batch_deck_mismatch"

_NO_FALLBACK = "(no local fallback: OpenROAD was not run on this host)"

#: Directories under the request directory that are never uploaded.
_EXCLUDED_DIRS = frozenset({".klt", ".git"})


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #


def resolve_backend(flag: str | None, request: dict[str, Any], *, units: int) -> str:
    """``flag`` > ``request["backend"]`` > ``$KLT_SIM_BACKEND`` > ``"local"``
    (:func:`klayout_tools.sim.resolve_backend`), for a request of ``units``
    independent units.

    A host-default ``batch`` steps back to ``local`` when ``units`` is 1; an
    explicit ``batch`` with one unit is a :class:`PlaceAndRouteError`.
    """
    backend, from_host_default = sim.resolve_backend(flag, request)
    if backend not in SUPPORTED_BACKENDS:
        source = f"${sim.BACKEND_ENV}" if from_host_default else "backend"
        raise PlaceAndRouteError(
            f"unsupported {source} '{backend}' for place-and-route "
            f"(supported: {', '.join(SUPPORTED_BACKENDS)})"
        )
    backend = sim._yield_host_default_backend(
        backend, from_host_default, reason_ok=units > 1
    )
    if backend == "batch" and units <= 1:
        raise PlaceAndRouteError(
            "backend 'batch' dispatches one job per pdk.rebuild_corners unit "
            "and needs at least two corners; a single-unit request runs on "
            "this host (backend 'local')"
        )
    return backend


# --------------------------------------------------------------------------- #
# Upload: the request tree and the per-unit request
# --------------------------------------------------------------------------- #


def _tree_files(request_dir: str) -> list[tuple[str, str]]:
    """``(absolute path, archive name)`` for every file under
    ``request_dir`` outside :data:`_EXCLUDED_DIRS`, sorted."""
    files: list[tuple[str, str]] = []
    for current, dirs, names in os.walk(request_dir):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS)
        for name in sorted(names):
            path = os.path.join(current, name)
            if os.path.isfile(path):
                files.append((path, os.path.relpath(path, request_dir)))
    return files


def _declared_inputs(request: dict[str, Any], request_dir: str) -> list[str]:
    """Absolute paths of the netlist and each macro LEF/GDS the request names."""
    paths = [_resolve_netlist(request["netlist"], request_dir)]
    for macro in request.get("macros") or []:
        if not isinstance(macro, dict):
            continue
        for key in ("lef", "gds"):
            value = macro.get(key)
            if isinstance(value, str) and value:
                paths.append(
                    os.path.abspath(
                        value
                        if os.path.isabs(value)
                        else os.path.join(request_dir, value)
                    )
                )
    return paths


def package_tree(request: dict[str, Any], request_dir: str) -> list[tuple[str, str]]:
    """The files to upload, after refusing -- before any S3 write -- a
    declared input outside ``request_dir`` or a tree above
    :data:`MAX_BATCH_TREE_BYTES`."""
    root = os.path.realpath(request_dir)
    for path in _declared_inputs(request, request_dir):
        real = os.path.realpath(path)
        if os.path.commonpath([root, real]) != root:
            raise PlaceAndRouteError(
                f"batch backend: input '{path}' is outside the request "
                f"directory '{request_dir}', so it cannot be uploaded; move it "
                f"under the request directory {_NO_FALLBACK}",
                code=BATCH_INPUT_OUTSIDE_TREE_CODE,
            )
    files = _tree_files(request_dir)
    total = sum(os.path.getsize(path) for path, _ in files)
    if total > MAX_BATCH_TREE_BYTES:
        raise PlaceAndRouteError(
            f"batch backend: the request directory holds {total} bytes of "
            f"input files, above the {MAX_BATCH_TREE_BYTES}-byte upload cap; "
            f"move the request into a smaller directory {_NO_FALLBACK}",
            code=BATCH_INPUT_TOO_LARGE_CODE,
        )
    return files


def _build_tarball(files: list[tuple[str, str]], path: str) -> None:
    def _scrub(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    with tarfile.open(path, "w:gz") as archive:
        for source, name in files:
            archive.add(source, arcname=name, recursive=False, filter=_scrub)


def unit_request(request: dict[str, Any], corner: str) -> dict[str, Any]:
    """``request`` as one single-corner unit: ``pdk.rebuild_corners`` becomes
    ``pdk.corner`` and ``backend`` is dropped."""
    unit = copy.deepcopy(request)
    unit["pdk"].pop("rebuild_corners", None)
    unit["pdk"]["corner"] = corner
    unit.pop("backend", None)
    return unit


def build_job_spec(
    *,
    request_name: str,
    unit: dict[str, Any],
    tarball_path: str,
    pdk_variant: str | None,
    timeout_s: float,
    version_check: str = "enforce",
) -> sim_batch.BatchJobSpec:
    """One unit's job as a :class:`~klayout_tools.sim_batch.BatchJobSpec`.

    ``cmd`` is the runner-version preflight, then: extract the tree into a
    fresh directory, record that directory's path, install the unit request
    over the original, and run ``klt place-and-route`` there with ``--backend
    local --format json``. The report goes to ``report.json`` and stderr is
    teed to ``stderr.log`` (where klt writes an error envelope), the channels
    ``klt sim``'s batch job uses; the stage output is copied to
    ``$EDA_OUTPUT_DIR/artifacts/`` whether or not the run succeeded.
    """
    try:
        preflight = sim_batch._runner_preflight_script(
            _provenance._klt_version(), enforce=version_check == "enforce"
        )
    except Exception as exc:  # SimError: a client version unsafe to embed
        raise PlaceAndRouteError(str(exc)) from exc
    out = '"$EDA_OUTPUT_DIR/{}"'
    name = shlex.quote(request_name)
    setup = " && ".join(
        [
            'EDA_INPUT_DIR=$(cd "$EDA_INPUT_DIR" && pwd -P)',
            'EDA_OUTPUT_DIR=$(cd "$EDA_OUTPUT_DIR" && pwd -P)',
            "w=$(mktemp -d)",
            f'tar -xzf "$EDA_INPUT_DIR/{BATCH_TREE_FILENAME}" -C "$w"',
            f'cp "$EDA_INPUT_DIR/{BATCH_REQUEST_FILENAME}" "$w/"{name}',
            'cd "$w"',
            "printf '%s\\n%s\\n' \"$w/.klt/place-and-route\" "
            '"$(pwd -P)/.klt/place-and-route" >' + out.format(BATCH_TREE_ROOT_FILENAME),
        ]
    )
    argv = ["klt place-and-route", name]
    if pdk_variant:
        argv += ["--pdk", shlex.quote(pdk_variant)]
    argv += ["--backend local --format json"]
    collect = (
        "if [ -d .klt/place-and-route ]; then "
        f'mkdir -p "$EDA_OUTPUT_DIR/{sim_batch.BATCH_ARTIFACTS_DIRNAME}" && '
        "cp -a .klt/place-and-route/. "
        f'"$EDA_OUTPUT_DIR/{sim_batch.BATCH_ARTIFACTS_DIRNAME}/"; fi'
    )
    command = (
        preflight
        + setup
        + " && "
        + " ".join(argv)
        + f' > "$EDA_OUTPUT_DIR/{sim_batch.BATCH_REPORT_FILENAME}"'
        + f' 2> >(tee "$EDA_OUTPUT_DIR/{sim_batch.BATCH_STDERR_FILENAME}" >&2)'
        + f"; rc=$?; wait $! 2>/dev/null; {collect}; exit $rc"
    )
    return sim_batch.BatchJobSpec(
        cmd=command,
        tool=BATCH_JOB_TOOL,
        pdk_variant=pdk_variant,
        timeout_seconds=int(timeout_s),
        inputs=(
            sim_batch.BatchJobInput(
                name=BATCH_TREE_FILENAME,
                label="request tree",
                local_path=tarball_path,
            ),
            sim_batch.BatchJobInput(
                name=BATCH_REQUEST_FILENAME,
                label="unit request",
                content=json.dumps(unit),
            ),
        ),
    )


def new_job_id() -> str:
    """A fresh ``[A-Za-z0-9._-]+`` job id, recognisable in the bucket."""
    return f"klt-par-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Config and failure translation
# --------------------------------------------------------------------------- #


def _job_timeout_s(request: dict[str, Any]) -> float:
    value = (request.get("batch") or {}).get("job_timeout_s", DEFAULT_JOB_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PlaceAndRouteError(
            "request.batch.job_timeout_s must be a positive number of seconds"
        )
    return float(value)


def _resolve_config(
    request: dict[str, Any], pdk_variant: str | None, timeout_s: float
) -> sim_batch.BatchConfig:
    """:func:`sim_batch._resolve_batch_config` fed a synthetic request, so the
    fleet config resolves exactly as for ``klt sim``/``klt netlist``."""
    synthetic: dict[str, Any] = {}
    if pdk_variant:
        synthetic["models"] = {"pdk": pdk_variant}
    if isinstance(request.get("batch"), dict):
        synthetic["batch"] = request["batch"]
    try:
        return sim_batch._resolve_batch_config(
            synthetic, corner_count=1, timeout_s=timeout_s
        )
    except (sim.SimError, sim_batch.BatchError) as exc:
        raise PlaceAndRouteError(
            f"batch backend: {exc} {_NO_FALLBACK}", code=getattr(exc, "code", None)
        ) from exc


class _UnitFailed(Exception):
    """One unit's batch failure; becomes its ``corner_runs`` entry."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        job_id: str | None = None,
        remote: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.job_id = job_id
        self.remote = remote


def _transport_failure(job_id: str, exc: sim_batch.BatchError) -> _UnitFailed:
    if isinstance(exc, sim_batch.BatchPollTimeout):
        return _UnitFailed(
            f"batch job {job_id}: {exc} -- it may still finish on the fleet, but "
            f"no result was collected {_NO_FALLBACK}",
            code=BATCH_POLL_TIMEOUT_CODE,
            job_id=job_id,
        )
    return _UnitFailed(
        f"batch job {job_id}: {exc} {_NO_FALLBACK}", code=exc.code, job_id=job_id
    )


def _job_failure(
    config: sim_batch.BatchConfig,
    job_id: str,
    status: dict[str, Any],
    collected: str,
    remote: dict[str, Any],
    note: str,
) -> _UnitFailed:
    """A job that did not finish ``done``: the runner's own klt error when one
    can be recovered from the collected outputs, else a stderr tail."""
    state = str(status.get("state") or "")
    exit_code = sim_batch._status_exit_code(status)
    head = f"batch job {job_id} finished {state!r}" + (
        f" (exit {exit_code})" if exit_code is not None else ""
    )
    runner_error, error_note = _runner_error(collected)
    if runner_error is not None:
        message = f"{head}: the runner's klt reported: {runner_error['message']}"
        code = runner_error.get("code")
    else:
        message = f"{head} without a usable report"
        detail = "; ".join(item for item in (note, error_note) if item)
        if detail:
            message += f" [output recovery: {detail}]"
        message += (
            " -- full log: "
            + sim_batch.job_uri(config, job_id).rstrip("/")
            + "/harness.log"
        )
        tail = _remote_stderr_tail(collected)
        if tail:
            message += f"\nlast of the runner's stderr.log:\n{tail}"
        code = None
    if code is None:
        code = BATCH_JOB_TIMEOUT_CODE if state == "timeout" else BATCH_JOB_FAILED_CODE
    return _UnitFailed(
        f"{message} {_NO_FALLBACK}", code=code, job_id=job_id, remote=remote
    )


# --------------------------------------------------------------------------- #
# Collected output
# --------------------------------------------------------------------------- #


def _read_runner_roots(collected: str) -> list[str]:
    try:
        with open(
            os.path.join(collected, BATCH_TREE_ROOT_FILENAME), encoding="utf-8"
        ) as handle:
            lines = [line.strip().rstrip("/") for line in handle.read().splitlines()]
    except OSError:
        return []
    # Longest first, so a resolved path is rewritten before a prefix of it.
    return sorted(
        {line for line in lines if line.startswith("/")}, key=len, reverse=True
    )


def _rewrite_paths(value: Any, runner_roots: list[str], local_dir: str) -> Any:
    """``value`` with every string under a runner stage-output root re-rooted
    at ``local_dir`` (the directory a local run would have written)."""
    if isinstance(value, str):
        for root in runner_roots:
            if value == root or value.startswith(root + "/"):
                return local_dir + value[len(root) :]
        return value
    if isinstance(value, list):
        return [_rewrite_paths(item, runner_roots, local_dir) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_paths(item, runner_roots, local_dir)
            for key, item in value.items()
        }
    return value


def _read_unit_report(collected: str, job_id: str) -> dict[str, Any]:
    try:
        report = sim_batch.read_collected_report(collected)
    except sim_batch.BatchError as exc:
        raise _UnitFailed(
            f"batch job {job_id}: {exc} {_NO_FALLBACK}",
            code=BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        ) from exc
    if report.get("status") != "ok":
        raw_error = report.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        what = (
            f"the runner's klt reported: {error['message']}"
            if error.get("message")
            else f"the collected {sim_batch.BATCH_REPORT_FILENAME} is not a "
            "`klt place-and-route` report"
        )
        raise _UnitFailed(
            f"batch job {job_id}: {what} {_NO_FALLBACK}",
            code=error.get("code") or BATCH_JOB_FAILED_CODE,
            job_id=job_id,
        )
    return report


def _runner_deck_hash(report: dict[str, Any]) -> str | None:
    deck = (report.get("provenance") or {}).get("deck")
    value = deck.get("content_hash") if isinstance(deck, dict) else None
    return value if isinstance(value, str) else None


def _local_deck_hash(
    cell_library: str, corner: str, pdk_variant: str | None
) -> str | None:
    """This host's hash of the corner's liberty deck, or ``None`` when this
    host cannot resolve it (the runner's PDK is then the only evidence)."""
    try:
        path, _corner, _info = _resolve_liberty(
            cell_library, corner, variant=pdk_variant, root=None
        )
    except PlaceAndRouteError:
        return None
    return _content_hash(path)


def _preflight(
    request: dict[str, Any], request_dir: str, pdk_variant: str | None
) -> tuple[str | None, str, list[tuple[str, str]], float, sim_batch.BatchConfig]:
    """The request-level checks done before any upload: the netlist hash, the
    cell library, the packaged tree, the job timeout and the fleet config."""
    netlist_path = _resolve_netlist(request["netlist"], request_dir)
    input_hash = _content_hash(netlist_path)
    cell_library = request["pdk"].get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")
    files = package_tree(request, request_dir)
    timeout_s = _job_timeout_s(request)
    config = _resolve_config(request, pdk_variant, timeout_s)
    return input_hash, cell_library, files, timeout_s, config


def _collect_corner(
    name: str,
    config: sim_batch.BatchConfig,
    job_id: str | None,
    failure: _UnitFailed | None,
    *,
    scratch: str,
    unit_dir: str,
    input_hash: str | None,
    cell_library: str,
    pdk_variant: str | None,
    fleet: list[dict[str, Any]],
    runner: remote_transport.CommandRunner | None,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """One corner's ``corner_runs`` entry: its job's report, or the failure
    recorded in place. Appends the unit's fleet block to ``fleet``."""
    entry: dict[str, Any] = {
        "corner": name,
        "output_dir": unit_dir,
        "input_content_hash": input_hash,
        "deck_content_hash": None,
    }
    try:
        if failure is not None:
            raise failure
        assert job_id is not None
        entry["job_id"] = job_id
        report, remote = _finish_unit(
            config,
            job_id,
            os.path.join(scratch, name),
            unit_dir,
            runner=runner,
            sleep=sleep,
        )
        fleet.append({"corner": name, **remote})
        runner_deck = _runner_deck_hash(report)
        entry["deck_content_hash"] = runner_deck
        local_deck = _local_deck_hash(cell_library, name, pdk_variant)
        if local_deck is not None and local_deck != runner_deck:
            raise _UnitFailed(
                f"batch job {job_id}: the runner's '{name}' liberty deck "
                f"({runner_deck}) differs from this host's ({local_deck}); "
                "the fleet image's PDK does not match "
                f"{_NO_FALLBACK}",
                code=BATCH_DECK_MISMATCH_CODE,
                job_id=job_id,
            )
    except _UnitFailed as exc:
        entry.update({"status": "failed", "error": str(exc)})
        if exc.code is not None:
            entry["error_code"] = exc.code
        if exc.job_id is not None:
            entry["job_id"] = exc.job_id
        if exc.remote is not None:
            fleet.append({"corner": name, **exc.remote})
    else:
        entry.update(report)
    return entry


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_rebuild_corners_batch(
    request_path: str,
    request: dict[str, Any],
    corners: list[str],
    *,
    pdk_variant: str | None,
    ownership: contextlib.ExitStack,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """The ``batch`` counterpart of ``place_and_route._run_rebuild_corners``:
    one fleet job per corner, aggregated into the same ``corner_runs`` report.

    Request-level problems (an unreadable netlist, an input outside the
    request directory, an unresolvable fleet config) raise
    :class:`PlaceAndRouteError` before any upload. Past that point every unit
    is submitted and launched before any is polled, so the fleet runs them
    concurrently, and a unit that fails -- transport, runner, or a deck
    mismatch -- is recorded in its own entry without stopping the others;
    top-level ``status`` is ``"failed"`` if any did. No unit is ever run on
    this host.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    request_name = os.path.basename(request_path)
    input_hash, cell_library, files, timeout_s, config = _preflight(
        request, request_dir, pdk_variant
    )

    stage_root = os.path.join(request_dir, ".klt", "place-and-route")
    corner_runs: list[dict[str, Any]] = []
    fleet: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="klt-par-batch-") as scratch:
        tarball = os.path.join(scratch, BATCH_TREE_FILENAME)
        _build_tarball(files, tarball)
        submitted: dict[str, str] = {}
        failures: dict[str, _UnitFailed] = {}
        for name in corners:
            ownership.enter_context(
                output_lock.hold_output_dir(os.path.join(stage_root, name))
            )
            job_id = new_job_id()
            spec = build_job_spec(
                request_name=request_name,
                unit=unit_request(request, name),
                tarball_path=tarball,
                pdk_variant=pdk_variant,
                timeout_s=timeout_s,
                version_check=config.runner_version_check,
            )
            try:
                sim_batch.submit_job(config, job_id, spec, runner=runner)
                sim_batch.launch_job(config, job_id, runner=runner, sleep=sleep)
            except sim_batch.BatchError as exc:
                failures[name] = _transport_failure(job_id, exc)
            else:
                submitted[name] = job_id

        for name in corners:
            corner_runs.append(
                _collect_corner(
                    name,
                    config,
                    submitted.get(name),
                    failures.get(name),
                    scratch=scratch,
                    unit_dir=os.path.join(stage_root, name),
                    input_hash=input_hash,
                    cell_library=cell_library,
                    pdk_variant=pdk_variant,
                    fleet=fleet,
                    runner=runner,
                    sleep=sleep,
                )
            )

    failed = [run["corner"] for run in corner_runs if run["status"] != "ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": request.get("engine", "openroad"),
        "engine_version": next(
            (run.get("engine_version") for run in corner_runs if run["status"] == "ok"),
            None,
        ),
        "hdl_toplevel": request.get("hdl_toplevel"),
        "status": "failed" if failed else "ok",
        "target_stage": request.get("target_stage", "route"),
        "seed": request.get("seed"),
        "rebuild_corners": list(corners),
        "failed_corners": failed,
        "corner_runs": corner_runs,
        "environment": {
            "backend": "batch",
            "remote": {"provider": "aws-batch-fleet", "fleet": fleet},
        },
    }


def _finish_unit(
    config: sim_batch.BatchConfig,
    job_id: str,
    collected: str,
    unit_dir: str,
    *,
    runner: remote_transport.CommandRunner | None,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Poll ``job_id`` to a terminal state, collect it, install its artifacts
    into ``unit_dir`` and return ``(report, environment.remote block)``."""
    try:
        status = sim_batch.poll_status(config, job_id, runner=runner, sleep=sleep)
    except sim_batch.BatchError as exc:
        raise _transport_failure(job_id, exc) from exc
    remote = sim_batch._batch_environment(config, job_id, status)
    done = status.get("state") == "done" and (
        sim_batch._status_exit_code(status) in (0, None)
    )
    note = ""
    try:
        sim_batch.collect_outputs(config, job_id, collected, runner=runner)
    except sim_batch.BatchError as exc:
        if done:
            raise _transport_failure(job_id, exc) from exc
        note = str(exc)
    if not done:
        raise _job_failure(config, job_id, status, collected, remote, note)
    report = _read_unit_report(collected, job_id)
    remote.update(sim_batch._read_runner_identity(collected))
    artifacts = os.path.join(collected, sim_batch.BATCH_ARTIFACTS_DIRNAME)
    if os.path.isdir(artifacts):
        os.makedirs(unit_dir, exist_ok=True)
        shutil.copytree(artifacts, unit_dir, dirs_exist_ok=True)
    return _rewrite_paths(report, _read_runner_roots(collected), unit_dir), remote


__all__ = [
    "BACKEND_ENV",
    "BATCH_JOB_TOOL",
    "MAX_BATCH_TREE_BYTES",
    "SUPPORTED_BACKENDS",
    "build_job_spec",
    "package_tree",
    "resolve_backend",
    "run_rebuild_corners_batch",
    "unit_request",
]
