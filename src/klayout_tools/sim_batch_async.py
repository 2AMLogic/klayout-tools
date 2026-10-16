"""Asynchronous use of the ``batch`` backend: submit a job and return, then
read its state later by job id (issue #3073).

``klt sim --backend batch`` normally blocks in :func:`sim_batch.poll_status`
until the fleet job ends -- hours for a large Monte Carlo grid, with no
output. Two small entry points here let a caller hand the run off:

* :func:`submit_only` -- the same upload + launch as the blocking path
  (:func:`sim_batch.submit_job`, :func:`sim_batch.launch_job`), then return
  the job's identity **without polling or collecting**.
* :func:`read_job_status` -- one ``status.json`` read by job id.

What a status read can report is bounded by what the fleet publishes: the
harness writes ``status.json`` at state transitions only and carries **no
unit counts** (2am ``batch-fleet-harness.sh`` ``write_status``). So this
module reports state/detail/timing/instance fields and never invents a
"units done / total"; that needs a fleet-side change first.

Both functions take an injectable command ``runner`` like the rest of the
batch transport, so tests never touch ``aws``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from . import remote_transport, sim_batch
from .sim_batch import (
    BATCH_TERMINAL_STATES,
    BATCH_WAITING_STATES,
    BatchConfig,
    BatchError,
    _assess_job_ceiling,
    _aws_argv,
    _enforce_job_ceiling,
    _prepare_batch_job_spec,
    _read_status,
    _resolve_batch_config,
    job_uri,
    launch_job,
    new_job_id,
    submit_job,
)

#: Schema version of the two payloads this module builds. They are new
#: documents (not a field on the sim report), so they start at 1.
ASYNC_SCHEMA_VERSION = 1

#: ``error.code`` for a request ``--submit-only`` cannot represent as one job.
SUBMIT_ONLY_UNSUPPORTED_CODE = "submit_only_unsupported"
#: ``error.code`` for a job id with no ``job.json`` under the jobs prefix.
BATCH_JOB_NOT_FOUND_CODE = "batch_job_not_found"
#: ``error.code`` for a job id that is not a legal S3-prefix / EC2-tag token.
BATCH_JOB_ID_INVALID_CODE = "batch_job_id_invalid"

#: The state a job reports before the instance has written any
#: ``status.json`` (not booted yet) -- a normal condition, not an error.
PENDING_STATE = "pending"

#: ``status.json`` scalars passed through to the status payload verbatim
#: (absent ones are dropped, not nulled).
_STATUS_PASSTHROUGH = (
    "detail",
    "started_at",
    "updated_at",
    "elapsed_seconds",
    "exit_code",
    "tool",
)
_STATUS_INSTANCE_FIELDS = (
    "instance_id",
    "instance_type",
    "availability_zone",
    "region",
    "ami_id",
    "lifecycle",
    "spot",
    "concurrency",
    "physical_cores",
    "vcpus",
)

#: ``new_job_id``'s alphabet -- what ``batch-fleet-provision.sh launch``
#: accepts, and therefore the only ids that can name a job.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def request_sha256(request: dict[str, Any]) -> str:
    """Digest of the request document (canonical JSON), so a caller can tie a
    later status read back to the request it submitted."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_only(
    *,
    request: dict[str, Any],
    corner_points: list[Any],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    request_dir: str | None = None,
    runner: remote_transport.CommandRunner | None = None,
) -> dict[str, Any]:
    """Upload and launch one batch job, then return its identity at once.

    Never polls and never collects; the caller reads state with
    :func:`read_job_status`. Applies the same pre-submit gates as the
    blocking path (config/bucket resolution, runner-version check, the
    whole-job ceiling), all before the first S3 write.
    """
    from .sim import SimError

    config = _resolve_batch_config(
        request, corner_count=len(corner_points), timeout_s=timeout_s
    )
    job_ceiling = _assess_job_ceiling(
        unit_count=len(corner_points), per_unit_timeout_s=timeout_s, config=config
    )
    _enforce_job_ceiling(job_ceiling)
    job_id = new_job_id()
    try:
        spec = _prepare_batch_job_spec(
            request,
            netlist_path,
            config=config,
            corner_count=len(corner_points),
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            request_dir=request_dir,
        )
        submit_job(config, job_id, spec, runner=runner)
        launch_job(config, job_id, runner=runner)
    except BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc
    return {
        "schema_version": ASYNC_SCHEMA_VERSION,
        "status": "submitted",
        "job_id": job_id,
        "bucket": config.bucket,
        "region": config.region,
        "jobs_prefix": config.jobs_prefix,
        "job_uri": job_uri(config, job_id),
        "launched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_sha256": request_sha256(request),
        "unit_count": len(corner_points),
        "job_ceiling": job_ceiling,
    }


def _job_exists(
    config: BatchConfig, job_id: str, run: remote_transport.CommandRunner
) -> bool:
    """Whether ``job.json`` exists under this job's prefix (one
    ``head-object``). A 404 is "no such job"; any other failure (auth,
    network) is a transport error, never reported as "unknown job"."""
    bucket_key = f"{config.jobs_prefix}/{job_id}/job.json"
    argv = _aws_argv(
        config,
        "s3api",
        "head-object",
        "--bucket",
        config.bucket,
        "--key",
        bucket_key,
    )
    try:
        result = run(argv, 60.0)
    except Exception as exc:  # timeout / spawn failure
        raise BatchError(f"aws s3api head-object (job.json) failed: {exc}") from exc
    if result.returncode == 0:
        return True
    detail = (result.stderr or "").strip()
    if "404" in detail or "Not Found" in detail or "NoSuchKey" in detail:
        return False
    raise BatchError(
        f"aws s3api head-object (job.json) failed (exit {result.returncode}): {detail}"
    )


def read_job_status(
    request: dict[str, Any],
    job_id: str,
    *,
    runner: remote_transport.CommandRunner | None = None,
) -> dict[str, Any]:
    """One status read for ``job_id``: ``status.json`` fields as JSON.

    ``request`` supplies only the bucket/region/profile resolution (its
    ``batch`` block, the ``KLT_BATCH_*`` environment, the fleet config). A
    job with no ``status.json`` yet reports ``state: "pending"``; a job id
    with no ``job.json`` is a :class:`~klayout_tools.sim.SimError` carrying
    :data:`BATCH_JOB_NOT_FOUND_CODE`. Submits nothing and re-launches
    nothing, whatever the state (an ``interrupted`` job is the fleet's own
    reconcile to relaunch).
    """
    from .sim import SimError

    if not _JOB_ID_RE.match(job_id):
        raise SimError(
            f"invalid batch job id {job_id!r} "
            "(allowed: letters, digits, '.', '_', '-')",
            code=BATCH_JOB_ID_INVALID_CODE,
        )
    # Resolved at call time so a patched default transport is honoured.
    run = runner if runner is not None else sim_batch._run_subprocess
    config = _resolve_batch_config(
        request, corner_count=0, timeout_s=0.0, check_runner=False
    )
    try:
        status = _read_status(config, job_id, run)
        if status is None and not _job_exists(config, job_id, run):
            raise SimError(
                f"no batch job {job_id!r} under {config.bucket}/{config.jobs_prefix}/",
                code=BATCH_JOB_NOT_FOUND_CODE,
            )
    except BatchError as exc:
        raise SimError(f"batch backend failed: {exc}", code=exc.code) from exc

    state = PENDING_STATE if status is None else str(status.get("state") or "")
    document = status or {}
    payload: dict[str, Any] = {
        "schema_version": ASYNC_SCHEMA_VERSION,
        "job_id": job_id,
        "bucket": config.bucket,
        "region": config.region,
        "job_uri": job_uri(config, job_id),
        "state": state,
        "terminal": state in BATCH_TERMINAL_STATES,
        "known_state": state in BATCH_TERMINAL_STATES
        or state in BATCH_WAITING_STATES
        or state == PENDING_STATE,
    }
    for key in _STATUS_PASSTHROUGH:
        if key in document:
            payload[key] = document[key]
    instance = {k: document[k] for k in _STATUS_INSTANCE_FIELDS if k in document}
    if instance:
        payload["instance"] = instance
    return payload
