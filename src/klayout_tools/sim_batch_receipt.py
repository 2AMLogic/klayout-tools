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
    """Digest used by single-job corner receipts: submission-only batch
    fields (``capacity_wait_s``, ...) are excluded so a flag-less ``--attach``
    of the original request verifies (issue #5194)."""
    from . import sim_batch_shards

    return sim_batch_shards.receipt_request_sha256(request)


def corner_receipt_identity(
    request: dict[str, Any], netlist_sha256: str | None
) -> dict[str, str]:
    identity = {"request_sha256": receipt_request_sha256(request)}
    if netlist_sha256 is not None:
        identity["netlist_sha256"] = netlist_sha256
    return identity


def _legacy_digests(request: dict[str, Any]) -> set[str]:
    """Earlier writers used whole-request JSON or kept an empty batch block."""
    from . import sim_batch_shards

    accepted = {request_sha256(request), sim_batch_shards.request_sha256(request)}
    if "batch" not in request:
        accepted.add(sim_batch_shards.request_sha256({**request, "batch": {}}))
    return accepted


def check_attach_receipt(
    artifacts_dir: str,
    job_id: str,
    request: dict[str, Any],
    netlist_sha256: str | None = None,
) -> None:
    """Refuse ``--attach`` with a request other than the one the job was
    submitted from, when the local submit receipt says so. No receipt, an
    unreadable one, or one for a different job id -> proceed unchanged."""
    from . import sim_batch
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
    supplied = receipt_request_sha256(request)
    accepted = {supplied}
    if "netlist_sha256" not in receipt:
        accepted.update(_legacy_digests(request))
    if isinstance(recorded, str) and recorded not in accepted:
        raise SimError(
            f"--attach {job_id}: the supplied request (sha256 {supplied}) is not "
            f"the request this job was submitted from (receipt {path} records "
            f"request_sha256 {recorded}); pass the original request file",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )
    recorded_netlist = receipt.get("netlist_sha256")
    if recorded_netlist is not None and recorded_netlist != netlist_sha256:
        raise SimError(
            f"--attach {job_id}: the top-level netlist (sha256 {netlist_sha256}) "
            f"differs from the submitted deck (receipt {path} records "
            f"netlist_sha256 {recorded_netlist}); supply the original netlist",
            code=sim_batch.BATCH_SHARD_MANIFEST_CODE,
        )


def units_digest(corner_points: list[Any]) -> str | None:
    """Use the same ordered corner identity as the fleet shard manifest."""
    from .sim_batch import _units_digest

    if all(hasattr(point, "corner_id") for point in corner_points):
        return _units_digest(corner_points)
    return None


def new_receipt(
    config: Any,
    job_id: str,
    request: dict[str, Any],
    corner_points: list[Any],
    netlist_sha256: str | None = None,
) -> dict[str, Any]:
    """The submit receipt a fresh launch writes. ``getattr``: it is
    best-effort and must never break a run; no bucket (its name carries the
    cloud account id, issue #4079). ``unit_count`` (issue #5350) lets
    ``--resume`` re-attach only to a job that covered exactly the units it is
    about to dispatch. The digest distinguishes equal-sized subsets."""
    digest = units_digest(corner_points)
    return {
        "job_id": job_id,
        "jobs_prefix": getattr(config, "jobs_prefix", None),
        "region": getattr(config, "region", None),
        "profile": getattr(config, "profile", None),
        "launched_at": datetime.now(timezone.utc).isoformat(),
        **corner_receipt_identity(request, netlist_sha256),
        "unit_count": len(corner_points),
        **({"units_sha256": digest} if digest is not None else {}),
        "state": None,
    }


def resumable_job_id(
    artifacts_dir: str,
    request: dict[str, Any],
    unit_count: int,
    resume: bool,
    netlist_sha256: str | None = None,
    *,
    units_sha256: str | None = None,
    require_units_digest: bool = False,
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
    if receipt.get("netlist_sha256", netlist_sha256) != netlist_sha256:
        return None
    if receipt.get("unit_count") != unit_count:
        return None
    # A legacy whole-grid job is identified by request and count. On a
    # partial resume its count alone cannot distinguish two different slices.
    legacy_units = None if require_units_digest else units_sha256
    if receipt.get("units_sha256", legacy_units) != units_sha256:
        return None
    accepted = {receipt_request_sha256(request)}
    if "netlist_sha256" not in receipt:
        accepted.update(_legacy_digests(request))
    if receipt.get("request_sha256") not in accepted:
        return None
    sys.stderr.write(
        f"klt sim: --resume re-attaching to batch job {job_id} instead of "
        "resubmitting\n"
    )
    return job_id
