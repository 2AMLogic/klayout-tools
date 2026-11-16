"""``klt sim --job-status``: the "never started" diagnostic fields (#5332).

The status-read twin of the blocking poll's "never started" check: a job
whose ``job.json`` was written longer ago than the provision slack but that
never produced a ``status.json``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import sim_batch


def unstarted_seconds(
    status: dict[str, Any] | None,
    job_head: dict[str, Any] | None,
    now: datetime | None,
) -> float | None:
    """Age of ``job.json`` when no ``status.json`` ever appeared and that age
    exceeds the provision slack; ``None`` otherwise."""
    if status is not None or not job_head:
        return None
    stamp = job_head.get("LastModified")
    if not isinstance(stamp, str):
        return None
    try:
        written = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - written).total_seconds()
    return age if age >= sim_batch._BATCH_PROVISION_SLACK_S else None


def unstarted_fields(
    job_id: str,
    status: dict[str, Any] | None,
    job_head: dict[str, Any] | None,
    now: datetime | None,
) -> dict[str, Any]:
    """Payload fields for a never-started job (``{}`` when it is not one)."""
    seconds = unstarted_seconds(status, job_head, now)
    if seconds is None:
        return {}
    return {
        "never_started": True,
        "unstarted_seconds": seconds,
        "diagnostic": sim_batch._format_never_started(job_id, seconds),
    }
