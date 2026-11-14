"""``--attach`` request verification against the local submit receipt
(issue #4971). Split out of ``sim_batch`` (a file-size-ratchet monolith)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


def request_sha256(request: dict[str, Any]) -> str:
    """Digest of the whole request (what receipts written before #5194 recorded)."""
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def receipt_request_sha256(request: dict[str, Any]) -> str:
    """Digest the synchronous submit receipt records: submission-only batch
    fields (``capacity_wait_s``, ...) are excluded so a flag-less ``--attach``
    of the original request verifies (issue #5194)."""
    from . import sim_batch_shards

    return sim_batch_shards.receipt_request_sha256(request)


def check_attach_receipt(
    artifacts_dir: str, job_id: str, request: dict[str, Any]
) -> None:
    """Refuse ``--attach`` with a request other than the one the job was
    submitted from, when the local submit receipt says so. No receipt, an
    unreadable one, or one for a different job id -> proceed unchanged."""
    from . import sim_batch, sim_batch_shards
    from .sim import SimError

    path = os.path.join(artifacts_dir, sim_batch.BATCH_RECEIPT_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(receipt, dict) or receipt.get("job_id") != job_id:
        return
    recorded = receipt.get("request_sha256")
    # Writers exclude submission-only batch fields (sync: receipt_request_sha256;
    # --submit-only: request_sha256); the whole-request digest is still
    # accepted for receipts written before #5194.
    supplied = request_sha256(request)
    accepted = {
        supplied,
        sim_batch_shards.request_sha256(request),
        receipt_request_sha256(request),
    }
    if isinstance(recorded, str) and recorded not in accepted:
        raise SimError(
            f"--attach {job_id}: the supplied request (sha256 {supplied}) is not "
            f"the request this job was submitted from (receipt {path} records "
            f"request_sha256 {recorded}); pass the original request file",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )


def new_receipt(
    config: Any, job_id: str, request: dict[str, Any], corner_points: list[Any]
) -> dict[str, Any]:
    """The submit receipt a fresh launch writes. ``getattr``: it is
    best-effort and must never break a run; no bucket (its name carries the
    cloud account id, issue #4079). ``unit_count`` (issue #5350) lets
    ``--resume`` re-attach only to a job that covered exactly the units it is
    about to dispatch."""
    return {
        "job_id": job_id,
        "jobs_prefix": getattr(config, "jobs_prefix", None),
        "region": getattr(config, "region", None),
        "profile": getattr(config, "profile", None),
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "request_sha256": receipt_request_sha256(request),
        "unit_count": len(corner_points),
        "state": None,
    }


def resumable_job_id(
    artifacts_dir: str, request: dict[str, Any], unit_count: int, resume: bool
) -> str | None:
    """The job id ``--resume`` should re-attach to instead of resubmitting
    (issue #5350): the one a prior unsharded submit recorded in the local
    receipt, when that receipt's request digest and unit count match this
    run's. No receipt, an unreadable or older one (no ``unit_count``), or any
    mismatch -> ``None`` (submit afresh; never attach stale results)."""
    if not resume:
        return None
    from . import sim_batch

    path = os.path.join(artifacts_dir, sim_batch.BATCH_RECEIPT_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    job_id = receipt.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    if receipt.get("unit_count") != unit_count:
        return None
    accepted = {request_sha256(request), receipt_request_sha256(request)}
    if receipt.get("request_sha256") not in accepted:
        return None
    sys.stderr.write(
        f"klt sim: --resume re-attaching to batch job {job_id} instead of "
        "resubmitting\n"
    )
    return job_id
