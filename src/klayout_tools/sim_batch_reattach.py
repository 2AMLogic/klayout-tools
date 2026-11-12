"""Shard re-attach policy for ``klt sim --resume`` (issues #3300, #5371);
split out of :mod:`sim_batch` to keep it under its file-size ceiling."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from .sim_batch_contract import BatchClientInterrupted, BatchError

#: Grace applied to a resumed re-attach (issue #5371): the prior run already
#: waited out ``interrupted_grace_s``, so a job still ``interrupted`` is
#: reported after about one poll interval instead of another full window.
_RESUME_INTERRUPTED_GRACE_S = 1.0


def _try_reattach_shard(
    attach: Callable[
        [], tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]
    ],
    *,
    job_id: str,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None] | None:
    """Re-attach to a shard's recorded job (issue #3300), or ``None`` when the
    caller should submit it afresh: the job was never submitted, its outputs
    cannot be collected, it ended in a terminal failure, or it stayed
    ``interrupted`` past its grace. A client
    interrupt is never swallowed, and a poll-budget overrun is returned as
    is -- that job may still be running, so resubmitting would bill it twice.
    """
    try:
        result = attach()
    except BatchClientInterrupted:
        raise
    except BatchError as exc:
        print(
            f"klt sim: notice: cannot re-attach batch job {job_id} ({exc}); "
            "submitting that shard again",
            file=sys.stderr,
        )
        return None
    corners = result[0]
    for code, what in (
        ("batch_job_failed", "had failed"),
        (
            "batch_job_interrupted",
            "stayed interrupted (Spot reclaimed) past its grace",
        ),
    ):
        if corners and all(
            any(d.get("code") == code for d in corner.get("diagnostics") or [])
            for corner in corners
        ):
            print(
                f"klt sim: notice: batch job {job_id} {what}; submitting "
                "that shard again",
                file=sys.stderr,
            )
            return None
    return result
