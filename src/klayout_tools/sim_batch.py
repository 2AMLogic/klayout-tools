"""S3 job-contract dispatch for ``klt sim``'s ``batch`` backend (issue #2080).

The consumer half of 2am's EDA batch fleet (``2AMLogic/2am``'s
``infra/aws/batch-fleet.md``, ``batch-job-runner.md``,
``batch-fleet-harness.sh``): 2am#117 shipped the *fleet* -- a diversified,
Spot EC2-Fleet job runner with an S3 job contract -- and left the
submit/wait/collect wrapper as a **consumer-repo deliverable**. This module
is that wrapper for `klt sim`.

The seam is the four steps ``batch-fleet.md`` §"The seam: what a
consumer-repo wrapper does" fixes, and nothing more:

1. write   ``s3://<bucket>/jobs/<job-id>/job.json`` (+ ``inputs/``)
2. launch  ``batch-fleet-provision.sh launch --job <job-id> --apply``
3. poll    ``s3://<bucket>/jobs/<job-id>/status.json`` until
   ``done``/``failed``/``timeout``
4. collect ``s3://<bucket>/jobs/<job-id>/outputs/``

Relationship to :mod:`klayout_tools.sim_remote`
-----------------------------------------------
``batch`` is a *third* backend beside ``local-parallel`` and ``remote``, not
a variant of ``remote``. What it shares with ``remote`` is deliberate and
narrow: the generated request document (:func:`sim_remote._build_remote_request`,
already generic over transport -- ``backend`` is forced to
``local-parallel`` so the job instance runs #255's worker pool directly),
the fleet shard/merge engine (:func:`sim_remote._run_sharded`,
:func:`sim_remote._lost_shard_corner`), the unrun-corner report shape
(:func:`sim_remote._unrun_corner_report`), and the artifact-path reducer
(:func:`sim_remote._rewrite_remote_artifact_paths`). All of those are reused
here unmodified.

What it does **not** share is the transport. ``remote_transport``'s
:class:`~klayout_tools.remote_transport.JobDescription`/``JobInput``/
``push_job``/``run_remote_job``/``pull_artifacts`` are SSH/SCP-typed -- they
shell into a live host and have no S3 equivalent -- so nothing in this
module imports them (only the two job-relative filename constants and the
injectable :data:`~klayout_tools.remote_transport.CommandRunner` type, both
transport-neutral). 2am's ``job.json`` is likewise **not** a serialization
of ``JobDescription``: it is a *shell-command* contract
(``{tool, cmd, pdk_variant, pdk_root, cores_per_job, timeout_seconds}``,
all optional except ``cmd``) that the harness runs as
``cd "$WORKDIR" && bash -c "$JOB_CMD"``. :class:`BatchJobSpec` is this
module's own, small analog.

Why the job command redirects its report to a file
--------------------------------------------------
The harness redirects **all** stdout/stderr -- its own ``[harness ...]``
log lines *and* the job command's -- into one ``harness.log``
(``exec > >(tee -a "$HARNESS_LOG") 2>&1``). Stdout is therefore *not* a
clean channel back to the submitter, unlike ``remote``'s SSH channel (which
``remote_transport.run_remote_job`` ``json.loads()``es directly). Only
``outputs/**`` and ``status.json`` are structured collection channels, so
:func:`_build_batch_job_spec`'s ``cmd`` redirects `klt sim`'s JSON report to
``$EDA_OUTPUT_DIR/report.json`` and this module reads it back out of the
collected ``outputs/`` tree.

Credentials, buckets, and the launch script
-------------------------------------------
No credential, key path, or bucket name is baked into this module. The
bucket/region/jobs-prefix resolve from ``request.batch.*``, then a
``KLT_BATCH_*`` environment variable, then the fleet's own
``batch-fleet.env`` sitting beside the resolved provision script (the single
source of truth 2am's own docs bind to its IAM policy) -- and raise
:class:`~klayout_tools.sim.SimError` naming all three when unresolvable.
The only literal default is the **profile name** ``batch-runner-submit``,
which is an `aws` CLI profile *name*, not a secret: the credential it
resolves to lives in the operator's own AWS config.

Hermeticity: every AWS/launch invocation goes through an injectable
``runner`` (the same discipline :mod:`klayout_tools.remote_transport` and
:mod:`klayout_tools.remote_launcher` already use), so no test in this
repo's suite ever calls the real `aws` CLI, the real provision script, or a
network socket -- mirroring 2am's own ``scripts/tests/test-batch-fleet.sh``
recorded-call-log discipline on the other side of the same seam.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import remote_transport
from .remote_launcher import ASSUMED_THREADS_PER_CORNER
from .sim_remote import (
    _build_remote_request,
    _rewrite_remote_artifact_paths,
    _run_sharded,
    _unrun_corner_report,
)

if TYPE_CHECKING:
    from .sim import CornerPoint, _Checkpoint

# --------------------------------------------------------------------------- #
# Contract constants
# --------------------------------------------------------------------------- #

#: Filename the netlist is uploaded to under ``jobs/<id>/inputs/``. Aliased
#: to ``remote_transport``'s own constant (not re-declared) because
#: :func:`sim_remote._build_remote_request` -- reused verbatim here -- writes
#: exactly that name into the generated request's ``netlist`` field. The
#: harness copies ``inputs/`` to ``$EDA_INPUT_DIR``, and `klt sim` resolves a
#: relative ``netlist`` against its *request file's* own directory, so both
#: files landing side by side in ``$EDA_INPUT_DIR`` is what makes the
#: relative path resolve on the job instance.
BATCH_NETLIST_FILENAME = remote_transport.REMOTE_NETLIST_FILENAME

#: Filename the generated request document is uploaded to under
#: ``jobs/<id>/inputs/`` -- the path the job instance's own `klt sim`
#: invocation is pointed at. Same aliasing rationale as
#: :data:`BATCH_NETLIST_FILENAME`.
BATCH_REQUEST_FILENAME = remote_transport.REMOTE_REQUEST_FILENAME

#: Filename the job command redirects `klt sim`'s ``--format json`` report
#: to, under ``$EDA_OUTPUT_DIR`` -- see this module's docstring on why
#: stdout is not a usable channel here.
BATCH_REPORT_FILENAME = "report.json"

#: ``$EDA_OUTPUT_DIR``-relative directory the job command points `klt sim`'s
#: ``--outdir`` at when ``options.keep_artifacts`` is set, so per-corner
#: logs/rawfiles land inside the one tree the harness collects.
BATCH_ARTIFACTS_DIRNAME = "artifacts"

#: ``job.json``'s ``tool`` field -- informational only (the harness echoes it
#: into ``status.json`` and never inspects it). The default when
#: ``remote_request`` names no override -- see :func:`_batch_job_tool_label`
#: (issue #2423) for the ``options.ngspice_binary``-aware label this
#: constant is the fallback for.
BATCH_JOB_TOOL = "ngspice"


def _batch_job_tool_label(remote_request: dict[str, Any]) -> str:
    """``job.json``'s ``tool`` label for this batch job (issue #2423):
    ``options.ngspice_binary`` when the forwarded request document
    explicitly names one, else :data:`BATCH_JOB_TOOL`.

    Purely cosmetic, matching :data:`BATCH_JOB_TOOL`'s own "informational
    only, the harness echoes it into ``status.json`` and never inspects it"
    contract -- **never** resolved against ``$PATH`` here. This process (the
    *submitting* host) and the job instance that actually runs
    ``ngspice_binary`` are different machines with different filesystems and
    `$PATH`s; a `shutil.which()` lookup on the submit side would answer a
    question about the wrong host. The job instance's own `klt sim`
    invocation (the ``cmd`` :func:`_build_batch_job_spec` builds) resolves
    the real binary itself, fresh, from this same forwarded
    ``options.ngspice_binary``/its own ``$KLT_NGSPICE_BINARY`` -- see
    ``sim.py``'s ``_resolve_ngspice_binary``. ``$KLT_NGSPICE_BINARY`` itself
    is not read here either, for the same "describes the submit host, not
    the job host" reason ``_host_max_workers_cap``'s own docstring gives for
    ``$KLT_SIM_MAX_WORKERS``.
    """
    options = remote_request.get("options") or {}
    override = options.get("ngspice_binary")
    if isinstance(override, str) and override:
        return override
    return BATCH_JOB_TOOL


#: `aws` CLI profile *name* the submit path runs under by default -- 2am's
#: ``BATCH_SUBMIT_PROFILE``. A profile name is not a credential (see this
#: module's docstring); overridable via ``request.batch.profile``.
DEFAULT_BATCH_PROFILE = "batch-runner-submit"

#: S3 key prefix jobs live under, when neither the request nor the fleet
#: config names one -- 2am's ``BATCH_JOBS_PREFIX`` default.
DEFAULT_BATCH_JOBS_PREFIX = "jobs"

#: How often :func:`poll_status` re-reads ``status.json``. Overridable via
#: ``request.batch.poll_interval_s``.
DEFAULT_POLL_INTERVAL_S = 30.0

#: Slack added to the fully-serial worst case when deriving a default
#: wall-clock poll budget: Spot capacity acquisition, instance boot,
#: cloud-init, and the harness's own S3 round trips all happen before the
#: job command starts. Overridable wholesale via ``request.batch.poll_timeout_s``.
_BATCH_PROVISION_SLACK_S = 1800.0

#: Per-invocation timeout for one `aws` CLI call (upload/poll/collect).
_BATCH_AWS_TIMEOUT_S = 900.0

#: Per-invocation timeout for one ``batch-fleet-provision.sh launch`` call.
#: The script's own capacity-shortfall retry loop
#: (``BATCH_LAUNCH_RETRIES``) runs inside this budget.
_BATCH_LAUNCH_TIMEOUT_S = 900.0

#: ``status.json`` states that mean the job is over, one way or another.
BATCH_TERMINAL_STATES = ("done", "failed", "timeout")

#: ``status.json`` states that mean "keep waiting". ``interrupted`` is
#: included on purpose: Spot reclaimed the instance, and 2am's own
#: ``reconcile`` (scheduled on the captain, 2am#533) re-launches the job.
#: The client waits; it never re-submits an ``interrupted`` job itself.
BATCH_WAITING_STATES = ("running", "interrupted")

#: `klt sim` exit codes that still mean "the sweep ran and wrote a report":
#: 0 (pass) / 3 (measurement failure) / 4 (corner error). The harness marks
#: any non-zero exit ``failed``, so a ``failed`` job whose ``exit_code`` is
#: one of these still has a collectable ``outputs/report.json`` -- the same
#: distinction ``sim_remote._REMOTE_SIM_SUCCESS_EXIT_CODES`` draws for the
#: SSH transport.
_BATCH_SIM_SUCCESS_EXIT_CODES = (0, 3, 4)

#: Environment-variable fallbacks, consulted after ``request.batch.*`` and
#: before the fleet config file.
PROVISION_SCRIPT_ENV = "KLT_BATCH_PROVISION_SCRIPT"
BUCKET_ENV = "KLT_BATCH_JOB_BUCKET"
REGION_ENV = "KLT_BATCH_REGION"
PROFILE_ENV = "KLT_BATCH_PROFILE"

#: 2am's own fleet config, read (never written) from beside the resolved
#: provision script -- the file whose ``BATCH_JOB_BUCKET``/``BATCH_JOBS_PREFIX``
#: values 2am's docs describe as bound to the IAM policy the submit identity
#: was minted under. Reading it is how this module avoids baking a bucket
#: name into source while still having a working default on any host that
#: has the script.
FLEET_CONFIG_FILENAME = "batch-fleet.env"


class BatchError(Exception):
    """Raised when the S3/launch transport itself fails: an `aws` CLI call
    or the provision script returns non-zero, the collected ``outputs/``
    holds no readable report, or ``status.json`` reports a state outside the
    documented set.

    Mirrors ``remote_transport.RemoteTransportError``'s role for the SSH
    transport -- :func:`_run_batch` re-raises it as
    :class:`klayout_tools.sim.SimError` (no corner ever ran: the same
    "sweep never started" class as an unresolvable netlist).
    """


class BatchPollTimeout(BatchError):
    """The wall-clock poll budget elapsed with the job still non-terminal.

    Carries the last observed ``status.json`` state (``None`` when no
    status object ever appeared). Unlike a plain :class:`BatchError` this is
    *not* re-raised as a ``SimError``: the job may still be running on the
    fleet, so the sweep reports every one of its units through
    :func:`sim_remote._unrun_corner_report` instead of claiming the run
    failed.
    """

    def __init__(self, message: str, *, last_state: str | None = None) -> None:
        super().__init__(message)
        self.last_state = last_state


# --------------------------------------------------------------------------- #
# job.json (2am's shell-command contract)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchJobInput:
    """One file uploaded to ``s3://<bucket>/<jobs>/<id>/inputs/<name>``.

    Exactly one of ``local_path`` (an existing file) or ``content`` (a
    string written to a temp file first) must be given. ``label`` is a short
    human-readable name used only in an upload failure's message.
    """

    name: str
    label: str
    local_path: str | None = None
    content: str | None = None

    def __post_init__(self) -> None:
        if (self.local_path is None) == (self.content is None):
            raise ValueError(
                "BatchJobInput requires exactly one of local_path or content "
                f"(name={self.name!r})"
            )


@dataclass(frozen=True)
class BatchJobSpec:
    """2am's ``job.json`` -- everything optional except ``cmd``.

    Field-for-field the contract ``batch-fleet-harness.sh`` parses:
    ``cmd`` runs under ``bash -c`` in the job directory; ``tool`` is echoed
    into ``status.json``; ``pdk_variant``/``pdk_root`` are exported (the
    latter is layer 1 of the image's own PDK resolution order -- 2am's FSx
    hook); ``cores_per_job`` divides the instance's *physical* core count to
    derive ``$EDA_JOB_CONCURRENCY``; ``timeout_seconds`` caps the job,
    clamped down by the instance watchdog.

    ``inputs`` is **not** part of that contract -- it is this module's own
    upload list (what lands under ``inputs/``), excluded from
    :meth:`to_job_json` for exactly that reason.
    """

    cmd: str
    tool: str | None = None
    pdk_variant: str | None = None
    pdk_root: str | None = None
    cores_per_job: int | None = None
    timeout_seconds: int | None = None
    inputs: tuple[BatchJobInput, ...] = ()

    def to_job_json(self) -> dict[str, Any]:
        """The ``job.json`` document, in ``batch-fleet.md``'s own field
        order, omitting every unset optional field (the harness treats an
        absent field and an empty one identically, and a smaller document
        is a smaller contract surface)."""
        document: dict[str, Any] = {}
        if self.tool is not None:
            document["tool"] = self.tool
        document["cmd"] = self.cmd
        for key in ("pdk_variant", "pdk_root", "cores_per_job", "timeout_seconds"):
            value = getattr(self, key)
            if value is not None:
                document[key] = value
        return document


def _build_batch_job_spec(
    remote_request: dict[str, Any],
    netlist_path: str,
    *,
    corner_count: int,
    timeout_s: float,
    keep_artifacts: bool,
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
    """
    models = remote_request.get("models") or {}
    command = (
        f'klt sim "$EDA_INPUT_DIR/{BATCH_REQUEST_FILENAME}" '
        "--backend local-parallel --format json"
    )
    if keep_artifacts:
        command += f' --outdir "$EDA_OUTPUT_DIR/{BATCH_ARTIFACTS_DIRNAME}"'
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
            BatchJobInput(
                name=BATCH_NETLIST_FILENAME, label="netlist", local_path=netlist_path
            ),
            BatchJobInput(
                name=BATCH_REQUEST_FILENAME,
                label="request",
                content=json.dumps(remote_request),
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
    up, so an over-large value here is safe.
    """
    return per_corner_timeout_s * max(1, corner_count) + 120.0


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


# --------------------------------------------------------------------------- #
# Configuration resolution (nothing secret, nothing hardcoded)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BatchConfig:
    """Everything the submit/poll/collect calls need, fully resolved."""

    provision_script: str
    bucket: str
    jobs_prefix: str
    profile: str
    region: str | None
    poll_interval_s: float
    poll_timeout_s: float
    aws_binary: str = "aws"


def _read_fleet_config(provision_script: str) -> dict[str, str]:
    """Best-effort read of 2am's ``batch-fleet.env`` sitting beside the
    resolved provision script.

    Deliberately *not* a shell evaluation: only literal ``KEY=value`` /
    ``KEY="value"`` lines are honored, anything interpolated (``$``/backtick)
    or malformed is skipped, and an unreadable/absent file yields ``{}``.
    This is a convenience source for non-secret fleet identifiers
    (bucket, jobs prefix, region, submit profile), never a credential path.
    """
    path = os.path.join(
        os.path.dirname(os.path.abspath(provision_script)), FLEET_CONFIG_FILENAME
    )
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key.startswith("BATCH_") or "$" in value or "`" in value:
            continue
        values[key] = value
    return values


def _resolve_provision_script(batch_spec: dict[str, Any]) -> str:
    """``request.batch.provision_script_path`` > ``$KLT_BATCH_PROVISION_SCRIPT``
    > :class:`SimError` naming both -- never a guessed path into a sibling
    checkout (too fragile across hosts/usernames).

    Raised **before** any S3 write by :func:`_resolve_batch_config`'s
    caller, mirroring ``_run_remote``'s missing-``ssh_key_path`` check: a
    partial upload must never happen because the launch script turned out to
    be unfindable.
    """
    from .sim import SimError

    candidate = batch_spec.get("provision_script_path") or os.environ.get(
        PROVISION_SCRIPT_ENV
    )
    if not candidate:
        raise SimError(
            "backend 'batch' requires 2am's batch-fleet-provision.sh -- set "
            "request.batch.provision_script_path or the "
            f"${PROVISION_SCRIPT_ENV} environment variable (see "
            "docs/cli/sim.md's 'Batch backend')"
        )
    resolved = os.path.abspath(os.path.expanduser(candidate))
    if not os.path.isfile(resolved):
        raise SimError(
            f"backend 'batch': provision script not found at {resolved} "
            "(resolved from request.batch.provision_script_path or "
            f"${PROVISION_SCRIPT_ENV})"
        )
    return resolved


def _resolve_batch_config(
    request: dict[str, Any], *, corner_count: int, timeout_s: float
) -> BatchConfig:
    """Resolve every ``batch.*`` knob, raising
    :class:`~klayout_tools.sim.SimError` before any S3 write.

    Resolution order per field: ``request.batch.<field>``, then a
    ``KLT_BATCH_*`` environment variable, then 2am's own
    ``batch-fleet.env`` beside the provision script, then (for the profile
    and jobs prefix only) a documented literal default. The **bucket has no
    literal default** -- an unresolvable bucket is an error naming all three
    sources, never a guess (see this module's docstring).
    """
    from .sim import SimError

    batch_spec = request.get("batch") or {}
    provision_script = _resolve_provision_script(batch_spec)
    fleet = _read_fleet_config(provision_script)

    bucket = (
        batch_spec.get("bucket")
        or os.environ.get(BUCKET_ENV)
        or fleet.get("BATCH_JOB_BUCKET")
    )
    if not bucket:
        raise SimError(
            "backend 'batch' requires a job bucket -- set request.batch.bucket, "
            f"the ${BUCKET_ENV} environment variable, or BATCH_JOB_BUCKET in the "
            f"{FLEET_CONFIG_FILENAME} beside the provision script"
        )
    return BatchConfig(
        provision_script=provision_script,
        bucket=bucket,
        jobs_prefix=(
            batch_spec.get("jobs_prefix")
            or fleet.get("BATCH_JOBS_PREFIX")
            or DEFAULT_BATCH_JOBS_PREFIX
        ),
        profile=(
            batch_spec.get("profile")
            or os.environ.get(PROFILE_ENV)
            or fleet.get("BATCH_SUBMIT_PROFILE")
            or DEFAULT_BATCH_PROFILE
        ),
        region=(
            batch_spec.get("region")
            or os.environ.get(REGION_ENV)
            or fleet.get("BATCH_REGION")
            or None
        ),
        poll_interval_s=float(
            batch_spec.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)
        ),
        poll_timeout_s=float(
            batch_spec.get(
                "poll_timeout_s",
                _default_batch_poll_timeout_s(corner_count, timeout_s),
            )
        ),
    )


# --------------------------------------------------------------------------- #
# S3 / launch transport (every call through an injectable runner)
# --------------------------------------------------------------------------- #


def _run_subprocess(
    argv: list[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)


def job_uri(config: BatchConfig, job_id: str) -> str:
    """``s3://<bucket>/<jobs-prefix>/<job-id>`` -- the one prefix every key
    this module reads or writes lives under."""
    return f"s3://{config.bucket}/{config.jobs_prefix}/{job_id}"


def new_job_id() -> str:
    """A fresh job id. Constrained to ``[A-Za-z0-9._-]+`` because it becomes
    both an S3 prefix and an EC2 tag (``batch-fleet-provision.sh``'s
    ``cmd_launch`` refuses anything else)."""
    return f"klt-sim-{uuid.uuid4().hex[:12]}"


def _aws_argv(config: BatchConfig, *args: str) -> list[str]:
    argv = [config.aws_binary]
    if config.region:
        argv += ["--region", config.region]
    argv += ["--profile", config.profile]
    return argv + list(args)


def _run_checked(
    runner: remote_transport.CommandRunner,
    argv: list[str],
    timeout_s: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(argv, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise BatchError(f"{label} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise BatchError(f"{label} could not be spawned: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise BatchError(f"{label} failed (exit {result.returncode}): {detail}")
    return result


def submit_job(
    config: BatchConfig,
    job_id: str,
    spec: BatchJobSpec,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> None:
    """Upload ``inputs/**`` and then ``job.json`` to
    ``s3://<bucket>/<jobs>/<job-id>/``.

    ``job.json`` is uploaded **last**, on purpose: it is the object
    ``batch-fleet-provision.sh launch`` asserts exists (``s3api
    head-object``) and the harness fetches first, so writing it last makes
    the submission atomic from the fleet's point of view -- a launch can
    never observe a job spec whose inputs are still uploading.
    """
    run = runner if runner is not None else _run_subprocess
    prefix = job_uri(config, job_id)
    with tempfile.TemporaryDirectory(prefix="klt-batch-") as staging:
        for item in spec.inputs:
            local_path = item.local_path
            if local_path is None:
                local_path = os.path.join(staging, item.name)
                with open(local_path, "w", encoding="utf-8") as handle:
                    handle.write(item.content or "")
            _run_checked(
                run,
                _aws_argv(
                    config,
                    "s3",
                    "cp",
                    local_path,
                    f"{prefix}/inputs/{item.name}",
                    "--only-show-errors",
                ),
                _BATCH_AWS_TIMEOUT_S,
                f"aws s3 cp ({item.label} input)",
            )
        job_json_path = os.path.join(staging, "job.json")
        with open(job_json_path, "w", encoding="utf-8") as handle:
            json.dump(spec.to_job_json(), handle)
        _run_checked(
            run,
            _aws_argv(
                config,
                "s3",
                "cp",
                job_json_path,
                f"{prefix}/job.json",
                "--only-show-errors",
            ),
            _BATCH_AWS_TIMEOUT_S,
            "aws s3 cp (job.json)",
        )


def launch_job(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> None:
    """Shell out to 2am's ``batch-fleet-provision.sh launch --job <id>
    --apply --profile <profile>`` -- exactly one invocation per job.

    Shelling out (rather than issuing a native ``CreateFleet``) is the
    recorded decision for this backend: ``launch`` is not a thin API
    wrapper. It enforces the diversification floors (>= 3 AZs, >= 3 instance
    types, refused *before* any AWS call), resolves the live subnet/AZ
    pool cross-product, retries a capacity shortfall, and -- most
    importantly -- enforces ``BATCH_MAX_CONCURRENT_INSTANCES`` against a
    live ``describe-instances``, the cap that protects the *shared* fleet
    budget from a runaway submitter. Re-deriving any of that here would
    duplicate already-tested logic, and skipping it would be a safety
    regression.
    """
    run = runner if runner is not None else _run_subprocess
    argv = [
        config.provision_script,
        "launch",
        "--job",
        job_id,
        "--apply",
        "--profile",
        config.profile,
    ]
    if config.region:
        argv += ["--region", config.region]
    _run_checked(run, argv, _BATCH_LAUNCH_TIMEOUT_S, "batch-fleet-provision.sh launch")


def _read_status(
    config: BatchConfig,
    job_id: str,
    run: remote_transport.CommandRunner,
) -> dict[str, Any] | None:
    """One ``aws s3 cp <prefix>/status.json -`` read.

    Returns ``None`` -- meaning "keep waiting", never an error -- when the
    object does not exist yet (the instance has not booted) or holds a
    partially-uploaded document: ``status.json`` is written repeatedly by
    the harness, so a transient unreadable read is expected, not a failure.
    """
    argv = _aws_argv(config, "s3", "cp", f"{job_uri(config, job_id)}/status.json", "-")
    try:
        result = run(argv, _BATCH_AWS_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        status = json.loads(result.stdout or "")
    except (TypeError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def poll_status(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Poll ``status.json`` until the job reaches a terminal state
    (:data:`BATCH_TERMINAL_STATES`) and return that status document.

    ``running`` and ``interrupted`` both mean *wait*
    (:data:`BATCH_WAITING_STATES`). ``interrupted`` specifically is **not**
    re-submitted here: Spot reclaimed the instance and 2am's own scheduled
    ``reconcile`` re-launches the job -- a client that also re-launched
    would double-spend the shared fleet budget.

    Raises :class:`BatchPollTimeout` once ``config.poll_timeout_s`` elapses
    (the caller reports unrun corners rather than a failed sweep), and
    :class:`BatchError` on a state outside the documented set -- a contract
    violation, surfaced loudly rather than waited on forever.
    """
    run = runner if runner is not None else _run_subprocess
    started = monotonic()
    last_state: str | None = None
    while True:
        status = _read_status(config, job_id, run)
        if status is not None:
            last_state = str(status.get("state") or "")
            if last_state in BATCH_TERMINAL_STATES:
                return status
            if last_state not in BATCH_WAITING_STATES:
                raise BatchError(
                    f"job {job_id}: status.json reported unknown state "
                    f"{last_state!r} (expected one of "
                    f"{', '.join(BATCH_WAITING_STATES + BATCH_TERMINAL_STATES)})"
                )
        elapsed = monotonic() - started
        if elapsed >= config.poll_timeout_s:
            raise BatchPollTimeout(
                f"job {job_id} did not reach a terminal state within "
                f"{config.poll_timeout_s:g}s (last observed state: "
                f"{last_state or 'no status.json yet'})",
                last_state=last_state,
            )
        sleep(config.poll_interval_s)


def collect_outputs(
    config: BatchConfig,
    job_id: str,
    local_dir: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> str:
    """Pull ``s3://<bucket>/<jobs>/<job-id>/outputs/`` into ``local_dir``
    and return it. ``outputs/**`` is the only structured collection channel
    the harness offers (``harness.log`` mixes the harness's own log lines
    with the job command's, and is not parsed here)."""
    run = runner if runner is not None else _run_subprocess
    os.makedirs(local_dir, exist_ok=True)
    _run_checked(
        run,
        _aws_argv(
            config,
            "s3",
            "cp",
            f"{job_uri(config, job_id)}/outputs/",
            local_dir,
            "--recursive",
            "--only-show-errors",
        ),
        _BATCH_AWS_TIMEOUT_S,
        "aws s3 cp (outputs)",
    )
    return local_dir


def read_collected_report(local_dir: str) -> dict[str, Any]:
    """Read the `klt sim` JSON report the job command redirected to
    ``$EDA_OUTPUT_DIR/report.json`` out of the collected tree."""
    path = os.path.join(local_dir, BATCH_REPORT_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except OSError as exc:
        raise BatchError(
            f"collected outputs hold no {BATCH_REPORT_FILENAME} "
            f"({path}) -- the job command never wrote its report"
        ) from exc
    except ValueError as exc:
        raise BatchError(
            f"collected {BATCH_REPORT_FILENAME} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise BatchError(f"collected {BATCH_REPORT_FILENAME} is not a JSON object")
    return report


# --------------------------------------------------------------------------- #
# Artifact collection
# --------------------------------------------------------------------------- #


def _install_batch_artifacts(
    corners: list[dict[str, Any]], *, collected_dir: str, artifacts_dir: str
) -> None:
    """Move the collected ``outputs/artifacts/`` tree into the caller's own
    artifacts directory and rewrite every corner's ``artifacts.*`` path to
    point at it.

    The rewrite itself is :func:`sim_remote._rewrite_remote_artifact_paths`,
    reused unmodified -- only the *remote root* has to be recovered
    differently. The `remote` backend knows its remote job directory (it
    chose it); here the job instance's ``$EDA_OUTPUT_DIR`` is picked by the
    harness at boot and never reported back, so the root is recovered from
    the reported paths themselves: every one of them is
    ``<$EDA_OUTPUT_DIR>/artifacts/...`` because
    :func:`_build_batch_job_spec` is what pointed ``--outdir`` there.
    """
    source = os.path.join(collected_dir, BATCH_ARTIFACTS_DIRNAME)
    if not os.path.isdir(source):
        return
    os.makedirs(artifacts_dir, exist_ok=True)
    shutil.copytree(source, artifacts_dir, dirs_exist_ok=True)
    marker = f"/{BATCH_ARTIFACTS_DIRNAME}/"
    remote_root: str | None = None
    for corner in corners:
        for value in (corner.get("artifacts") or {}).values():
            if value and marker in value:
                remote_root = value[: value.rindex(marker) + len(marker) - 1]
                break
        if remote_root is not None:
            break
    if remote_root is None:
        return
    _rewrite_remote_artifact_paths(
        corners, remote_root=remote_root, local_root=artifacts_dir
    )


# --------------------------------------------------------------------------- #
# One job: submit -> wait -> collect
# --------------------------------------------------------------------------- #


def _batch_environment(
    config: BatchConfig, job_id: str, status: dict[str, Any]
) -> dict[str, Any]:
    """The additive ``environment.remote`` block a batch job reports -- the
    same slot the `remote` backend populates (one block for ``hosts == 1``,
    a ``fleet[]`` array under sharding), carrying what ``status.json``
    actually observed rather than what was requested."""
    return {
        "provider": "aws-batch-fleet",
        "job_id": job_id,
        "bucket": config.bucket,
        "region": status.get("region") or config.region,
        "instance_id": status.get("instance_id"),
        "instance_type": status.get("instance_type"),
        "availability_zone": status.get("availability_zone"),
        "ami_id": status.get("ami_id"),
        "lifecycle": status.get("lifecycle"),
        "spot": status.get("spot"),
        "state": status.get("state"),
        "exit_code": status.get("exit_code"),
        "concurrency": status.get("concurrency"),
        "physical_cores": status.get("physical_cores"),
        "elapsed_seconds": status.get("elapsed_seconds"),
    }


def _status_exit_code(status: dict[str, Any]) -> int | None:
    """``status.json``'s ``exit_code`` as an int -- the harness writes it as
    a string (and as ``""`` when the job never ran)."""
    try:
        return int(str(status.get("exit_code", "")).strip())
    except (TypeError, ValueError):
        return None


def _job_failure_corners(
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    status: dict[str, Any],
    job_id: str,
) -> list[dict[str, Any]]:
    """Every unit of a job that reached ``failed``/``timeout`` without
    writing a usable report, reported through the shared unrun-corner shape
    (``batch_job_failed``/``batch_job_timeout``) rather than aborting the
    sweep. The client never retries these: a genuine job failure is
    terminal, and an ``interrupted`` job (the only re-runnable state) is
    2am's ``reconcile``'s business, not the client's."""
    state = str(status.get("state") or "")
    code = "batch_job_timeout" if state == "timeout" else "batch_job_failed"
    detail = str(status.get("detail") or "").strip()
    message = f"batch job {job_id} finished {state!r} without a usable report" + (
        f": {detail}" if detail else ""
    )
    return [
        _unrun_corner_report(point, measurements_spec, code, message=message)
        for point in corner_points
    ]


def _poll_timeout_corners(
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    exc: BatchPollTimeout,
) -> list[dict[str, Any]]:
    """Every unit of a job whose wall-clock poll budget elapsed, reported
    through the shared unrun-corner shape (``batch_poll_timeout``). The job
    may well still be running on the fleet -- which is exactly why this is
    not a ``SimError``: the sweep reports what it knows instead of claiming
    the run failed."""
    return [
        _unrun_corner_report(
            point, measurements_spec, "batch_poll_timeout", message=str(exc)
        )
        for point in corner_points
    ]


def _run_batch_job(
    *,
    config: BatchConfig,
    job_id: str,
    corner_points: list[CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    explicit_points: list[CornerPoint] | None = None,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Any = time.sleep,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """Submit, wait for, and collect **one** batch job's worth of corners --
    the shared body of the single-job ``batch`` backend (:func:`_run_batch`)
    and one shard of a sharded run (:func:`_run_batch_fleet`).

    ``explicit_points`` is threaded to
    :func:`sim_remote._build_remote_request` so a shard's job runs exactly
    its own slice (with already-derived Monte Carlo seeds) instead of
    re-expanding the whole matrix on the job instance -- ``None`` (the
    single-job case) forwards ``corners``/``monte_carlo`` unchanged, exactly
    as ``_run_remote`` does.

    Returns the same ``(corners, engine_version, remote_environment)``
    triple every backend returns. A terminal job failure or a poll-budget
    overrun returns *unrun corner reports* rather than raising; only a
    transport failure (a failed upload/launch/collect, an unreadable report,
    an undocumented status state) raises :class:`BatchError`.
    """
    remote_request = _build_remote_request(
        request,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        explicit_points=explicit_points,
    )
    # The `batch` block is submit-side configuration (bucket, profile, the
    # launch script's path on *this* host); it is meaningless on the job
    # instance, which runs `local-parallel`. `_build_remote_request` drops
    # `remote` for the same reason and is reused verbatim otherwise.
    remote_request.pop("batch", None)

    spec = _build_batch_job_spec(
        remote_request,
        netlist_path,
        corner_count=len(corner_points),
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
    )
    submit_job(config, job_id, spec, runner=runner)
    launch_job(config, job_id, runner=runner)
    try:
        status = poll_status(config, job_id, runner=runner, sleep=sleep)
    except BatchPollTimeout as exc:
        return (
            _poll_timeout_corners(corner_points, measurements_spec, exc),
            None,
            {
                "provider": "aws-batch-fleet",
                "job_id": job_id,
                "bucket": config.bucket,
                "region": config.region,
                "state": exc.last_state,
            },
        )

    environment = _batch_environment(config, job_id, status)
    if status.get("state") != "done" and (
        _status_exit_code(status) not in _BATCH_SIM_SUCCESS_EXIT_CODES
    ):
        return (
            _job_failure_corners(corner_points, measurements_spec, status, job_id),
            None,
            environment,
        )

    with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
        collect_outputs(config, job_id, collected, runner=runner)
        report = read_collected_report(collected)
        corners = report.get("corners") or []
        if keep_artifacts:
            _install_batch_artifacts(
                corners, collected_dir=collected, artifacts_dir=artifacts_dir
            )
    engine_version = (report.get("environment") or {}).get("engine_version")
    return corners, engine_version, environment


# --------------------------------------------------------------------------- #
# Backend entry points
# --------------------------------------------------------------------------- #


def _run_batch(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    max_workers: int | None,
    request: dict[str, Any],
    deadline: float | None = None,
    initial_ppid: int | None = None,
    checkpoint: _Checkpoint | None = None,
    probe_abort: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``batch`` backend: submit the whole corner matrix to 2am's EDA
    batch fleet as one job and collect its report back out of S3.

    Like ``remote``, this is **the same code path as ``local-parallel``, run
    on a different box** -- the job command is a plain
    ``klt sim ... --backend local-parallel --format json`` invocation on the
    fleet's own pinned image, so corner expansion, ordering, measurement
    extraction, and pass/fail classification are never reimplemented here.
    Unlike ``remote``, this process provisions nothing, holds no SSH key,
    and needs no ``RunInstances`` authority: it writes an S3 job contract
    and asks 2am's launch script to acquire diversified Spot capacity for
    it.

    ``analysis``/``measurements_spec``/``models_lib``/``max_workers`` are
    accepted for signature parity with the other backends
    (``measurements_spec`` is additionally *used*, to shape an unrun-corner
    report when a job fails); the rest are already embedded in ``request``
    or resolved by the job instance itself, exactly as for ``remote``.

    ``deadline``/``initial_ppid``/``checkpoint``/``probe_abort`` are
    likewise accepted but not honored, for the same reason ``_run_remote``
    does not honor them: the corner dispatch loop runs in a *different*
    process on a machine this function only observes through ``status.json``
    (``options.resume`` is rejected up front by ``run_sim`` for exactly this
    reason). The batch path's own wall-clock bound is
    ``request.batch.poll_timeout_s``, whose overrun reports every unit
    through the same unrun-corner shape.
    """
    from .sim import SimError

    del analysis, models_lib, max_workers  # see docstring
    del deadline, initial_ppid, checkpoint, probe_abort  # not honored

    config = _resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )
    try:
        return _run_batch_job(
            config=config,
            job_id=new_job_id(),
            corner_points=corner_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
        )
    except BatchError as exc:
        raise SimError(f"batch backend failed: {exc}") from exc


def _run_batch_fleet(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    hosts: int,
    measurements_spec: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``batch`` backend's ``hosts > 1`` dispatch: one **job** per shard.

    Structurally the ``remote`` fleet's shape (:func:`sim_remote._run_remote_fleet`)
    with the provisioning half removed -- ``remote`` must launch, guard, and
    tear down K instances itself, whereas a batch shard is K independent job
    contracts the fleet schedules on its own diversified Spot capacity. That
    leaves exactly the shard/merge engine, so this reuses
    :func:`sim_remote._run_sharded` unmodified: contiguous shards in global
    unit order, concurrent dispatch, deterministic merge by shard index, and
    a shard whose job raises reported unit-by-unit via
    :func:`sim_remote._lost_shard_corner` instead of aborting its siblings.
    ``environment.remote`` becomes the same ``fleet[]`` array a sharded
    ``remote`` run produces.

    ``hosts`` exceeding the unit count is rejected up front, mirroring
    ``_run_remote_fleet``: every shard is a real billed instance, so there
    is no benign reading of "more hosts than units".
    """
    from .sim import SimError

    if hosts > len(corner_points):
        raise SimError(
            f"remote.hosts ({hosts}) exceeds the number of units to "
            f"dispatch ({len(corner_points)}) -- an idle fleet member would "
            "still be billed; use hosts <= corner/sample count"
        )

    config = _resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )

    def _shard_runner(
        shard_points: list[CornerPoint],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
        # The job id doubles as the shard's artifacts subdirectory, so
        # per-shard artifact trees never collide and each one is traceable
        # back to the S3 prefix it was collected from.
        job_id = new_job_id()
        shard_artifacts_dir = (
            os.path.join(artifacts_dir, job_id) if keep_artifacts else artifacts_dir
        )
        return _run_batch_job(
            config=config,
            job_id=job_id,
            corner_points=shard_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=shard_artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
            explicit_points=shard_points,
        )

    return _run_sharded(
        shard_runner=_shard_runner,
        corner_points=corner_points,
        hosts=hosts,
        measurements_spec=measurements_spec,
    )


#: Re-exported so ``sim.py``'s own module namespace keeps the same
#: "every shard/merge helper is reachable from `klayout_tools.sim`" property
#: the `remote` backend established -- see that module's import block.
__all__ = [
    "BATCH_ARTIFACTS_DIRNAME",
    "BATCH_JOB_TOOL",
    "BATCH_NETLIST_FILENAME",
    "BATCH_REPORT_FILENAME",
    "BATCH_REQUEST_FILENAME",
    "BATCH_TERMINAL_STATES",
    "BATCH_WAITING_STATES",
    "BatchConfig",
    "BatchError",
    "BatchJobInput",
    "BatchJobSpec",
    "BatchPollTimeout",
    "collect_outputs",
    "job_uri",
    "launch_job",
    "new_job_id",
    "poll_status",
    "read_collected_report",
    "submit_job",
]
