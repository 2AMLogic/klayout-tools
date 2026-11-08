"""Batch job contract: constants, ``job.json`` spec, feature gates, preflight.

First seam split out of :mod:`klayout_tools.sim_batch` (issue #4065): the
``Contract constants`` and ``job.json`` job-spec / feature-gate / preflight
block, moved verbatim. :mod:`klayout_tools.sim_batch` re-exports every name
defined here, so ``from klayout_tools.sim_batch import _x`` keeps working.
Names a test patches to affect code in this module must be patched here.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import (
    _provenance,
    remote_transport,
    sim_eye,
    sim_search,
    sim_settled,
    sim_snm_meas,
    sim_staging,
    sim_step_resolution,
    sim_stress,
    sim_transfer_meas,
    sim_twoport,
)
from .remote_launcher import (
    ASSUMED_THREADS_PER_CORNER,
    RemoteLaunchError,
    published_runner_identities,
)

if TYPE_CHECKING:
    from .sim_batch import BatchConfig

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


def _release_segment(version: str) -> str | None:
    """The PEP 440 release segment of a ``klt --version`` build string, or
    ``None`` when it has none (issue #3184).

    Steps: drop the PEP 440 local part (``+...``), keep the leading
    ``[0-9.]`` run, drop one trailing ``.``, and accept the result only if it
    is ``N(.N)*``. This equals ``packaging.version.Version(v).release`` for
    the strings klt produces, without importing ``packaging`` (not a runtime
    dependency). The strings klt produces (``_version_scheme.render``) are
    ``X.Y.Z`` for a clean build on a release tag, ``X.Y.(Z+1).devN+g<sha>``
    for any commit after the released base's tag (so ``0.7.1.dev212+g<sha>``
    on a checkout past ``v0.7.0``), ``X.Y.Z.devN+g<sha>`` before the base is
    tagged, either with ``.dirty`` appended to the local part, and
    ``X.Y.Z.dev0+unknown`` with no git facts. Their release segments are
    ``X.Y.Z``, ``X.Y.(Z+1)``, ``X.Y.Z`` and ``X.Y.Z``.

    The gate does not compare this whole segment: it compares
    :func:`_runner_gate_release`, its first two components.
    """
    head = version.strip().split("+", 1)[0]
    prefix = re.match(r"[0-9.]*", head).group(0)  # always matches
    if prefix.endswith("."):
        prefix = prefix[:-1]
    return prefix if re.fullmatch(r"\d+(?:\.\d+)*", prefix) else None


def _runner_gate_release(version: str) -> str | None:
    """The ``major.minor`` the runner version gate compares, or ``None``
    (issue #3184).

    The gate compares major.minor only: patch releases, ``.devN`` builds and
    ``+g<sha>`` local builds count as the same version as their major.minor.
    So a post-tag git client ``0.7.1.dev212+g<sha>`` matches a ``0.7.0``
    runner, ``0.7.3`` matches ``0.7.0`` in either direction, and
    ``0.8.0.devN`` or ``0.8.0`` against any ``0.7.x`` is a mismatch. A
    version whose release segment (:func:`_release_segment`) has fewer than
    two components (``7``) has no gate release and counts as unknown.

    This rests on a release discipline: a change that breaks the runner
    contract ships in a minor release, never a patch, and every minor
    release is published to PyPI and gets a rebaked runner image.
    :func:`_runner_preflight_script` repeats the same steps in POSIX shell on
    the runner, so both sides of the gate use one rule.
    """
    release = _release_segment(version)
    if release is None:
        return None
    parts = release.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


#: Request features a client can send that a runner built before the feature
#: landed cannot parse (issue #3249): ``name -> (first runner release that
#: supports it, detector)``. The major.minor gate (:func:`_runner_gate_release`)
#: deliberately lets a post-tag client talk to a patch-level older runner, so
#: it cannot see these; this registry compares the *full* release segment
#: instead. ``analysis_steps[]`` (#2482) landed after ``v0.7.0``, so the first
#: release segment that can carry it is ``0.7.1`` (post-tag builds render as
#: ``0.7.1.devN``). A ``0.7.1.devN`` runner built before the feature landed is
#: indistinguishable from one built after, so it counts as supporting it.
#: ``transfer`` (``measurements[].transfer``, #2923/#2924) was merged after
#: ``v0.7.0`` too, so ``0.7.1`` is likewise its first release segment, as is
#: ``stimulus`` (the declarative timed stimulus block, #2977), and
#: ``dual_injection`` (``analysis.passes`` / ``measurements[].tian``,
#: #3159/#3160/#3161), ``complex_vectors`` (``options.complex_vectors``,
#: #3337), ``twoport`` (``analysis.kind: "twoport"``, #3338), and
#: ``twoport_noise`` (``analysis.noise``, #3339), and ``snm``
#: (``measurements[].snm``, #3895), and ``search`` (``measurements[].search``,
#: #4121). ``eye`` (``measurements[].eye``, #4292) landed after ``v0.8.0``, so
#: its first release segment is ``0.8.1``, as is ``stress`` (#4592/#4858).
def _declared(finder: Callable[[list[Any]], Any]) -> Callable[[dict[str, Any]], bool]:
    """Detector: the request declares at least one measurement ``finder`` finds."""
    return lambda request: bool(finder(request.get("measurements") or []))


_step_res_declared = _declared(sim_step_resolution.step_resolution_measurements)
_RUNNER_REQUEST_FEATURES: dict[str, tuple[tuple[int, int, int], Any]] = {
    "analysis_steps": ((0, 7, 1), lambda r: r.get("analysis_steps") is not None),
    "transfer": ((0, 7, 1), _declared(sim_transfer_meas.transfer_measurements)),
    "snm": ((0, 7, 1), _declared(sim_snm_meas.snm_measurements)),
    "eye": ((0, 8, 1), _declared(sim_eye.eye_measurements)),
    "step_resolution": ((0, 8, 1), _step_res_declared),
    "settled": ((0, 8, 1), _declared(sim_settled.settled_measurements)),
    "stress": ((0, 8, 1), _declared(sim_stress.stress_measurements)),
    "search": ((0, 7, 1), _declared(sim_search.search_measurements)),
    "stimulus": (
        (0, 7, 1),
        lambda request: request.get("stimulus") is not None,
    ),
    "dual_injection": (
        (0, 7, 1),
        lambda request: _uses_dual_injection(request),
    ),
    "complex_vectors": (
        (0, 7, 1),
        lambda request: (
            (request.get("options") or {}).get("complex_vectors") is not None
        ),
    ),
    "twoport": (
        (0, 7, 1),
        lambda request: sim_twoport.is_twoport_request(request.get("analysis")),
    ),
    "twoport_noise": (
        (0, 7, 1),
        lambda request: sim_twoport.has_noise_request(request.get("analysis")),
    ),
}

#: Registry features a runner must be *known* to support (issue #2924): a
#: runner that predates one, or whose version cannot be read, is refused
#: whatever ``runner_version_check`` says, because the alternative is a worker
#: that accepts the request and omits the metric. Every other feature keeps the
#: warn-and-run behaviour of #3249. ``stimulus`` is mandatory because an older
#: runner would simulate with undriven control lines, exiting 0 (#2977).
#: ``dual_injection`` is mandatory because an older runner would ignore
#: ``analysis.passes`` and run a single AC pass or drop ``tian`` (#3161).
#: ``complex_vectors`` is mandatory because an older runner would return no
#: sidecar (#3337). ``twoport`` is mandatory because an older runner would
#: reject the unknown analysis kind or run a plain AC (#3338).
#: ``twoport_noise`` is mandatory because an older runner would reject
#: ``analysis.noise`` or omit ``nf_db`` (#3339). ``corners.params`` is
#: mandatory because an older runner ignores the axis, running one corner per
#: process x temperature with ``status: pass`` (#4235).
_MANDATORY_RUNNER_FEATURES = frozenset(
    {
        "transfer",
        "snm",
        "search",
        "eye",
        "step_resolution",
        "settled",
        "stress",
        "stimulus",
        "dual_injection",
        "complex_vectors",
        "twoport",
        "twoport_noise",
        "corners.params",
    }
)


def _uses_dual_injection(request: dict[str, Any]) -> bool:
    analysis = request.get("analysis")
    if isinstance(analysis, dict) and analysis.get("passes") is not None:
        return True
    return any(
        isinstance(spec, dict) and spec.get("tian") is not None
        for spec in request.get("measurements") or []
    )


def _uses_measurement_expr(request: dict[str, Any]) -> bool:
    return any(
        isinstance(spec, dict) and spec.get("expr") is not None
        for spec in request.get("measurements") or []
    )


def _uses_process_section_objects(request: dict[str, Any]) -> bool:
    bundles = (request.get("corners") or {}).get("process") or []
    return any(
        isinstance(section, dict)
        for bundle in bundles
        if isinstance(bundle, dict)
        for section in bundle.get("sections") or []
    )


def _uses_option(name: str) -> Any:
    return lambda request: bool((request.get("options") or {}).get(name))


#: Detectors for the :data:`REQUEST_FEATURE_MIN_RUNNER_VERSION` entries that
#: name a released version (issue #2901), keyed by the registry key's name
#: before its `` (`` note. A ``None`` (unreleased) entry has no detector on
#: purpose: it is never turned into "every runner is too old" -- the feature
#: it describes is already gated by ``analysis_steps`` above.
_RELEASED_FEATURE_DETECTORS: dict[str, Any] = {
    "measurements[].expr": _uses_measurement_expr,
    "corners.process[].sections[]": _uses_process_section_objects,
    "corners.params": lambda request: bool(
        (request.get("corners") or {}).get("params")
    ),
    "corners.units": lambda request: "units" in (request.get("corners") or {}),
    "corners.temperature_param": lambda request: (
        "temperature_param" in (request.get("corners") or {})
    ),
    "options.ngspice_init": _uses_option("ngspice_init"),
    "options.ngspice_options": _uses_option("ngspice_options"),
    "options.osdi_preload": _uses_option("osdi_preload"),
    "options.stage_model_inputs": _uses_option("stage_model_inputs"),
}

# One registry, not two: the #3232 minimum versions feed the same table the
# pre-launch, runner-side and post-run checks already read.
for _key, _version in REQUEST_FEATURE_MIN_RUNNER_VERSION.items():
    _name = _key.split(" (")[0]
    if _version is not None:
        _RUNNER_REQUEST_FEATURES[_name] = (
            tuple(int(part) for part in _version.split(".")),  # type: ignore[assignment]
            _RELEASED_FEATURE_DETECTORS[_name],
        )
del _key, _version, _name


def _release_key(release: str) -> int | None:
    """A sortable integer for the first three components of a
    :func:`_release_segment` (missing ones are 0), or ``None``. The runner
    preflight computes the same number in shell."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", release):
        return None
    parts = [int(part) for part in release.split(".")[:3]]
    parts += [0] * (3 - len(parts))
    return (parts[0] * 10**6 + parts[1]) * 10**6 + parts[2]


def _request_features(request: dict[str, Any]) -> list[str]:
    """The :data:`_RUNNER_REQUEST_FEATURES` names ``request`` uses."""
    return [
        name for name, (_, uses) in _RUNNER_REQUEST_FEATURES.items() if uses(request)
    ]


def _feature_release_key(name: str) -> int:
    """Comparable key of the runner release that introduced registry feature
    ``name``. Registry versions are literal tuples, so they always parse."""
    key = _release_key(".".join(map(str, _RUNNER_REQUEST_FEATURES[name][0])))
    assert key is not None, name
    return key


def _missing_runner_features(
    features: list[str], runner_version: str | None
) -> list[str] | None:
    """The ``features`` a runner of ``runner_version`` predates, or ``None``
    when its version is unknown (unknown is never "missing")."""
    release = _release_segment(runner_version) if runner_version else None
    key = _release_key(release) if release else None
    if key is None:
        return None
    return [name for name in features if key < _feature_release_key(name)]


#: Filename the worker capture writes under ``$EDA_OUTPUT_DIR`` (issue #2834):
#: what the *job instance* actually resolved -- simulator binary and banner,
#: PDK variant revision stamp, CPU model, ``OMP_NUM_THREADS``, a worker-local
#: image manifest when present. Written by the same runner preflight that
#: writes :data:`BATCH_RUNNER_IDENTITY_FILENAME`; absent on older workers.
BATCH_WORKER_ENVIRONMENT_FILENAME = "worker_environment.json"

#: Bounds shared by the worker-side capture and the client-side re-validation.
WORKER_CAPTURE_BANNER_MAX = 2000
WORKER_CAPTURE_STR_MAX = 300
WORKER_CAPTURE_FILE_MAX = 65536
WORKER_CAPTURE_PROBE_TIMEOUT_S = 10
#: Most staged model files the worker capture digests / the client re-reads.
WORKER_CAPTURE_MODEL_INPUTS_MAX = 64

#: The worker-side capture program. It is passed to ``python3 -I -c`` as one
#: shell-quoted argument (never interpolated with untrusted text); request
#: values arrive as ``sys.argv`` and everything it observes is serialised with
#: :mod:`json`. Stdlib only, every probe individually guarded and bounded, and
#: it always exits 0 -- a failed capture is a ``diagnostics`` entry, never a
#: job failure. Only allowlisted environment variables are read.
_WORKER_CAPTURE_PROGRAM = r"""
import hashlib, json, os, shutil, sys
import subprocess as sp
BAN, STR, T = @BAN@, @STR@, @TIMEOUT@
out, req_bin, variant, staged = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
model_names = sys.argv[5:5 + @MODELS@]
diag = []
def clip(v, n=STR):
    if not isinstance(v, str):
        return None
    v = "".join(c if (c >= " " or c in "\n\t") else "?" for c in v)
    return v[:n]
def guard(label, fn, default=None):
    try:
        return fn()
    except BaseException as exc:
        diag.append(label + ": " + clip(type(exc).__name__ + " " + str(exc), 160))
        return default
def read(path, n):
    with open(path, "rb") as h:
        return h.read(n + 1)[:n].decode("utf-8", "replace")
doc = {"schema": 1}
def sim():
    sel = req_bin or os.environ.get("KLT_NGSPICE_BINARY") or "ngspice"
    src = "request" if req_bin else ("KLT_NGSPICE_BINARY" if os.environ.get("KLT_NGSPICE_BINARY") else "PATH")
    path = shutil.which(sel)
    s = {"requested_binary": clip(sel), "binary_source": src,
         "selected_path": clip(path) if path else None, "version_banner": None,
         "version_probe": "unavailable"}
    if path:
        try:
            r = sp.run([path, "--version"], stdin=sp.DEVNULL,
                               stdout=sp.PIPE, stderr=sp.STDOUT,
                               timeout=T)
            s["version_banner"] = clip(r.stdout[:BAN * 4].decode("utf-8", "replace"), BAN)
            s["version_probe"] = "ok" if r.returncode == 0 else "exit_%d" % r.returncode
        except sp.TimeoutExpired:
            s["version_probe"] = "timeout"
        except BaseException as exc:
            s["version_probe"] = "error"
            diag.append("simulator probe: " + clip(type(exc).__name__, 80))
    else:
        diag.append("simulator binary not found on worker")
    return s
doc["simulator"] = guard("simulator", sim, {"selected_path": None, "version_banner": None})
def pdk():
    root = os.environ.get("PDK_ROOT")
    p = {"variant": clip(variant) or None, "root": clip(root), "stamp_scheme": None,
         "stamp_sha256": None, "stamp_text": None, "models_staged": staged == "1"}
    if not (root and variant):
        diag.append("pdk root or variant unknown on worker")
        return p
    stamp = os.path.join(root, variant, "SOURCES")
    try:
        text = read(stamp, 16384)
    except OSError as exc:
        diag.append("pdk stamp unreadable: " + clip(type(exc).__name__, 80))
        return p
    norm = "\n".join(l.rstrip() for l in text.replace("\r\n", "\n").strip().split("\n"))
    p["stamp_scheme"] = "sources-sha256"
    p["stamp_sha256"] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    p["stamp_text"] = clip(norm, 1000)
    return p
doc["pdk"] = guard("pdk", pdk, {"variant": clip(variant) or None})
def cpu():
    for line in read("/proc/cpuinfo", 1 << 18).split("\n"):
        if line.lower().startswith("model name") and ":" in line:
            return clip(line.split(":", 1)[1].strip())
    diag.append("cpu model not reported")
doc["cpu_model"] = guard("cpu", cpu)
doc["omp_num_threads"] = clip(os.environ.get("OMP_NUM_THREADS"), 40)
def manifest():
    path = os.environ.get("KLT_IMAGE_MANIFEST")
    for cand in ([path] if path else []) + ["/etc/klt-image-manifest.json", "/opt/klt/image-manifest.json"]:
        if not os.path.isfile(cand):
            continue
        data = json.loads(read(cand, 65536))
        if not isinstance(data, dict):
            raise ValueError("manifest is not an object")
        keep = ("pdk", "pdk_snapshot", "pdk_ref", "pdk_pinned", "ngspice_version",
                "klt_ref", "built_at", "image_id")
        m = {k: (data[k] if isinstance(data[k], (bool, int)) else clip(data[k]))
             for k in keep if k in data and isinstance(data[k], (str, bool, int))}
        m["path"] = clip(cand)
        return m
    return None
doc["image_manifest"] = guard("image manifest", manifest)
def model_inputs():
    if staged != "1":
        return None
    base = os.environ.get("EDA_INPUT_DIR") or "."
    found = []
    for name in model_names:
        h = hashlib.sha256()
        with open(os.path.join(base, name), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        found.append({"name": clip(name), "sha256": h.hexdigest()})
    return found
doc["model_inputs"] = guard("model inputs", model_inputs)
doc["diagnostics"] = diag[:20]
body = json.dumps(doc, ensure_ascii=True)
tmp = out + ".tmp"
with open(tmp, "w") as h:
    h.write(body)
os.replace(tmp, out)
"""  # noqa: E501
_WORKER_CAPTURE_PROGRAM = (
    _WORKER_CAPTURE_PROGRAM.replace("@BAN@", str(WORKER_CAPTURE_BANNER_MAX))
    .replace("@STR@", str(WORKER_CAPTURE_STR_MAX))
    .replace("@TIMEOUT@", str(WORKER_CAPTURE_PROBE_TIMEOUT_S))
    .replace("@MODELS@", str(WORKER_CAPTURE_MODEL_INPUTS_MAX))
)


def _worker_capture_command(
    remote_request: dict[str, Any], model_names: Sequence[str] = ()
) -> str:
    """The best-effort worker capture invocation (issue #2834), run from the
    runner preflight. Bounded by ``timeout`` when present, stderr discarded,
    ``|| true`` so it can never fail or alter the job."""
    options = remote_request.get("options") or {}
    models = remote_request.get("models") or {}
    binary = options.get("ngspice_binary")
    variant = models.get("pdk")
    staged = "1" if options.get("stage_model_inputs") else "0"
    args = [
        shlex.quote(str(binary) if isinstance(binary, str) else ""),
        shlex.quote(str(variant) if isinstance(variant, str) else ""),
        staged,
        *(shlex.quote(name) for name in model_names[:WORKER_CAPTURE_MODEL_INPUTS_MAX]),
    ]
    target = f'"$EDA_OUTPUT_DIR/{BATCH_WORKER_ENVIRONMENT_FILENAME}"'
    return (
        'wt=; command -v timeout >/dev/null 2>&1 && wt="timeout 30"; '
        "{ command -v python3 >/dev/null 2>&1 && "
        f"$wt python3 -I -c {shlex.quote(_WORKER_CAPTURE_PROGRAM)} "
        f"{target} {' '.join(args)} 2>/dev/null; true; }}"
    )


def _runner_supports_decks(version: str) -> bool | None:
    """Whether a runner ``klt --version`` build string knows ``decks[]``
    (issue #3303): ``None`` when it cannot be established.

    Unlike the major.minor gate (:func:`_runner_gate_release`) this reads the
    full release segment and the ``.devN`` distance, because the capability
    arrived mid-``0.7`` line: after ``0.7.1`` is released, or on a
    ``0.7.1.dev<N>`` build with ``N`` at/after :data:`_DECKS_FIRST_DEV`, or in
    any later minor. :func:`_deck_preflight_lines` repeats this in shell.
    """
    return _runner_supports_since(version, _DECKS_FIRST_DEV)


def _runner_supports_par_backend(version: str) -> bool | None:
    """Whether a runner ``klt --version`` build string accepts ``--backend``
    on ``place-and-route`` (issue #4124); same rule as
    :func:`_runner_supports_decks` with :data:`_PAR_BACKEND_FIRST_DEV`."""
    return _runner_supports_since(version, _PAR_BACKEND_FIRST_DEV)


def _runner_supports_since(version: str, first_dev: int) -> bool | None:
    """A capability that arrived at ``0.7.1.dev<first_dev>``: ``None`` when
    ``version`` cannot be read."""
    release = _release_segment(version)
    if release is None:
        return None
    parts = [int(part) for part in release.split(".")]
    if len(parts) < 2:
        return None
    parts += [0] * (3 - len(parts))
    if tuple(parts[:2]) != _DECKS_BASE_RELEASE:
        return tuple(parts[:2]) > _DECKS_BASE_RELEASE
    if parts[2] >= 2:
        return True
    if parts[2] == 0:
        return False
    dev = re.search(r"\.dev(\d+)", version.split("+", 1)[0])
    return dev is None or int(dev.group(1)) >= first_dev


def _deck_preflight_lines() -> str:
    """Shell appended to the runner preflight of a deck-list job (issue
    #3303; needs ``$rv`` from :func:`_runner_preflight_script`). Unlike the
    version gate this is not relaxed by ``runner_version_check: warn``: a
    runner that cannot read ``decks[]`` fails the job for certain, so the
    job exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` with a klt error envelope
    naming :data:`BATCH_RUNNER_LACKS_DECKS_CODE` before ``klt sim`` runs."""
    return _capability_preflight_lines(
        command="sim",
        code=BATCH_RUNNER_LACKS_DECKS_CODE,
        first_dev=_DECKS_FIRST_DEV,
        lacks="deck-list (decks[]) requests",
        remedy="rebuild the runner image from a klt that includes decks[]",
    )


def par_backend_preflight_lines() -> str:
    """:func:`_deck_preflight_lines` for a ``place-and-route`` batch job
    (issue #4124): a runner whose ``klt place-and-route`` predates
    ``--backend`` exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` naming
    :data:`BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE`."""
    return _capability_preflight_lines(
        command="place-and-route",
        code=BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
        first_dev=_PAR_BACKEND_FIRST_DEV,
        lacks="--backend on place-and-route",
        remedy="rebuild the runner image from a klt that includes "
        "place-and-route --backend",
    )


def _capability_preflight_lines(
    *, command: str, code: str, first_dev: int, lacks: str, remedy: str
) -> str:
    """The shell behind :func:`_deck_preflight_lines` and
    :func:`par_backend_preflight_lines`: reject a runner older than
    ``0.7.1.dev<first_dev>``. ``lacks`` and ``remedy`` are embedded in a
    double-quoted shell string, so they must not hold ``"``, ``$`` or a
    backtick."""
    report_path = f'"$EDA_OUTPUT_DIR/{BATCH_REPORT_FILENAME}"'
    base_major, base_minor = _DECKS_BASE_RELEASE
    return (
        "dk=; rb=${rv%%+*}; dn=; "
        'case "$rb" in *.dev[0-9]*) dn=${rb##*.dev}; dn=${dn%%[!0-9]*}; '
        "rb=${rb%%.dev*} ;; esac; "
        "rb=${rb%%[!0-9.]*}; rb=${rb%.}; "
        'case "$rb" in ""|.*|*..*) ;; *.*) '
        "dma=${rb%%.*}; drest=${rb#*.}; dmi=${drest%%.*}; dpa=0; "
        'case "$drest" in *.*) dpa=${drest#*.}; dpa=${dpa%%.*} ;; esac; '
        f'if [ "$dma" -gt {base_major} ] || '
        f'{{ [ "$dma" -eq {base_major} ] && [ "$dmi" -gt {base_minor} ]; }}; '
        "then dk=yes; "
        f'elif [ "$dma" -eq {base_major} ] && [ "$dmi" -eq {base_minor} ]; then '
        'if [ "$dpa" -ge 2 ]; then dk=yes; '
        'elif [ "$dpa" -eq 1 ] && { [ -z "$dn" ] || '
        f'[ "$dn" -ge {first_dev} ]; }}; then dk=yes; fi; fi ;; esac; '
        'if [ -z "$dk" ]; then '
        'dmsg="the fleet runner runs klt ${rv:-<unknown>}, which does not know '
        f'{lacks} -- the request was not run ({remedy})"; '
        'printf \'{"schema_version": 1, "error": {"command": "' + command + '", '
        f'"code": "{code}", "message": "%s"}}}}\\n\' '
        '"$dmsg" >' + report_path + "; "
        'echo "$dmsg" >&2; '
        "exit " + str(_BATCH_PREFLIGHT_REJECT_EXIT) + "; fi; "
    )


def _assess_deck_support(request: dict[str, Any], config: BatchConfig) -> None:
    """Refuse a deck-list batch submit before any S3 write or launch when
    every image the AMI manifest could boot is known to predate ``decks[]``
    (issue #3303). Same candidate selection as
    :func:`_assess_published_runner`; anything less certain (no manifest,
    a legacy entry without ``klt_version``, a mix) proceeds to the
    runner-side probe in :func:`_deck_preflight_lines`. Raises regardless of
    ``runner_version_check``: the job could not run on any such image."""
    _assess_capability_support(
        request,
        config,
        supports=_runner_supports_decks,
        code=BATCH_RUNNER_LACKS_DECKS_CODE,
        predates="deck-list (decks[]) support",
        remedy="rebuild the runner image from a klt that includes decks[]; "
        "see docs/cli/sim.md",
    )


def assess_par_backend_support(request: dict[str, Any], config: BatchConfig) -> None:
    """:func:`_assess_deck_support` for ``place-and-route --backend batch``
    (issue #4124); ``request`` only needs ``models.pdk``."""
    _assess_capability_support(
        request,
        config,
        supports=_runner_supports_par_backend,
        code=BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
        predates="place-and-route --backend (needs klt "
        f"0.7.1.dev{_PAR_BACKEND_FIRST_DEV} or later)",
        remedy="rebuild the runner image from a klt that includes "
        "place-and-route --backend; see docs/cli/place-and-route.md",
    )


def _assess_capability_support(
    request: dict[str, Any],
    config: BatchConfig,
    *,
    supports: Callable[[str], bool | None],
    code: str,
    predates: str,
    remedy: str,
) -> None:
    from .sim import SimError

    pdk = (request.get("models") or {}).get("pdk")
    try:
        images = published_runner_identities(str(pdk) if pdk else None, config.region)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return
    if not images:
        return
    support = [
        supports(image["klt_version"]) if image["klt_version"] else None
        for image in images
    ]
    if any(item is not False for item in support):
        return
    published = "; ".join(
        f"{image['ami_id']} (pdk {image['pdk']}, region {image['region']}) "
        f"runs klt {image['klt_version']}"
        + (f" from ref {image['klt_ref']}" if image["klt_ref"] else "")
        for image in images
    )
    raise SimError(
        f"backend 'batch' refused before submission: the AMI manifest "
        f"publishes the fleet runner image as {published}, which predates "
        f"{predates} -- the request was not submitted and no "
        f"instance was launched ({remedy})",
        code=code,
    )


def _runner_preflight_script(
    client_version: str,
    *,
    enforce: bool,
    capture: str = "",
    required_features: tuple[str, ...] = (),
) -> str:
    """The shell prefix of the job command that pins the runner's ``klt``
    identity (issue #2719).

    It is generated by the *submitting* client and uses only ``klt
    --version`` -- which every klt release has -- so it runs on a fleet image
    that predates any request key the client accepts. It (1) records the
    runner's ``klt --version`` build string in
    :data:`BATCH_RUNNER_IDENTITY_FILENAME` (``null`` when it cannot be read),
    and (2) under ``enforce`` writes a klt error envelope to ``report.json``
    and exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` *before* ``klt sim`` runs
    unless the runner's major.minor equals the client's
    (:func:`_runner_gate_release`, issue #3184; the mismatch message keeps
    both full build strings). So a post-tag git client
    ``0.7.1.dev212+g<sha>`` runs on a ``0.7.0`` runner, ``0.7.3`` and
    ``0.7.0`` run on each other, and ``0.8.0.devN`` or ``0.8.0`` against a
    ``0.7.x`` runner exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT`. A runner
    version that is empty, or whose release segment has fewer than two
    components, is ``batch_runner_version_unknown``. The runner string is
    reduced to ``[A-Za-z0-9._+ -]`` so it is safe inside the JSON the script
    prints.

    Redirects here deliberately have no space before ``>`` so the
    ``> "$EDA_OUTPUT_DIR/report.json"`` marker of the main command stays the
    first such occurrence.

    ``required_features`` (names from :data:`_RUNNER_REQUEST_FEATURES`, issue
    #3249) adds a second check once the version gate passes: a runner whose
    full release segment predates a feature the request uses exits
    :data:`_BATCH_PREFLIGHT_REJECT_EXIT` with
    :data:`BATCH_RUNNER_FEATURE_CODE` naming it, instead of letting ``klt
    sim`` fail every corner. An unreadable runner version never trips it.

    A client version string outside ``[A-Za-z0-9._+-]``, or one with no
    major.minor release segment, cannot be pinned and raises
    :class:`~klayout_tools.sim.SimError` (a structured CLI error, never a
    traceback) before any S3 write.
    """
    client_release = (
        _runner_gate_release(client_version)
        if re.fullmatch(r"[A-Za-z0-9._+-]+", client_version)
        else None
    )
    # Only a validated N.N is ever embedded in the shell below.
    if client_release is None or not re.fullmatch(r"\d+\.\d+", client_release):
        from .sim import SimError

        raise SimError(
            f"the submitting client's klt version {client_version!r} cannot be "
            "pinned against the fleet runner (expected only [A-Za-z0-9._+-] "
            "and a major.minor release segment such as 0.7.0) -- submit from a "
            "klt install with a well-formed version"
        )
    out = '"$EDA_OUTPUT_DIR/{}"'
    identity_path = out.format(BATCH_RUNNER_IDENTITY_FILENAME)
    report_path = out.format(BATCH_REPORT_FILENAME)
    lines = [
        "rv=$(klt --version 2>/dev/null | head -n 1 | tr -cd 'A-Za-z0-9._+ -'"
        " | cut -c1-80); rv=${rv#klt }; rv=${rv# }",
        # _runner_gate_release in shell: drop +local, keep the leading
        # [0-9.] run, drop one trailing dot, require N(.N)* (that is
        # _release_segment), then require a second component and keep only
        # the first two (major.minor).
        "rr=${rv%%+*}; rr=${rr%%[!0-9.]*}; rr=${rr%.}",
        'case "$rr" in ""|.*|*.|*..*) rr= ;; *.*) ;; *) rr= ;; esac',
        "rfull=$rr",
        'if [ -n "$rr" ]; then rmn=${rr#*.}; rr=${rr%%.*}.${rmn%%.*}; fi',
        'if [ -z "$rv" ]; then rj=null; rcode=' + BATCH_RUNNER_UNKNOWN_CODE + "; "
        'rmsg="the fleet runner klt version could not be established"; '
        'elif [ -z "$rr" ]; then rj="\\"$rv\\""; '
        "rcode=" + BATCH_RUNNER_UNKNOWN_CODE + "; "
        'rmsg="the fleet runner klt version $rv has no major.minor release segment"; '
        'elif [ "$rr" = "' + client_release + '" ]; then rj="\\"$rv\\""; rcode=; '
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
    if required_features:
        lines.append(_feature_gate_shell(required_features, client_version))
    mandatory = tuple(
        name for name in required_features if name in _MANDATORY_RUNNER_FEATURES
    )
    if mandatory:
        lines.append(
            _mandatory_feature_gate_shell(
                mandatory, report_path=report_path
            ).removesuffix("; ")
        )
    if capture:
        # Issue #2834: the worker capture rides the same preflight, *before*
        # the enforce gate so a rejected job still leaves it collectable.
        lines.append(capture)
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


def _mandatory_feature_gate_shell(
    features: tuple[str, ...], *, report_path: str | None = None
) -> str:
    """Shell that rejects a runner not known to support a
    :data:`_MANDATORY_RUNNER_FEATURES` entry (issue #2924); needs ``$rv``.
    Unlike the version gate and :func:`_feature_gate_shell` it is not relaxed
    by ``runner_version_check: "warn"``, and an unreadable runner version is
    rejected too. Exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` with the message
    on stderr and, when ``report_path`` is given (the batch job), a klt error
    envelope carrying :data:`BATCH_RUNNER_FEATURE_CODE` there -- before
    ``klt sim`` runs."""
    envelope = ""
    if report_path is not None:
        envelope = (
            'printf \'{"schema_version": 1, "error": {"command": "sim", '
            f'"code": "{BATCH_RUNNER_FEATURE_CODE}", "message": "%s"}}}}\\n\' '
            f'"$mmsg" >{report_path}; '
        )
    return (
        _mandatory_feature_probe_shell(features)
        + 'if [ -n "$mmiss" ]; then '
        + 'mmsg="the worker runs klt ${rv:-<unknown>}, which is not known '
        + "to support the request feature(s) $mmiss -- the request was not run "
        + '(rebuild the runner image from a release that has the feature)"; '
        + envelope
        + 'echo "$mmsg" >&2; exit '
        + str(_BATCH_PREFLIGHT_REJECT_EXIT)
        + "; fi; "
    )


def remote_feature_preflight(request: dict[str, Any]) -> str:
    """Shell prefix for the ``remote`` backend's ``klt sim`` command (issue
    #2924): on the provisioned host, read ``klt --version`` and refuse a
    request that uses a :data:`_MANDATORY_RUNNER_FEATURES` entry the host's
    ``klt`` is not known to support, before any simulation. Empty (the
    command is byte-identical to before) when the request uses none."""
    features = tuple(
        name
        for name in _request_features(request)
        if name in _MANDATORY_RUNNER_FEATURES
    )
    if not features:
        return ""
    return (
        "rv=$(klt --version 2>/dev/null | head -n 1 | tr -cd 'A-Za-z0-9._+ -'"
        " | cut -c1-80); rv=${rv#klt }; rv=${rv# }; "
        + _mandatory_feature_gate_shell(features)
    )


def assess_remote_runner_features(
    request: dict[str, Any], pdk: str | None, region: str | None, manifest: Any
) -> None:
    """Refuse a ``remote`` request before any billable launch when the AMI
    manifest says the image it would boot predates a mandatory registry
    feature the request uses (issue #2924). Anything less certain (no
    manifest, a legacy entry without ``klt_version``) proceeds to
    :func:`remote_feature_preflight` on the host. Never relaxed by a warn
    policy: ``remote`` has none, and a silently dropped metric is worse."""
    from .sim import SimError

    features = [
        name
        for name in _request_features(request)
        if name in _MANDATORY_RUNNER_FEATURES
    ]
    if not features:
        return
    try:
        images = published_runner_identities(pdk, region, manifest)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return
    gaps = [
        _missing_runner_features(features, image["klt_version"]) for image in images
    ]
    if not gaps or any(not gap for gap in gaps):
        return
    missing = sorted({name for gap in gaps for name in gap or ()})
    versions = ", ".join(sorted({str(image["klt_version"]) for image in images}))
    raise SimError(
        f"backend 'remote' refused before launch: the request uses "
        f"{', '.join(missing)}, which the AMI manifest's runner image "
        f"(klt {versions}) predates -- no instance was launched (rebuild the "
        "remote-sim AMI from a release that has the feature; see "
        "docs/cli/sim.md)",
        code=BATCH_RUNNER_FEATURE_CODE,
    )


def _mandatory_feature_probe_shell(features: tuple[str, ...]) -> str:
    """Shell setting ``$mmiss`` to the ``features`` the runner is not known
    to support, from ``$rv`` (the runner's ``klt --version`` string). It
    derives the full release segment itself so the remote backend can reuse
    it without the batch preflight's ``$rfull``."""
    checks = " ".join(
        f'if [ "$mk" -lt {_feature_release_key(name)} ]; '
        f'then mmiss="${{mmiss}}${{mmiss:+, }}{name}"; fi;'
        for name in features
    )
    names = ", ".join(features)
    return (
        "mmiss=; mr=${rv%%+*}; mr=${mr%%[!0-9.]*}; mr=${mr%.}; "
        'case "$mr" in ""|.*|*..*) mr= ;; esac; '
        'if [ -z "$mr" ]; then '
        f'mmiss="{names}"; else '
        'IFS=. read -r f1 f2 f3 _ <<<"$mr"; '
        "mk=$(( (10#${f1:-0} * 1000000 + 10#${f2:-0}) * 1000000 "
        "+ 10#${f3:-0} )); " + checks + " fi; "
    )


def _feature_gate_shell(features: tuple[str, ...], client_version: str) -> str:
    """The shell that sets ``rcode``/``rmsg`` for a runner predating a request
    feature (issue #3249). Runs after the version gate, so it never overrides
    a version rejection; ``$rfull`` is the runner's full release segment.
    Only registry names and integers are embedded."""
    checks = []
    for name in features:
        key = _feature_release_key(name)
        checks.append(
            f'if [ "$rk" -lt {key} ]; then fmiss="${{fmiss}}${{fmiss:+, }}{name}"; fi;'
        )
    return (
        'fmiss=; if [ -n "$rfull" ]; then '
        'IFS=. read -r f1 f2 f3 _ <<<"$rfull"; '
        "rk=$(( (10#${f1:-0} * 1000000 + 10#${f2:-0}) * 1000000 "
        "+ 10#${f3:-0} )); " + " ".join(checks) + " fi; "
        'if [ -z "$rcode" ] && [ -n "$fmiss" ]; then '
        "rcode=" + BATCH_RUNNER_FEATURE_CODE + "; "
        'rmsg="the fleet runner runs klt $rv, which predates the request '
        "feature(s) $fmiss the submitting client sent (client "
        + client_version
        + ')"; fi'
    )


def _job_stderr_capture_prefix() -> str:
    """The first line(s) of the batch job command (issue #3818): tee the whole
    job's stderr into ``$EDA_OUTPUT_DIR/stderr.log`` -- forwarding it too, so
    ``harness.log`` still sees it -- and install an EXIT trap.

    Teeing from the first line (not only from the ``klt sim`` line) means a
    failure *before* ``klt sim`` -- the generated preflight, a shell error --
    leaves ``stderr.log`` behind. The harness runs ``bash -c "$JOB_CMD"``, so
    process substitution is available.

    The trap closes the shell's stderr so the tee drains (``wait``) before the
    harness uploads ``outputs/``, then -- only when the command exited
    non-zero, ``report.json`` is absent/empty and ``stderr.log`` holds no klt
    error envelope (``"schema_version"``) -- writes one itself, carrying the
    exit code and a bounded, JSON-safe tail of ``stderr.log``. So the
    submitter's ``batch_job_failed`` message names the reason without S3
    listing access. An existing envelope (the preflight's, klt's own) is never
    overwritten, and the job's exit code is unchanged. Redirects here have no
    space before ``>`` so the main command's ``> "$EDA_OUTPUT_DIR/report.json"``
    stays the first such occurrence.
    """
    out = '"$EDA_OUTPUT_DIR/{}"'
    stderr_path = out.format(BATCH_STDERR_FILENAME)
    report_path = out.format(BATCH_REPORT_FILENAME)
    body = (
        "rc=$?; exec 2>&-; wait $tp 2>/dev/null; "
        f'if [ "$rc" -ne 0 ] && [ ! -s {report_path} ] '
        f"&& ! grep -q schema_version {stderr_path} 2>/dev/null; then "
        f"tl=$(tail -c {_BATCH_JOB_FAILURE_TAIL_BYTES} {stderr_path} 2>/dev/null "
        "| tr -c 'A-Za-z0-9._+:/=,() -' ' ' | tr -s ' '); "
        'printf \'{"schema_version": 1, "error": {"command": "sim", '
        f'"code": "{BATCH_JOB_COMMAND_FAILED_CODE}", '
        '"message": "the job command exited %s before klt sim produced a '
        "report; stderr tail: %s\"}}\\n' "
        f'"$rc" "$tl" >{report_path}; fi'
    )
    return (
        f"exec 2> >(tee {stderr_path} >&2); tp=$!; "
        f"_klt_job_exit() {{ {body}; }}; trap _klt_job_exit EXIT; "
    )


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
