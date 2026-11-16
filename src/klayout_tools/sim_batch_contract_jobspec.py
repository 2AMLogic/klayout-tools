"""Batch contract: job spec builder.

Split from :mod:`klayout_tools.sim_batch_contract` (issue #4065), moved verbatim.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from . import (
    _provenance,
    sim_staging,
)
from .remote_launcher import (
    ASSUMED_THREADS_PER_CORNER,
)
from .sim_batch_contract import (
    _BATCH_JOB_OVERHEAD_S,
    _BATCH_PROVISION_SLACK_S,
    BATCH_ARTIFACTS_DIRNAME,
    BATCH_NETLIST_FILENAME,
    BATCH_REPORT_FILENAME,
    BATCH_REQUEST_FILENAME,
    _batch_job_tool_label,
)
from .sim_batch_contract_preflight import (
    _job_stderr_capture_prefix,
    _runner_preflight_script,
)
from .sim_batch_contract_spec import (
    BatchJobInput,
    BatchJobSpec,
    _request_features,
)
from .sim_batch_contract_worker import (
    _worker_capture_command,
)

if TYPE_CHECKING:
    pass


def _build_batch_job_spec(
    remote_request: dict[str, Any],
    netlist_path: str,
    *,
    corner_count: int,
    timeout_s: float,
    keep_artifacts: bool,
    request_dir: str | None = None,
    client_version: str | None = None,
    version_check: str = "enforce",
) -> BatchJobSpec:
    """Build the `klt sim` fan-out job as a :class:`BatchJobSpec` -- batch's
    analog of :func:`sim_remote._build_remote_job_description`, and the one
    and only place `klt sim`'s ``job.json`` shape is constructed.

    ``cmd`` runs the *same* ``local-parallel`` worker-pool code the
    ``local``/``remote`` backends run (never a reimplementation of corner
    expansion, ordering, measurement extraction, or pass/fail
    classification), pointed at the uploaded request document and
    **redirected to a file under ``$EDA_OUTPUT_DIR``** -- see this module's
    docstring on why stdout cannot be used. When the caller wants artifacts,
    ``--outdir`` is pointed inside ``$EDA_OUTPUT_DIR`` too, so the harness's
    ``outputs/`` collection picks them up with no second channel.

    ``cores_per_job`` is the same per-corner sizing assumption
    ``remote_launcher.select_instance_type`` derives its instance size from
    (:data:`~klayout_tools.remote_launcher.ASSUMED_THREADS_PER_CORNER`), so
    the harness's ``$EDA_JOB_CONCURRENCY`` = physical cores / cores-per-job
    lands on the same "how many corners fit at once" answer the `remote`
    backend sizes an instance for.

    ``inputs`` carries the netlist's resolved ``.include``/``.inc`` closure
    alongside the netlist itself (issue #2485), via the *same*
    :func:`sim_staging.stage_sim_job` call
    :func:`sim_remote._build_remote_job_description` makes -- every staged
    file lands flat under ``inputs/`` (hence side by side in
    ``$EDA_INPUT_DIR``, the same property :data:`BATCH_NETLIST_FILENAME`
    already relies on), with the directives naming it rewritten to its
    staged name. An include that resolves nowhere on the submitting host
    raises ``SimError`` from here -- before :func:`submit_job`'s first S3
    write, mirroring :func:`_resolve_provision_script`'s own
    "raise before any partial upload" discipline.

    Under ``options.stage_model_inputs`` (issue #2668) the same
    :func:`sim_staging.stage_sim_job` result also carries the request's
    staged model closure (resolved against ``request_dir``) and the
    rewritten worker request uploaded as ``request.json``. ``pdk_variant``
    still comes from ``models.pdk`` -- image selection is unchanged and
    independent of where the model files come from.
    """
    models = remote_request.get("models") or {}
    staged = sim_staging.stage_sim_job(
        remote_request,
        netlist_path,
        request_dir=request_dir,
        netlist_staged_name=BATCH_NETLIST_FILENAME,
        reserved_names=(BATCH_REQUEST_FILENAME,),
        backend="batch",
    )
    command = (
        _job_stderr_capture_prefix()
        + _runner_preflight_script(
            client_version
            if client_version is not None
            else _provenance._klt_version(),
            enforce=version_check == "enforce",
            capture=_worker_capture_command(
                remote_request, [asset.name for asset in staged.model_assets]
            ),
            required_features=tuple(_request_features(remote_request)),
        )
        + (
            f'klt sim "$EDA_INPUT_DIR/{BATCH_REQUEST_FILENAME}" '
            "--backend local-parallel --format json"
        )
    )
    # One ngspice per PHYSICAL core on the job instance. `local-parallel`'s
    # own default divides os.cpu_count() by _ASSUMED_THREADS_PER_NGSPICE (8),
    # which on a 16-vCPU Spot box is 2 workers: a 5-corner grid ran in three
    # waves (575 s) where one wave (~230 s) was available. That default is a
    # guard for SHARED hosts; a job instance is dedicated to this one job, and
    # 2am#117's probe measured one-per-physical-core at 99% efficiency. The
    # harness exports $EDA_PHYSICAL_CORES from lscpu; `${var:+...}` leaves an
    # image that predates it on the old default rather than on a wrong guess.
    # An explicit `options.max_workers` in the request still wins.
    if not (remote_request.get("options") or {}).get("max_workers"):
        command += ' ${EDA_PHYSICAL_CORES:+--max-workers "$EDA_PHYSICAL_CORES"}'
    # Always collect the per-corner engine logs: without them a deck abort
    # (`could not find a valid modelname`) never reaches the report (#4369).
    # Only `keep_artifacts` installs them into the caller's artifact dir.
    command += f' --outdir "$EDA_OUTPUT_DIR/{BATCH_ARTIFACTS_DIRNAME}"'
    # stdout -> report.json (the success channel); stderr was teed into
    # stderr.log from the job command's first line (see
    # _job_stderr_capture_prefix), so it also covers the preflight.
    command += f' > "$EDA_OUTPUT_DIR/{BATCH_REPORT_FILENAME}"'
    return BatchJobSpec(
        cmd=command,
        tool=_batch_job_tool_label(remote_request),
        pdk_variant=models.get("pdk"),
        pdk_root=models.get("pdk_root"),
        cores_per_job=ASSUMED_THREADS_PER_CORNER,
        timeout_seconds=int(
            math.ceil(_default_batch_job_timeout_s(corner_count, timeout_s))
        ),
        inputs=(
            *(
                BatchJobInput(
                    name=item.staged_name,
                    label=item.label,
                    local_path=item.local_path,
                    content=item.content,
                )
                for item in staged.files
            ),
            BatchJobInput(
                name=BATCH_REQUEST_FILENAME,
                label="request",
                content=json.dumps(staged.request),
            ),
        ),
    )


def _default_batch_job_timeout_s(
    corner_count: int, per_corner_timeout_s: float
) -> float:
    """``job.json``'s ``timeout_seconds``: the fully-serial worst case
    (every corner burns its own ``options.timeout_s``, one after another)
    plus slack for `klt` startup.

    The job instance is concurrent (``$EDA_JOB_CONCURRENCY``), so a real run
    finishes far inside this bound -- it exists only so a wedged job is
    killed by the harness rather than billing until the instance watchdog
    fires. The harness clamps it down to its own ``MAX_JOB_SECONDS``, never
    up, so an over-large value here is safe *for the harness* -- but the job
    is then killed at that ceiling, which is what
    :func:`_assess_job_ceiling` warns about before submission (issue #2833).
    """
    return per_corner_timeout_s * max(1, corner_count) + _BATCH_JOB_OVERHEAD_S


def _default_batch_poll_timeout_s(
    corner_count: int, per_corner_timeout_s: float
) -> float:
    """Default wall-clock budget :func:`poll_status` waits for a terminal
    ``status.json``: the job's own timeout plus
    :data:`_BATCH_PROVISION_SLACK_S` for Spot capacity acquisition, boot,
    and cloud-init (none of which have started when the client begins
    polling). Overridable via ``request.batch.poll_timeout_s``.
    """
    return (
        _default_batch_job_timeout_s(corner_count, per_corner_timeout_s)
        + _BATCH_PROVISION_SLACK_S
    )
