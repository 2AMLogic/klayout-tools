"""``--attach`` request verification against the local submit receipt
(issue #4971). Split out of ``sim_batch`` (a file-size-ratchet monolith)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def request_sha256(request: dict[str, Any]) -> str:
    """Digest of the whole request (what the synchronous submit receipt records)."""
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
    # Two writers: the synchronous submit hashes the whole request; the
    # --submit-only receipt excludes submission-only batch fields.
    supplied = request_sha256(request)
    accepted = {supplied, sim_batch_shards.request_sha256(request)}
    if isinstance(recorded, str) and recorded not in accepted:
        raise SimError(
            f"--attach {job_id}: the supplied request (sha256 {supplied}) is not "
            f"the request this job was submitted from (receipt {path} records "
            f"request_sha256 {recorded}); pass the original request file",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
