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
``$EDA_OUTPUT_DIR/stderr.log`` from its first line -- still forwarding it to
the harness's stderr, so ``harness.log`` is unchanged. A failed job's own
error envelope is recovered from that file (see
:func:`_recover_failed_job_corners`); an EXIT trap writes a fallback envelope
to ``report.json`` when the job died with neither (issue #3818).

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
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, Any

from . import (
    _batch_common,
    _provenance,
    _warn,
    remote_transport,
    sim_batch_image,
    sim_batch_observer,
    sim_batch_pdk,
    sim_batch_receipt,
    sim_staging,
)
from .pdk_families import family_subset, pdk_variant_family
from .remote_launcher import (
    _signal_handlers_install,
    _signal_handlers_restore,
)
from .sim_batch_capacity_watch import _CapacityWatch
from .sim_batch_features import (
    annotate_gap,
    annotate_unrecovered_failure,
    collected_report_env,
    gap_note,
    refusal_diagnostic,
    warn_unverified_features,
)
from .sim_batch_hints import (
    _BATCH_ENGINE_LOG_HEAD_BYTES,
    _find_engine_logs,
    attribute_modelname,
    hint_existing_fatal,
    with_runner_hints,
)
from .sim_batch_log import read_collected_log
from .sim_batch_pdk import _client_pdk_stamp as _client_pdk_stamp
from .sim_batch_pdk import _compare_pdk_stamps as _compare_pdk_stamps
from .sim_batch_reattach import (
    _RESUME_INTERRUPTED_GRACE_S,
    _resume_attach_config,
    _resume_or_submit_one_job,
    _try_reattach_shard,
)
from .sim_batch_transport import (
    _RETRYABLE_LAUNCH_CODES,
    LAUNCH_LIMIT_EXHAUSTED,
    LAUNCH_LIMIT_UNSUPPORTED,
    _classify_launch_failure,
    _run_checked,
    _verify_launch_limit,
)
from .sim_batch_version_remedy import older_client_remedy
from .sim_batch_warn import warn_over_serial_bound_once
from .sim_batch_worker_engine_build import _compare_engine_builds
from .sim_echo_checks import check_runner_option_echoes
from .sim_errors import SimError
from .sim_exit_reason import status_exit_code as _status_exit_code
from .sim_pin_settle import _enforce_published_engine_version_pin
from .sim_remote import (
    _build_remote_request,
    _points_to_push,
    _rewrite_remote_artifact_paths,
    _run_sharded,
    _shard_corner_points,
    _unrun_corner_report,
)

if TYPE_CHECKING:
    from .sim_checkpoint import _Checkpoint
    from .sim_corner_point import CornerPoint

# Keep the contract names that callers import from sim_batch (issue #4065).
from .sim_batch_contract import (  # noqa: F401
    _ATTACH_UNSTARTED_GRACE_S,
    _BATCH_AWS_TIMEOUT_S,
    _BATCH_INPUTS_TIMEOUT_S,
    _BATCH_JOB_OVERHEAD_S,
    _BATCH_LAUNCH_TIMEOUT_S,
    _BATCH_PREFLIGHT_REJECT_EXIT,
    _BATCH_PROVISION_SLACK_S,
    _BATCH_SIM_SUCCESS_EXIT_CODES,
    _BATCH_STDERR_TAIL_BYTES,
    _CAPACITY_BACKOFF_BASE_S,
    _CAPACITY_BACKOFF_CAP_S,
    _POLL_PROGRESS_EVERY,
    BATCH_ARTIFACTS_DIRNAME,
    BATCH_CONCURRENCY_CAP_CODE,
    BATCH_IMAGE_PROFILE_CODE,
    BATCH_INTERRUPTED_CODE,
    BATCH_JOB_CEILING_CODE,
    BATCH_JOB_COMMAND_FAILED_CODE,
    BATCH_JOB_INTERRUPTED_CODE,
    BATCH_JOB_TOOL,
    BATCH_MAX_CONCURRENT_CODE,
    BATCH_MAX_UNITS_CODE,
    BATCH_NETLIST_FILENAME,
    BATCH_NO_CAPACITY_CODE,
    BATCH_RECEIPT_FILENAME,
    BATCH_REPORT_FILENAME,
    BATCH_REQUEST_FILENAME,
    BATCH_RUNNER_FEATURE_CODE,
    BATCH_RUNNER_FEATURE_PREDATED_CODE,
    BATCH_RUNNER_IDENTITY_FILENAME,
    BATCH_RUNNER_LACKS_PLACE_AND_ROUTE_BACKEND_CODE,
    BATCH_RUNNER_MISMATCH_CODE,
    BATCH_RUNNER_UNKNOWN_CODE,
    BATCH_STDERR_FILENAME,
    BATCH_TERMINAL_STATES,
    BATCH_WAITING_STATES,
    BUCKET_ENV,
    DEFAULT_BATCH_JOBS_PREFIX,
    DEFAULT_BATCH_MAX_JOB_SECONDS,
    DEFAULT_BATCH_PROFILE,
    DEFAULT_INTERRUPTED_GRACE_S,
    FLEET_CONFIG_FILENAME,
    FLEET_MAX_JOB_SECONDS_KEY,
    JOB_CEILING_BASIS,
    JOB_CEILING_CHECK_MODES,
    MAX_JOB_SECONDS_ENV,
    PROFILE_ENV,
    PROVISION_SCRIPT_ENV,
    REGION_ENV,
    REQUEST_FEATURE_MIN_RUNNER_VERSION,
    RUNNER_VERSION_CHECK_MODES,
    BatchClientInterrupted,
    BatchError,
    BatchJobInterrupted,
    BatchPollTimeout,
    _batch_job_tool_label,
)
from .sim_batch_contract_jobspec import (
    _build_batch_job_spec,
    _default_batch_job_timeout_s,
    _default_batch_poll_timeout_s,
)
from .sim_batch_contract_preflight import (  # noqa: F401
    _job_stderr_capture_prefix,
    _runner_preflight_script,
    assess_remote_runner_features,
    remote_feature_preflight,
)
from .sim_batch_contract_spec import (  # noqa: F401
    _MANDATORY_RUNNER_FEATURES,
    _RUNNER_REQUEST_FEATURES,
    BatchJobInput,
    BatchJobSpec,
    _missing_runner_features,
    _release_segment,
    _request_features,
    _runner_gate_release,
)
from .sim_batch_contract_worker import (  # noqa: F401
    _WORKER_CAPTURE_PROGRAM,
    BATCH_WORKER_ENVIRONMENT_FILENAME,
    WORKER_CAPTURE_BANNER_MAX,
    WORKER_CAPTURE_FILE_MAX,
    WORKER_CAPTURE_MODEL_INPUTS_MAX,
    WORKER_CAPTURE_PROBE_TIMEOUT_S,
    WORKER_CAPTURE_STR_MAX,
    _assess_deck_support,
    _bounded_model_inputs,
    _bounded_simulator,
    _bounded_str,
    _deck_preflight_lines,
    _runner_supports_decks,
    _runner_supports_par_backend,
    _worker_capture_command,
    assess_par_backend_support,
    par_backend_preflight_lines,
    runner_ngspice_init,
    runner_threads_per_worker,
)
from .sim_batch_job_timeout import (
    BATCH_POLL_KNOB_CODE,
    _resolve_job_ceiling_check,
    _resolve_max_units_per_job,
    assess_caller_job_timeout,
    resolve_job_timeout_s,
    resolve_poll_interval_s,
    resolve_poll_timeout_s,
)
from .sim_batch_knobs import (
    _read_fleet_config,
    _resolve_image_profile,
    _resolve_interrupted_grace_s,
    _resolve_max_concurrent_jobs,
    _resolve_max_job_seconds,
    _resolve_max_launch_attempts,
    _resolve_runner_version_check,
    _validate_batch_request_knobs,
    resolve_capacity_retries,
    resolve_capacity_wait_s,
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
    #: (``request.batch.capacity_wait_s``, issue #2721; ``0`` = one try).
    capacity_wait_s: float = 0.0
    #: Extra passes over capacity-refused shards of a sharded run
    #: (``--capacity-retries``, issue #5567). ``0`` -- the default -- is off.
    capacity_retries: int = 0
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
    #: units of one job; ``None`` leaves the split to ``job_ceiling_check``.
    max_units_per_job: int | None = None
    #: ``request.batch.max_concurrent_jobs`` (issue #3914): opt-in bound on
    #: the shard jobs in flight at once; ``None`` submits all at once.
    max_concurrent_jobs: int | None = None
    #: ``request.batch.job_timeout_s`` (issue #5544): the caller's stated
    #: wall bound for one job; ``None`` -- the default -- keeps the
    #: fully-serial upper bound.
    job_timeout_s: float | None = None
    #: ``request.batch.interrupted_grace_s`` (issue #2992); ``0`` disables.
    interrupted_grace_s: float = DEFAULT_INTERRUPTED_GRACE_S
    #: Pre-launch ``warn`` diagnostic for request features the published image's
    #: runner predates (issue #2901); reported as a top-level diagnostic.
    feature_diagnostic: dict[str, Any] | None = None
    image_pointer_key: str = "images/eda-batch-runner/current.json"
    runner_images: list[dict[str, Any]] | None = None
    image_profile: str | None = None
    max_launch_attempts: int | None = None

    def launch_environment(
        self,
        *,
        include_concurrency: bool = False,
        shards: Sequence[dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Report a shared fleet limit only when every supplied shard confirms it."""
        fields: dict[str, Any] = {}
        if self.image_profile is not None:
            fields["image_profile"] = self.image_profile
        if shards is not None:
            _batch_common.set_shared_launch_limit(fields, shards)
        elif self.max_launch_attempts is not None:
            fields["max_launch_attempts"] = self.max_launch_attempts
        if include_concurrency and self.max_concurrent_jobs is not None:
            fields["max_concurrent_jobs"] = self.max_concurrent_jobs
        return fields

    def runner_identities(self, pdk: str | None) -> list[dict[str, Any]]:
        """Reuse the pointer snapshot across all gates and shards of this request."""
        if self.runner_images is not None:
            return self.runner_images
        command = _aws_argv(
            self,
            "s3api",
            "get-object",
            "--bucket",
            self.bucket,
            "--key",
            self.image_pointer_key,
        )
        return sim_batch_image.read_batch_runner_images(
            pdk, self.region, command, _run_subprocess
        )


def _resolve_provision_script(batch_spec: dict[str, Any]) -> str:
    """``request.batch.provision_script_path`` > ``$KLT_BATCH_PROVISION_SCRIPT``
    > :class:`SimError` naming both -- never a guessed path into a sibling
    checkout (too fragile across hosts/usernames). Raised **before** any S3
    write, mirroring ``_run_remote``'s missing-``ssh_key_path`` check: a
    partial upload must never happen because the launch script turned out
    to be unfindable.
    """
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


#: PDK keys the batch fleet's runner image bakes (issue #2727). Deliberately
#: **not** :data:`remote_launcher.SUPPORTED_PDKS`: the batch fleet runs the EDA
#: batch-runner image published by 2AMLogic/2am, not the ``remote`` AMIs, so
#: the two sets are independent (``remote`` still needs a published AMI per
#: PDK; batch does not). Same key shape as ``SUPPORTED_PDKS``: ``sky130A`` is
#: a real variant, the others are bare families that variants reduce to.
BATCH_PUBLISHED_PDKS: tuple[str, ...] = ("sky130A", "gf180mcu", "sg13g2", "sg13cmos5l")

_BATCH_PDK_FAMILIES: frozenset[str] = family_subset(
    "gf180mcu", "sky130", "sg13g2", "sg13cmos5l"
)


def batch_pdk_key(pdk: str) -> str:
    """Map ``models.pdk`` to the batch-published key, mirroring
    :func:`remote_launcher.ami_pdk_key`: exact key wins, otherwise the variant
    is reduced via :func:`pdk_families.pdk_variant_family` and accepted only
    if the batch image publishes that family. Raises :class:`SimError`."""
    if pdk in BATCH_PUBLISHED_PDKS:
        return pdk
    family = pdk_variant_family(pdk)
    if family in _BATCH_PDK_FAMILIES and family in BATCH_PUBLISHED_PDKS:
        return family
    raise SimError(
        f"unsupported PDK '{pdk}' for the batch backend "
        f"(supported: {', '.join(BATCH_PUBLISHED_PDKS)}). `models.pdk` carries "
        "the local variant name and is reduced to its family (e.g. "
        "'gf180mcuC' -> 'gf180mcu'), so a variant whose family the batch "
        "runner image does not bake is unsupported even when it resolves "
        "locally."
    )


def _validate_batch_pdk(request: dict[str, Any]) -> None:
    """Refuse a ``models.pdk`` the batch fleet has no image for, **before**
    :func:`_resolve_batch_config`'s caller writes anything to S3 (issue
    #2523).

    The accepted set is :data:`BATCH_PUBLISHED_PDKS`, the PDKs the batch
    runner image bakes (issue #2727) -- independent of the ``remote`` AMI
    manifest, which gates only the ``remote`` backend. A variant reducible to
    a published family is accepted (``"gf180mcuC"`` -> ``"gf180mcu"``); the
    *variant* keeps flowing into ``job.json``'s ``pdk_variant`` (see
    :func:`_build_batch_job_spec`), because the job instance resolves the PDK
    locally exactly as the ``local`` backend does.

    A request that names **no** PDK at all is untouched: without a
    ``corners.process`` axis there is no model library to resolve, so there
    is nothing to validate and nothing to refuse.
    """
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        return
    batch_pdk_key(str(pdk))


#: ``SimError.code`` for an absolute host-path model library on the batch
#: backend (issue #3296).
BATCH_MODELS_LIB_HOST_PATH_CODE = "models_lib_host_path"


def _all_lib_refs(request: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(where, ref)`` string model-library ref in ``request``:
    ``models.lib`` and each ``corners.process[].sections[]`` object's ``lib``."""
    refs: list[tuple[str, str]] = []
    models = request.get("models")
    if isinstance(models, dict) and isinstance(models.get("lib"), str):
        refs.append(("models.lib", models["lib"]))
    corners = request.get("corners")
    process = corners.get("process") if isinstance(corners, dict) else None
    for i, entry in enumerate(process if isinstance(process, list) else []):
        sections = entry.get("sections") if isinstance(entry, dict) else None
        for j, section in enumerate(sections if isinstance(sections, list) else []):
            if isinstance(section, dict) and isinstance(section.get("lib"), str):
                refs.append((f"corners.process[{i}].sections[{j}].lib", section["lib"]))
    return refs


def _host_path_lib_refs(request: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(where, ref)`` model-library ref in ``request`` that is an
    absolute host path. A ref that names an environment variable
    (``$PDK_ROOT/...``) is left alone -- the runner expands it against its own
    environment (but see :func:`_unresolved_pdk_var_refs` for ``$PDK``) -- while
    a ``~`` is expanded on *this* host, so ``~/...`` is a host path too."""
    return [
        (where, ref)
        for where, ref in _all_lib_refs(request)
        if "$" not in ref and os.path.isabs(os.path.expanduser(ref))
    ]


#: ``$PDK`` / ``${PDK}`` but not ``$PDK_ROOT`` or ``$PDKS``.
_PDK_VAR_RE = re.compile(r"\$(?:PDK(?![A-Za-z0-9_])|\{PDK\})")

#: ``SimError.code`` for a ``$PDK`` model-library ref the runner cannot expand
#: (issue #3917).
BATCH_MODELS_LIB_PDK_VAR_CODE = "models_lib_pdk_unresolved"


def _unresolved_pdk_var_refs(request: dict[str, Any]) -> list[tuple[str, str]]:
    """Model-library refs naming ``$PDK`` / ``${PDK}`` when the request declares
    no ``models.pdk``: the runner exports ``PDK_ROOT`` always but ``PDK`` only
    from the declared variant, so the reference would reach ngspice unexpanded."""
    models = request.get("models")
    if isinstance(models, dict) and models.get("pdk"):
        return []
    return [(w, r) for w, r in _all_lib_refs(request) if _PDK_VAR_RE.search(r)]


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
    (issue #3296), and an unexpandable ``$PDK`` ref (issue #3917).

    The runner resolves ``models.lib`` on its own box, where this host's
    ``/home/<user>/.volare/...`` does not exist, so the job would pay for an
    instance launch and then fail with ``model library not found``. Not
    refused: PDK-relative libs, ``$VAR`` forms (the runner expands them, except
    ``$PDK`` with no ``models.pdk``), and any request with
    ``options.stage_model_inputs: true`` (the library ships with the job).
    """
    from . import sim_staging

    if sim_staging.stage_model_inputs_requested(request):
        return
    pdk_var_refs = _unresolved_pdk_var_refs(request)
    if pdk_var_refs:
        raise SimError(
            "backend 'batch': the runner does not define $PDK unless the request "
            "declares models.pdk, so the model library would reach ngspice "
            "unexpanded ("
            + "; ".join(f"{where}={ref!r}" for where, ref in pdk_var_refs)
            + "). Declare models.pdk, or write the variant literally "
            '(or use {"pdk": "<variant>", "lib": "<path under the variant dir>"}); '
            "nothing was submitted",
            code=BATCH_MODELS_LIB_PDK_VAR_CODE,
        )
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


#: ``SimError.code`` for a ``models.pdk_root`` that is a directory on the
#: submitting host (issue #4097).
BATCH_MODELS_PDK_ROOT_HOST_PATH_CODE = "models_pdk_root_host_path"


def _validate_batch_pdk_root(request: dict[str, Any]) -> None:
    """Refuse a ``models.pdk_root`` that exists on this host **before** any S3
    write (issue #4097). The value is forwarded verbatim to the runner, where
    a submitter-local path is absent, so the job would launch and die with
    ``no supported-layout PDK install``. A path that is not a directory here
    is assumed runner-valid and passes; ``models.pdk``-only requests are
    unaffected."""
    models = request.get("models")
    root = models.get("pdk_root") if isinstance(models, dict) else None
    if not isinstance(root, str) or not root.strip() or "$" in root:
        return
    if not os.path.isdir(os.path.expanduser(root)):
        return
    raise SimError(
        f"backend 'batch': models.pdk_root {root!r} is a directory on the "
        "submitting host, but it is forwarded to the runner verbatim, where "
        "that path does not exist. Drop models.pdk_root, name the variant with "
        "models.pdk (the runner uses its own install) and pin this client's "
        "install with $PDK_ROOT if needed; nothing was submitted",
        code=BATCH_MODELS_PDK_ROOT_HOST_PATH_CODE,
    )


def _validate_batch_pdk_variant(
    request: dict[str, Any], images: list[dict[str, Any]]
) -> None:
    """Refuse a requested variant absent from every known inventory."""
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
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
        f"image the batch pointer publishes (it carries: {', '.join(carried)}) "
        "-- the request was not submitted and no instance was launched; use "
        "one of those variants as models.pdk"
    )


def _resolve_batch_config(
    request: dict[str, Any],
    *,
    corner_count: int,
    timeout_s: float,
    check_runner: bool = True,
    sim_image_profiles: bool = True,
) -> BatchConfig:
    """Resolve every ``batch.*`` knob, raising
    :class:`~klayout_tools.sim.SimError` before any S3 write. Per-field
    resolution order: ``request.batch.<field>``, then a ``KLT_BATCH_*``
    environment variable, then 2am's own ``batch-fleet.env`` beside the
    provision script, then (profile and jobs prefix only) a documented
    literal default. The **bucket has no literal default** -- an
    unresolvable bucket errors naming all three sources, never a guess.

    ``models.pdk`` is validated here too (:func:`_validate_batch_pdk`),
    first: a PDK the fleet publishes no image for has no compliant execution
    path on this backend at all -- a property of the *request*, reported
    even when the provision script or bucket is also unresolvable.

    Last, the runner ``klt`` the batch image pointer publishes is compared with
    this client (:func:`_assess_published_runner`, issue #2851): under
    ``enforce`` a known mismatch raises ``batch_runner_version_mismatch``.
    A pin excluded by every published engine version also fails before any
    upload or launch (#5851). Read-only callers skip both with ``check_runner=False``.
    """
    _validate_batch_pdk(request)
    _validate_batch_models_lib(request)
    _validate_batch_pdk_root(request)
    batch_spec = request.get("batch") or {}
    # Non-sim batch verbs also use this config resolver.
    max_launch_attempts = (
        _resolve_max_launch_attempts(batch_spec) if sim_image_profiles else None
    )
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
    capacity_wait_s = resolve_capacity_wait_s(batch_spec)
    runner_version_check = _resolve_runner_version_check(batch_spec)
    image_profile = _resolve_image_profile(batch_spec) if sim_image_profiles else None
    max_job_seconds, max_job_seconds_source = _resolve_max_job_seconds(
        batch_spec, fleet, image_profile
    )
    job_ceiling_check = _resolve_job_ceiling_check(batch_spec)
    max_units_per_job = _resolve_max_units_per_job(batch_spec, job_ceiling_check)
    max_concurrent_jobs = _resolve_max_concurrent_jobs(batch_spec)
    job_timeout_s = resolve_job_timeout_s(batch_spec)
    default_poll_timeout_s = _default_batch_poll_timeout_s(
        corner_count, timeout_s, job_timeout_s=job_timeout_s
    )
    config = BatchConfig(
        provision_script=provision_script,
        image_pointer_key=f"{fleet.get('BATCH_IMAGES_PREFIX', 'images').strip('/')}/"
        f"{fleet.get('BATCH_LAUNCH_TEMPLATE', 'eda-batch-runner')}/current.json",
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
        poll_interval_s=resolve_poll_interval_s(batch_spec),
        poll_timeout_s=resolve_poll_timeout_s(batch_spec, default_poll_timeout_s),
        capacity_wait_s=capacity_wait_s,
        max_launch_attempts=max_launch_attempts,
        capacity_retries=resolve_capacity_retries(batch_spec),
        runner_version_check=runner_version_check,
        max_job_seconds=max_job_seconds,
        max_job_seconds_source=max_job_seconds_source,
        image_profile=image_profile,
        job_ceiling_check=job_ceiling_check,
        max_units_per_job=max_units_per_job,
        max_concurrent_jobs=max_concurrent_jobs,
        job_timeout_s=job_timeout_s,
        interrupted_grace_s=_resolve_interrupted_grace_s(batch_spec),
    )
    # Issue #2851: compare the runner klt the batch image pointer publishes with
    # this client, once per request (so once for every shard of a
    # `hosts > 1` run), before any S3 write or launch.
    if check_runner:
        assessment = _assess_published_runner(request, config)
        _enforce_published_runner_version(assessment)
        diagnostic = _enforce_published_runner_features(request, assessment)
        config = replace(
            config, feature_diagnostic=diagnostic, runner_images=assessment["images"]
        )
        _enforce_published_engine_version_pin(request, assessment)
        # Issue #3251: refuse a PDK variant the published image does not ship.
        _validate_batch_pdk_variant(request, assessment["images"])
    return config


def dry_run_runner_check(
    request: dict[str, Any],
    *,
    corner_count: int,
    timeout_s: float,
    runner_out: dict[str, Any] | None = None,
    config_out: dict[str, BatchConfig] | None = None,
) -> dict[str, Any] | None:
    """Run submit preflight for batch dry runs before upload or launch (#4120).

    :func:`_validate_batch_request_knobs` checks request values before a dry run
    can return None for missing host configuration. Polling fields
    ``poll_interval_s`` and ``poll_timeout_s`` are parsed only after the provision
    script and bucket resolve.

    Return a warned or unverified feature diagnostic, otherwise None. runner_out stores
    status, client_klt_version and images for match, unknown and warn-mode mismatch
    (#4675). An enforce mismatch raises BATCH_RUNNER_MISMATCH_CODE first (#4680).
    Feature, engine-pin and job-size refusals propagate. config_out saves the resolved
    configuration so the caller avoids resolving and warning twice (#5674)."""
    from .sim import ENGINE_VERSION_UNMET_CODE

    _validate_batch_request_knobs(request.get("batch") or {})
    try:
        config = _resolve_batch_config(
            request, corner_count=corner_count, timeout_s=timeout_s
        )
    except SimError as exc:
        if getattr(exc, "code", None) in (
            BATCH_RUNNER_FEATURE_CODE,
            BATCH_RUNNER_MISMATCH_CODE,
            BATCH_MAX_UNITS_CODE,
            BATCH_IMAGE_PROFILE_CODE,
            ENGINE_VERSION_UNMET_CODE,
            # Issue #7543: a malformed poll knob is the request author's,
            # never the host's, so the dry run refuses it like the submit.
            BATCH_POLL_KNOB_CODE,
        ):
            raise
        return None
    if runner_out is not None:
        assessment = _assess_published_runner(request, config)
        runner_out.update(
            status=assessment["status"],
            client_klt_version=assessment["client_klt_version"],
            images=assessment["images"],
        )
    if config_out is not None:
        config_out["config"] = config
    return config.feature_diagnostic


def _assess_published_runner(
    request: dict[str, Any], config: BatchConfig
) -> dict[str, Any]:
    """Compare the published sim image's klt major.minor with the client,
    appending the #7857 pinned-client remedy when the client is the older
    side. Unavailable metadata leaves the runner-side preflight
    authoritative."""
    client = _provenance._klt_version()
    assessment: dict[str, Any] = {
        "status": "unknown",
        "check": config.runner_version_check,
        "client_klt_version": client,
        "images": [],
        "message": None,
    }
    pdk = (request.get("models") or {}).get("pdk")
    images = config.runner_identities(str(pdk) if pdk else None)
    assessment["images"] = images
    client_release = _runner_gate_release(client)
    if not images or client_release is None:
        return assessment
    releases = [_runner_gate_release(image["klt_version"] or "") for image in images]
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
        f"the batch image pointer publishes the fleet runner image as {published}, "
        f"but the submitting client is klt {client}; their major.minor "
        "releases differ, so the runner-side version gate would reject the job"
    ) + older_client_remedy(client_release, releases)
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
    _warn.warn(f"klt sim: warning: {message}; submitting anyway")


def _feature_diagnostic(
    missing: list[str], runner: str, client: str | None, source: str
) -> dict[str, Any]:
    """The top-level report diagnostic for request features a runner predates
    (issue #2901): one entry naming every such feature and the runner
    version; ``source`` is ``"manifest"`` (known before launch) or
    ``"runner"`` (read from the job's identity file after the run)."""
    return {
        "code": BATCH_RUNNER_FEATURE_PREDATED_CODE,
        "severity": "warning",
        "message": (
            f"the request uses {', '.join(missing)}, which the fleet runner "
            f"(klt {runner}) predates; the runner may have ignored or "
            "rejected it"
        ),
        "features": list(missing),
        "runner_klt_version": runner,
        "client_klt_version": client,
        "source": source,
    }


def _enforce_published_runner_features(
    request: dict[str, Any], assessment: dict[str, Any]
) -> dict[str, Any] | None:
    """Check all features before upload or launch (#3249). A legacy entry, unknown
    runner version or capable image defers to runner preflight. With no images,
    warn_unverified_features warns for mandatory features (#7062). If all images lack
    features, mandatory gaps always refuse. Other gaps refuse under enforce or warn
    on stderr and return the _feature_diagnostic result under warn (#2901)."""
    features = _request_features(request)
    images = assessment["images"]
    if not images:
        return warn_unverified_features(
            sorted(_MANDATORY_RUNNER_FEATURES.intersection(features)),
            assessment.get("client_klt_version"),
        )
    if not features:
        return None
    gaps = [
        _missing_runner_features(features, image["klt_version"]) for image in images
    ]
    if any(not gap for gap in gaps):  # unknown (None) or complete (empty)
        return None
    missing = sorted({name for gap in gaps for name in gap or ()})
    mandatory = bool(_MANDATORY_RUNNER_FEATURES.intersection(missing))
    versions = ", ".join(sorted({str(image["klt_version"]) for image in images}))
    message = (
        f"the request uses {', '.join(missing)}, which the fleet runner image "
        f"(klt {versions}) predates; the submitting client is klt "
        f"{assessment['client_klt_version']}"
    )
    if mandatory:
        raise SimError(
            f"backend 'batch' refused before submission: {message} -- the "
            "request was not submitted and no instance was launched; a "
            "runner that predates this feature would drop it, so "
            "runner_version_check does not relax this (rebuild the runner "
            "image from a release that has the feature; see docs/cli/sim.md)",
            code=BATCH_RUNNER_FEATURE_CODE,
        )
    if assessment["check"] != "enforce":
        _warn.warn(f"klt sim: warning: {message}; submitting anyway")
        return _feature_diagnostic(
            missing, versions, assessment["client_klt_version"], "manifest"
        )
    raise SimError(
        f"backend 'batch' refused before submission: {message} -- the request "
        "was not submitted and no instance was launched (rebuild the runner "
        "image from a release that has the feature, or set "
        'request.batch.runner_version_check to "warn"; see docs/cli/sim.md)',
        code=BATCH_RUNNER_FEATURE_CODE,
    )


def refuse_hosts_over_units(hosts: int, unit_count: int) -> None:
    """Refuse more hosts than units (#5281): an idle member is still billed."""
    if hosts > unit_count:
        raise SimError(
            f"remote.hosts ({hosts}) exceeds the number of units to dispatch "
            f"({unit_count}) -- an idle fleet member would still be billed; "
            "use hosts <= corner/sample count"
        )


def _plan_job_split(
    assessment: dict[str, Any], config: BatchConfig, hosts: int = 1
) -> int:
    """The effective ``remote.hosts`` for an opted-in split (issue #3446).

    The shard cap is ``config.max_units_per_job`` when given, else the
    largest shard the ceiling allows (``assessment["max_units_per_job"]``),
    and the answer is the fewest balanced shards respecting it and the
    requested host count. Raises before any S3 write with
    :data:`BATCH_JOB_CEILING_CODE` when even one unit exceeds the ceiling
    (no split can help; lower ``options.timeout_s``). An explicit cap whose
    shards exceed the (serial upper-bound) ceiling is honored with a stderr
    warning (issue #4982).
    """
    units = assessment["unit_count"]
    per_job = assessment["max_units_per_job"]
    requested = config.max_units_per_job
    if requested is None and not assessment["exceeds_ceiling"]:
        return hosts
    if per_job == 0:
        raise SimError(
            f"backend 'batch' refused before submission: {assessment['warning']}",
            code=BATCH_JOB_CEILING_CODE,
        )
    cap = requested if requested is not None else per_job
    hosts = max(hosts, math.ceil(units / cap))
    largest = math.ceil(units / hosts)
    if largest > per_job:
        # An explicit cap is the caller's call; the bound is serial (#4982).
        warn_over_serial_bound_once(requested, largest, per_job, assessment)
    return hosts


def _assess_job_ceiling(
    *, unit_count: int, per_unit_timeout_s: float, config: BatchConfig, hosts: int = 1
) -> dict[str, Any]:
    """Deterministic pre-submit runtime-risk assessment of **one** batch job
    against the fleet's whole-job ceiling (issue #2833).

    The bound is :func:`_default_batch_job_timeout_s` -- every unit burning
    its full ``options.timeout_s`` one after another, plus
    :data:`_BATCH_JOB_OVERHEAD_S`: a conservative *upper bound* derived from
    the request alone, never a runtime estimate (``basis`` says so).
    ``hosts`` (the resolved ``remote.hosts``, issue #8011) shards the units
    first, as :func:`sim_remote._shard_corner_points` balances them, so the
    bound describes the largest shard, ``ceil(unit_count / hosts)`` units
    (``job_unit_count``); ``unit_count`` stays the request's total, and a
    bound *equal* to ``config.max_job_seconds`` does not exceed it.

    ``max_units_per_job`` is the largest shard whose own bound stays within
    the ceiling (``0`` when even one unit's exceeds it), ``recommended_hosts``
    the smallest ``remote.hosts`` whose largest shard fits it, else ``None``
    (lower ``options.timeout_s``). An exceeded ceiling adds a ``warning``.

    ``config.job_timeout_s`` (issue #5544) replaces the whole assessment with
    :func:`sim_batch_job_timeout.assess_caller_job_timeout`'s caller-stated
    bound; ``hosts`` changes nothing there.
    """
    if getattr(config, "job_timeout_s", None) is not None:
        return assess_caller_job_timeout(
            unit_count=unit_count,
            per_unit_timeout_s=per_unit_timeout_s,
            config=config,
        )
    units = max(1, unit_count)
    largest = math.ceil(units / max(1, hosts))
    ceiling = config.max_job_seconds
    bound = _default_batch_job_timeout_s(largest, per_unit_timeout_s)
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
        "job_unit_count": largest,
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
            f"x {largest} units + {_BATCH_JOB_OVERHEAD_S:g}s; a conservative "
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
    under ``"enforce"`` an exceeded ceiling raises
    :data:`BATCH_JOB_CEILING_CODE`; under ``"warn"`` the same message is
    printed to stderr (so a caller piping ``--format json`` to a file still
    sees it) and the assessment is reported in
    ``environment.remote.job_ceiling``."""
    warning = assessment.get("warning")
    if warning is None:
        return
    if assessment["check"] == "enforce":
        raise SimError(
            f"backend 'batch' refused before submission: {warning} (or set "
            'request.batch.job_ceiling_check to "warn" to submit anyway)',
            code=BATCH_JOB_CEILING_CODE,
        )
    _warn.warn(f"klt sim: warning: {warning}")


# --------------------------------------------------------------------------- #
# S3 / launch transport (every call through an injectable runner)
# --------------------------------------------------------------------------- #


# One slot per concurrent CLI spawn across every shard thread. A hosts=96 run
# otherwise keeps ~96 ``aws`` processes alive on the submitting host (each poll
# is a fresh CLI start-up), which saturates a small worker. Held only around the
# spawn, never around poll sleeps, so threads queue for a slot instead.
_SPAWN_SLOTS = min(os.cpu_count() or 1, 8)
_SPAWN_SEMAPHORE = threading.BoundedSemaphore(_SPAWN_SLOTS)


def _run_subprocess(
    argv: list[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    with _SPAWN_SEMAPHORE:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)


def job_uri(config: BatchConfig, job_id: str) -> str:
    """``s3://<bucket>/<jobs-prefix>/<job-id>`` -- the one prefix every key
    this module reads or writes lives under."""
    return f"s3://{config.bucket}/{config.jobs_prefix}/{job_id}"


def report_job_uri(config: BatchConfig, job_id: str) -> str:
    """:func:`job_uri` with the bucket elided, for text that lands in a
    committable report (#3777): the bucket name embeds the fleet's AWS
    account number, so it must never reach ``--report-out`` evidence."""
    return job_uri(config, job_id).replace(config.bucket, "<bucket>", 1)


def new_job_id() -> str:
    """A fresh job id. Constrained to ``[A-Za-z0-9._-]+`` because it becomes
    both an S3 prefix and an EC2 tag (``batch-fleet-provision.sh``'s
    ``cmd_launch`` refuses anything else)."""
    return _batch_common.make_job_id("sim")


def _aws_argv(config: BatchConfig, *args: str) -> list[str]:
    argv = [config.aws_binary]
    if config.region:
        argv += ["--region", config.region]
    argv += ["--profile", config.profile]
    return argv + list(args)


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

    **One transfer for the inputs (issue #5539).** They are materialised
    into a local ``inputs/`` tree (:meth:`BatchJobInput.materialize`) and
    sent by one recursive copy, not one process per object: that
    per-object latency and process spawn is what made a deck-list grid
    spend over three hours on 17,000 serial objects. The keys are
    unchanged (``<prefix>/inputs/<name>``, subdirectories included).

    ``progress`` (issue #2992) prints the job id on stderr before uploading;
    a verb whose stderr carries its error envelope (``klt netlist``) passes
    ``False``.
    """
    run = runner if runner is not None else _run_subprocess
    prefix = job_uri(config, job_id)
    if progress:
        _progress(
            f"klt: batch job {job_id} submitting to {report_job_uri(config, job_id)}"
        )
    with tempfile.TemporaryDirectory(prefix="klt-batch-") as staging:
        inputs_root = os.path.join(staging, "inputs")
        for item in spec.inputs:
            try:
                item.materialize(inputs_root)
            except OSError as exc:
                # One error type for every input-upload failure.
                raise BatchError(
                    f"could not stage the {item.label} input for upload: {exc}"
                ) from exc
        if spec.inputs:
            copy = ("s3", "cp", inputs_root, f"{prefix}/inputs", "--recursive")
            _run_checked(
                run,
                _aws_argv(config, *copy, "--only-show-errors"),
                _BATCH_INPUTS_TIMEOUT_S,
                f"aws s3 cp ({len(spec.inputs)} input(s))",
                redact=config.bucket,
            )
        job_json_path = os.path.join(staging, "job.json")
        with open(job_json_path, "w", encoding="utf-8") as handle:
            # Every sim mode and shard publishes its profile through this write.
            if config.image_profile == "simlong":
                spec = replace(spec, image_profile=config.image_profile)
            json.dump(spec.to_job_json(), handle)
        spec_copy = ("s3", "cp", job_json_path, f"{prefix}/job.json")
        _run_checked(
            run,
            _aws_argv(config, *spec_copy, "--only-show-errors"),
            _BATCH_AWS_TIMEOUT_S,
            "aws s3 cp (job.json)",
            redact=config.bucket,
        )


def _ignore_phase(state: str) -> None:
    """The ``on_phase`` default: no shard map to tell."""


def launch_job(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    jitter: Callable[[], float] | None = None,
    on_refusal: Callable[[BatchError, int], None] | None = None,
    on_phase: Callable[[str], None] = _ignore_phase,
) -> None:
    """Shell out to 2am's ``batch-fleet-provision.sh launch --job <id>
    --apply --profile <profile>``. For a capped job the report-only form must
    confirm the uploaded limit first. Capacity refusals can retry below.

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
    for lack of Spot capacity, a vCPU ceiling, or because the fleet is at
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

    ``on_refusal`` (issue #4018) is called with each refusal being waited out
    and the attempt count, just before the backoff sleep -- how a sharded run
    reports "waiting for capacity" across all its shards.
    ``on_phase`` records ``launching`` immediately before each paid command
    and ``launch_refused`` when the fleet confirms a capacity refusal.
    """
    run = runner if runner is not None else _run_subprocess
    sleep = sleep if sleep is not None else time.sleep
    monotonic = monotonic if monotonic is not None else time.monotonic
    jitter = jitter if jitter is not None else random.random
    launch = [config.provision_script, "launch", "--job", job_id]
    options = ["--profile", config.profile]
    if config.region:
        options += ["--region", config.region]
    needs_report = config.max_launch_attempts is not None
    argv = [*launch, "--apply", *options]
    started = monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            if needs_report:
                _verify_launch_limit(config, [*launch, *options], run)
                needs_report = False
            on_phase("launching")
            _run_checked(
                run,
                argv,
                _BATCH_LAUNCH_TIMEOUT_S,
                "batch-fleet-provision.sh launch",
                classify=_classify_launch_failure,
                redact=config.bucket,
            )
            return
        except BatchError as exc:
            if exc.code not in _RETRYABLE_LAUNCH_CODES:
                raise
            on_phase("launch_refused")
            if config.capacity_wait_s <= 0:
                raise
            remaining = config.capacity_wait_s - (monotonic() - started)
            if remaining <= 0:
                raise BatchError(
                    f"{exc} (capacity wait budget of {config.capacity_wait_s:g}s "
                    f"exhausted after {attempts} launch attempt(s))",
                    code=exc.code,
                ) from exc
            if on_refusal is not None:
                on_refusal(exc, attempts)
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


def _remote_state_note(config: BatchConfig, job_id: str) -> str:
    """Issue #4721: a one-shot ``status.json`` read appended to a lost
    shard's reason, so the caller can tell whether the remote job reached a
    terminal state (``finished`` / ``failed`` / ``timeout``: re-attaching can
    collect it) or is still ``running`` / ``unknown`` (status unreadable)."""
    try:
        status = _read_status(config, job_id, _run_subprocess)
    except Exception:  # - a diagnostic probe must never raise
        status = None
    state = status.get("state") if status else None
    label = {"done": "finished"}.get(str(state), str(state)) if state else "unknown"
    return f" [remote job {job_id}: {label}]"


#: Coarse cadence of the stderr heartbeat :func:`poll_status` prints while a
#: job's state is unchanged (issue #2978). A state change prints at once.
BATCH_HEARTBEAT_INTERVAL_S = 300.0

#: ``BatchError.code`` for ``--attach`` to a job id with no ``job.json`` in
#: the bucket (never submitted, wrong bucket/profile, or already expired).
BATCH_UNKNOWN_JOB_CODE = "batch_unknown_job"

#: Filename of the shard map a sharded batch run keeps under its artifacts
#: directory (issue #3300): which job id each shard was submitted as, so a
#: ``--resume`` re-run re-attaches instead of paying for the shard again.
BATCH_SHARDS_FILENAME = "batch-shards.json"

#: ``SimError.code`` for ``--resume`` against a shard map that belongs to a
#: different request (or is unreadable): refused before any S3 write.
BATCH_SHARD_MANIFEST_CODE = "resume_fingerprint_mismatch"


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
    submitted (issue #2978). One ``head-object`` probe shared with
    ``--job-status`` (issue #5613): a 404 is "absent"; any other failure
    (credentials, throttling, timeout) raises :class:`BatchError`."""
    from .sim_batch_async import _job_head  # imports this module at load time

    run = runner if runner is not None else _run_subprocess
    return _job_head(config, job_id, run) is not None


def _submitted_job_config(
    config: BatchConfig,
    job_id: str,
    runner: remote_transport.CommandRunner | None = None,
) -> BatchConfig:
    """Read the launch limit from the job being collected, including legacy jobs."""
    uri = f"{job_uri(config, job_id)}/job.json"
    try:
        result = _run_checked(
            runner if runner is not None else _run_subprocess,
            _aws_argv(config, "s3", "cp", uri, "-"),
            _BATCH_AWS_TIMEOUT_S,
            "aws s3 cp (submitted job.json)",
            redact=config.bucket,
        )
        spec = json.loads(result.stdout)
        if not isinstance(spec, dict):
            raise ValueError("expected a job object")
        limit = _resolve_max_launch_attempts(spec)
    except (BatchError, ValueError, SimError) as exc:
        # This job still exists. A contract read error must not invite a paid resubmit.
        raise SimError(f"cannot read launch limit from job {job_id!r}: {exc}") from exc
    return replace(config, max_launch_attempts=limit)


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


def _format_never_started(job_id: str, elapsed_s: float) -> str:
    """The ``--attach`` diagnostic for a job with no ``status.json`` yet."""
    return (
        f"klt sim: batch job {job_id} was never started: it has a job.json "
        f"but no status.json after {elapsed_s:.0f}s, so no instance has "
        "booted for it. Its launch was probably refused (for example "
        "batch_concurrency_cap while earlier jobs sit pending) or never "
        "issued; still waiting, but it may never run"
    )


def _check_interrupted_grace(
    config: BatchConfig,
    job_id: str,
    interrupted_since: float | None,
    now: float,
) -> None:
    """Raise :class:`BatchJobInterrupted` after ``interrupted_grace_s`` (0 = off)."""
    if (
        interrupted_since is None
        or config.interrupted_grace_s <= 0
        or now - interrupted_since < config.interrupted_grace_s
    ):
        return
    interrupted_s = now - interrupted_since
    raise BatchJobInterrupted(
        f"job {job_id} stayed 'interrupted' (Spot reclaimed) for {interrupted_s:.0f}s, "
        f"past batch.interrupted_grace_s={config.interrupted_grace_s:g}s, with no "
        "re-launch observed; it may still be recoverable on the fleet (check "
        f"{report_job_uri(config, job_id)}/status.json) -- the client does not "
        "re-launch it itself; to recover, re-run the same request/set with the same "
        "-o and --resume (shard map: --attach <batch-shards.json> --collect-partial, "
        "then --resume)",
        interrupted_s=interrupted_s,
    )


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


def _report_if_unstarted(
    grace_s: float | None,
    *,
    reported: bool,
    last_state: str | None,
    status: dict[str, Any] | None,
    job_id: str,
    elapsed: float,
) -> bool:
    """Print the never-started diagnostic once ``grace_s`` has passed with no
    ``status.json`` ever observed (issue #4291); return the updated
    ``reported`` flag. Kept out of :func:`poll_status` for its complexity
    budget."""
    due = (
        grace_s is not None
        and not reported
        and last_state is None
        and status is None
        and elapsed >= grace_s
    )
    if due:
        print(_format_never_started(job_id, elapsed), file=sys.stderr)
    return reported or due


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
    unstarted_grace_s: float | None = None,
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

    ``unstarted_grace_s`` (issue #4291; ``--attach`` sets it) prints one
    stderr diagnostic once that many seconds pass with no ``status.json``
    ever observed (a ``job.json`` but no launch): it only reports -- polling
    continues, a slow boot being indistinguishable from a refused launch.

    Raises :class:`BatchPollTimeout` once ``config.poll_timeout_s`` elapses
    (the caller reports unrun corners rather than a failed sweep), or once
    :data:`_BATCH_PROVISION_SLACK_S` passes with no ``status.json`` ever seen
    (``last_state`` ``None``): a never-launched job must not wait out the
    possibly hours-long serial budget. Raises :class:`BatchError` on any
    state outside the documented set (a contract violation, surfaced loudly)
    and ``batch_job_cancelled`` on a fleet-cancelled job (issue #4351).

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
    unstarted_reported = False
    while True:
        polls += 1
        status = _read_status(config, job_id, run)
        now = monotonic()
        unstarted_reported = _report_if_unstarted(
            unstarted_grace_s,
            reported=unstarted_reported,
            last_state=last_state,
            status=status,
            job_id=job_id,
            elapsed=now - started,
        )
        if status is not None:
            new_state = str(status.get("state") or "")
            if on_state is not None and new_state != last_state:
                on_state(new_state)
            last_state = new_state
            if last_state in BATCH_TERMINAL_STATES:
                from .sim_batch_cancel import terminal_or_cancelled  # deferred

                return terminal_or_cancelled(job_id, status)
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
        redact=config.bucket,
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
            if isinstance(value, str) and marker in value:
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


def _batch_job_environment(config: BatchConfig, job_id: str) -> dict[str, Any]:
    """Submit-side identifiers available before the worker reports a status."""
    return {
        "provider": "aws-batch-fleet",
        "job_id": job_id,
        **config.launch_environment(),
    }


def _batch_environment(
    config: BatchConfig, job_id: str, status: dict[str, Any]
) -> dict[str, Any]:
    """The additive ``environment.remote`` block a batch job reports -- the
    same slot the `remote` backend populates (one block for ``hosts == 1``,
    a ``fleet[]`` array under sharding), carrying what ``status.json``
    actually observed rather than what was requested."""
    return {
        **_batch_job_environment(config, job_id),
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


def _compare_staged_model_inputs(
    client_inputs: Any,
    worker_inputs: Any,
    client_stamp: dict[str, Any] | None,
) -> dict[str, Any]:
    """``pdk_stamp_comparison`` for a run whose models were staged (issue
    #4020): the worker's ``pdk.stamp_*`` then describes the image tree, not
    the executed files, so compare the digests of the files the client shipped
    (``environment.staged_model_inputs``) with the digests the worker hashed
    in ``$EDA_INPUT_DIR`` (``model_inputs``). ``match`` proves those files are
    byte-identical, not that the whole model closure is. ``client_stamp``
    (the submitter's ``SOURCES`` stamp, when readable) is carried so a caller
    can pin the revision."""
    result: dict[str, Any] = {"basis": "staged_model_inputs"}
    if client_stamp is not None:
        result["client_stamp"] = client_stamp
    clients = [
        entry
        for entry in (client_inputs if isinstance(client_inputs, list) else [])
        if isinstance(entry, dict) and entry.get("name") and entry.get("sha256")
    ]
    if not clients:
        return {
            **result,
            "status": "not_comparable",
            "reason": "client_staged_inputs_unavailable",
        }
    if not isinstance(worker_inputs, list):
        return {
            **result,
            "status": "not_comparable",
            "reason": "worker_model_inputs_unavailable",
        }
    if len(clients) > WORKER_CAPTURE_MODEL_INPUTS_MAX:
        return {
            **result,
            "status": "not_comparable",
            "reason": "worker_model_inputs_truncated",
        }
    worker_digests: dict[str, set[str | None]] = {}
    for entry in worker_inputs:
        worker_digests.setdefault(entry.get("name"), set()).add(entry.get("sha256"))
    differing = sorted(
        {
            entry["name"]
            for entry in clients
            if worker_digests.get(entry["name"]) != {entry["sha256"]}
        }
    )
    if not differing:
        return {**result, "status": "match"}
    return {
        **result,
        "status": "mismatch",
        "differing": differing[:WORKER_CAPTURE_MODEL_INPUTS_MAX],
        "warning": (
            "the model files the worker read differ from the ones the submitter "
            f"staged ({', '.join(differing[:5])}"
            f"{', ...' if len(differing) > 5 else ''}); results may differ "
            "from a local run"
        ),
    }


def attach_staged_model_comparison(
    remote_environment: dict[str, Any] | None,
    staged_model_inputs: list[dict[str, Any]] | None,
    request: dict[str, Any] | None,
) -> None:
    """Replace ``pdk_stamp_comparison`` with the staged-model-inputs
    comparison on each ``worker_environment`` block (single job or
    ``fleet[]`` member, each compared independently) whose capture says
    ``pdk.models_staged``, in place (issue #4020). Other blocks keep the
    stamp-based comparison unchanged."""
    if not isinstance(remote_environment, dict):
        return
    entries = [remote_environment]
    fleet = remote_environment.get("fleet")
    if isinstance(fleet, list):
        entries = [entry for entry in fleet if isinstance(entry, dict)]
    client_stamp = _client_pdk_stamp(request)
    for entry in entries:
        worker_env = entry.get("worker_environment")
        if not isinstance(worker_env, dict):
            continue
        pdk = worker_env.get("pdk")
        if isinstance(pdk, dict) and pdk.get("models_staged") is True:
            key = (
                "staged_model_comparison"
                if (worker_env.get("pdk_stamp_comparison") or {}).get("required")
                else "pdk_stamp_comparison"
            )
            worker_env[key] = _compare_staged_model_inputs(
                staged_model_inputs, worker_env.get("model_inputs"), client_stamp
            )


def attach_engine_build_comparison(
    remote_environment: dict[str, Any] | None, client_build: dict[str, Any] | None
) -> None:
    """Add ``engine_build_comparison`` to each ``worker_environment`` block of
    a batch ``environment.remote`` (single job or ``fleet[]``), in place."""
    if not isinstance(remote_environment, dict):
        return
    entries = [remote_environment]
    fleet = remote_environment.get("fleet")
    if isinstance(fleet, list):
        entries = [entry for entry in fleet if isinstance(entry, dict)]
    for entry in entries:
        worker_env = entry.get("worker_environment")
        if isinstance(worker_env, dict):
            worker_env["engine_build_comparison"] = _compare_engine_builds(
                worker_env.get("simulator"), client_build
            )


def _read_worker_environment(
    local_dir: str | None, request: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The ``worker_environment`` block of ``environment.remote`` (issue #2834).

    Worker-observed: it describes what the job instance resolved, never the submitter's
    values. Always present; ``capture`` is ``"ok"``, ``"absent"`` (legacy worker or no
    artifact) or ``"malformed"``, and a non-ok capture carries ``None`` observations.
    Every field is re-validated and bounded here -- the file is untrusted input.
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
        "pdk_stamp_comparison": {
            "status": "not_comparable",
            "reason": "worker_stamp_unavailable",
        },
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
        simulator = _bounded_simulator(doc.get("simulator"), request)
        pdk_in = doc.get("pdk")
        pdk_in = pdk_in if isinstance(pdk_in, dict) else {}
        pdk = {
            "variant": _bounded_str(pdk_in.get("variant")),
            "root": _bounded_str(pdk_in.get("root")),
            "stamp_scheme": _bounded_str(pdk_in.get("stamp_scheme"), 40),
            "stamp_sha256": _bounded_str(
                sim_batch_pdk.observed_stamp(pdk_in.get("stamp_sha256"), request), 64
            ),
            "stamp_text": _bounded_str(pdk_in.get("stamp_text"), 1000),
            "models_staged": pdk_in.get("models_staged") is True,
            # Issue #4020/#7981: ``stamp_*`` always describes the worker
            # image's tree, which staged models or a deck executing a
            # declared input make differ from the model bytes that ran.
            "stamp_scope": sim_batch_pdk.stamp_scope(pdk_in, request),
            **sim_batch_pdk.bounded_store_evidence(pdk_in),  # issue #8007
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
                pdk,
                _client_pdk_stamp(request),
                sim_batch_pdk.pdk_expectation(request),
                ((request or {}).get("models") or {}).get("pdk"),
            ),
        }
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return {**unknown, "capture": "malformed"}


def _runner_feature_report(
    request: dict[str, Any] | None, runner: str | None, step_probe: object = None
) -> dict[str, Any] | None:
    """Summarize requested capabilities from release floors and worker observations.
    Missing evidence yields unknown. Decks use their own version check.
    """
    features = _request_features(request) if request else []
    uses_decks = bool(request and request.get("decks"))
    if uses_decks:
        features.append("decks")
    if not features:
        return None
    registry = [name for name in features if name != "decks"]
    missing = _missing_runner_features(registry, runner, step_probe) if registry else []
    if uses_decks:
        decks_ok = _runner_supports_decks(runner) if runner else None
        if decks_ok is None:
            missing = None
        elif not decks_ok and missing is not None:
            missing = [*missing, "decks"]
    if missing is None:
        status = "unknown"
    else:
        status = "unsupported" if missing else "supported"
    return {"required": features, "missing": missing, "status": status}


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
    docs/cli/sim.md. A non-empty ``decks[]`` (issue #4109) adds ``decks``
    to ``required``, judged by :func:`_runner_supports_decks` since it is
    not a plain min-version entry.
    """
    client = _provenance._klt_version()
    runner: str | None = None
    step_probe = None
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
                step_probe = document.get("step_on_unreached")
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
    report = _runner_feature_report(request, runner, step_probe)
    if report is not None:
        fields["runner_request_features"] = report
        missing = report["missing"]
        if missing:
            fields["request_feature_diagnostic"] = _feature_diagnostic(
                missing, str(runner), client, "runner"
            )
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
    local_dir: str | None,
    request: dict[str, Any] | None = None,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Runner identity plus the worker-observed environment capture, merged
    into ``environment.remote``. Neither reader can raise."""
    worker_environment = _read_worker_environment(local_dir, request)
    sim_batch_pdk.mark_required(worker_environment, request)
    worker_environment["ngspice_init"] = runner_ngspice_init(report)
    worker_environment["threads_per_worker"] = runner_threads_per_worker(report)
    return {
        **_read_runner_identity(local_dir, request),
        "worker_environment": worker_environment,
        # ``None`` (not ``[]``) when the runner did not report it, so "none
        # staged" and "unknown / older runner" stay distinguishable.
        "runner_model_inputs": worker_environment.get("model_inputs"),
    }


#: Lines/chars kept in the failure tail of an engine log.
_BATCH_ENGINE_LOG_TAIL_LINES = 12
_BATCH_ENGINE_LOG_TAIL_CHARS = 1500


def _fatal_log_diagnostic(
    log_text: str, worker_environment: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Classify an ngspice deck abort and record whether analysis started.
    For a modelname abort, report the PDK stamp captured by the job's worker.
    Return None if the log contains no classified fatal diagnostic."""
    from .sim import (
        _NGSPICE_FATAL_CODES,
        _classify_diagnostics,
        _mark_analysis_not_started,
    )

    for diagnostic in _classify_diagnostics(log_text):
        if diagnostic.get("code") in _NGSPICE_FATAL_CODES:
            _mark_analysis_not_started([diagnostic], log_text)
            return with_runner_hints(
                attribute_modelname(diagnostic, log_text, worker_environment)
            )
    return None


def _surface_log_fatal(
    corners: list[dict[str, Any]],
    collected: str,
    worker_environment: dict[str, Any] | None = None,
) -> None:
    """Add the ngspice deck-abort diagnostic (``missing_include``,
    ``netlist``, ``model_bin_range``, ``ngspice_fatal``) to every errored
    corner whose retrieved engine log shows ngspice failing while reading the
    deck but whose report (written by the runner's own, possibly older,
    ``klt``) carries no such code -- otherwise the caller sees only
    ``measurement ... produced no value``. Corners are matched to logs through
    ``artifacts.log`` (``<$EDA_OUTPUT_DIR>/artifacts/<rel>``); with exactly one
    log in the tree it applies to every such corner. A no-op when no log was
    collected. ``worker_environment`` (issue #5593) feeds the modelname
    attribution of :func:`_fatal_log_diagnostic`."""
    logs = _find_engine_logs(collected)
    if not logs:
        return
    marker = f"/{BATCH_ARTIFACTS_DIRNAME}/"
    for corner in corners:
        if corner.get("status") != "error":
            continue
        diagnostics = corner.setdefault("diagnostics", [])
        if hint_existing_fatal(diagnostics):
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
        _, evidence = read_collected_log(log_path, _BATCH_ENGINE_LOG_HEAD_BYTES)
        found = _fatal_log_diagnostic(evidence, worker_environment)
        if found is not None:
            diagnostics.append(found)


def _engine_log_note(
    collected: str, worker_environment: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Return a readable engine-log tail and its worker-attributed fatal cause.
    A job without a collected log gets an explanation and no diagnostic."""
    logs = _find_engine_logs(collected)
    if not logs:
        return (
            "engine log unavailable: no ngspice.log/xyce.log in the collected "
            "outputs (engine logs are retrieved only when artifacts are kept)",
            None,
        )
    text, evidence = read_collected_log(logs[0], _BATCH_ENGINE_LOG_HEAD_BYTES)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-_BATCH_ENGINE_LOG_TAIL_LINES:])
    tail = tail[-_BATCH_ENGINE_LOG_TAIL_CHARS:]
    diagnostic = _fatal_log_diagnostic(evidence, worker_environment)
    return f"{os.path.basename(logs[0])} tail: {tail}", diagnostic


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
            report = None
            try:
                report = read_collected_report(collected)
                kind, runner_error = _classify_collected_report(report)
                if kind == "report":
                    note = "collected report is not a klt error envelope"
                elif kind == "unusable":
                    note = "collected report has an unrecognized shape"
            except BatchError as exc:
                note = str(exc)
            identity = _read_job_identity(collected, request, report=report)
            log_note, log_diagnostic = _engine_log_note(
                collected, identity.get("worker_environment")
            )
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
    if runner_error is None:
        _annotate_unrecovered_preflight_reject(corners, status, identity)
    if (runner_error or {}).get("code") == BATCH_RUNNER_MISMATCH_CODE or (
        runner_error is None
        and _status_exit_code(status) == _BATCH_PREFLIGHT_REJECT_EXIT
    ):
        d = refusal_diagnostic(
            identity, _runner_gate_release, BATCH_RUNNER_MISMATCH_CODE
        )
        identity = {**identity, "runner_version_diagnostic": d} if d else identity
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
                annotate_gap(diagnostic, identity, versions=True)


def _annotate_unrecovered_preflight_reject(
    corners: list[dict[str, Any]],
    status: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    """Issue #4161: a job that exited :data:`_BATCH_PREFLIGHT_REJECT_EXIT` but whose
    klt error envelope could not be recovered (no ``report.json``, no ``stderr.log``)
    would otherwise read as the bare ``job command exited 87``. Say what the exit means
    and, when the post-run runner identity shows a major.minor mismatch, classify the
    failure as a version mismatch (``runner_code`` :data:`BATCH_RUNNER_MISMATCH_CODE`
    plus both klt identities). Any other non-zero exit code gets the generic
    :func:`.sim_batch_features.annotate_unrecovered_failure` wording (issue #4217)."""
    exit_code = _status_exit_code(status)
    if exit_code is None or exit_code == 0:
        return
    if exit_code != _BATCH_PREFLIGHT_REJECT_EXIT:
        annotate_unrecovered_failure(corners, exit_code)
        return
    note = (
        f"exit {_BATCH_PREFLIGHT_REJECT_EXIT} is the runner preflight reject "
        "(the fleet runner's klt version/feature gate refused the request "
        "before klt sim ran); the runner's error envelope was not recovered"
    )
    mismatch = identity.get("runner_compatibility") == "mismatch"
    if mismatch:
        note += (
            f"; the runner ran klt {identity.get('runner_klt_version')} but the "
            f"client is {identity.get('client_klt_version')} -- update the "
            "runner image, use a compatible client, or set "
            'request.batch.runner_version_check to "warn" to submit anyway'
        )
    note += gap_note(identity)
    for corner in corners:
        for diagnostic in corner.get("diagnostics") or []:
            if diagnostic.get("code") != "batch_job_failed":
                continue
            diagnostic["message"] = f"{diagnostic['message']} [{note}]"
            annotate_gap(diagnostic, identity, versions=mismatch)
            if mismatch:
                diagnostic["runner_code"] = BATCH_RUNNER_MISMATCH_CODE


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
        job_timeout_s=config.job_timeout_s,
        max_launch_attempts=config.max_launch_attempts,
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
        _warn.warn(f"klt sim: warning: could not write batch receipt {path}: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _manifest_difference(stored_fingerprint: Any, fingerprint: str) -> str:
    """What a shard map refusal compared: the fingerprint only (issue #6197;
    the ``hosts`` arm was dead by construction -- #4821 made ``hosts`` not
    identity, resume re-splits under the new count -- and it is one opaque
    hash of the units, seed, netlist and settings, so it can only be reported
    as differing, with prefixes)."""
    return (
        f"fingerprint: map {str(stored_fingerprint)[:12]}, request "
        f"{fingerprint[:12]} -- one hash of units, seed, netlist and "
        "settings, so which of those changed is not recoverable"
    )


class _ShardManifest:
    """The persisted shard-to-job-id map of a sharded batch run (issues #3300,
    #3896).

    ``<artifacts_dir>/batch-shards.json`` records the resume fingerprint from
    :func:`sim_checkpoint._checkpoint_fingerprint`, shard count, and job location
    (``jobs_prefix``, ``region``, ``profile``; the bucket is omitted, issue #4079).
    Each shard entry records its index, submitted job ID, a digest of the corner IDs it
    carries, and its client-side lifecycle -- ``state`` is ``pending`` (id
    minted, not yet submitted), ``submitted``, ``launching`` (``launch_job``
    started, outcome unknown), ``launched`` or ``collected``, with the
    matching ``submitted_at`` / ``launch_started_at`` / ``launched_at`` /
    ``collected_at`` (``null`` until reached). An entry is written *before*
    the shard's ``submit_job``, so a client killed between minting the id and
    submitting still leaves a mapping (the re-attach then finds no
    ``job.json`` and falls back to a fresh submit). Writes are atomic
    (temp + rename) and serialized: shard threads record concurrently.
    """

    def __init__(
        self,
        path: str,
        fingerprint: str,
        hosts: int,
        prior: dict[str, dict[str, Any]] | None = None,
        location: dict[str, Any] | None = None,
    ) -> None:
        self._path = path
        self._fingerprint = fingerprint
        self._hosts = hosts
        self._location = location or {}
        self._lock = threading.Lock()
        self.prior: dict[str, dict[str, Any]] = prior or {}
        self._entries: dict[str, dict[str, Any]] = {}

    @classmethod
    def open(
        cls,
        artifacts_dir: str,
        fingerprint: str,
        hosts: int,
        *,
        resume: bool,
        location: dict[str, Any] | None = None,
        submit_only: bool = False,
    ) -> _ShardManifest:
        """Open the manifest for this run. Without ``resume`` any existing
        file is ignored (and overwritten by the first write). With ``resume``
        a missing file means nothing to re-attach to; an unreadable one, or
        one written for a different request, raises :class:`SimError` --
        never a silent restart that would re-bill every shard (the shard
        count is not compared, issue #4821: resume re-splits under the new
        count)."""
        path = os.path.join(artifacts_dir, BATCH_SHARDS_FILENAME)
        manifest = cls(path, fingerprint, hosts, location=location)
        if not resume or not os.path.isfile(path):
            return manifest
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            shards = {str(entry["index"]): dict(entry) for entry in data["shards"]}
            stored_fingerprint = data["fingerprint"]
            str(data["hosts"])
            if data.get("schema_version") != 1:
                raise ValueError("unsupported schema_version")
            for entry in shards.values():
                str(entry["job_id"])
                str(entry["units_sha256"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SimError(
                f"--resume: shard map {path} is unreadable ({exc}); delete it "
                "or drop --resume (resuming would otherwise re-submit shards "
                "that may already have finished)",
                code=BATCH_SHARD_MANIFEST_CODE,
            ) from exc
        # Issue #4821: ``hosts`` is not identity; resume re-splits remaining units.
        if stored_fingerprint != fingerprint:
            from . import sim_batch_shards

            raise sim_batch_shards.mismatch_error(path, data, fingerprint, submit_only)
        manifest.prior = shards
        return manifest

    @property
    def path(self) -> str:
        return self._path

    def record(self, index: int, job_id: str, units_sha256: str, **extra: Any) -> None:
        with self._lock:
            self._entries[str(index)] = {
                "index": index,
                "job_id": job_id,
                "units_sha256": units_sha256,
                "state": "pending",
                "submitted_at": None,
                "launch_started_at": None,
                "launched_at": None,
                "collected_at": None,
                **{k: v for k, v in extra.items() if v is not None},
            }
            self._flush_locked()

    def planned_job_id(self, index: int) -> str | None:
        """The job id :meth:`record` minted for ``index`` this run, if any."""
        with self._lock:
            entry = self._entries.get(str(index))
            return None if entry is None else str(entry["job_id"])

    #: lifecycle state -> the timestamp field it stamps.
    _STATE_FIELDS = {
        "submitted": "submitted_at",
        "launching": "launch_started_at",
        "launched": "launched_at",
        "collected": "collected_at",
    }

    def mark(self, index: int, state: str) -> None:
        """Advance shard ``index`` to ``state`` and stamp its timestamp. A
        field already stamped is preserved. A capacity refusal returns an
        attempted launch to submitted so it can safely be retried."""
        key = str(index)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                prior = self.prior.get(key)
                if prior is None:
                    return
                entry = self._entries[key] = dict(prior)
            order = list(self._STATE_FIELDS)
            current = entry.get("state")
            if state == "launch_refused":
                if current == "launching":
                    entry["state"] = "submitted"
                    self._flush_locked()
                return
            if current in order and order.index(current) >= order.index(state):
                return
            entry["state"] = state
            field = self._STATE_FIELDS[state]
            if entry.get(field) is None:
                entry[field] = datetime.now(timezone.utc).isoformat()
            self._flush_locked()

    def unlaunched(self) -> int:
        """Shards this run left submitted but never launched."""
        with self._lock:
            entries = {**self.prior, **self._entries}
            return sum(1 for e in entries.values() if e.get("state") == "submitted")

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        # Entries of shards this run has not reached yet stay as the prior
        # run left them, so a second interruption keeps their mapping.
        entries = {**self.prior, **self._entries}
        document = {
            "schema_version": 1,
            "fingerprint": self._fingerprint,
            "hosts": self._hosts,
            **self._location,
            "shards": [entries[key] for key in sorted(entries, key=int)],
        }
        _write_batch_receipt(self._path, document)


def _remove_shard_manifest(artifacts_dir: str) -> None:
    """Drop a finished sweep's shard map -- nothing is left to re-attach to."""
    try:
        os.remove(os.path.join(artifacts_dir, BATCH_SHARDS_FILENAME))
    except OSError:
        pass


def _units_digest(points: Sequence[CornerPoint]) -> str:
    """Digest of a shard's ordered corner ids: a manifest entry only applies
    to a re-run that hands the shard the same units."""
    blob = json.dumps([point.corner_id for point in points]).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _poll_status_interruptibly(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None,
    sleep: Any,
    heartbeat: bool = False,
    progress: bool = True,
    on_state: Callable[[str], None] | None = None,
    unstarted_grace_s: float | None = None,
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
            unstarted_grace_s=unstarted_grace_s,
        )

    def _handle(signum: int, _frame: Any) -> None:
        raise BatchClientInterrupted(
            f"client terminated by {signal.Signals(signum).name} while waiting "
            f"on job {job_id} (region {config.region}); "
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
            unstarted_grace_s=unstarted_grace_s,
        )
    except BatchClientInterrupted as exc:
        print(f"klt sim: {exc}", file=sys.stderr)
        raise
    finally:
        _signal_handlers_restore(prev_handlers)


def _launch_attached_if_unlaunched(
    config: BatchConfig,
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None,
    now: Callable[[], str],
    notify: Callable[[str], None],
    launch_kwargs: dict[str, Any] | None = None,
) -> str | None:
    """Launch a re-attached job the shard map says was never launched (issue
    #3896), but only while the fleet has published no ``status.json`` -- one
    that exists means a launch already happened, and a second would
    double-bill. Returns the launch timestamp, or ``None`` when no launch was
    needed."""
    run = runner if runner is not None else _run_subprocess
    if _read_status(config, job_id, run) is not None:
        notify("launched")
        return None
    launch_job(config, job_id, runner=runner, on_phase=notify, **(launch_kwargs or {}))
    launched_at = now()
    notify("launched")
    sys.stderr.write(
        f"klt sim: batch job {job_id} had been submitted but never "
        "launched; launched it\n"
    )
    sys.stderr.flush()
    return launched_at


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
    netlist_sha256: str | None = None,
    on_phase: Callable[[str], None] | None = None,
    launch_if_unlaunched: bool = False,
    launch_kwargs: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """Submit and collect one corner job, either standalone or part of a fleet run.

    Each shard supplies ``explicit_points`` (including derived Monte Carlo
    seeds). A single job forwards the original corner matrix. Fleet failures
    and poll timeouts yield unrun corner reports; transport errors raise
    :class:`BatchError`.

    Attach polls an existing job with a job.json (#2978). It launches only
    when the shard map records an unlaunched job and status.json is absent
    (#3896). Spot-reclaimed jobs keep polling. The phase hook records submit,
    launch and capacity refusal (#4018); launch_kwargs is passed to launch_job.

    The returned environment also carries client-observed wall-clock
    timestamps (``client_submitted_at``, ``client_launched_at``,
    ``client_status_terminal_at``, ``client_collected_at``; ``now`` is the
    clock seam). A phase that was not reached is ``None``, never fabricated.
    """
    notify = on_phase if on_phase is not None else _ignore_phase
    if attach:
        if not job_exists(config, job_id, runner=runner):
            raise BatchError(
                f"no batch job {job_id!r} in the configured bucket under "
                f"prefix {config.jobs_prefix!r} (never submitted, wrong "
                "bucket/profile/region, or expired)",
                code=BATCH_UNKNOWN_JOB_CODE,
            )
        submitted = _submitted_job_config(config, job_id, runner)
        attach_timestamps: dict[str, str | None] = {
            "client_submitted_at": None,
            "client_launched_at": None,
            "client_status_terminal_at": None,
            "client_collected_at": None,
        }
        if launch_if_unlaunched:
            # A paid relaunch must still verify the caller's requested ceiling,
            # even when the existing job was uploaded by an uncapped legacy client.
            attach_timestamps["client_launched_at"] = _launch_attached_if_unlaunched(
                config if config.max_launch_attempts is not None else submitted,
                job_id,
                runner=runner,
                now=now,
                notify=notify,
                launch_kwargs=launch_kwargs,
            )
        return _poll_and_collect(
            config=submitted,
            job_id=job_id,
            corner_points=corner_points,
            keep_artifacts=keep_artifacts,
            artifacts_dir=artifacts_dir,
            request=request,
            measurements_spec=measurements_spec,
            runner=runner,
            sleep=sleep,
            expected_netlist=lambda: sim_batch_receipt.attached_netlist_sha256(
                request, netlist_path
            ),
            timestamps=attach_timestamps,
            now=now,
            attach=True,
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
    staged_digest = sim_batch_receipt.submitted_netlist_sha256(spec, netlist_sha256)
    submit_job(config, job_id, spec, runner=runner)
    notify("submitted")
    sim_batch_observer.report_submitted(job_id)
    launch_job(config, job_id, runner=runner, on_phase=notify, **(launch_kwargs or {}))
    timestamps["client_launched_at"] = now()
    notify("launched")
    # Emit the job id before polling so an interrupted client can attach.
    sys.stderr.write(
        f"klt sim: batch job {job_id} launched; "
        f"re-attach with `klt sim <request> --backend batch --attach {job_id}`\n"
    )
    sys.stderr.flush()
    receipt_path = os.path.join(
        artifacts_dir,
        BATCH_RECEIPT_FILENAME if receipt_name is None else receipt_name,
    )
    receipt = sim_batch_receipt.new_receipt(
        config, job_id, request, corner_points, netlist_sha256
    )
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
        expected_netlist=lambda: staged_digest,
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
    expected_netlist: Callable[[], str],
    now: Callable[[], str],
    receipt_path: str | None = None,
    receipt: dict[str, Any] | None = None,
    attach: bool = False,
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
            on_state=sim_batch_observer.state_hook(job_id),
            unstarted_grace_s=_ATTACH_UNSTARTED_GRACE_S if attach else None,
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
                **_batch_job_environment(config, job_id),
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
        environment["client_collected_at"] = now()
        report = read_collected_report(collected)
        check_runner_option_echoes(request, report)
        sim_batch_receipt.check_collected_netlist(job_id, report, expected_netlist)
        identity = _read_job_identity(collected, request, report=report)
        environment = {**environment, **identity}
        environment.update(collected_report_env(status, report))
        corners = report.get("corners") or []
        # Issue #5593: the surfaced modelname abort names the fleet's own
        # PDK stamp from this job's worker capture, not the submitter's.
        _surface_log_fatal(corners, collected, identity.get("worker_environment"))
        if keep_artifacts:
            _install_batch_artifacts(
                corners, collected_dir=collected, artifacts_dir=artifacts_dir
            )
    worker = identity["worker_environment"]
    comparison = worker["pdk_stamp_comparison"]
    if comparison.get("required") and comparison["status"] != "match":
        code = (
            "pdk_stamp_mismatch"
            if comparison["status"] == "mismatch"
            else "pdk_stamp_not_comparable"
        )
        message = sim_batch_pdk.failure_message(worker, _client_pdk_stamp(request))
        corners = [
            _unrun_corner_report(point, measurements_spec, code, message=message)
            for point in corner_points
        ]
    engine_version = (report.get("environment") or {}).get("engine_version")
    return corners, engine_version, environment


# --------------------------------------------------------------------------- #
# Backend entry points
# --------------------------------------------------------------------------- #


def _apply_job_split(
    job_ceiling: dict[str, Any], config: BatchConfig, hosts: int = 1
) -> tuple[dict[str, Any], int]:
    """Apply the split and ceiling policy and return the assessed job count."""
    if config.job_ceiling_check == "split" or config.max_units_per_job is not None:
        hosts = _plan_job_split(job_ceiling, config, hosts)
        if hosts > 1:
            print(
                f"klt sim: notice: splitting {job_ceiling['unit_count']} units "
                f"into {hosts} batch jobs of at most "
                f"{math.ceil(job_ceiling['unit_count'] / hosts)} units "
                "each (request.batch.job_ceiling_check / max_units_per_job)",
                file=sys.stderr,
            )
    else:
        _enforce_job_ceiling(job_ceiling)
        if hosts == 1:
            return job_ceiling, hosts
    return {**job_ceiling, "split": hosts > 1, "effective_hosts": hosts}, hosts


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
    collect_partial: bool = False,
    shard_fingerprint: str | Callable[[], str] | None = None,
    netlist_sha256: str | None = None,
    explicit_points: list[CornerPoint] | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``batch`` backend: submit the whole corner matrix to 2am's EDA
    batch fleet as one job and collect its report back out of S3.

    The pinned worker runs ``klt sim ... --backend local-parallel --format json``.
    Unlike ``remote``, this process provisions nothing, holds no SSH key,
    and needs no ``RunInstances`` authority: it writes an S3 job contract
    and asks 2am's launch script to acquire diversified Spot capacity for
    it.

    ``analysis``/``measurements_spec``/``models_lib``/``max_workers`` are
    accepted for signature parity with the other backends
    (``measurements_spec`` is additionally *used*, to shape an unrun-corner
    report when a job fails); the rest are already embedded in ``request``
    or resolved by the job instance itself, exactly as for ``remote``.

    ``deadline``/``initial_ppid``/``probe_abort`` are
    likewise accepted but not honored, for the same reason ``_run_remote``
    does not honor them (``checkpoint`` only supplies the resume fingerprint
    for a split reroute's shard map, issue #3300): the corner dispatch loop
    runs in a *different* process on a machine this function only observes
    through ``status.json`` (``run_sim`` records the returned reports into
    the resume checkpoint after dispatch, skipping ``lost_shard`` corners --
    issue #3367). The batch path's own wall-clock
    bound is ``request.batch.poll_timeout_s``, whose overrun reports every
    unit through the same unrun-corner shape.

    Before submitting, the job's conservative bound is checked against the
    fleet's whole-job ceiling (:func:`_assess_job_ceiling`, issue #2833):
    ``"enforce"`` refuses an overlong job, the default ``"warn"`` submits
    with a stderr warning, and either way the assessment is reported as
    ``environment.remote.job_ceiling``. The ``hosts > 1`` path
    (:func:`_run_batch_fleet`) is unchanged.

    ``shard_fingerprint`` (issue #3896) is the request's resume fingerprint for
    a split reroute that has no checkpoint (a run without ``--resume``): it
    makes the split write its shard map anyway, so a stopped client leaves
    a record of which jobs were submitted and which launched.

    ``attach_job_id`` (issue #2978) re-attaches to that already-submitted job
    instead of submitting a new one: no job-ceiling assessment, no upload, no
    launch. The request supplies the corner list and measurement grading, so
    it must be the request the job was submitted from.
    """
    from . import sim_batch_shards as shards

    del analysis, models_lib, max_workers  # see docstring
    del deadline, initial_ppid, probe_abort  # not honored

    config = _resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )
    if attach_job_id is not None and attach_job_id.endswith(".json"):
        return shards.attach_batch_shards(
            manifest_path=attach_job_id,
            config=config,
            corner_points=corner_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            request=request,
            measurements_spec=measurements_spec,
            request_dir=request_dir,
            shard_fingerprint=shard_fingerprint,
            collect_partial=collect_partial,
        )
    if attach_job_id is not None:
        sim_batch_receipt.check_attach_receipt(
            artifacts_dir, attach_job_id, request, netlist_sha256
        )
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
    # Issue #2833: assess the job against the fleet's whole-job ceiling before
    # `submit_job`'s first S3 write (an "enforce" refusal leaves no partial contract).
    from .sim_batch_plan import plan_batch_job

    job_ceiling, effective_hosts = plan_batch_job(
        config, unit_count=len(corner_points), timeout_s=timeout_s
    )
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
                shard_fingerprint=shards.resolve_fingerprint(shard_fingerprint)
                or (None if checkpoint is None else checkpoint.fingerprint),
                resume=checkpoint is not None,
            )
        except BatchError as exc:
            raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
        if environment is not None:
            environment = {
                **environment,
                "job_ceiling": job_ceiling,
                "image_profile": config.image_profile or "sim",
                **_feature_diagnostic_field(config),
            }
        return corners, engine_version, environment
    try:
        corners, engine_version, environment = _resume_or_submit_one_job(
            config=config,
            resume_grace_s=_RESUME_INTERRUPTED_GRACE_S,
            netlist_sha256=netlist_sha256,
            resume_job_id=sim_batch_receipt.resumable_job_id(
                artifacts_dir,
                request,
                len(corner_points),
                checkpoint is not None,
                netlist_sha256,
                units_sha256=sim_batch_receipt.units_digest(corner_points),
                require_units_digest=explicit_points is not None,
            ),
            corner_points=corner_points,
            explicit_points=explicit_points,
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
        environment = {
            **environment,
            "job_ceiling": job_ceiling,
            "image_profile": config.image_profile or "sim",
            **_feature_diagnostic_field(config),
        }
    return corners, engine_version, environment


def _feature_diagnostic_field(config: BatchConfig) -> dict[str, Any]:
    """``environment.remote.request_feature_diagnostic`` for the pre-launch
    ``warn`` result (issue #2901); empty when there is none, so a post-run one
    from :func:`_read_runner_identity` is left alone. ``run_sim`` hoists it to
    the report's top-level ``diagnostics[]``."""
    if config.feature_diagnostic is None:
        return {}
    return {"request_feature_diagnostic": config.feature_diagnostic}


def _open_shard_manifest(
    artifacts_dir: str,
    shard_fingerprint: str | None,
    hosts: int,
    *,
    resume: bool,
    config: BatchConfig,
    request_identity: dict[str, Any] | None = None,
    submit_only: bool = False,
) -> _ShardManifest | None:
    """Open the run's shard map, recording where the jobs live (issue #3896);
    ``None`` when the request has no fingerprint to key it by."""
    if shard_fingerprint is None:
        return None
    return _ShardManifest.open(
        artifacts_dir,
        shard_fingerprint,
        hosts,
        resume=resume,
        submit_only=submit_only,
        location={
            "jobs_prefix": getattr(config, "jobs_prefix", None),
            "region": getattr(config, "region", None),
            "profile": getattr(config, "profile", None),
            # Only a sharded --submit-only records it (issue #4620).
            **(request_identity or {}),
        },
    )


def _prerecord_shards(
    manifest: _ShardManifest | None,
    shards: Sequence[Sequence[CornerPoint]],
) -> None:
    """Pre-record every non-empty shard's entry and ``units`` ids (#3896/#5775)."""
    if manifest is None:
        return
    for index, points in enumerate(shards):
        if not points:
            continue
        digest = _units_digest(points)
        prior = manifest.prior.get(str(index))
        if prior is None or prior["units_sha256"] != digest:
            manifest.record(
                index, new_job_id(), digest, units=[p.corner_id for p in points]
            )


def _warn_unlaunched(manifest: _ShardManifest | None, hosts: int) -> None:
    """Tell the caller about shards left submitted but never launched, and how
    to launch them (issue #3896)."""
    if manifest is None or not manifest.unlaunched():
        return
    print(
        f"klt sim: {manifest.unlaunched()} of {hosts} batch job(s) were "
        f"submitted but never launched; rerun with --resume and the "
        f"same outdir to launch them (shard map: {manifest.path})",
        file=sys.stderr,
    )


def _abortable_sleep(abort: threading.Event) -> Callable[[float], None]:
    """A ``sleep`` that raises :class:`BatchClientInterrupted` once ``abort``
    is set, in slices of at most one second, so a shard thread left behind by
    an absorbed interrupt exits promptly instead of holding the process open
    for its whole poll interval (issue #4018)."""

    def _sleep(seconds: float) -> None:
        remaining = seconds
        while True:
            if abort.is_set():
                raise BatchClientInterrupted(
                    "client interrupted; shard abandoned (its job, if "
                    "launched, keeps running -- the fleet has no cancel)",
                    job_id="",
                    signum=signal.SIGINT,
                )
            if remaining <= 0:
                return
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    return _sleep


_ShardResult = tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]


def _raise_client_interrupted(signum: int, _frame: Any) -> None:
    raise BatchClientInterrupted(
        f"client terminated by {signal.Signals(signum).name}",
        job_id="",
        signum=signum,
    )


def _require_shard_environment(result: _ShardResult) -> _ShardResult:
    if result[2] is None:
        raise BatchError("batch shard returned no remote environment")
    return result


class _BatchFleetRun:
    """The per-shard half of :func:`_run_batch_fleet`: runs one shard's job
    (submit, or re-attach under ``--resume``) and carries the run-wide state
    the shards share -- the capacity-wait deadline, the abort flag, and the
    "waiting for capacity" progress line.

    Issue #4018: ``capacity_wait_s`` bounds the whole sharded run -- the fleet
    caps are shared, so every shard competes for the same slots -- not each
    shard's launch separately.
    """

    def __init__(
        self,
        *,
        config: BatchConfig,
        manifest: _ShardManifest | None,
        shards: Sequence[Sequence[CornerPoint]],
        netlist_path: str,
        timeout_s: float,
        keep_artifacts: bool,
        want_waveforms: bool,
        artifacts_dir: str,
        request: dict[str, Any],
        measurements_spec: list[dict[str, Any]],
        request_dir: str | None,
        attach_only: bool = False,
        collect_partial: bool = False,
    ) -> None:
        self.attach_only = attach_only
        self.collect_partial = collect_partial
        self.unfinished: dict[int, dict[str, Any]] = {}
        self.job_observer = sim_batch_observer.current_job_observer()
        self.config = config
        self.manifest = manifest
        self.shard_indexes = {_units_digest(p): i for i, p in enumerate(shards) if p}
        self.netlist_path = netlist_path
        self.timeout_s = timeout_s
        self.keep_artifacts = keep_artifacts
        self.want_waveforms = want_waveforms
        self.artifacts_dir = artifacts_dir
        self.request = request
        self.measurements_spec = measurements_spec
        self.request_dir = request_dir
        self.started = time.monotonic()
        self.abort = threading.Event()
        self.budget_spent = threading.Event()
        #: Shards whose last attempt was refused for fleet capacity
        #: (issue #5567): the set a ``--capacity-retries`` pass re-runs.
        self.capacity_refused: set[int] = set()
        self.watch = _CapacityWatch(len(self.shard_indexes))
        if manifest is not None:
            for index in self.shard_indexes.values():
                prior = manifest.prior.get(str(index))
                self.watch.seed(index, None if prior is None else prior.get("state"))
        self.abortable_sleep = _abortable_sleep(self.abort)

    def shard_config(self) -> BatchConfig:
        if self.config.capacity_wait_s <= 0:
            return self.config
        remaining = self.config.capacity_wait_s - (time.monotonic() - self.started)
        if remaining <= 0:
            self.budget_spent.set()  # one attempt, no waiting
        return replace(self.config, capacity_wait_s=max(remaining, 0.0))

    def _phase_hook(self, index: int | None) -> Callable[[str], None]:
        def _on_phase(state: str) -> None:
            if index is None:
                return
            self.watch.phase(index, state)
            if self.manifest is None:
                return
            self.manifest.mark(index, state)
            if state == "launched":
                self.watch.launched(index)

        return _on_phase

    def _launch_kwargs(
        self, shard_config: BatchConfig, index: int | None
    ) -> dict[str, Any] | None:
        # Sleep and refusal hooks only matter when a refusal is waited out.
        if shard_config.capacity_wait_s <= 0:
            return None
        on_refusal = (
            None
            if index is None
            else lambda exc, _attempts: self.watch.refused(index, exc)
        )
        return {"sleep": self.abortable_sleep, "on_refusal": on_refusal}

    def _run_job(
        self,
        shard_points: list[CornerPoint],
        job_id: str,
        *,
        attach: bool,
        index: int | None = None,
        launch_if_unlaunched: bool = False,
        resumed: bool = False,
    ) -> _ShardResult:
        # Each retained artifact directory maps back to its job's S3 prefix.
        shard_artifacts_dir = (
            os.path.join(self.artifacts_dir, job_id)
            if self.keep_artifacts
            else self.artifacts_dir
        )
        shard_config = self.shard_config()
        if resumed:
            # An interrupted shard already spent its grace on the prior run.
            shard_config = _resume_attach_config(
                shard_config, _RESUME_INTERRUPTED_GRACE_S
            )
        try:
            result = _run_batch_job(
                config=shard_config,
                job_id=job_id,
                corner_points=shard_points,
                netlist_path=self.netlist_path,
                timeout_s=self.timeout_s,
                keep_artifacts=self.keep_artifacts,
                want_waveforms=self.want_waveforms,
                artifacts_dir=shard_artifacts_dir,
                request=self.request,
                measurements_spec=self.measurements_spec,
                explicit_points=shard_points,
                request_dir=self.request_dir,
                receipt_name=f"batch-receipt-{job_id}.json",
                attach=attach,
                launch_if_unlaunched=launch_if_unlaunched,
                on_phase=self._phase_hook(index),
                sleep=self.abortable_sleep,
                launch_kwargs=self._launch_kwargs(shard_config, index),
            )
            return _require_shard_environment(result)
        except BatchError as exc:
            if type(exc) is not BatchError:
                # Poll timeout / interruption already say where the job is.
                raise
            raise BatchError(
                f"shard {index}: {exc}{_remote_state_note(shard_config, job_id)}",
                code=exc.code,
            ) from exc

    def start_retry_pass(self) -> list[int]:
        """Reset the wait budget and pending count for the capacity-refused shards."""
        self.started = time.monotonic()
        self.budget_spent.clear()
        self.watch.start_retry_pass(self.capacity_refused)
        return sorted(self.capacity_refused)

    def run_shard_points(self, shard_points: list[CornerPoint]) -> _ShardResult:
        return self._guarded(shard_points, self._run_one_shard)

    def retry_shard_points(self, shard_points: list[CornerPoint]) -> _ShardResult:
        """Re-run a shard a capacity refusal lost: launch the job this run
        already submitted (never a second submit under another id)."""
        return self._guarded(shard_points, self._relaunch_shard)

    def _relaunch_shard(
        self, shard_points: list[CornerPoint], index: int
    ) -> _ShardResult:
        manifest = self.manifest
        job_id = None if manifest is None else manifest.planned_job_id(index)
        if job_id is None:
            return self._run_one_shard(shard_points, index)
        result = self._run_job(
            shard_points, job_id, attach=True, index=index, launch_if_unlaunched=True
        )
        _mark_collected(manifest, index, result)
        return _mark_resumed(result, False)

    def _guarded(
        self,
        shard_points: list[CornerPoint],
        body: Callable[[list[CornerPoint], int], _ShardResult],
    ) -> _ShardResult:
        index = self.shard_indexes[_units_digest(shard_points)]
        try:
            with sim_batch_observer.observe_batch_jobs(self.job_observer):
                result = body(shard_points, index)
            self.capacity_refused.discard(index)
            return result
        except BatchError as exc:
            # The lost-shard diagnostic carries the refusal code.
            if exc.code in _RETRYABLE_LAUNCH_CODES:
                self.capacity_refused.add(index)
                raise BatchError(
                    f"{exc}{self._budget_note(exc)} [{exc.code}]", code=exc.code
                ) from exc
            self.capacity_refused.discard(index)
            if exc.code in (LAUNCH_LIMIT_UNSUPPORTED, LAUNCH_LIMIT_EXHAUSTED):
                # The shard merger exposes exception text in its lost_shard diagnostic.
                raise BatchError(f"{exc} [{exc.code}]", code=exc.code) from exc
            raise
        finally:
            self.watch.finished(index)

    def _budget_note(self, exc: BatchError) -> str:
        if self.budget_spent.is_set() and "exhausted" not in str(exc):
            return (
                f" (run-wide capacity wait budget of "
                f"{self.config.capacity_wait_s:g}s exhausted)"
            )
        return ""

    def _run_one_shard(
        self, shard_points: list[CornerPoint], index: int
    ) -> _ShardResult:
        manifest = self.manifest
        digest = _units_digest(shard_points)
        prior = manifest.prior.get(str(index)) if manifest is not None else None
        job_id = None if manifest is None else manifest.planned_job_id(index)
        if self.attach_only:
            if prior is None or prior["units_sha256"] != digest:
                raise BatchError(
                    f"--attach: the shard map has no job for shard {index} "
                    "(its units differ from the submitted ones)",
                    code=BATCH_SHARD_MANIFEST_CODE,
                )
            if self.collect_partial:
                from . import sim_batch_shards

                sim_batch_shards.gate_partial_shard(self, index, prior)
            attached = self._run_job(
                shard_points,
                str(prior["job_id"]),
                attach=True,
                index=index,
                launch_if_unlaunched=prior.get("state") == "submitted",
            )
            _mark_collected(manifest, index, attached)
            return _mark_resumed(attached, True)
        if prior is not None and prior["units_sha256"] == digest:
            result = _try_reattach_shard(
                lambda: self._run_job(
                    shard_points,
                    str(prior["job_id"]),
                    attach=True,
                    index=index,
                    # Only an entry that says "submitted" (launch never
                    # started) is launched; older maps carry no state.
                    launch_if_unlaunched=prior.get("state") == "submitted",
                    resumed=True,
                ),
                job_id=str(prior["job_id"]),
            )
            if result is not None:
                _mark_collected(manifest, index, result)
                return _mark_resumed(result, True)
            job_id = None
        if job_id is None:
            job_id = new_job_id()
            if manifest is not None:
                manifest.record(
                    index, job_id, digest, units=[p.corner_id for p in shard_points]
                )
        result = self._run_job(shard_points, job_id, attach=False, index=index)
        _mark_collected(manifest, index, result)
        return _mark_resumed(result, False)

    def on_interrupt(self, exc: BaseException) -> str | None:
        if not isinstance(exc, (KeyboardInterrupt, BatchClientInterrupted)):
            return None
        self.abort.set()
        _progress(
            "klt sim: interrupted; reporting the shards that finished and "
            "marking the rest lost (re-run with --resume to finish them; "
            "launched jobs keep running -- the fleet has no cancel)"
        )
        return "interrupted by client before this shard finished"


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
    shard_fingerprint: str | None = None,
    resume: bool = False,
    attach_only: bool = False,
    collect_partial: bool = False,
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

    ``shard_fingerprint`` (issue #3300) is the request's resume fingerprint;
    given, each shard's job id is recorded in ``batch-shards.json`` under
    ``artifacts_dir`` *before* the shard is submitted. With ``resume`` a
    shard the manifest already maps is re-attached
    (``_run_batch_job(attach=True)``) -- ``done`` collected, live polled --
    and only a shard with no usable job (never submitted, outputs gone, or
    terminally failed) is submitted again under a fresh id. A manifest from a
    different request is refused (:data:`BATCH_SHARD_MANIFEST_CODE`) before
    any S3 write. Every ``environment.remote.fleet[]`` entry carries
    ``resumed: true|false``.

    ``attach_only`` (issue #4620; ``--attach <shard map>``) only re-attaches:
    every shard must be in the manifest (a shard it cannot reach is reported
    lost, never submitted again), and nothing is staged or pre-recorded.
    ``collect_partial`` (``--collect-partial``) reads each shard's status once
    instead of waiting: terminal shards merge, the rest are lost units listed
    in ``environment.remote.unfinished_shards``.
    """
    refuse_hosts_over_units(hosts, len(corner_points))

    if config is None:
        config = _resolve_batch_config(
            request, corner_count=len(corner_points), timeout_s=timeout_s
        )
    manifest = _open_shard_manifest(
        artifacts_dir, shard_fingerprint, hosts, resume=resume, config=config
    )
    shards = _shard_corner_points(corner_points, hosts)
    if not attach_only:
        _stage_job_closure_once(request, netlist_path, request_dir)
        _prerecord_shards(manifest, shards)

    fleet = _BatchFleetRun(
        config=config,
        manifest=manifest,
        shards=shards,
        netlist_path=netlist_path,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        artifacts_dir=artifacts_dir,
        request=request,
        measurements_spec=measurements_spec,
        request_dir=request_dir,
        attach_only=attach_only,
        collect_partial=collect_partial,
    )

    prev_handlers: dict[int, Any] = {}
    in_main = threading.current_thread() is threading.main_thread()
    if in_main:
        _signal_handlers_install(prev_handlers, _raise_client_interrupted)
    try:
        corners, engine_version, environment = _run_sharded(
            shard_runner=fleet.run_shard_points,
            corner_points=corner_points,
            hosts=hosts,
            measurements_spec=measurements_spec,
            max_concurrent=config.max_concurrent_jobs,
            on_interrupt=fleet.on_interrupt,
        )
        if config.capacity_retries > 0 and fleet.capacity_refused:
            from .sim_batch_capacity_retry import retry_capacity_refused

            corners, engine_version, environment = retry_capacity_refused(
                fleet,
                _shard_corner_points(corner_points, hosts),
                (corners, engine_version, environment),
                retries=config.capacity_retries,
                measurements_spec=measurements_spec,
                max_concurrent=config.max_concurrent_jobs,
            )
        if fleet.abort.is_set():
            environment = {**(environment or {}), "interrupted": True}
        if environment is not None or config.max_launch_attempts is not None:
            environment = {
                **(environment or {}),
                **config.launch_environment(
                    include_concurrency=True,
                    shards=(environment or {}).get("fleet", []) if resume else None,
                ),
            }
        environment = _batch_common.resumable(environment, manifest)
        if fleet.unfinished:
            from . import sim_batch_shards

            environment = sim_batch_shards.with_unfinished(
                environment, fleet.unfinished, manifest
            )
        return corners, engine_version, environment
    finally:
        if in_main:
            _signal_handlers_restore(prev_handlers)
        _warn_unlaunched(manifest, hosts)


def _stage_job_closure_once(
    request: dict[str, Any], netlist_path: str, request_dir: str | None
) -> None:
    """Stage (and discard) the whole job closure once, before any shard's
    first S3 write: an unstageable include or model input (issue #2668) must
    refuse the submit, not surface as K lost shards after some uploaded."""
    sim_staging.stage_sim_job(
        request,
        netlist_path,
        request_dir=request_dir,
        netlist_staged_name=BATCH_NETLIST_FILENAME,
        reserved_names=(BATCH_REQUEST_FILENAME,),
        backend="batch",
    )


def _submit_batch_shards(
    *,
    config: BatchConfig,
    request: dict[str, Any],
    corner_points: list[CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    hosts: int,
    shard_fingerprint: str,
    request_identity: dict[str, Any],
    request_dir: str | None = None,
    runner: remote_transport.CommandRunner | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Submit and launch every shard of a split request without waiting
    (issue #4620): the submit half of :func:`_run_batch_fleet`. Each shard is
    recorded in the ``batch-shards.json`` shard map (with the request digest)
    as it advances, so ``--attach <map>`` can wait for and merge them later.
    Returns the per-shard identities and the map's path. A failed submit or
    launch raises :class:`BatchError`; shards already launched stay recorded
    and keep running."""
    # Issue #4686: a re-run reuses the shard map of the same request, so a
    # shard that already has a job id never gets a second one.
    manifest = _open_shard_manifest(
        artifacts_dir,
        shard_fingerprint,
        hosts,
        resume=True,
        config=config,
        request_identity=request_identity,
        submit_only=True,
    )
    assert manifest is not None
    shards = _shard_corner_points(corner_points, hosts)
    _stage_job_closure_once(request, netlist_path, request_dir)
    _prerecord_shards(manifest, shards)
    identities: list[dict[str, Any]] = []
    unit_start = 0
    for index, points in enumerate(shards):
        if not points:
            continue
        job_id = manifest.planned_job_id(index)
        if job_id is not None:
            state = "pending"
        else:
            # Recorded by an earlier run; maps without a state predate the
            # lifecycle and are treated as launched (never resubmitted).
            prior = manifest.prior[str(index)]
            job_id = str(prior["job_id"])
            state = str(prior.get("state") or "launched")
            if state == "pending" and job_exists(config, job_id, runner=runner):
                # Killed between submit_job and the "submitted" mark.
                state = "submitted"
                manifest.mark(index, "submitted")
        if state == "pending":
            spec = _prepare_batch_job_spec(
                request,
                netlist_path,
                config=config,
                corner_count=len(points),
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                explicit_points=_points_to_push(request, points, points),
                request_dir=request_dir,
            )
            submit_job(config, job_id, spec, runner=runner)
            manifest.mark(index, "submitted")
            state = "submitted"
        if state == "submitted":
            launch_job(
                config, job_id, runner=runner, on_phase=partial(manifest.mark, index)
            )
            manifest.mark(index, "launched")
        elif state == "launching":
            # Outcome unknown: launching again could bill the shard twice.
            print(
                f"klt sim: notice: shard {index} (job {job_id}) was being "
                "launched when an earlier submit stopped; not launching it "
                "again -- --attach will report it if it never started",
                file=sys.stderr,
            )
        identities.append(
            {
                "index": index,
                "job_id": job_id,
                "job_uri": job_uri(config, job_id),
                "unit_start": unit_start,
                "unit_count": len(points),
            }
        )
        unit_start += len(points)
    return identities, manifest.path


def _mark_collected(
    manifest: _ShardManifest | None,
    index: int,
    result: tuple[list[dict[str, Any]], str | None, dict[str, Any] | None],
) -> None:
    """Record a shard whose outputs were actually collected (issue #3896);
    a failed or timed-out job returns unrun corners and stays ``launched``."""
    environment = result[2]
    if manifest is not None and environment and environment.get("client_collected_at"):
        manifest.mark(index, "collected")


def _mark_resumed(
    result: tuple[list[dict[str, Any]], str | None, dict[str, Any] | None],
    resumed: bool,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """Stamp the additive ``resumed`` flag on a shard's ``fleet[]`` entry."""
    corners, engine_version, environment = result
    if environment is not None:
        environment = {**environment, "resumed": resumed}
    return corners, engine_version, environment


#: Re-exported so ``sim.py``'s own module namespace keeps the same
#: "every shard/merge helper is reachable from `klayout_tools.sim`" property
#: the `remote` backend established -- see that module's import block.
__all__ = [
    "BATCH_ARTIFACTS_DIRNAME",
    "BATCH_INTERRUPTED_CODE",
    "BATCH_JOB_CEILING_CODE",
    "BATCH_MAX_CONCURRENT_CODE",
    "BATCH_MAX_UNITS_CODE",
    "BATCH_JOB_TOOL",
    "BATCH_CONCURRENCY_CAP_CODE",
    "BATCH_NO_CAPACITY_CODE",
    "BATCH_UNKNOWN_JOB_CODE",
    "BATCH_NETLIST_FILENAME",
    "BATCH_RECEIPT_FILENAME",
    "BATCH_SHARDS_FILENAME",
    "BATCH_SHARD_MANIFEST_CODE",
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
    "report_job_uri",
    "launch_job",
    "new_job_id",
    "poll_status",
    "read_collected_report",
    "submit_job",
]
