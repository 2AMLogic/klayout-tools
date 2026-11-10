"""``--attach`` request verification against the local submit receipt
(issue #4971). Split out of ``sim_batch`` (a file-size-ratchet monolith)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def request_sha256(request: dict[str, Any]) -> str:
    """Digest of the whole request (what receipts written before #5194 recorded)."""
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


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
        sim_batch_shards.receipt_request_sha256(request),
    }
    if isinstance(recorded, str) and recorded not in accepted:
        raise SimError(
            f"--attach {job_id}: the supplied request (sha256 {supplied}) is not "
            f"the request this job was submitted from (receipt {path} records "
            f"request_sha256 {recorded}); pass the original request file",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
