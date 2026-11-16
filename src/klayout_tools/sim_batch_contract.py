"""Batch job contract constants and exceptions.

First seam split out of :mod:`klayout_tools.sim_batch` (issue #4065), moved
verbatim, then split by section so each file stays within the size limit:
this module holds the constants and exceptions; ``sim_batch_contract_spec``,
``_worker``, ``_preflight`` and ``_jobspec`` hold the rest.
:mod:`klayout_tools.sim_batch` re-exports every name, so ``from
klayout_tools.sim_batch import _x`` keeps working. Names a test patches must be
patched in the module that defines them (e.g. ``_WORKER_CAPTURE_PROGRAM`` in
``sim_batch_contract_worker``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import (
    remote_transport,
)

if TYPE_CHECKING:
    pass

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
BATCH_RUNNER_FEATURE_CODE = "batch_runner_feature_unsupported"
#: ``error.code`` of the envelope the job command's EXIT trap writes to
#: ``report.json`` when the command exits non-zero having left neither a
#: report nor a klt error envelope (issue #3818).
BATCH_JOB_COMMAND_FAILED_CODE = "batch_job_command_failed"
#: Top-level report diagnostic under ``runner_version_check: "warn"`` naming
#: the request features the runner predates (issue #2901).
BATCH_RUNNER_FEATURE_PREDATED_CODE = "batch_runner_request_feature_predated"
#: A deck-list (``decks[]``, #2804) request needs a runner ``klt`` that knows
#: the deck-list contract; the major.minor gate cannot see that (issue #3303).
BATCH_RUNNER_LACKS_DECKS_CODE = "batch_runner_lacks_decks"

#: ``decks[]`` landed in ``f44df5ad`` (#2804), 307 commits after ``v0.7.0``
#: (``git describe``: ``v0.7.0-307-gf44df5ad``). A runner whose version is
#: ``0.7.0`` or earlier, or ``0.7.1.devN`` with ``N`` below this, predates it.
_DECKS_BASE_RELEASE = (0, 7)
_DECKS_FIRST_DEV = 307

#: ``klt place-and-route --backend`` (what every ``place-and-route`` batch job
#: ends in) landed in ``7fd087787`` (#3518), 1091 commits after ``v0.7.0``
#: (``git describe``: ``v0.7.0-1091-g7fd087787``). A runner that predates it
#: rejects the argument on every corner (issue #4124).
BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE = (
    "batch_runner_lacks_place_and_route_backend"
)
_PAR_BACKEND_FIRST_DEV = 1091

#: Exit code the preflight rejects with. Deliberately outside
#: :data:`_BATCH_SIM_SUCCESS_EXIT_CODES` so the job is never mistaken for a
#: sweep that ran.
_BATCH_PREFLIGHT_REJECT_EXIT = 87

#: ``request.batch.runner_version_check`` values: ``enforce`` (default) rejects
#: a job whose runner ``klt`` differs from / cannot be compared with the
#: client's before any simulation runs; ``warn`` runs anyway and reports the
#: skew in ``environment.remote``.
RUNNER_VERSION_CHECK_MODES = ("enforce", "warn")

#: Request feature -> minimum ``klt`` release whose runner understands it
#: (#2877). Pure data, importable without side effects, so a client-side field
#: preflight (#2901) can consume it. ``None`` means the feature exists only on
#: unreleased ``main`` (no git tag contains it yet) -- never a guessed version.
#: Versions were derived with ``git log -S<token>`` plus
#: ``git tag --contains <commit>``; the commit is cited per entry. The 0.5.0
#: fleet image predates every entry below that has a version.
REQUEST_FEATURE_MIN_RUNNER_VERSION: dict[str, str | None] = {
    # 328b552b feat(sim): add measurements[].expr (#2533); first in v0.7.0.
    "measurements[].expr": "0.7.0",
    # 31baf0bc per-section model libraries in a corners.process bundle (#2538);
    # first in v0.7.0. (The plain-string sections[] form dates from 9bea8456,
    # #357, v0.2.0 line.)
    "corners.process[].sections[] (object form)": "0.7.0",
    # 1b51954f options.ngspice_init (#2530); first in v0.7.0.
    "options.ngspice_init": "0.7.0",
    # 75f738422 options.ngspice_options (#3111); first in v0.8.0
    # (`git tag --contains 75f738422` -> v0.8.0 only, #4212).
    "options.ngspice_options": "0.8.0",
    # 7849a377c corners.params parameter axis (#3035); first in v0.8.0
    # (`git tag --contains 7849a377c` lists v0.8.0 only; `git describe` gives
    # v0.7.0-709-g7849a377c). Post-tag dev builds render as `0.7.1.devN`, which
    # compares below 0.8.0, so a 0.7.1.dev7xx+ runner that has the feature is
    # refused; that fails safe.
    "corners.params": "0.8.0",
    # b24141ef9 corners.units (#2993), after v0.7.0; first in v0.8.0
    # (`git tag --contains b24141ef9`). An older runner ignores the axis and
    # runs one default corner.
    "corners.units": "0.8.0",
    # 0380f2a05 corners.temperature_param, after v0.7.0; first in v0.8.0
    # (`git tag --contains 0380f2a05`).
    "corners.temperature_param": "0.8.0",
    # 8296828c options.osdi_preload (#2534); first in v0.7.0.
    "options.osdi_preload": "0.7.0",
    # 07440b8f options.stage_model_inputs (#2668); first in v0.7.0.
    "options.stage_model_inputs": "0.7.0",
    # d4d497ee ordered analysis_steps[] (#2482), 2026-10-09, after v0.7.0:
    # the only route to .meas cards on a noise analysis. Untagged.
    "analysis_steps[].measurements[] (noise analysis .meas cards)": None,
    # measurements[].search (#4121), after v0.7.0: a mandatory feature (an
    # older runner would drop the measurement). Untagged; its runner-side
    # detector is the "search" row of _RUNNER_REQUEST_FEATURES.
    "measurements[].search": None,
}

#: Upper bound on how much of the collected stderr file recovery reads (its
#: tail). The envelope is the last thing klt writes; anything earlier is
#: progress/warning noise that is never parsed or copied into the JSON.
_BATCH_STDERR_TAIL_BYTES = 64 * 1024

#: Bytes of ``stderr.log`` the job command's EXIT trap quotes into the
#: ``report.json`` envelope it writes when nothing else did (issue #3818).
_BATCH_JOB_FAILURE_TAIL_BYTES = 1500

#: ``$EDA_OUTPUT_DIR``-relative directory the job command points `klt sim`'s
#: ``--outdir`` at, so per-corner logs/rawfiles land inside the one tree the
#: harness collects (issue #4369: always, so a deck-abort log is readable;
#: ``options.keep_artifacts`` only gates installing them for the caller).
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
#: How long ``--attach`` waits with no ``status.json`` before saying on
#: stderr that the job looks never to have been started (issue #4291). Far
#: shorter than :data:`_BATCH_PROVISION_SLACK_S`: an attached job was
#: submitted earlier, so an instance that has not published a status by now
#: was most likely refused at launch (e.g. ``batch_concurrency_cap``).
_ATTACH_UNSTARTED_GRACE_S = 120.0

#: Fixed per-job slack :func:`_default_batch_job_timeout_s` adds to the
#: fully-serial unit budget for `klt` startup on the job instance.
_BATCH_JOB_OVERHEAD_S = 120.0

#: The fleet's whole-job ceiling when no named setting supplies one (issue
#: #2833). This is not a client-side guess: it is the value 2am's
#: ``batch-fleet-harness.sh`` itself falls back to when the instance's
#: ``MaxJobSeconds`` tag (written from ``BATCH_MAX_JOB_SECONDS``) is absent
#: or malformed. A report that used it says so
#: (``max_job_seconds_source: "default"``).
DEFAULT_BATCH_MAX_JOB_SECONDS = 3600.0

#: 2am's ``batch-fleet.env`` key for the whole-job ceiling -- the value its
#: ``launch`` tags the instance with and its harness kills the job at.
FLEET_MAX_JOB_SECONDS_KEY = "BATCH_MAX_JOB_SECONDS"

#: ``request.batch.job_ceiling_check`` values (issue #2833): ``"warn"``
#: (default) submits and reports a structured warning; ``"enforce"`` refuses
#: before any S3 write; ``"split"`` (issue #3446) reroutes an over-ceiling
#: ``hosts == 1`` request through the one-job-per-shard path instead.
JOB_CEILING_CHECK_MODES = ("warn", "enforce", "split")

#: Machine-readable ``error.code`` of an ``"enforce"`` refusal.
BATCH_JOB_CEILING_CODE = "batch_job_ceiling_exceeded"

#: Machine-readable ``error.code`` of a ``max_units_per_job`` the fleet's
#: ceiling cannot honor (issue #3446).
BATCH_MAX_UNITS_CODE = "batch_max_units_per_job_invalid"

#: Machine-readable ``error.code`` of an invalid
#: ``request.batch.max_concurrent_jobs`` (issue #3914).
BATCH_MAX_CONCURRENT_CODE = "batch_max_concurrent_jobs_invalid"

#: The ``environment.remote.job_ceiling.basis`` value: the assessment is the
#: request-derived fully-serial upper bound, never a measured runtime.
JOB_CEILING_BASIS = "conservative_serial_bound"

#: Per-invocation timeout for one `aws` CLI call (upload/poll/collect).
_BATCH_AWS_TIMEOUT_S = 900.0

#: Timeout for the **one** ``aws s3 cp --recursive`` that uploads a job's
#: whole ``inputs/`` tree (issue #5539). Before #5539 each input had
#: :data:`_BATCH_AWS_TIMEOUT_S` to itself; one call now covers the whole
#: set, so the ceiling is doubled rather than kept at the per-object value.
#:
#: Why doubling, rather than ``inputs x 900 s``: the per-object cost that
#: made the old loop slow was request latency and one ``aws`` process
#: spawn each, both of which the single call pays once, and
#: ``cp --recursive`` transfers concurrently (the CLI's own
#: ``max_concurrent_requests``, 10 by default), so aggregate throughput is
#: no worse than the serial loop's. The ceiling is a runaway guard, not a
#: budget, and the payloads it has to cover are small: a deck grid's whole
#: staged closure is a few MB (object *count*, not bytes, is what scaled),
#: and the biggest single object any caller ships -- the source tarball of
#: ``klt netlist`` / ``klt extract`` / ``klt signoff`` / ``klt
#: place-and-route`` -- had 900 s to itself before and now has 1800 s,
#: shared with at most one small request document.
_BATCH_INPUTS_TIMEOUT_S = 1800.0

#: Per-invocation timeout for one ``batch-fleet-provision.sh launch`` call.
#: The script's own capacity-shortfall retry loop
#: (``BATCH_LAUNCH_RETRIES``) runs inside this budget.
_BATCH_LAUNCH_TIMEOUT_S = 900.0

#: Machine-readable ``error.code`` a launch refused for lack of Spot capacity
#: carries (issue #2721): a retryable, fleet-wide infrastructure condition,
#: distinct from a request error a caller must not retry.
BATCH_NO_CAPACITY_CODE = "batch_no_capacity"

#: ``error.code`` for a launch refused by the shared fleet's concurrency cap
#: (``N instance(s) already running + 1 requested exceeds
#: BATCH_MAX_CONCURRENT_INSTANCES=M``, issue #2917): retryable like
#: :data:`BATCH_NO_CAPACITY_CODE` and waited out under the same
#: ``request.batch.capacity_wait_s`` budget, but a distinct code so a caller
#: can tell slot contention from a Spot shortage.
BATCH_CONCURRENCY_CAP_CODE = "batch_concurrency_cap"

#: ``error.code`` of a client terminated (SIGINT/SIGTERM) while waiting on a
#: submitted job (issue #3065). The remote job is deliberately left running.
BATCH_INTERRUPTED_CODE = "batch_client_interrupted"

#: Submit receipt written into the artifacts directory right after launch
#: (issue #3065) -- the on-disk record of which fleet job this client
#: orphaned if it dies mid-wait. A sharded run writes
#: ``batch-receipt-<job-id>.json`` per shard instead.
BATCH_RECEIPT_FILENAME = "batch-receipt.json"

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
#: The client waits (at most ``request.batch.interrupted_grace_s`` of
#: continuous ``interrupted``, issue #2992); it never re-submits an
#: ``interrupted`` job itself.
BATCH_WAITING_STATES = ("running", "interrupted")

#: Machine-readable diagnostic/``error.code`` of a poll that gave up on a job
#: that stayed ``interrupted`` for longer than
#: ``request.batch.interrupted_grace_s`` (issue #2992).
BATCH_JOB_INTERRUPTED_CODE = "batch_job_interrupted"

#: Diagnostic/``error.code`` of a poll whose wall-clock budget elapsed.
BATCH_POLL_TIMEOUT_CODE = "batch_poll_timeout"

#: Default for ``request.batch.interrupted_grace_s``: how long a job may sit
#: continuously ``interrupted`` (waiting for 2am's scheduled ``reconcile`` to
#: re-launch it) before :func:`poll_status` stops waiting. ``0`` disables the
#: fail-fast and restores waiting out the whole ``poll_timeout_s``.
DEFAULT_INTERRUPTED_GRACE_S = 1800.0

#: :func:`poll_status` prints a progress line on stderr on every state change
#: and otherwise once every this many polls.
_POLL_PROGRESS_EVERY = 10

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
MAX_JOB_SECONDS_ENV = "KLT_BATCH_MAX_JOB_SECONDS"

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
    "unclassified"; today :data:`BATCH_NO_CAPACITY_CODE` and
    :data:`BATCH_CONCURRENCY_CAP_CODE` are set.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class BatchClientInterrupted(BatchError):
    """SIGINT/SIGTERM arrived while waiting on a submitted job (issue #3065).

    Re-raised by :func:`_run_batch` as a ``SimError`` carrying
    :data:`BATCH_INTERRUPTED_CODE`. The fleet exposes no cancel primitive, so
    the remote job keeps running; the message names it so the caller can
    tell whether resubmitting would pay twice.
    """

    def __init__(self, message: str, *, job_id: str, signum: int) -> None:
        super().__init__(message, code=BATCH_INTERRUPTED_CODE)
        self.job_id = job_id
        self.signum = signum


class BatchPollTimeout(BatchError):
    """The wall-clock poll budget elapsed with the job still non-terminal.

    Carries the last observed ``status.json`` state (``None`` when no
    status object ever appeared). Unlike a plain :class:`BatchError` this is
    *not* re-raised as a ``SimError``: the job may still be running on the
    fleet, so the sweep reports every one of its units through
    :func:`sim_remote._unrun_corner_report` instead of claiming the run
    failed.
    """

    def __init__(
        self,
        message: str,
        *,
        last_state: str | None = None,
        code: str | None = BATCH_POLL_TIMEOUT_CODE,
    ) -> None:
        super().__init__(message, code=code)
        self.last_state = last_state


class BatchJobInterrupted(BatchPollTimeout):
    """The job stayed ``interrupted`` for longer than
    ``config.interrupted_grace_s`` (issue #2992), so polling stopped early
    rather than waiting out ``poll_timeout_s`` for a ``reconcile`` re-launch
    that is not coming.

    A :class:`BatchPollTimeout` subclass: every handler of the poll budget
    reports the job's units as unrun and not failed -- the job may still be
    recoverable on the fleet -- but with ``code ==
    "batch_job_interrupted"`` and ``interrupted_s`` recording how long it
    had been interrupted.
    """

    def __init__(self, message: str, *, interrupted_s: float) -> None:
        super().__init__(
            message, last_state="interrupted", code=BATCH_JOB_INTERRUPTED_CODE
        )
        self.interrupted_s = interrupted_s
