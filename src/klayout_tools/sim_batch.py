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

import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import _provenance, remote_transport, sim_staging
from .remote_launcher import (
    ASSUMED_THREADS_PER_CORNER,
    RemoteLaunchError,
    _signal_handlers_install,
    _signal_handlers_restore,
    ami_pdk_key,
    published_runner_identities,
)
from .sim_remote import (
    _build_remote_request,
    _points_to_push,
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
BATCH_RUNNER_FEATURE_CODE = "batch_runner_feature_unsupported"
#: A deck-list (``decks[]``, #2804) request needs a runner ``klt`` that knows
#: the deck-list contract; the major.minor gate cannot see that (issue #3303).
BATCH_RUNNER_LACKS_DECKS_CODE = "batch_runner_lacks_decks"

#: ``decks[]`` landed in ``f44df5ad`` (#2804), 307 commits after ``v0.7.0``
#: (``git describe``: ``v0.7.0-307-gf44df5ad``). A runner whose version is
#: ``0.7.0`` or earlier, or ``0.7.1.devN`` with ``N`` below this, predates it.
_DECKS_BASE_RELEASE = (0, 7)
_DECKS_FIRST_DEV = 307

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
    # 8296828c options.osdi_preload (#2534); first in v0.7.0.
    "options.osdi_preload": "0.7.0",
    # d4d497ee ordered analysis_steps[] (#2482), 2026-10-09, after v0.7.0:
    # the only route to .meas cards on a noise analysis. Untagged.
    "analysis_steps[].measurements[] (noise analysis .meas cards)": None,
}

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
#: Next candidate entry: ``decks[]`` (#3303).
_RUNNER_REQUEST_FEATURES: dict[str, tuple[tuple[int, int, int], Any]] = {
    "analysis_steps": (
        (0, 7, 1),
        lambda request: request.get("analysis_steps") is not None,
    ),
}


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
    return dev is None or int(dev.group(1)) >= _DECKS_FIRST_DEV


def _deck_preflight_lines() -> str:
    """Shell appended to the runner preflight of a deck-list job (issue
    #3303; needs ``$rv`` from :func:`_runner_preflight_script`). Unlike the
    version gate this is not relaxed by ``runner_version_check: warn``: a
    runner that cannot read ``decks[]`` fails the job for certain, so the
    job exits :data:`_BATCH_PREFLIGHT_REJECT_EXIT` with a klt error envelope
    naming :data:`BATCH_RUNNER_LACKS_DECKS_CODE` before ``klt sim`` runs."""
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
        f'[ "$dn" -ge {_DECKS_FIRST_DEV} ]; }}; then dk=yes; fi; fi ;; esac; '
        'if [ -z "$dk" ]; then '
        'dmsg="the fleet runner runs klt ${rv:-<unknown>}, which does not know '
        "deck-list (decks[]) requests -- the request was not run (rebuild the "
        'runner image from a klt that includes decks[])"; '
        'printf \'{"schema_version": 1, "error": {"command": "sim", '
        f'"code": "{BATCH_RUNNER_LACKS_DECKS_CODE}", "message": "%s"}}}}\\n\' '
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
    from .sim import SimError

    pdk = (request.get("models") or {}).get("pdk")
    try:
        images = published_runner_identities(str(pdk) if pdk else None, config.region)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return
    if not images:
        return
    support = [
        _runner_supports_decks(image["klt_version"]) if image["klt_version"] else None
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
        "deck-list (decks[]) support -- the request was not submitted and no "
        "instance was launched (rebuild the runner image from a klt that "
        "includes decks[]; see docs/cli/sim.md)",
        code=BATCH_RUNNER_LACKS_DECKS_CODE,
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


def _feature_gate_shell(features: tuple[str, ...], client_version: str) -> str:
    """The shell that sets ``rcode``/``rmsg`` for a runner predating a request
    feature (issue #3249). Runs after the version gate, so it never overrides
    a version rejection; ``$rfull`` is the runner's full release segment.
    Only registry names and integers are embedded."""
    checks = []
    for name in features:
        key = _feature_release_key(name)
        checks.append(
            f'if [ "$rk" -lt {key} ]; then fmiss="${{fmiss}}${{fmiss:+, }}{name}"; fi'
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
        capture=_worker_capture_command(
            remote_request, [asset.name for asset in staged.model_assets]
        ),
        required_features=tuple(_request_features(remote_request)),
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
    #: ``batch_no_capacity`` / ``batch_concurrency_cap`` refusal
    #: (``request.batch.capacity_wait_s``,
    #: issue #2721). ``0`` -- the default -- is one launch attempt.
    capacity_wait_s: float = 0.0
    #: ``request.batch.runner_version_check`` (issue #2719).
    runner_version_check: str = "enforce"
    #: The fleet's whole-job ceiling in seconds and where it came from
    #: (``"request"``, ``"env"``, ``"fleet_config"`` or ``"default"``) --
    #: see :func:`_resolve_max_job_seconds` (issue #2833).
    max_job_seconds: float = DEFAULT_BATCH_MAX_JOB_SECONDS
    max_job_seconds_source: str = "default"
    #: ``request.batch.job_ceiling_check`` (issue #2833).
    job_ceiling_check: str = "warn"
    #: ``request.batch.max_units_per_job`` (issue #3446): opt-in cap on the
    #: units of one job; ``None`` -- the default -- leaves the split to
    #: ``job_ceiling_check: "split"`` (or to no split at all).
    max_units_per_job: int | None = None
    #: ``request.batch.interrupted_grace_s`` (issue #2992); ``0`` disables.
    interrupted_grace_s: float = DEFAULT_INTERRUPTED_GRACE_S


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


#: ``SimError.code`` for an absolute host-path model library on the batch
#: backend (issue #3296).
BATCH_MODELS_LIB_HOST_PATH_CODE = "models_lib_host_path"


def _host_path_lib_refs(request: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(where, ref)`` model-library ref in ``request`` that is an
    absolute host path: ``models.lib`` and each ``corners.process[].sections[]``
    object's ``lib``. A ref that names an environment variable (``$PDK_ROOT/...``)
    is left alone -- the runner expands it against its own environment -- but a
    ``~`` is expanded on *this* host, so ``~/...`` is a host path too."""
    refs: list[tuple[str, str]] = []

    def check(where: str, ref: Any) -> None:
        if not isinstance(ref, str) or "$" in ref:
            return
        if os.path.isabs(os.path.expanduser(ref)):
            refs.append((where, ref))

    models = request.get("models")
    if isinstance(models, dict):
        check("models.lib", models.get("lib"))
    corners = request.get("corners")
    process = corners.get("process") if isinstance(corners, dict) else None
    for i, entry in enumerate(process if isinstance(process, list) else []):
        sections = entry.get("sections") if isinstance(entry, dict) else None
        for j, section in enumerate(sections if isinstance(sections, list) else []):
            if isinstance(section, dict):
                check(f"corners.process[{i}].sections[{j}].lib", section.get("lib"))
    return refs


def _pdk_relative_form(ref: str) -> str | None:
    """The ``{"pdk", "lib"}`` spelling of an absolute path laid out as
    ``<root>/<variant>/libs.tech/...``, or ``None`` when the path does not sit
    in a PDK variant directory."""
    parts = os.path.expanduser(ref).split(os.sep)
    if "libs.tech" not in parts:
        return None
    index = parts.index("libs.tech")
    if index < 2 or not parts[index - 1]:
        return None
    return json.dumps({"pdk": parts[index - 1], "lib": "/".join(parts[index:])})


def _validate_batch_models_lib(request: dict[str, Any]) -> None:
    """Refuse an absolute host-path model library **before** any S3 write
    (issue #3296).

    The runner resolves ``models.lib`` on its own box, where this host's
    ``/home/<user>/.volare/...`` does not exist, so the job would pay for an
    instance launch and then fail with ``model library not found``. Not
    refused: PDK-relative libs, ``$VAR`` forms (the runner expands them), and
    any request with ``options.stage_model_inputs: true`` (the library ships
    with the job).
    """
    from . import sim_staging
    from .sim import SimError

    if sim_staging.stage_model_inputs_requested(request):
        return
    refs = _host_path_lib_refs(request)
    if not refs:
        return
    details = []
    for where, ref in refs:
        relative = _pdk_relative_form(ref)
        hint = f" -- use {relative}" if relative else ""
        details.append(f"{where}={ref!r}{hint}")
    raise SimError(
        "backend 'batch': an absolute host-path model library does not exist "
        "on the runner (" + "; ".join(details) + "). Name the library PDK-relative "
        '({"pdk": "<variant>", "lib": "<path under the variant dir>"}) or set '
        "options.stage_model_inputs: true to ship it with the job",
        code=BATCH_MODELS_LIB_HOST_PATH_CODE,
    )


def _validate_batch_pdk_variant(request: dict[str, Any], region: str | None) -> None:
    """Refuse a ``models.pdk`` variant the fleet image does not ship,
    **before** any S3 write (issue #3251).

    :func:`_validate_batch_pdk` only reduces a variant to its family
    (``gf180mcuC`` -> ``gf180mcu``), so a variant the image lacks used to
    surface on the runner as one ``batch_job_failed`` per corner. When the
    AMI manifest's entries publish ``pdk_variants``, the request is refused
    only if **every** candidate image (see :func:`_assess_published_runner`
    for the selection) publishes the field and none lists the variant.
    Legacy entries, no manifest, no candidate or a request without a PDK
    stay unchanged. Never raises anything but :class:`SimError`.
    """
    from .sim import SimError

    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        return
    try:
        images = published_runner_identities(str(pdk), region)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return
    published = [image.get("pdk_variants") for image in images]
    listed: list[list[str]] = [v for v in published if v is not None]
    if not listed or len(listed) != len(published):
        return
    if any(str(pdk) in variants for variants in listed):
        return
    carried = sorted({v for variants in listed for v in variants})
    raise SimError(
        f"backend 'batch': PDK variant {str(pdk)!r} is not shipped by the fleet "
        f"image the AMI manifest publishes (it carries: {', '.join(carried)}) "
        "-- the request was not submitted and no instance was launched; use "
        "one of those variants as models.pdk"
    )


def _resolve_batch_config(
    request: dict[str, Any],
    *,
    corner_count: int,
    timeout_s: float,
    check_runner: bool = True,
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

    Last, the runner ``klt`` the AMI manifest publishes is compared with
    this client (:func:`_assess_published_runner`, issue #2851): under
    ``runner_version_check: "enforce"`` a known mismatch raises
    ``batch_runner_version_mismatch`` here, so neither ``klt sim`` (one job
    or every shard) nor ``klt netlist`` uploads or launches anything.
    ``check_runner=False`` skips that last comparison for a read-only caller
    (``klt sim --job-status``, issue #3073) that submits nothing.
    """
    from .sim import SimError

    _validate_batch_pdk(request)
    _validate_batch_models_lib(request)
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
    max_job_seconds, max_job_seconds_source = _resolve_max_job_seconds(
        batch_spec, fleet
    )
    job_ceiling_check = _resolve_job_ceiling_check(batch_spec)
    max_units_per_job = _resolve_max_units_per_job(batch_spec, job_ceiling_check)
    config = BatchConfig(
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
        max_job_seconds=max_job_seconds,
        max_job_seconds_source=max_job_seconds_source,
        job_ceiling_check=job_ceiling_check,
        max_units_per_job=max_units_per_job,
        interrupted_grace_s=_resolve_interrupted_grace_s(batch_spec),
    )
    # Issue #2851: compare the runner klt the AMI manifest publishes with
    # this client, once per request (so once for every shard of a
    # `hosts > 1` run), before any S3 write or launch.
    if check_runner:
        assessment = _assess_published_runner(request, config)
        _enforce_published_runner_version(assessment)
        _enforce_published_runner_features(request, assessment)
        # Issue #3251: refuse a PDK variant the published image does not ship.
        _validate_batch_pdk_variant(request, config.region)
    return config


def _assess_published_runner(
    request: dict[str, Any], config: BatchConfig
) -> dict[str, Any]:
    """Compare the ``klt`` build the AMI manifest says the fleet image runs
    with this client's, **before** any submit or launch (issue #2851).

    The source is :func:`remote_launcher.published_runner_identities`: the
    most recently built manifest entry per ``(pdk, region)`` for the
    request's ``models.pdk`` (every PDK when it names none) and the batch
    ``region`` (every region when none is configured, because the fleet
    picks its own Spot pool). Versions are compared with the runner gate's
    own release-segment rule, :func:`_runner_gate_release` (issue #3184), so
    this check and the runner-side preflight can never disagree about what
    counts as compatible.

    Returns ``{"status", "check", "client_klt_version", "images",
    "message"}``. ``status`` is ``"mismatch"`` only when *every* candidate
    image publishes a ``klt_version`` and none of them matches the client:
    then the job could not pass the runner-side gate on any image the fleet
    might boot. Anything less certain -- no manifest, no candidate, a legacy
    entry without ``klt_version``, a mix of matching and mismatching images,
    an unparseable version -- is ``"unknown"`` (or ``"match"``) and the
    request proceeds to the existing runner-side preflight unchanged. This
    function never raises.
    """
    client = _provenance._klt_version()
    assessment: dict[str, Any] = {
        "status": "unknown",
        "check": config.runner_version_check,
        "client_klt_version": client,
        "images": [],
        "message": None,
    }
    pdk = (request.get("models") or {}).get("pdk")
    try:
        images = published_runner_identities(str(pdk) if pdk else None, config.region)
    except (RemoteLaunchError, OSError, TypeError, ValueError, AttributeError):
        return assessment
    assessment["images"] = images
    client_release = _runner_gate_release(client)
    if not images or client_release is None:
        return assessment
    releases = [
        _runner_gate_release(image["klt_version"]) if image["klt_version"] else None
        for image in images
    ]
    if any(release is None for release in releases):
        return assessment
    if any(release == client_release for release in releases):
        assessment["status"] = "match"
        return assessment
    assessment["status"] = "mismatch"
    published = "; ".join(
        f"{image['ami_id']} (pdk {image['pdk']}, region {image['region']}) "
        f"runs klt {image['klt_version']}"
        + (f" from ref {image['klt_ref']}" if image["klt_ref"] else "")
        for image in images
    )
    assessment["message"] = (
        f"the AMI manifest publishes the fleet runner image as {published}, "
        f"but the submitting client is klt {client}; their major.minor "
        "releases differ, so the runner-side version gate would reject the job"
    )
    return assessment


def _enforce_published_runner_version(assessment: dict[str, Any]) -> None:
    """Act on :func:`_assess_published_runner` before any S3 write or
    launch: under ``runner_version_check: "enforce"`` a known mismatch is
    one request-level :class:`~klayout_tools.sim.SimError` with code
    :data:`BATCH_RUNNER_MISMATCH_CODE` -- nothing is submitted, so no
    instance launches and no per-corner ``batch_job_failed`` diagnostics
    exist. Under ``"warn"`` the same message goes to stderr and the request
    is submitted as before. ``unknown``/``match`` do nothing here; the
    runner-side preflight (issue #2719) still decides on the job instance."""
    from .sim import SimError

    if assessment["status"] != "mismatch":
        return
    message = assessment["message"]
    if assessment["check"] == "enforce":
        raise SimError(
            f"backend 'batch' refused before submission: {message} -- the "
            "request was not submitted and no instance was launched (rebuild "
            "the runner image, use a client of the runner's major.minor, or "
            'set request.batch.runner_version_check to "warn" to submit '
            "anyway; see docs/cli/sim.md)",
            code=BATCH_RUNNER_MISMATCH_CODE,
        )
    print(f"klt sim: warning: {message}; submitting anyway", file=sys.stderr)


def _enforce_published_runner_features(
    request: dict[str, Any], assessment: dict[str, Any]
) -> None:
    """Refuse before any S3 write or launch when *every* image the AMI
    manifest publishes predates a request feature (issue #3249). An unknown
    runner version, a legacy entry, or one image that has the feature leaves
    the decision to the runner-side preflight. ``warn`` only reports."""
    from .sim import SimError

    features = _request_features(request)
    images = assessment["images"]
    if not features or not images:
        return
    gaps = [
        _missing_runner_features(features, image["klt_version"]) for image in images
    ]
    if any(not gap for gap in gaps):  # unknown (None) or complete (empty)
        return
    missing = sorted({name for gap in gaps for name in gap or ()})
    versions = ", ".join(sorted({str(image["klt_version"]) for image in images}))
    message = (
        f"the request uses {', '.join(missing)}, which the fleet runner image "
        f"(klt {versions}) predates; the submitting client is klt "
        f"{assessment['client_klt_version']}"
    )
    if assessment["check"] != "enforce":
        print(f"klt sim: warning: {message}; submitting anyway", file=sys.stderr)
        return
    raise SimError(
        f"backend 'batch' refused before submission: {message} -- the request "
        "was not submitted and no instance was launched (rebuild the runner "
        "image from a release that has the feature, or set "
        'request.batch.runner_version_check to "warn"; see docs/cli/sim.md)',
        code=BATCH_RUNNER_FEATURE_CODE,
    )


def _resolve_max_job_seconds(
    batch_spec: dict[str, Any], fleet: dict[str, str]
) -> tuple[float, str]:
    """The fleet's whole-job ceiling and its provenance (issue #2833).

    Resolution order: ``request.batch.max_job_seconds`` (``"request"``),
    ``$KLT_BATCH_MAX_JOB_SECONDS`` (``"env"``), ``BATCH_MAX_JOB_SECONDS`` in
    the ``batch-fleet.env`` beside the provision script (``"fleet_config"``,
    the value 2am's ``launch`` tags the instance with), else
    :data:`DEFAULT_BATCH_MAX_JOB_SECONDS` (``"default"``, the harness's own
    fallback). A value that is present but not a positive, finite number is
    a :class:`~klayout_tools.sim.SimError` naming its source -- raised
    before any S3 write -- rather than silently falling through to a less
    specific source.
    """
    from .sim import SimError

    candidates: tuple[tuple[str, str, Any], ...] = (
        ("request", "request.batch.max_job_seconds", batch_spec.get("max_job_seconds")),
        ("env", f"${MAX_JOB_SECONDS_ENV}", os.environ.get(MAX_JOB_SECONDS_ENV)),
        (
            "fleet_config",
            f"{FLEET_MAX_JOB_SECONDS_KEY} in {FLEET_CONFIG_FILENAME}",
            fleet.get(FLEET_MAX_JOB_SECONDS_KEY),
        ),
    )
    for source, label, raw in candidates:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        value: Any = raw
        if isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                value = None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise SimError(
                f"{label} must be a positive number of seconds (got {raw!r})"
            )
        return float(value), source
    return DEFAULT_BATCH_MAX_JOB_SECONDS, "default"


def _resolve_job_ceiling_check(batch_spec: dict[str, Any]) -> str:
    """``request.batch.job_ceiling_check``: ``"warn"`` (default),
    ``"enforce"`` or ``"split"`` (issue #3446), else
    :class:`~klayout_tools.sim.SimError` before any S3 write (issue #2833)."""
    from .sim import SimError

    value = batch_spec.get("job_ceiling_check", "warn")
    if value not in JOB_CEILING_CHECK_MODES:
        raise SimError(
            "request.batch.job_ceiling_check must be one of "
            f"{', '.join(repr(m) for m in JOB_CEILING_CHECK_MODES)} "
            f"(got {value!r})"
        )
    return value


def _resolve_max_units_per_job(
    batch_spec: dict[str, Any], job_ceiling_check: str
) -> int | None:
    """``request.batch.max_units_per_job`` (issue #3446): a positive integer
    cap on the units of one job, which opts the request into splitting; else
    :class:`~klayout_tools.sim.SimError` before any S3 write. Combining it
    with ``job_ceiling_check: "enforce"`` is contradictory (one refuses an
    over-ceiling request, the other splits it) and is refused."""
    from .sim import SimError

    value = batch_spec.get("max_units_per_job")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SimError(
            "request.batch.max_units_per_job must be a positive integer "
            f"(got {value!r})",
            code=BATCH_MAX_UNITS_CODE,
        )
    if job_ceiling_check == "enforce":
        raise SimError(
            "request.batch.max_units_per_job splits an over-ceiling request "
            'and cannot be combined with job_ceiling_check "enforce", which '
            'refuses it; use "warn" or "split"',
            code=BATCH_MAX_UNITS_CODE,
        )
    return value


def _plan_job_split(assessment: dict[str, Any], config: BatchConfig) -> int:
    """The effective ``remote.hosts`` for an opted-in split (issue #3446).

    The shard cap is ``config.max_units_per_job`` when given, else the
    largest shard the ceiling allows (``assessment["max_units_per_job"]``),
    and the answer is the fewest balanced shards that respect it. Returns
    ``1`` when nothing needs splitting. Raises
    :class:`~klayout_tools.sim.SimError` before any S3 write when no split
    can keep every job inside the ceiling: code
    :data:`BATCH_JOB_CEILING_CODE` when even one unit exceeds it (lower
    ``options.timeout_s``), :data:`BATCH_MAX_UNITS_CODE` when the requested
    cap leaves a shard over it.
    """
    from .sim import SimError

    units = assessment["unit_count"]
    per_job = assessment["max_units_per_job"]
    requested = config.max_units_per_job
    if requested is None and not assessment["exceeds_ceiling"]:
        return 1
    if per_job == 0:
        raise SimError(
            f"backend 'batch' refused before submission: {assessment['warning']}",
            code=BATCH_JOB_CEILING_CODE,
        )
    cap = requested if requested is not None else per_job
    hosts = math.ceil(units / cap)
    largest = math.ceil(units / hosts)
    if largest > per_job:
        raise SimError(
            f"request.batch.max_units_per_job {requested} leaves a job of "
            f"{largest} units, over the {per_job}-unit limit the fleet's "
            f"{assessment['max_job_seconds']:g}s whole-job ceiling allows at "
            f"options.timeout_s {assessment['per_unit_timeout_s']:g}s; use at "
            f"most {per_job}",
            code=BATCH_MAX_UNITS_CODE,
        )
    return hosts


def _assess_job_ceiling(
    *, unit_count: int, per_unit_timeout_s: float, config: BatchConfig
) -> dict[str, Any]:
    """Deterministic pre-submit runtime-risk assessment of **one** batch job
    against the fleet's whole-job ceiling (issue #2833).

    The bound is :func:`_default_batch_job_timeout_s` -- every unit burning
    its full ``options.timeout_s`` one after another, plus
    :data:`_BATCH_JOB_OVERHEAD_S`. It is derived from the request alone and
    is a conservative *upper bound*, never a measured or predicted ngspice
    runtime (``basis`` says so); a job whose units finish well inside their
    ``timeout_s`` can run far below it. It is compared with
    ``config.max_job_seconds``: a bound *equal* to the ceiling does not
    exceed it.

    ``max_units_per_job`` is the largest shard whose own bound stays within
    the ceiling (``0`` when even one unit's bound exceeds it), and
    ``recommended_hosts`` the smallest ``remote.hosts`` whose largest shard
    -- :func:`sim_remote._shard_corner_points` balances shards to at most
    ``ceil(units / hosts)`` -- fits it, or ``None`` when no ``hosts`` value
    can (lower ``options.timeout_s`` instead). When the bound exceeds the
    ceiling the result carries a human-readable ``warning``.
    """
    units = max(1, unit_count)
    ceiling = config.max_job_seconds
    bound = _default_batch_job_timeout_s(units, per_unit_timeout_s)
    if per_unit_timeout_s > 0:
        per_job = max(0, int((ceiling - _BATCH_JOB_OVERHEAD_S) // per_unit_timeout_s))
        # Settle float rounding against the bound function itself, so the
        # recommendation and the comparison can never disagree.
        while per_job > 0 and (
            _default_batch_job_timeout_s(per_job, per_unit_timeout_s) > ceiling
        ):
            per_job -= 1
        while _default_batch_job_timeout_s(per_job + 1, per_unit_timeout_s) <= ceiling:
            per_job += 1
    else:
        per_job = units if bound <= ceiling else 0
    recommended = math.ceil(units / per_job) if per_job > 0 else None
    exceeds = bound > ceiling
    assessment: dict[str, Any] = {
        "basis": JOB_CEILING_BASIS,
        "unit_count": units,
        "per_unit_timeout_s": per_unit_timeout_s,
        "overhead_s": _BATCH_JOB_OVERHEAD_S,
        "job_bound_s": bound,
        "max_job_seconds": ceiling,
        "max_job_seconds_source": config.max_job_seconds_source,
        "exceeds_ceiling": exceeds,
        "check": config.job_ceiling_check,
        "max_units_per_job": per_job,
        "recommended_hosts": recommended,
    }
    if exceeds:
        message = (
            f"batch job bound {bound:g}s (options.timeout_s {per_unit_timeout_s:g}s "
            f"x {units} units + {_BATCH_JOB_OVERHEAD_S:g}s; a conservative "
            "fully-serial upper bound, not a runtime estimate) exceeds the "
            f"fleet's whole-job ceiling of {ceiling:g}s (max_job_seconds from "
            f"{config.max_job_seconds_source}); a job still running at the "
            "ceiling is killed and every unit is reported batch_job_timeout. "
        )
        if recommended is not None:
            message += (
                f"Set request.remote.hosts to at least {recommended} to run one "
                f"job per shard of at most {per_job} units, each within the "
                "ceiling"
            )
        else:
            message += (
                "No remote.hosts value fits: a single unit's bound exceeds the "
                "ceiling -- lower options.timeout_s to at most "
                f"{ceiling - _BATCH_JOB_OVERHEAD_S:g}s"
            )
        assessment["warning"] = message
    return assessment


def _enforce_job_ceiling(assessment: dict[str, Any]) -> None:
    """Act on :func:`_assess_job_ceiling`'s result before any S3 write:
    under ``"enforce"`` an exceeded ceiling is a
    :class:`~klayout_tools.sim.SimError` with code
    :data:`BATCH_JOB_CEILING_CODE`; under ``"warn"`` the same message is
    printed to stderr (so a caller piping ``--format json`` to a file still
    sees it before the job launches) and the assessment is reported in
    ``environment.remote.job_ceiling``."""
    from .sim import SimError

    warning = assessment.get("warning")
    if warning is None:
        return
    if assessment["check"] == "enforce":
        raise SimError(
            f"backend 'batch' refused before submission: {warning} (or set "
            'request.batch.job_ceiling_check to "warn" to submit anyway)',
            code=BATCH_JOB_CEILING_CODE,
        )
    print(f"klt sim: warning: {warning}", file=sys.stderr)


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


def _resolve_interrupted_grace_s(batch_spec: dict[str, Any]) -> float:
    """``request.batch.interrupted_grace_s``: a finite, non-negative number of
    seconds (default :data:`DEFAULT_INTERRUPTED_GRACE_S`; ``0`` disables the
    fail-fast), else :class:`~klayout_tools.sim.SimError` -- raised before any
    S3 write. A ``bool`` is refused, as for ``capacity_wait_s``."""
    from .sim import SimError

    value = batch_spec.get("interrupted_grace_s", DEFAULT_INTERRUPTED_GRACE_S)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise SimError(
            "request.batch.interrupted_grace_s must be a non-negative number "
            f"of seconds (got {value!r})"
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

#: The fragment of the same script's concurrency-cap refusal (``fail "N
#: instance(s) already running + K requested exceeds
#: BATCH_MAX_CONCURRENT_INSTANCES=M"``), checked before any launch record is
#: opened (issue #2917).
_CONCURRENCY_CAP_MARKER = "exceeds BATCH_MAX_CONCURRENT_INSTANCES"

#: The refusal codes :func:`submit_launch` waits out under ``capacity_wait_s``.
_RETRYABLE_LAUNCH_CODES = frozenset(
    {BATCH_NO_CAPACITY_CODE, BATCH_CONCURRENCY_CAP_CODE}
)


def _classify_launch_failure(stderr: str) -> str | None:
    """:data:`BATCH_NO_CAPACITY_CODE` when a failed ``launch``'s stderr is
    the provisioner's Spot capacity refusal,
    :data:`BATCH_CONCURRENCY_CAP_CODE` when it is the shared-fleet
    concurrency-cap refusal, else ``None``.

    **Coupling:** this is a text match against 2am's
    ``infra/aws/batch-fleet-provision.sh`` (``cmd_launch``'s final
    ``fail "no capacity in any of the ..."``), because that script exits ``1``
    for *every* refusal -- the diversification floors, the concurrency cap,
    a missing ``job.json``, an IAM denial, and a capacity shortfall alike --
    and exposes no distinct exit code. Kept in this one helper so a change on
    the 2am side is a one-line fix here. Every other launch failure stays
    unclassified, so it is never mistaken for something worth retrying.
    """
    if _CAPACITY_REFUSAL_MARKER in stderr:
        return BATCH_NO_CAPACITY_CODE
    if _CONCURRENCY_CAP_MARKER in stderr:
        return BATCH_CONCURRENCY_CAP_CODE
    return None


def _capacity_backoff_s(retry_index: int, jitter: Callable[[], float]) -> float:
    """The jittered, capped delay before capacity re-launch ``retry_index``
    (``0`` = the first re-launch). ``jitter`` returns a float in ``[0, 1)``."""
    nominal = min(
        _CAPACITY_BACKOFF_CAP_S, _CAPACITY_BACKOFF_BASE_S * (2.0**retry_index)
    )
    return nominal * (0.5 + 0.5 * jitter())


def _progress(line: str) -> None:
    """One progress line on stderr (never stdout: that carries the JSON
    envelope). Resolves ``sys.stderr`` at call time so it can be captured."""
    print(line, file=sys.stderr, flush=True)


def submit_job(
    config: BatchConfig,
    job_id: str,
    spec: BatchJobSpec,
    *,
    runner: remote_transport.CommandRunner | None = None,
    progress: bool = True,
) -> None:
    """Upload ``inputs/**`` and then ``job.json`` to
    ``s3://<bucket>/<jobs>/<job-id>/``.

    ``job.json`` is uploaded **last**, on purpose: it is the object
    ``batch-fleet-provision.sh launch`` asserts exists (``s3api
    head-object``) and the harness fetches first, so writing it last makes
    the submission atomic from the fleet's point of view -- a launch can
    never observe a job spec whose inputs are still uploading.

    ``progress`` (issue #2992) prints the job id on stderr before uploading;
    a verb whose stderr carries its error envelope (``klt netlist``) passes
    ``False``.
    """
    run = runner if runner is not None else _run_subprocess
    prefix = job_uri(config, job_id)
    if progress:
        _progress(f"klt: batch job {job_id} submitting to {prefix}")
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

    **Capacity wait (issues #2721, #2917).** A launch the provisioner refuses
    for lack of Spot capacity, or because the shared fleet is at
    ``BATCH_MAX_CONCURRENT_INSTANCES``, raises :class:`BatchError` with
    ``code == "batch_no_capacity"`` / ``"batch_concurrency_cap"``
    (:func:`_classify_launch_failure`). With
    ``config.capacity_wait_s > 0`` such a refusal is instead retried -- the
    same ``launch`` invocation for the same job id, after a jittered, capped
    exponential backoff (:func:`_capacity_backoff_s`) -- until that budget
    elapses, then raised with the same code. Any *other* launch failure is
    raised at once, never retried. Re-invoking is safe because a refused
    launch creates no instance and closes the launch record it opened (2am's
    ``cmd_launch`` writes a ``start`` record and its matching ``end`` on that
    path; the concurrency-cap check runs before any record is opened), and
    2am's own Spot-interruption ``reconcile`` already re-launches under an
    existing job id. The wait is measured on its own clock and is
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
            if exc.code not in _RETRYABLE_LAUNCH_CODES or config.capacity_wait_s <= 0:
                raise
            remaining = config.capacity_wait_s - (monotonic() - started)
            if remaining <= 0:
                raise BatchError(
                    f"{exc} (capacity wait budget of {config.capacity_wait_s:g}s "
                    f"exhausted after {attempts} launch attempt(s))",
                    code=exc.code,
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


#: Coarse cadence of the stderr heartbeat :func:`poll_status` prints while a
#: job's state is unchanged (issue #2978). A state change prints at once.
BATCH_HEARTBEAT_INTERVAL_S = 300.0

#: ``BatchError.code`` for ``--attach`` to a job id with no ``job.json`` in
#: the bucket (never submitted, wrong bucket/profile, or already expired).
BATCH_UNKNOWN_JOB_CODE = "batch_unknown_job"


def _heartbeat(job_id: str, state: str | None, elapsed_s: float) -> None:
    """One progress line on stderr (never stdout: ``--format json`` stdout
    must stay byte-identical, issue #2978)."""
    sys.stderr.write(
        f"klt sim: batch job {job_id} state={state or 'no status.json yet'} "
        f"elapsed={elapsed_s:.0f}s\n"
    )
    sys.stderr.flush()


def job_exists(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> bool:
    """Whether ``<prefix>/job.json`` exists, i.e. the job was really
    submitted (issue #2978)."""
    run = runner if runner is not None else _run_subprocess
    argv = _aws_argv(config, "s3", "cp", f"{job_uri(config, job_id)}/job.json", "-")
    try:
        result = run(argv, _BATCH_AWS_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise BatchError(f"job.json lookup could not complete: {exc}") from exc
    return result.returncode == 0


def _advance_interrupted_clock(
    state: str, interrupted_since: float | None, now: float
) -> float | None:
    """Start the continuous-``interrupted`` clock on entry, reset it on any
    other state."""
    if state != "interrupted":
        return None
    return now if interrupted_since is None else interrupted_since


def _format_progress(
    job_id: str, status: dict[str, Any], state: str, elapsed_s: float
) -> str:
    """The stderr progress line for one observed ``status.json``."""
    instance = status.get("instance_id")
    return f"klt: batch job {job_id} state={state} elapsed={elapsed_s:.0f}s" + (
        f" instance={instance}" if instance else ""
    )


def _check_interrupted_grace(
    config: BatchConfig,
    job_id: str,
    interrupted_since: float | None,
    now: float,
) -> None:
    """Raise :class:`BatchJobInterrupted` once the job has been continuously
    ``interrupted`` for ``config.interrupted_grace_s`` (``0`` = unbounded)."""
    if (
        interrupted_since is None
        or config.interrupted_grace_s <= 0
        or now - interrupted_since < config.interrupted_grace_s
    ):
        return
    interrupted_s = now - interrupted_since
    raise BatchJobInterrupted(
        f"job {job_id} stayed 'interrupted' (Spot reclaimed) for "
        f"{interrupted_s:.0f}s, past batch.interrupted_grace_s="
        f"{config.interrupted_grace_s:g}s, with no re-launch observed; "
        "it may still be recoverable on the fleet (check "
        f"{job_uri(config, job_id)}/status.json) -- the client does "
        "not re-launch it itself",
        interrupted_s=interrupted_s,
    )


def _report_progress(job_id: str, state: str) -> None:
    """One per-job progress line on stderr (stdout carries the JSON report)."""
    print(f"klt sim: batch job {job_id}: {state}", file=sys.stderr, flush=True)


def _check_never_started(
    config: BatchConfig, job_id: str, last_state: str | None, elapsed: float
) -> None:
    """Raise :class:`BatchPollTimeout` when no ``status.json`` was ever
    observed within :data:`_BATCH_PROVISION_SLACK_S` (and the overall poll
    budget has not itself elapsed, which reports the ordinary timeout)."""
    if (
        last_state is None
        and elapsed >= _BATCH_PROVISION_SLACK_S
        and elapsed < config.poll_timeout_s
    ):
        raise BatchPollTimeout(
            f"job {job_id} never started: no status.json after "
            f"{_BATCH_PROVISION_SLACK_S:g}s (last observed state: "
            "no status.json yet)",
            last_state=None,
        )


def poll_status(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
    heartbeat: bool = False,
    progress: bool = True,
    on_state: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Poll ``status.json`` until the job reaches a terminal state
    (:data:`BATCH_TERMINAL_STATES`) and return that status document.

    ``running`` and ``interrupted`` both mean *wait*
    (:data:`BATCH_WAITING_STATES`). ``interrupted`` specifically is **not**
    re-submitted here: Spot reclaimed the instance and 2am's own scheduled
    ``reconcile`` re-launches the job -- a client that also re-launched
    would double-spend the shared fleet budget. The wait is bounded, though:
    a job continuously ``interrupted`` for longer than
    ``config.interrupted_grace_s`` (``0`` = unbounded) raises
    :class:`BatchJobInterrupted`; returning to ``running`` resets that clock,
    and an unreadable ``status.json`` neither starts nor resets it.

    Unless ``progress=False``, progress goes to stderr (:func:`_progress`): a
    line on every state change and one per :data:`_POLL_PROGRESS_EVERY` polls
    otherwise (state, elapsed, the instance id when ``status.json`` has one).

    ``on_state`` is called once per *change* of observed state (including a
    flip back, e.g. ``interrupted`` -> ``running``); unchanged and unreadable
    polls do not call it.

    Raises :class:`BatchPollTimeout` once ``config.poll_timeout_s`` elapses
    (the caller reports unrun corners rather than a failed sweep) -- or
    once :data:`_BATCH_PROVISION_SLACK_S` passes with no ``status.json`` ever
    observed, so a job that never launched does not wait out the (possibly
    hours-long) serial budget. ``last_state`` is ``None`` in that case.
    Raises
    :class:`BatchError` on a state outside the documented set -- a contract
    violation, surfaced loudly rather than waited on forever.

    With ``heartbeat=True`` (opt-in; ``klt sim`` sets it, with
    ``progress=False`` so the two stderr streams do not duplicate each other;
    ``klt netlist`` does not, so its stderr stays a bare JSON envelope), a
    heartbeat line
    (job id, last observed state, elapsed) goes to **stderr** on every state
    change and otherwise every :data:`BATCH_HEARTBEAT_INTERVAL_S`
    (issue #2978).
    """
    run = runner if runner is not None else _run_subprocess
    started = monotonic()
    last_state: str | None = None
    beat_state: str | None = None
    beat_at: float | None = None
    reported_state: str | None = None
    interrupted_since: float | None = None
    polls = 0
    while True:
        polls += 1
        status = _read_status(config, job_id, run)
        now = monotonic()
        if status is not None:
            new_state = str(status.get("state") or "")
            if on_state is not None and new_state != last_state:
                on_state(new_state)
            last_state = new_state
            if last_state in BATCH_TERMINAL_STATES:
                return status
            if last_state not in BATCH_WAITING_STATES:
                raise BatchError(
                    f"job {job_id}: status.json reported unknown state "
                    f"{last_state!r} (expected one of "
                    f"{', '.join(BATCH_WAITING_STATES + BATCH_TERMINAL_STATES)})"
                )
            interrupted_since = _advance_interrupted_clock(
                last_state, interrupted_since, now
            )
            if progress and (
                last_state != reported_state or polls % _POLL_PROGRESS_EVERY == 0
            ):
                reported_state = last_state
                _progress(_format_progress(job_id, status, last_state, now - started))
        elif progress and polls % _POLL_PROGRESS_EVERY == 0:
            _progress(
                f"klt: batch job {job_id} no readable status.json "
                f"elapsed={now - started:.0f}s"
            )
        _check_interrupted_grace(config, job_id, interrupted_since, now)
        elapsed = now - started
        _check_never_started(config, job_id, last_state, elapsed)
        if elapsed >= config.poll_timeout_s:
            raise BatchPollTimeout(
                f"job {job_id} did not reach a terminal state within "
                f"{config.poll_timeout_s:g}s (last observed state: "
                f"{last_state or 'no status.json yet'})",
                last_state=last_state,
            )
        if heartbeat and (
            beat_at is None
            or last_state != beat_state
            or elapsed - beat_at >= BATCH_HEARTBEAT_INTERVAL_S
        ):
            _heartbeat(job_id, last_state, elapsed)
            beat_state, beat_at = last_state, elapsed
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


def _bounded_str(value: Any, limit: int = WORKER_CAPTURE_STR_MAX) -> str | None:
    """A JSON-safe, bounded, control-character-free copy of a captured string
    (``None`` for anything that is not a string)."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch if (ch >= " " or ch in "\n\t") else "?" for ch in value)
    return cleaned[:limit]


def _client_pdk_stamp(request: dict[str, Any] | None) -> dict[str, Any] | None:
    """The submitter's own local view of the requested PDK variant's
    ``SOURCES`` stamp, in the same ``sources-sha256`` scheme the worker
    capture uses. ``None`` when nothing comparable can be read locally."""
    models = (request or {}).get("models") or {}
    variant = models.get("pdk")
    root = models.get("pdk_root") or os.environ.get("PDK_ROOT")
    if not (isinstance(variant, str) and variant and isinstance(root, str) and root):
        return None
    try:
        with open(os.path.join(root, variant, "SOURCES"), "rb") as handle:
            text = handle.read(16385)[:16384].decode("utf-8", "replace")
    except OSError:
        return None
    normalized = "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n")
    )
    return {
        "variant": variant,
        "stamp_scheme": "sources-sha256",
        "stamp_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _compare_pdk_stamps(
    worker_pdk: dict[str, Any], client: dict[str, Any] | None
) -> dict[str, Any]:
    """Compare worker and client PDK stamps -- only when both provide the
    same variant under the same stamp scheme. Anything else is
    ``not_comparable``, never a proven mismatch. A match is not model-closure
    equality (see docs/cli/sim.md)."""
    worker_ok = (
        worker_pdk.get("variant")
        and worker_pdk.get("stamp_scheme")
        and worker_pdk.get("stamp_sha256")
    )
    if (
        not worker_ok
        or client is None
        or client.get("variant") != worker_pdk.get("variant")
        or client.get("stamp_scheme") != worker_pdk.get("stamp_scheme")
    ):
        return {"status": "not_comparable"}
    if client.get("stamp_sha256") == worker_pdk.get("stamp_sha256"):
        return {"status": "match"}
    return {
        "status": "mismatch",
        "warning": (
            f"the worker's PDK revision stamp for {worker_pdk['variant']} differs "
            "from the one the submitter resolved locally; results may differ "
            "from a local run (a matching stamp would not prove identical models)"
        ),
    }


def _bounded_model_inputs(doc: dict[str, Any]) -> list[dict[str, str | None]] | None:
    """The worker-reported ``model_inputs`` list, re-validated and bounded.
    ``None`` (not ``[]``) when the capture carries no list at all."""
    models_in = doc.get("model_inputs")
    if not isinstance(models_in, list):
        return None
    return [
        {
            "name": _bounded_str(entry.get("name")),
            "sha256": _bounded_str(entry.get("sha256"), 64),
        }
        for entry in models_in[:WORKER_CAPTURE_MODEL_INPUTS_MAX]
        if isinstance(entry, dict)
    ]


def _read_worker_environment(
    local_dir: str | None, request: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``worker_environment`` block of ``environment.remote`` (issue #2834).

    Worker-observed: it describes what the job instance resolved, never the
    submitter's values. Always present; ``capture`` is ``"ok"``, ``"absent"``
    (legacy worker or no collected artifact) or ``"malformed"``, and a
    non-ok capture carries ``None`` observations. Every field is
    re-validated and bounded here -- the file is untrusted input.
    """
    unknown: dict[str, Any] = {
        "capture": "absent",
        "simulator": None,
        "pdk": None,
        "cpu_model": None,
        "omp_num_threads": None,
        "image_manifest": None,
        "model_inputs": None,
        "diagnostics": [],
        "pdk_stamp_comparison": {"status": "not_comparable"},
    }
    if local_dir is None:
        return unknown
    path = os.path.join(local_dir, BATCH_WORKER_ENVIRONMENT_FILENAME)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(WORKER_CAPTURE_FILE_MAX + 1)
    except OSError:
        return unknown
    try:
        if len(raw) > WORKER_CAPTURE_FILE_MAX:
            raise ValueError("oversized")
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not an object")
        sim_in = doc.get("simulator")
        sim_in = sim_in if isinstance(sim_in, dict) else {}
        simulator = {
            "requested_binary": _bounded_str(sim_in.get("requested_binary")),
            "binary_source": _bounded_str(sim_in.get("binary_source"), 40),
            "selected_path": _bounded_str(sim_in.get("selected_path")),
            "version_banner": _bounded_str(
                sim_in.get("version_banner"), WORKER_CAPTURE_BANNER_MAX
            ),
            "version_probe": _bounded_str(sim_in.get("version_probe"), 40),
        }
        pdk_in = doc.get("pdk")
        pdk_in = pdk_in if isinstance(pdk_in, dict) else {}
        pdk = {
            "variant": _bounded_str(pdk_in.get("variant")),
            "root": _bounded_str(pdk_in.get("root")),
            "stamp_scheme": _bounded_str(pdk_in.get("stamp_scheme"), 40),
            "stamp_sha256": _bounded_str(pdk_in.get("stamp_sha256"), 64),
            "stamp_text": _bounded_str(pdk_in.get("stamp_text"), 1000),
            "models_staged": pdk_in.get("models_staged") is True,
        }
        manifest_in = doc.get("image_manifest")
        manifest: dict[str, Any] | None = None
        if isinstance(manifest_in, dict):
            manifest = {}
            for key, value in list(manifest_in.items())[:16]:
                if isinstance(value, bool | int):
                    manifest[_bounded_str(key, 40) or "?"] = value
                elif isinstance(value, str):
                    manifest[_bounded_str(key, 40) or "?"] = _bounded_str(value)
        diagnostics = [
            _bounded_str(item, 200)
            for item in (doc.get("diagnostics") or [])[:20]
            if isinstance(item, str)
        ]
        return {
            "capture": "ok",
            "simulator": simulator,
            "pdk": pdk,
            "cpu_model": _bounded_str(doc.get("cpu_model")),
            "omp_num_threads": _bounded_str(doc.get("omp_num_threads"), 40),
            "image_manifest": manifest,
            "model_inputs": _bounded_model_inputs(doc),
            "diagnostics": diagnostics,
            "pdk_stamp_comparison": _compare_pdk_stamps(
                pdk, _client_pdk_stamp(request)
            ),
        }
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return {**unknown, "capture": "malformed"}


def _read_runner_identity(
    local_dir: str | None, request: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``environment.remote`` runner-identity fields (issue #2719).

    ``runner_klt_version`` is what the fleet image's own ``klt --version``
    printed -- never the client's. It is ``None`` when the identity file is
    absent, malformed, or the preflight could not read a version, and
    ``runner_compatibility`` is then ``"unknown"``; so it is when either
    version has no major.minor release segment. Otherwise the two
    major.minor values are compared (:func:`_runner_gate_release`, issue
    #3184 -- a post-tag git client ``0.7.1.dev212+g<sha>`` and a ``0.7.3``
    release both match a ``0.7.0`` runner, while ``0.8.0.devN`` does not),
    giving ``"match"`` / ``"mismatch"``; both fields keep the full build
    strings. The comparison is done here, not trusted from the file. A
    non-match carries ``runner_compatibility_warning``. A match does not
    prove identical code (every build of one minor matches), so when
    ``request`` uses a :data:`_RUNNER_REQUEST_FEATURES` entry the fields also
    carry ``runner_request_features`` (issue #3249): ``required`` names,
    ``missing`` names (``null`` when the runner version is unknown) and a
    ``status`` of ``supported`` / ``unsupported`` / ``unknown``; see
    docs/cli/sim.md.
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
    runner_release = _runner_gate_release(runner) if runner is not None else None
    client_release = _runner_gate_release(client)
    if runner_release is None or client_release is None:
        status = "unknown"
        warning = (
            "the fleet runner's klt version could not be established; request "
            "options newer than the runner may have been ignored"
        )
    elif runner_release == client_release:
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
    features = _request_features(request) if request else []
    if features:
        missing = _missing_runner_features(features, runner)
        fields["runner_request_features"] = {
            "required": features,
            "missing": missing,
            "status": (
                "unknown"
                if missing is None
                else "unsupported"
                if missing
                else "supported"
            ),
        }
        if missing and warning is None:
            warning = (
                f"the fleet runner ran klt {runner}, which predates the request "
                f"feature(s) {', '.join(missing)}; the job may have failed to "
                "parse them -- update the runner image"
            )
    if warning is not None:
        fields["runner_compatibility_warning"] = warning
    return fields


def _read_job_identity(
    local_dir: str | None, request: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Runner identity plus the worker-observed environment capture, merged
    into ``environment.remote``. Neither reader can raise."""
    worker_environment = _read_worker_environment(local_dir, request)
    return {
        **_read_runner_identity(local_dir, request),
        "worker_environment": worker_environment,
        # ``None`` (not ``[]``) when the runner did not report it, so "none
        # staged" and "unknown / older runner" stay distinguishable.
        "runner_model_inputs": worker_environment.get("model_inputs"),
    }


def _status_exit_code(status: dict[str, Any]) -> int | None:
    """``status.json``'s ``exit_code`` as an int -- the harness writes it as
    a string (and as ``""`` when the job never ran)."""
    try:
        return int(str(status.get("exit_code", "")).strip())
    except (TypeError, ValueError):
        return None


#: Engine logs a collected ``outputs/`` tree may hold (``keep_artifacts`` runs
#: copy each corner's log under ``artifacts/``). Only read to surface the
#: engine's fatal error; never copied wholesale into the JSON.
_BATCH_ENGINE_LOG_NAMES = ("ngspice.log", "xyce.log")

#: Bytes read from the head of an engine log (a fatal include/parse error is
#: printed before the simulation starts) and lines kept in a failure tail.
_BATCH_ENGINE_LOG_HEAD_BYTES = 256 * 1024
_BATCH_ENGINE_LOG_TAIL_LINES = 12
_BATCH_ENGINE_LOG_TAIL_CHARS = 1500


def _find_engine_logs(collected: str) -> list[str]:
    """Paths of every engine log under a collected ``outputs/`` tree, sorted."""
    found: list[str] = []
    for root, _dirs, files in os.walk(collected):
        found.extend(
            os.path.join(root, name)
            for name in files
            if name in _BATCH_ENGINE_LOG_NAMES
        )
    return sorted(found)


def _read_log_head(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(_BATCH_ENGINE_LOG_HEAD_BYTES).decode(
                "utf-8", errors="replace"
            )
    except OSError:
        return ""


def _missing_include_diagnostic(log_text: str) -> dict[str, Any] | None:
    """The ``missing_include`` diagnostic the local classifier would emit for
    ``log_text`` (naming the file ngspice could not find), or ``None``."""
    from .sim import _classify_diagnostics

    for diagnostic in _classify_diagnostics(log_text):
        if diagnostic.get("code") == "missing_include":
            return diagnostic
    return None


def _surface_missing_include(corners: list[dict[str, Any]], collected: str) -> None:
    """Add a ``missing_include`` diagnostic to every errored corner whose
    retrieved engine log shows ngspice failing on a ``.include`` but whose
    report (written by the runner's own, possibly older, ``klt``) carries no
    such code -- otherwise the caller sees only ``measurement ... produced no
    value``. Corners are matched to logs through ``artifacts.log``
    (``<$EDA_OUTPUT_DIR>/artifacts/<rel>``); with exactly one log in the tree
    it applies to every such corner. A no-op when no log was collected."""
    logs = _find_engine_logs(collected)
    if not logs:
        return
    marker = f"/{BATCH_ARTIFACTS_DIRNAME}/"
    for corner in corners:
        if corner.get("status") != "error":
            continue
        diagnostics = corner.setdefault("diagnostics", [])
        if any(d.get("code") == "missing_include" for d in diagnostics):
            continue
        log_path = None
        remote = (corner.get("artifacts") or {}).get("log")
        if isinstance(remote, str) and marker in remote:
            candidate = os.path.join(
                collected,
                BATCH_ARTIFACTS_DIRNAME,
                remote[remote.index(marker) + len(marker) :],
            )
            if os.path.isfile(candidate):
                log_path = candidate
        if log_path is None and len(logs) == 1:
            log_path = logs[0]
        if log_path is None:
            continue
        found = _missing_include_diagnostic(_read_log_head(log_path))
        if found is not None:
            diagnostics.append(found)


def _engine_log_note(collected: str) -> tuple[str, dict[str, Any] | None]:
    """For a job that failed without a usable report: a short message
    fragment carrying the tail of the retrieved engine log, or stating the
    log was unavailable, plus the ``missing_include`` diagnostic when the log
    shows one."""
    logs = _find_engine_logs(collected)
    if not logs:
        return (
            "engine log unavailable: no ngspice.log/xyce.log in the collected "
            "outputs (engine logs are retrieved only when artifacts are kept)",
            None,
        )
    text = _read_log_head(logs[0])
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-_BATCH_ENGINE_LOG_TAIL_LINES:])
    tail = tail[-_BATCH_ENGINE_LOG_TAIL_CHARS:]
    return f"{os.path.basename(logs[0])} tail: {tail}", _missing_include_diagnostic(
        text
    )


def _job_failure_corners(
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    status: dict[str, Any],
    job_id: str,
    *,
    runner_error: dict[str, Any] | None = None,
    collection_note: str | None = None,
    log_note: str | None = None,
    log_diagnostic: dict[str, Any] | None = None,
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
    replaces the primary job-failure code or the failed status.

    ``log_note`` is the tail of the retrieved engine log (or a statement that
    none was available) and ``log_diagnostic`` a ``missing_include``
    diagnostic derived from it; both are additive."""
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
    if log_note:
        message += f" [{log_note}]"
    corners = [
        _unrun_corner_report(point, measurements_spec, code, message=message)
        for point in corner_points
    ]
    if log_diagnostic is not None:
        for corner in corners:
            corner["diagnostics"].append(dict(log_diagnostic))
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
    request: dict[str, Any] | None = None,
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
    log_note: str | None = "engine log unavailable: outputs could not be collected"
    log_diagnostic: dict[str, Any] | None = None
    identity = _read_job_identity(None)
    try:
        with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
            collect_outputs(config, job_id, collected, runner=runner)
            identity = _read_job_identity(collected, request)
            log_note, log_diagnostic = _engine_log_note(collected)
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
        log_note=log_note,
        log_diagnostic=log_diagnostic,
    )
    _annotate_preflight_rejection(corners, runner_error, identity)
    return corners, identity


def _annotate_preflight_rejection(
    corners: list[dict[str, Any]],
    runner_error: dict[str, Any] | None,
    identity: dict[str, Any],
) -> None:
    """When the runner preflight rejected the job before ``klt sim`` ran
    (a version mismatch/unknown ``runner_error``), carry the two ``klt``
    identities on each matching diagnostic so the cause is machine-readable.
    A no-op for any other failure."""
    if runner_error is None or runner_error.get("code") not in (
        BATCH_RUNNER_MISMATCH_CODE,
        BATCH_RUNNER_UNKNOWN_CODE,
        BATCH_RUNNER_FEATURE_CODE,
    ):
        return
    for corner in corners:
        for diagnostic in corner.get("diagnostics") or []:
            if diagnostic.get("runner_code") == runner_error["code"]:
                diagnostic["runner_klt_version"] = identity["runner_klt_version"]
                diagnostic["client_klt_version"] = identity["client_klt_version"]


def _poll_timeout_corners(
    corner_points: list[CornerPoint],
    measurements_spec: list[dict[str, Any]],
    exc: BatchPollTimeout,
) -> list[dict[str, Any]]:
    """Every unit of a job whose wall-clock poll budget elapsed, reported
    through the shared unrun-corner shape (``batch_poll_timeout``, or
    ``batch_job_interrupted`` for :class:`BatchJobInterrupted`). The job
    may well still be running on the fleet -- which is exactly why this is
    not a ``SimError``: the sweep reports what it knows instead of claiming
    the run failed."""
    return [
        _unrun_corner_report(
            point,
            measurements_spec,
            "batch_job_interrupted"
            if isinstance(exc, BatchJobInterrupted)
            else "batch_poll_timeout",
            message=str(exc),
        )
        for point in corner_points
    ]


def _prepare_batch_job_spec(
    request: dict[str, Any],
    netlist_path: str,
    *,
    config: BatchConfig,
    corner_count: int,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    explicit_points: list[CornerPoint] | None = None,
    request_dir: str | None = None,
) -> BatchJobSpec:
    """Build one job's :class:`BatchJobSpec` from the client's request --
    the shared front half of :func:`_run_batch_job` and the submit-only path
    (:mod:`klayout_tools.sim_batch_async`). Uploads nothing."""
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

    return _build_batch_job_spec(
        remote_request,
        netlist_path,
        corner_count=corner_count,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        request_dir=request_dir,
        version_check=config.runner_version_check,
    )


def _utc_now() -> str:
    """The current wall clock as an RFC3339 UTC string (the default ``now``
    seam of :func:`_run_batch_job`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_batch_receipt(path: str, receipt: dict[str, Any]) -> None:
    """Atomically (temp + rename) write the submit receipt. A failure to
    write is a stderr warning, never a failed sweep: the receipt is
    provenance, not part of the result."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        print(
            f"klt sim: warning: could not write batch receipt {path}: {exc}",
            file=sys.stderr,
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _poll_status_interruptibly(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None,
    sleep: Any,
    heartbeat: bool = False,
    progress: bool = True,
    on_state: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """:func:`poll_status`, with SIGINT/SIGTERM converted into
    :class:`BatchClientInterrupted` for the duration of the wait only
    (issue #3065). Prior handlers are restored on every exit path. Handlers
    can only be installed from the main thread, so a sharded run's worker
    threads poll unwrapped (their receipts are still written)."""
    if threading.current_thread() is not threading.main_thread():
        return poll_status(
            config,
            job_id,
            runner=runner,
            sleep=sleep,
            heartbeat=heartbeat,
            progress=progress,
            on_state=on_state,
        )

    def _handle(signum: int, _frame: Any) -> None:
        raise BatchClientInterrupted(
            f"client terminated by {signal.Signals(signum).name} while waiting "
            f"on job {job_id} (bucket {config.bucket}, region {config.region}); "
            "the remote job is left running -- the fleet has no cancel",
            job_id=job_id,
            signum=signum,
        )

    prev_handlers: dict[int, Any] = {}
    _signal_handlers_install(prev_handlers, _handle)
    try:
        return poll_status(
            config,
            job_id,
            runner=runner,
            sleep=sleep,
            heartbeat=heartbeat,
            progress=progress,
            on_state=on_state,
        )
    except BatchClientInterrupted as exc:
        print(f"klt sim: {exc}", file=sys.stderr)
        raise
    finally:
        _signal_handlers_restore(prev_handlers)


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
    attach: bool = False,
    now: Callable[[], str] = _utc_now,
    receipt_name: str | None = None,
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

    ``attach=True`` (issue #2978) resumes an *existing* ``job_id``: nothing
    is staged, submitted or launched -- the job must already have a
    ``job.json`` (else :data:`BATCH_UNKNOWN_JOB_CODE`) and the call goes
    straight to polling and collecting. A job Spot-reclaimed mid-run is
    still waited on, never re-launched (see :func:`poll_status`).

    The returned environment also carries client-observed wall-clock
    timestamps (``client_submitted_at``, ``client_launched_at``,
    ``client_status_terminal_at``, ``client_collected_at``; ``now`` is the
    clock seam). A phase that was not reached is ``None``, never fabricated.
    """
    if attach:
        if not job_exists(config, job_id, runner=runner):
            raise BatchError(
                f"no batch job {job_id!r} in bucket {config.bucket!r} under "
                f"prefix {config.jobs_prefix!r} (never submitted, wrong "
                "bucket/profile/region, or expired)",
                code=BATCH_UNKNOWN_JOB_CODE,
            )
        return _poll_and_collect(
            config=config,
            job_id=job_id,
            corner_points=corner_points,
            keep_artifacts=keep_artifacts,
            artifacts_dir=artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
            runner=runner,
            sleep=sleep,
            timestamps={
                "client_submitted_at": None,
                "client_launched_at": None,
                "client_status_terminal_at": None,
                "client_collected_at": None,
            },
            now=now,
        )
    spec = _prepare_batch_job_spec(
        request,
        netlist_path,
        config=config,
        corner_count=len(corner_points),
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        explicit_points=_points_to_push(request, corner_points, explicit_points),
        request_dir=request_dir,
    )
    timestamps: dict[str, str | None] = {
        "client_submitted_at": now(),
        "client_launched_at": None,
        "client_status_terminal_at": None,
        "client_collected_at": None,
    }
    submit_job(config, job_id, spec, runner=runner)
    _report_progress(job_id, "submitted")
    launch_job(config, job_id, runner=runner)
    timestamps["client_launched_at"] = now()
    # Issue #2978: name the job on stderr the moment it exists, so a caller
    # killed by an outer wall-clock limit knows what it orphaned.
    sys.stderr.write(
        f"klt sim: batch job {job_id} launched; "
        f"re-attach with `klt sim <request> --backend batch --attach {job_id}`\n"
    )
    sys.stderr.flush()
    receipt_path = os.path.join(
        artifacts_dir,
        BATCH_RECEIPT_FILENAME if receipt_name is None else receipt_name,
    )
    receipt: dict[str, Any] = {
        "job_id": job_id,
        # getattr: the receipt is best-effort and must never break a run.
        "bucket": getattr(config, "bucket", None),
        "jobs_prefix": getattr(config, "jobs_prefix", None),
        "region": getattr(config, "region", None),
        "profile": getattr(config, "profile", None),
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "request_sha256": hashlib.sha256(
            json.dumps(request, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "state": None,
    }
    _write_batch_receipt(receipt_path, receipt)
    return _poll_and_collect(
        config=config,
        job_id=job_id,
        corner_points=corner_points,
        keep_artifacts=keep_artifacts,
        artifacts_dir=artifacts_dir,
        request=request,
        measurements_spec=measurements_spec,
        runner=runner,
        sleep=sleep,
        timestamps=timestamps,
        now=now,
        receipt_path=receipt_path,
        receipt=receipt,
    )


def _poll_and_collect(
    *,
    config: BatchConfig,
    job_id: str,
    corner_points: list[CornerPoint],
    keep_artifacts: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    runner: remote_transport.CommandRunner | None,
    sleep: Any,
    timestamps: dict[str, str | None],
    now: Callable[[], str],
    receipt_path: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The wait-and-collect tail shared by a fresh submit and ``--attach``.

    ``timestamps`` carries the client-observed phase times already recorded
    (all ``None`` on ``--attach``, where this client never submitted or
    launched); the terminal/collected phases are filled in here.

    ``receipt_path``/``receipt`` (issue #3065) are the submit receipt a fresh
    submit wrote; ``--attach`` passes none and writes no receipt."""
    try:
        status = _poll_status_interruptibly(
            config,
            job_id,
            runner=runner,
            sleep=sleep,
            heartbeat=True,
            progress=False,
            on_state=lambda state: _report_progress(job_id, state),
        )
    except BatchPollTimeout as exc:
        if receipt_path is not None and receipt is not None:
            _write_batch_receipt(
                receipt_path,
                {**receipt, "poll_timed_out": True, "last_state": exc.last_state},
            )
        return (
            _poll_timeout_corners(corner_points, measurements_spec, exc),
            None,
            {
                "provider": "aws-batch-fleet",
                "job_id": job_id,
                "bucket": config.bucket,
                "region": config.region,
                "state": exc.last_state,
                **(
                    {"interrupted_s": round(exc.interrupted_s, 1)}
                    if isinstance(exc, BatchJobInterrupted)
                    else {}
                ),
                **_read_job_identity(None),
                **timestamps,
            },
        )

    timestamps["client_status_terminal_at"] = now()
    if receipt_path is not None and receipt is not None:
        _write_batch_receipt(
            receipt_path,
            {
                **receipt,
                "state": status.get("state"),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    environment = {**_batch_environment(config, job_id, status), **timestamps}
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
            request=request,
        )
        return failed_corners, None, {**environment, **identity}

    with tempfile.TemporaryDirectory(prefix="klt-batch-outputs-") as collected:
        collect_outputs(config, job_id, collected, runner=runner)
        from .sim import check_ngspice_options_echo

        environment["client_collected_at"] = now()
        report = read_collected_report(collected)
        check_ngspice_options_echo(request, report)
        environment = {**environment, **_read_job_identity(collected, request)}
        corners = report.get("corners") or []
        _surface_missing_include(corners, collected)
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
    attach_job_id: str | None = None,
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
    (``checkpoint`` is therefore unused here: ``run_sim`` records the
    returned reports into the resume checkpoint after dispatch, skipping
    ``lost_shard`` corners -- issue #3367). The batch path's own wall-clock
    bound is ``request.batch.poll_timeout_s``, whose overrun reports every
    unit through the same unrun-corner shape.

    Before submitting, the job's conservative bound is checked against the
    fleet's whole-job ceiling (:func:`_assess_job_ceiling`, issue #2833):
    ``request.batch.job_ceiling_check: "enforce"`` refuses an overlong job,
    the default ``"warn"`` submits it with a stderr warning, and either way
    the assessment is reported as ``environment.remote.job_ceiling``. The
    ``hosts > 1`` path (:func:`_run_batch_fleet`) is unchanged.

    ``attach_job_id`` (issue #2978) re-attaches to that already-submitted job
    instead of submitting a new one: no job-ceiling assessment, no upload, no
    launch. The request supplies the corner list and measurement grading, so
    it must be the request the job was submitted from.
    """
    from .sim import SimError

    del analysis, models_lib, max_workers  # see docstring
    del deadline, initial_ppid, checkpoint, probe_abort  # not honored

    config = _resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )
    if attach_job_id is not None:
        try:
            return _run_batch_job(
                config=config,
                job_id=attach_job_id,
                corner_points=corner_points,
                netlist_path=netlist_path,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                artifacts_dir=artifacts_dir,
                request=request,
                measurements_spec=measurements_spec,
                request_dir=request_dir,
                attach=True,
            )
        except BatchError as exc:
            raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    # Issue #2833: assess the one job against the fleet's whole-job ceiling
    # before `submit_job`'s first S3 write, so an "enforce" refusal can never
    # leave a partial contract or launch a billable job.
    job_ceiling = _assess_job_ceiling(
        unit_count=len(corner_points), per_unit_timeout_s=timeout_s, config=config
    )
    effective_hosts = 1
    if config.job_ceiling_check == "split" or config.max_units_per_job is not None:
        effective_hosts = _plan_job_split(job_ceiling, config)
        job_ceiling = {
            **job_ceiling,
            "split": effective_hosts > 1,
            "effective_hosts": effective_hosts,
        }
        if effective_hosts > 1:
            print(
                f"klt sim: notice: splitting {job_ceiling['unit_count']} units "
                f"into {effective_hosts} batch jobs of at most "
                f"{math.ceil(job_ceiling['unit_count'] / effective_hosts)} units "
                "each (request.batch.job_ceiling_check / max_units_per_job)",
                file=sys.stderr,
            )
    else:
        _enforce_job_ceiling(job_ceiling)
    if effective_hosts > 1:
        try:
            corners, engine_version, environment = _run_batch_fleet(
                corner_points=corner_points,
                netlist_path=netlist_path,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                artifacts_dir=artifacts_dir,
                request=request,
                hosts=effective_hosts,
                measurements_spec=measurements_spec,
                request_dir=request_dir,
                config=config,
            )
        except BatchError as exc:
            raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
        if environment is not None:
            environment = {**environment, "job_ceiling": job_ceiling}
        return corners, engine_version, environment
    try:
        corners, engine_version, environment = _run_batch_job(
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
    if environment is not None:
        # Additive (issue #2833): the assessment and the ceiling's provenance
        # ride every one-job batch report, exceeded or not.
        environment = {**environment, "job_ceiling": job_ceiling}
    return corners, engine_version, environment


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
    config: BatchConfig | None = None,
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

    ``config`` is for :func:`_run_batch`'s split reroute (issue #3446), which
    has already resolved it (and run the once-per-request runner check); a
    direct caller leaves it ``None``.
    """
    from .sim import SimError

    if hosts > len(corner_points):
        raise SimError(
            f"remote.hosts ({hosts}) exceeds the number of units to "
            f"dispatch ({len(corner_points)}) -- an idle fleet member would "
            "still be billed; use hosts <= corner/sample count"
        )

    if config is None:
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
            receipt_name=f"batch-receipt-{job_id}.json",
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
    "BATCH_INTERRUPTED_CODE",
    "BATCH_JOB_CEILING_CODE",
    "BATCH_MAX_UNITS_CODE",
    "BATCH_JOB_TOOL",
    "BATCH_CONCURRENCY_CAP_CODE",
    "BATCH_NO_CAPACITY_CODE",
    "BATCH_UNKNOWN_JOB_CODE",
    "BATCH_NETLIST_FILENAME",
    "BATCH_RECEIPT_FILENAME",
    "BATCH_REPORT_FILENAME",
    "BATCH_RUNNER_IDENTITY_FILENAME",
    "BATCH_RUNNER_FEATURE_CODE",
    "BATCH_RUNNER_MISMATCH_CODE",
    "BATCH_RUNNER_UNKNOWN_CODE",
    "BATCH_STDERR_FILENAME",
    "BATCH_REQUEST_FILENAME",
    "BATCH_TERMINAL_STATES",
    "BATCH_JOB_INTERRUPTED_CODE",
    "BATCH_WAITING_STATES",
    "BatchJobInterrupted",
    "BatchConfig",
    "BatchClientInterrupted",
    "BatchError",
    "BatchJobInput",
    "BatchJobSpec",
    "BatchPollTimeout",
    "collect_outputs",
    "job_exists",
    "job_uri",
    "launch_job",
    "new_job_id",
    "poll_status",
    "read_collected_report",
    "submit_job",
]
