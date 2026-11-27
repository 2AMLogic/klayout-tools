"""Cancel one ``batch`` fleet job by id, and the client side of the
``cancelled`` terminal state (issue #4351; the fleet primitive is
2AMLogic/2am#6502).

``klt sim --cancel JOB_ID`` shells out to 2am's
``batch-fleet-provision.sh cancel <job-id> --apply`` -- never a native AWS
call from klt: the script's bookkeeping order is load-bearing (mark
``status.json`` ``cancelled``, write the end record, only then terminate),
and terminating first would read as ``abandoned`` to the fleet's
``reconcile`` and get the job re-launched.

This module reads :mod:`klayout_tools.sim_batch`'s transport helpers, which
is why :func:`klayout_tools.sim_batch.poll_status` imports
:func:`terminal_or_cancelled` deferred, at call time, instead of at module
scope -- a top-level import here would be circular.
"""

from __future__ import annotations

from typing import Any

from . import remote_transport, sim_batch
from .sim_batch_async import _JOB_ID_RE, BATCH_JOB_ID_INVALID_CODE
from .sim_batch_contract import (
    BATCH_JOB_CANCELLED_CODE,
    CANCELLED_STATE,
    BatchError,
)
from .sim_errors import SimError

#: Machine-readable ``error.code`` when the fleet script refuses a cancel
#: because the job has nothing live to terminate ("has no pending or running
#: instance ... nothing to cancel, nothing written").
CANCEL_NOTHING_TO_CANCEL_CODE = "batch_cancel_nothing_to_cancel"

#: The stderr fragment of 2am's refusal line that identifies that case -- the
#: same text-match approach :func:`sim_batch._classify_launch_failure` takes
#: for ``launch`` refusals (the script exits ``1`` for every one of them).
_NOTHING_TO_CANCEL_MARKER = "nothing to cancel"

#: One cancel is an instance listing, one S3 write and one terminate call.
_CANCEL_TIMEOUT_S = 900.0

#: Schema version of the cancel envelope (a new document, so it starts at 1).
CANCEL_SCHEMA_VERSION = 1


def terminal_or_cancelled(job_id: str, status: dict[str, Any]) -> dict[str, Any]:
    """``status`` when its state is a plain terminal state; a
    :class:`BatchError` carrying ``batch_job_cancelled`` when it is
    ``cancelled`` (issue #4351).

    This is the terminal return of :func:`sim_batch.poll_status`: every
    waiting path (a blocking ``klt sim --backend batch``, ``--attach``) fails
    fast on a cancelled job instead of collecting one that will never
    produce results."""
    if str(status.get("state") or "") == CANCELLED_STATE:
        raise BatchError(
            f"job {job_id} was cancelled (klt sim --cancel); nothing is "
            "re-launched and no results are coming",
            code=BATCH_JOB_CANCELLED_CODE,
        )
    return status


def cancel_job(
    request: dict[str, Any],
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> dict[str, Any]:
    """Cancel the batch job ``job_id`` through the fleet's ``cancel`` verb.

    ``request`` supplies only the bucket/region/profile/script resolution
    (its ``batch`` block, the ``KLT_BATCH_*`` environment, the fleet config),
    exactly as :func:`sim_batch_async.read_job_status` resolves them; it may
    be ``{}`` for a request-less ``klt sim --cancel``. Returns the cancel
    envelope: ``schema_version``, ``job_id``, ``state: "cancelled"``,
    ``bucket``, ``region``, ``job_uri``, plus ``warnings`` -- the script's
    stderr lines, bucket redacted -- when it warned (e.g. an instance it
    could not terminate). Raises :class:`SimError` with
    :data:`sim_batch_async.BATCH_JOB_ID_INVALID_CODE` for a malformed id
    (before any subprocess), :data:`CANCEL_NOTHING_TO_CANCEL_CODE` when the
    fleet reports nothing live to cancel, and the script's bucket-redacted
    stderr for any other failure."""
    if not _JOB_ID_RE.match(job_id):
        raise SimError(
            f"invalid batch job id {job_id!r} "
            "(allowed: letters, digits, '.', '_', '-')",
            code=BATCH_JOB_ID_INVALID_CODE,
        )
    # Resolved at call time so a patched default transport is honoured.
    run = runner if runner is not None else sim_batch._run_subprocess
    config = sim_batch._resolve_batch_config(
        request, corner_count=0, timeout_s=0.0, check_runner=False
    )
    # The provisioner reads the job store from its own fleet config. Its
    # cancel verb has no bucket or prefix argument.
    fleet = sim_batch._read_fleet_config(config.provision_script)
    if (fleet.get("BATCH_JOB_BUCKET"), fleet.get("BATCH_JOBS_PREFIX")) != (
        config.bucket,
        config.jobs_prefix,
    ):
        raise SimError(
            "cannot cancel: the job store must match BATCH_JOB_BUCKET and "
            "BATCH_JOBS_PREFIX in batch-fleet.env beside the provision script; "
            "check the fleet configuration and any batch bucket or prefix override"
        )
    argv = [
        config.provision_script,
        "cancel",
        job_id,
        "--apply",
        "--profile",
        config.profile,
    ]
    if config.region:
        argv += ["--region", config.region]
    try:
        result = sim_batch._run_checked(
            run,
            argv,
            _CANCEL_TIMEOUT_S,
            "batch-fleet-provision.sh cancel",
            classify=_classify_cancel_failure,
            redact=config.bucket,
        )
    except BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    payload: dict[str, Any] = {
        "schema_version": CANCEL_SCHEMA_VERSION,
        "job_id": job_id,
        "state": CANCELLED_STATE,
        "bucket": config.bucket,
        "region": config.region,
        "job_uri": sim_batch.job_uri(config, job_id),
    }
    warnings = [
        line.strip().replace(config.bucket, "<bucket>")
        for line in (result.stderr or "").splitlines()
        if line.strip()
    ]
    if warnings:
        payload["warnings"] = warnings
    return payload


def _classify_cancel_failure(stderr: str, _returncode: int) -> str | None:
    """:data:`CANCEL_NOTHING_TO_CANCEL_CODE` when the failed cancel's stderr
    is the provisioner's "nothing to cancel" refusal, else ``None`` (every
    other failure stays unclassified, as with ``launch``)."""
    if _NOTHING_TO_CANCEL_MARKER in stderr:
        return CANCEL_NOTHING_TO_CANCEL_CODE
    return None
