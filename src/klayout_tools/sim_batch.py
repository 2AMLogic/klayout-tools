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

klt writes its ``--format json`` *error* envelope to **stderr**, not stdout
(``cli.output.emit_error``), so the same ``cmd`` also tees stderr into
``$EDA_OUTPUT_DIR/stderr.log`` -- still forwarding it to the harness's
stderr, so ``harness.log`` is unchanged. A failed job's own error envelope
is recovered from that file (see :func:`_recover_failed_job_corners`).

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
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import _provenance, remote_transport, sim_staging
from .remote_launcher import (
    ASSUMED_THREADS_PER_CORNER,
    RemoteLaunchError,
    ami_pdk_key,
)
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

#: Filename the job command tees `klt sim`'s stderr to, under
#: ``$EDA_OUTPUT_DIR`` -- klt's JSON *error* envelope goes to stderr
#: (``cli.output.emit_error``), so this is where a failed job's own reason is
#: recovered from. The tee keeps forwarding stderr, so ``harness.log`` still
#: carries it too.
BATCH_STDERR_FILENAME = "stderr.log"

#: Filename the job command's runner-version preflight writes under
#: ``$EDA_OUTPUT_DIR`` (issue #2719): the ``klt --version`` the fleet image
#: actually runs, recorded *before* the simulation starts so it is collectable
#: even when the preflight rejects the job.
BATCH_RUNNER_IDENTITY_FILENAME = "runner_identity.json"

#: ``error.code`` the preflight's error envelope carries when the runner's
#: ``klt`` build differs from the submitting client's / when its identity
#: cannot be established. Surfaced as ``runner_code`` on the job-failure
#: diagnostic.
BATCH_RUNNER_MISMATCH_CODE = "batch_runner_version_mismatch"
BATCH_RUNNER_UNKNOWN_CODE = "batch_runner_version_unknown"

#: Exit code the preflight rejects with. Deliberately outside
#: :data:`_BATCH_SIM_SUCCESS_EXIT_CODES` so the job is never mistaken for a
#: sweep that ran.
_BATCH_PREFLIGHT_REJECT_EXIT = 87

#: ``request.batch.runner_version_check`` values: ``enforce`` (default) rejects
#: a job whose runner ``klt`` differs from / cannot be compared with the
#: client's before any simulation runs; ``warn`` runs anyway and reports the
#: skew in ``environment.remote``.
RUNNER_VERSION_CHECK_MODES = ("enforce", "warn")

#: Upper bound on how much of the collected stderr file recovery reads (its
#: tail). The envelope is the last thing klt writes; anything earlier is
#: progress/warning noise that is never parsed or copied into the JSON.
_BATCH_STDERR_TAIL_BYTES = 64 * 1024

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

#: Machine-readable ``error.code`` a launch refused for lack of Spot capacity
#: carries (issue #2721): a retryable, fleet-wide infrastructure condition,
#: distinct from a request error a caller must not retry.
BATCH_NO_CAPACITY_CODE = "batch_no_capacity"

#: Capacity-wait backoff (``request.batch.capacity_wait_s``, issue #2721):
#: the first re-launch waits ~:data:`_CAPACITY_BACKOFF_BASE_S`, each further
#: one doubles, capped at :data:`_CAPACITY_BACKOFF_CAP_S`. Every delay is
#: jittered into ``[0.5, 1.0) x`` its nominal value so concurrent submitters
#: refused by the same shortage do not re-launch in lock-step.
_CAPACITY_BACKOFF_BASE_S = 30.0
_CAPACITY_BACKOFF_CAP_S = 600.0

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

    ``code`` is an optional machine-readable classification (issue #2721),
    carried through to that ``SimError`` and from there to the error
    envelope's ``error.code``. ``None`` -- the common case -- means
    "unclassified"; today only :data:`BATCH_NO_CAPACITY_CODE` is set.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


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

    ``name`` must be a relative name that stays inside ``inputs/`` -- the
    same rule (and the same validator)
    ``remote_transport.JobInput.remote_name`` applies, since the same
    netlist-declared ``.include``/``.inc`` closure feeds both transports
    (issue #2485).
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
        remote_transport._validate_job_relative_name(
            self.name, field="BatchJobInput.name"
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


def _runner_preflight_script(client_version: str, *, enforce: bool) -> str:
    """The shell prefix of the job command that pins the runner's ``klt``
    identity (issue #2719).

    It is generated by the *submitting* client and uses only ``klt
    --version`` -- which every klt release has -- so it runs on a fleet image
    that predates any request key the client accepts. It (1) records the
    runner's ``klt --version`` build string in
    :data:`BATCH_RUNNER_IDENTITY_FILENAME` (``null`` when it cannot be read),
    and (2) under ``enforce`` writes a klt error envelope to ``report.json``
    and exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` *before* ``klt sim`` runs
    unless the runner's build string equals the client's exactly. The runner
    string is reduced to ``[A-Za-z0-9._+ -]`` so it is safe inside the JSON
    the script prints.

    Redirects here deliberately have no space before ``>`` so the
    ``> "$EDA_OUTPUT_DIR/report.json"`` marker of the main command stays the
    first such occurrence.
    """
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", client_version):
        raise ValueError(f"unusable client klt version {client_version!r}")
    out = '"$EDA_OUTPUT_DIR/{}"'
    identity_path = out.format(BATCH_RUNNER_IDENTITY_FILENAME)
    report_path = out.format(BATCH_REPORT_FILENAME)
    lines = [
        "rv=$(klt --version 2>/dev/null | head -n 1 | tr -cd 'A-Za-z0-9._+ -'"
        " | cut -c1-80); rv=${rv#klt }; rv=${rv# }",
        'if [ -z "$rv" ]; then rj=null; rcode=' + BATCH_RUNNER_UNKNOWN_CODE + "; "
        'rmsg="the fleet runner klt version could not be established"; '
        'elif [ "$rv" = "' + client_version + '" ]; then rj="\\"$rv\\""; rcode=; '
        "rmsg=; "
        'else rj="\\"$rv\\""; rcode=' + BATCH_RUNNER_MISMATCH_CODE + "; "
        'rmsg="the fleet runner runs klt $rv but the submitting client is '
        + client_version
        + '"; fi',
        'printf \'{"runner_klt_version": %s, "client_klt_version": "'
        + client_version
        + '"}\\n\' "$rj" >'
        + identity_path,
    ]
    if enforce:
        lines.append(
            'if [ -n "$rcode" ]; then '
            'rmsg="$rmsg -- the request was not run (update the runner image '
            'or use a compatible client)"; '
            'printf \'{"schema_version": 1, "error": {"command": "sim", '
            '"code": "%s", "message": "%s"}}\\n\' '
            '"$rcode" "$rmsg" >' + report_path + "; "
            'echo "$rmsg" >&2; '
            "exit " + str(_BATCH_PREFLIGHT_REJECT_EXIT) + "; fi"
        )
    return "; ".join(lines) + "; "


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
    :func:`sim_staging.stage_sim_netlist` call
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
    command = _runner_preflight_script(
        client_version if client_version is not None else _provenance._klt_version(),
        enforce=version_check == "enforce",
    ) + (
        f'klt sim "$EDA_INPUT_DIR/{BATCH_REQUEST_FILENAME}" '
        "--backend local-parallel --format json"
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
    if keep_artifacts:
        command += f' --outdir "$EDA_OUTPUT_DIR/{BATCH_ARTIFACTS_DIRNAME}"'
    # stdout -> report.json (the success channel); stderr is teed into
    # stderr.log (klt's error envelope lands there) *and* forwarded, so
    # harness.log still sees it. The harness runs `bash -c "$JOB_CMD"`, so
    # process substitution is available; `wait $!` lets the tee drain before
    # the harness uploads outputs/, and `exit $rc` keeps klt's own exit code
    # (0/3/4/...) as the job's.
    command += (
        f' > "$EDA_OUTPUT_DIR/{BATCH_REPORT_FILENAME}"'
        f' 2> >(tee "$EDA_OUTPUT_DIR/{BATCH_STDERR_FILENAME}" >&2)'
        "; rc=$?; wait $! 2>/dev/null; exit $rc"
    )
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
    #: Wall-clock budget :func:`launch_job` keeps re-launching through a
    #: ``batch_no_capacity`` refusal (``request.batch.capacity_wait_s``,
    #: issue #2721). ``0`` -- the default -- is one launch attempt.
    capacity_wait_s: float = 0.0
    #: ``request.batch.runner_version_check`` (issue #2719).
    runner_version_check: str = "enforce"


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


def _validate_batch_pdk(request: dict[str, Any]) -> None:
    """Refuse a ``models.pdk`` the batch fleet has no image for, **before**
    :func:`_resolve_batch_config`'s caller writes anything to S3 (issue
    #2523).

    The check is :func:`remote_launcher.ami_pdk_key` itself -- not a copy of
    :data:`~klayout_tools.remote_launcher.SUPPORTED_PDKS` or its
    family-reduction rule -- so the ``batch`` and ``remote`` backends can
    never silently disagree about which PDKs are supported: a job instance
    boots an image from the same published set ``remote`` provisions from,
    which is why ``remote``'s pre-``run-instances`` refusal
    (``RemoteLauncher.provision`` -> :func:`remote_launcher.resolve_ami`)
    and this one must agree by construction. A variant reducible to a
    published family is accepted on the same terms (``"gf180mcuC"`` ->
    ``"gf180mcu"``); the *variant* keeps flowing into ``job.json``'s
    ``pdk_variant`` (see :func:`_build_batch_job_spec`), because the job
    instance resolves the PDK locally exactly as the ``local`` backend does.

    A request that names **no** PDK at all is untouched: without a
    ``corners.process`` axis there is no model library to resolve, so there
    is nothing to validate and nothing to refuse.
    """
    from .sim import SimError

    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        return
    try:
        ami_pdk_key(str(pdk), backend="batch")
    except RemoteLaunchError as exc:
        raise SimError(str(exc)) from exc


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

    ``models.pdk`` is validated here too (:func:`_validate_batch_pdk`),
    first: a PDK the fleet publishes no image for has no compliant execution
    path on this backend at all, so it is a property of the *request* rather
    than of this host's fleet configuration and should be reported even when
    the provision script or bucket is also unresolvable.
    """
    from .sim import SimError

    _validate_batch_pdk(request)
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
    capacity_wait_s = _resolve_capacity_wait_s(batch_spec)
    runner_version_check = _resolve_runner_version_check(batch_spec)
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
        capacity_wait_s=capacity_wait_s,
        runner_version_check=runner_version_check,
    )


def _resolve_runner_version_check(batch_spec: dict[str, Any]) -> str:
    """``request.batch.runner_version_check``: ``"enforce"`` (default) or
    ``"warn"``, else :class:`~klayout_tools.sim.SimError` before any S3 write."""
    from .sim import SimError

    value = batch_spec.get("runner_version_check", "enforce")
    if value not in RUNNER_VERSION_CHECK_MODES:
        raise SimError(
            "request.batch.runner_version_check must be one of "
            f"{', '.join(repr(m) for m in RUNNER_VERSION_CHECK_MODES)} "
            f"(got {value!r})"
        )
    return value


def _resolve_capacity_wait_s(batch_spec: dict[str, Any]) -> float:
    """``request.batch.capacity_wait_s``: a finite, non-negative number of
    seconds (default ``0``), else :class:`~klayout_tools.sim.SimError` --
    raised from :func:`_resolve_batch_config`, i.e. before any S3 write.

    A ``bool`` is refused even though it is an ``int`` subclass: ``true``
    reading as "wait one second" is never what a request author meant.
    """
    from .sim import SimError

    value = batch_spec.get("capacity_wait_s", 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SimError(
            "request.batch.capacity_wait_s must be a non-negative number of "
            f"seconds (got {value!r})"
        )
    return float(value)


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
    *,
    classify: Callable[[str], str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one transport command; any non-zero exit becomes a
    :class:`BatchError`. ``classify``, when given, maps the failed command's
    stderr to the error's machine-readable ``code`` (``None`` = unclassified).
    """
    try:
        result = runner(argv, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise BatchError(f"{label} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise BatchError(f"{label} could not be spawned: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise BatchError(
            f"{label} failed (exit {result.returncode}): {detail}",
            code=classify(detail) if classify is not None else None,
        )
    return result


#: The fragment of 2am's ``batch-fleet-provision.sh launch`` refusal line
#: (``fail "no capacity in any of the N pools after K attempt(s) -- this is
#: the capacity-refusal case, ..."``) that identifies a Spot capacity refusal.
_CAPACITY_REFUSAL_MARKER = "no capacity in any of the"


def _classify_launch_failure(stderr: str) -> str | None:
    """:data:`BATCH_NO_CAPACITY_CODE` when a failed ``launch``'s stderr is
    the provisioner's capacity refusal, else ``None``.

    **Coupling:** this is a text match against 2am's
    ``infra/aws/batch-fleet-provision.sh`` (``cmd_launch``'s final
    ``fail "no capacity in any of the ..."``), because that script exits ``1``
    for *every* refusal -- the diversification floors, the concurrency cap,
    a missing ``job.json``, an IAM denial, and a capacity shortfall alike --
    and exposes no distinct exit code. Kept in this one helper so a change on
    the 2am side is a one-line fix here. Every other launch failure stays
    unclassified, so it is never mistaken for something worth retrying.
    """
    return BATCH_NO_CAPACITY_CODE if _CAPACITY_REFUSAL_MARKER in stderr else None


def _capacity_backoff_s(retry_index: int, jitter: Callable[[], float]) -> float:
    """The jittered, capped delay before capacity re-launch ``retry_index``
    (``0`` = the first re-launch). ``jitter`` returns a float in ``[0, 1)``."""
    nominal = min(
        _CAPACITY_BACKOFF_CAP_S, _CAPACITY_BACKOFF_BASE_S * (2.0**retry_index)
    )
    return nominal * (0.5 + 0.5 * jitter())


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
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    jitter: Callable[[], float] | None = None,
) -> None:
    """Shell out to 2am's ``batch-fleet-provision.sh launch --job <id>
    --apply --profile <profile>`` -- exactly one invocation per job, unless
    ``config.capacity_wait_s`` opts into waiting out a capacity refusal.

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

    **Capacity wait (issue #2721).** A launch the provisioner refuses for
    lack of Spot capacity raises :class:`BatchError` with
    ``code == "batch_no_capacity"`` (:func:`_classify_launch_failure`). With
    ``config.capacity_wait_s > 0`` such a refusal is instead retried -- the
    same ``launch`` invocation for the same job id, after a jittered, capped
    exponential backoff (:func:`_capacity_backoff_s`) -- until that budget
    elapses, then raised with the same code. Any *other* launch failure is
    raised at once, never retried. Re-invoking is safe because a refused
    launch creates no instance and closes the launch record it opened (2am's
    ``cmd_launch`` writes a ``start`` record and its matching ``end`` on that
    path), and 2am's own Spot-interruption ``reconcile`` already re-launches
    under an existing job id. The wait is measured on its own clock and is
    **not** charged to ``poll_timeout_s``, whose clock starts in
    :func:`poll_status` once a launch succeeds. ``sleep``/``monotonic``/
    ``jitter`` are injectable for tests; ``None`` resolves to
    :func:`time.sleep`/:func:`time.monotonic`/:func:`random.random` at call
    time.
    """
    run = runner if runner is not None else _run_subprocess
    sleep = sleep if sleep is not None else time.sleep
    monotonic = monotonic if monotonic is not None else time.monotonic
    jitter = jitter if jitter is not None else random.random
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
    started = monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            _run_checked(
                run,
                argv,
                _BATCH_LAUNCH_TIMEOUT_S,
                "batch-fleet-provision.sh launch",
                classify=_classify_launch_failure,
            )
            return
        except BatchError as exc:
            if exc.code != BATCH_NO_CAPACITY_CODE or config.capacity_wait_s <= 0:
                raise
            remaining = config.capacity_wait_s - (monotonic() - started)
            if remaining <= 0:
                raise BatchError(
                    f"{exc} (capacity wait budget of {config.capacity_wait_s:g}s "
                    f"exhausted after {attempts} launch attempt(s))",
                    code=BATCH_NO_CAPACITY_CODE,
                ) from exc
            sleep(min(_capacity_backoff_s(attempts - 1, jitter), remaining))


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


def _read_runner_identity(local_dir: str | None) -> dict[str, Any]:
    """The ``environment.remote`` runner-identity fields (issue #2719).

    ``runner_klt_version`` is what the fleet image's own ``klt --version``
    printed -- never the client's. It is ``None`` when the identity file is
    absent, malformed, or the preflight could not read a version, and
    ``runner_compatibility`` is then ``"unknown"``. Otherwise the runner's
    build string is compared with the client's by exact equality
    (``"match"`` / ``"mismatch"``). The comparison is done here, not trusted
    from the file. A non-match carries ``runner_compatibility_warning``.
    Equal strings do not prove identical code (two builds can share one
    version string); see docs/cli/sim.md.
    """
    client = _provenance._klt_version()
    runner: str | None = None
    if local_dir is not None:
        try:
            with open(
                os.path.join(local_dir, BATCH_RUNNER_IDENTITY_FILENAME),
                encoding="utf-8",
            ) as handle:
                document = json.load(handle)
            value = document.get("runner_klt_version")
            if isinstance(value, str) and value.strip():
                runner = value.strip()
        except (OSError, ValueError, AttributeError):
            runner = None
    if runner is None:
        status = "unknown"
        warning = (
            "the fleet runner's klt version could not be established; request "
            "options newer than the runner may have been ignored"
        )
    elif runner == client:
        status, warning = "match", None
    else:
        status = "mismatch"
        warning = (
            f"the fleet runner ran klt {runner} but the submitting client is "
            f"{client}; request options the runner does not know may have "
            "been ignored -- update the runner image or use a compatible client"
        )
    fields: dict[str, Any] = {
        "runner_klt_version": runner,
        "client_klt_version": client,
        "runner_compatibility": status,
    }
    if warning is not None:
        fields["runner_compatibility_warning"] = warning
    return fields


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
    *,
    runner_error: dict[str, Any] | None = None,
    collection_note: str | None = None,
) -> list[dict[str, Any]]:
    """Every unit of a job that reached ``failed``/``timeout`` without
    writing a usable report, reported through the shared unrun-corner shape
    (``batch_job_failed``/``batch_job_timeout``) rather than aborting the
    sweep. The client never retries these: a genuine job failure is
    terminal, and an ``interrupted`` job (the only re-runnable state) is
    2am's ``reconcile``'s business, not the client's.

    ``runner_error`` is the recovered ``error`` object of the job's own klt
    error envelope (see :func:`_classify_collected_report`); its message is
    appended to the diagnostic (its ``code``, when present, attached as
    ``runner_code``) so the actionable reason is visible without S3 access.
    ``collection_note`` records why recovery found nothing usable. Neither
    replaces the primary job-failure code or the failed status."""
    state = str(status.get("state") or "")
    code = "batch_job_timeout" if state == "timeout" else "batch_job_failed"
    detail = str(status.get("detail") or "").strip()
    if runner_error is not None:
        message = (
            f"batch job {job_id} finished {state!r}"
            + (f" ({detail})" if detail else "")
            + f": the job's klt reported: {runner_error['message']}"
        )
    else:
        message = f"batch job {job_id} finished {state!r} without a usable report" + (
            f": {detail}" if detail else ""
        )
        if collection_note:
            message += f" [output recovery: {collection_note}]"
    corners = [
        _unrun_corner_report(point, measurements_spec, code, message=message)
        for point in corner_points
    ]
    if runner_error is not None and runner_error.get("code"):
        for corner in corners:
            for diagnostic in corner.get("diagnostics") or []:
                if diagnostic.get("code") == code:
                    diagnostic["runner_code"] = runner_error["code"]
    return corners


def _classify_collected_report(
    report: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Classify one collected ``report.json`` object.

    Returns ``("report", None)`` for an ordinary ``klt sim`` report (a
    non-empty ``corners`` list of objects), ``("error", error)`` for a klt
    error envelope (``{"schema_version", "error": {"command", "message"[,
    "code"]}}`` -- docs/json-contract.md "Error shape"), and
    ``("unusable", None)`` for anything else. Truthiness of ``corners`` is
    deliberately not used to detect success."""
    if not isinstance(report, dict):
        return "unusable", None
    corners = report.get("corners")
    if (
        isinstance(corners, list)
        and corners
        and all(isinstance(item, dict) for item in corners)
    ):
        return "report", None
    error = report.get("error")
    if (
        "corners" not in report
        and isinstance(error, dict)
        and isinstance(error.get("message"), str)
        and error["message"].strip()
    ):
        recovered: dict[str, Any] = {"message": error["message"].strip()}
        if isinstance(error.get("code"), str):
            recovered["code"] = error["code"]
        return "error", recovered
    return "unusable", None


def _read_stderr_envelope(local_dir: str) -> tuple[dict[str, Any] | None, str]:
    """Recover the klt error envelope from the collected stderr file.

    ``emit_error`` writes ``json.dump(indent=2)`` plus a newline as the last
    thing on stderr, possibly after warning/progress lines, so the envelope
    is the trailing JSON object that starts at column 0. Only the file's
    last :data:`_BATCH_STDERR_TAIL_BYTES` are read, candidates are tried from
    the last ``{`` line backwards, and each is run through
    :func:`_classify_collected_report`. Returns ``(error, "")`` on success or
    ``(None, note)`` -- the note never contains raw stderr text."""
    path = os.path.join(local_dir, BATCH_STDERR_FILENAME)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _BATCH_STDERR_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, f"collected outputs hold no {BATCH_STDERR_FILENAME}"
    decoder = json.JSONDecoder()
    starts = [0] if text.startswith("{") else []
    starts += [m.start() + 1 for m in re.finditer(r"\n\{", text)]
    for start in reversed(starts):
        try:
            payload, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        kind, error = _classify_collected_report(payload)
        if kind == "error":
            return error, ""
    return None, f"{BATCH_STDERR_FILENAME} holds no klt error envelope"


def _recover_failed_job_corners(
    *,
    config: BatchConfig,
    job_id: str,
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    status: dict[str, Any],
    runner: remote_transport.CommandRunner | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bounded, collection-only recovery after a terminal job failure: one
    outputs download, one read of ``report.json`` and, when that holds no
    error envelope, one bounded read of :data:`BATCH_STDERR_FILENAME` (where
    klt actually writes its ``--format json`` error envelope). Never
    resubmits and never raises -- a collection problem is recorded on the
    fallback diagnostic while the primary job failure stays authoritative.
    Only the envelope's structured ``message``/``code`` reach the JSON;
    raw stderr text never does."""
    runner_error: dict[str, Any] | None = None
    note: str | None = None
    identity = _read_runner_identity(None)
    try:
        with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
            collect_outputs(config, job_id, collected, runner=runner)
            identity = _read_runner_identity(collected)
            try:
                kind, runner_error = _classify_collected_report(
                    read_collected_report(collected)
                )
                if kind == "report":
                    note = "collected report is not a klt error envelope"
                elif kind == "unusable":
                    note = "collected report has an unrecognized shape"
            except BatchError as exc:
                note = str(exc)
            if runner_error is None:
                runner_error, stderr_note = _read_stderr_envelope(collected)
                if runner_error is None:
                    note = (
                        f"{note}; {stderr_note} -- see harness.log under the "
                        f"job's S3 prefix"
                    )
    except BatchError as exc:
        note = str(exc)
    corners = _job_failure_corners(
        corner_points,
        measurements_spec,
        status,
        job_id,
        runner_error=runner_error,
        collection_note=note,
    )
    if runner_error is not None and runner_error.get("code") in (
        BATCH_RUNNER_MISMATCH_CODE,
        BATCH_RUNNER_UNKNOWN_CODE,
    ):
        # The preflight rejected the job before `klt sim` ran: carry the two
        # identities on each diagnostic so the cause is machine-readable.
        for corner in corners:
            for diagnostic in corner.get("diagnostics") or []:
                if diagnostic.get("runner_code") == runner_error["code"]:
                    diagnostic["runner_klt_version"] = identity["runner_klt_version"]
                    diagnostic["client_klt_version"] = identity["client_klt_version"]
    return corners, identity


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
    request_dir: str | None = None,
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
        request_dir=request_dir,
        version_check=config.runner_version_check,
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
                **_read_runner_identity(None),
            },
        )

    environment = _batch_environment(config, job_id, status)
    if status.get("state") != "done" and (
        _status_exit_code(status) not in _BATCH_SIM_SUCCESS_EXIT_CODES
    ):
        failed_corners, identity = _recover_failed_job_corners(
            config=config,
            job_id=job_id,
            corner_points=corner_points,
            measurements_spec=measurements_spec,
            status=status,
            runner=runner,
        )
        return failed_corners, None, {**environment, **identity}

    with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
        collect_outputs(config, job_id, collected, runner=runner)
        report = read_collected_report(collected)
        environment = {**environment, **_read_runner_identity(collected)}
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
    request_dir: str | None = None,
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
            request_dir=request_dir,
        )
    except BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc


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
    request_dir: str | None = None,
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
    # Stage (and discard) the whole job closure once, before any shard's
    # first S3 write: an unstageable include or model input (issue #2668)
    # must refuse the submit, not surface as K lost shards after some of
    # them already uploaded.
    sim_staging.stage_sim_job(
        request,
        netlist_path,
        request_dir=request_dir,
        netlist_staged_name=BATCH_NETLIST_FILENAME,
        reserved_names=(BATCH_REQUEST_FILENAME,),
        backend="batch",
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
            request_dir=request_dir,
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
    "BATCH_NO_CAPACITY_CODE",
    "BATCH_NETLIST_FILENAME",
    "BATCH_REPORT_FILENAME",
    "BATCH_RUNNER_IDENTITY_FILENAME",
    "BATCH_RUNNER_MISMATCH_CODE",
    "BATCH_RUNNER_UNKNOWN_CODE",
    "BATCH_STDERR_FILENAME",
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
