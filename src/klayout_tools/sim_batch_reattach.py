"""Re-attach policy for ``klt sim --resume`` (issues #3300, #5371, #5459);
split out of :mod:`sim_batch` to keep it under its file-size ceiling."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, TypeVar

from .sim_batch_contract import BatchClientInterrupted, BatchError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .sim_batch import BatchConfig

#: Grace applied to a resumed re-attach (issue #5371): the prior run already
#: waited out ``interrupted_grace_s``, so a job still ``interrupted`` is
#: reported after about one poll interval instead of another full window.
_RESUME_INTERRUPTED_GRACE_S = 1.0

_ConfigT = TypeVar("_ConfigT", bound="BatchConfig")


def _resume_attach_config(config: _ConfigT, grace_s: float) -> _ConfigT:
    """``config`` with the interrupted grace shortened to ``grace_s`` for a
    resumed re-attach (issues #5371, #5459). A job still ``interrupted``
    already waited out the full window in the prior run, so surface it after
    ~one poll and let the caller submit afresh. ``0`` (grace disabled by the
    request) is left unbounded, exactly as today.

    ``grace_s`` is passed in rather than read from
    :data:`_RESUME_INTERRUPTED_GRACE_S` here so each caller resolves it from
    its own module namespace at call time (which is what the tests patch).
    """
    if config.interrupted_grace_s <= 0:
        return config
    return replace(config, interrupted_grace_s=grace_s)


def _try_reattach_job(
    attach: Callable[
        [], tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]
    ],
    *,
    job_id: str,
    resubmit_what: str = "the request",
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None] | None:
    """Re-attach to a recorded job (issue #3300), or ``None`` when the caller
    should submit it afresh: the job was never submitted, its outputs cannot
    be collected, it ended in a terminal failure, or it stayed ``interrupted``
    past its grace. A client interrupt is never swallowed, and a poll-budget
    overrun is returned as is -- that job may still be running, so
    resubmitting would bill it twice.

    ``resubmit_what`` names what gets submitted again in the stderr notice
    (``"that shard"`` for a fleet shard, ``"the request"`` for the single-job
    path, issue #5459).
    """
    try:
        result = attach()
    except BatchClientInterrupted:
        raise
    except BatchError as exc:
        print(
            f"klt sim: notice: cannot re-attach batch job {job_id} ({exc}); "
            f"submitting {resubmit_what} again",
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
                f"{resubmit_what} again",
                file=sys.stderr,
            )
            return None
    return result


def _try_reattach_shard(
    attach: Callable[
        [], tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]
    ],
    *,
    job_id: str,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None] | None:
    """:func:`_try_reattach_job` for one shard of a fleet run (issue #3300)."""
    return _try_reattach_job(attach, job_id=job_id, resubmit_what="that shard")


def _resume_or_submit_one_job(
    *,
    config: BatchConfig,
    resume_job_id: str | None,
    resume_grace_s: float,
    **job_kwargs: Any,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """Run the single-job (unsharded) batch path, re-attaching to
    ``resume_job_id`` when ``--resume`` recorded one and submitting afresh
    otherwise (issue #5459).

    This gives the one-job path the policy :func:`_try_reattach_shard` already
    gives a fleet shard (issue #5371). The recorded job waited out the full
    ``batch.interrupted_grace_s`` in the prior run, so the re-attach uses the
    short ``resume_grace_s`` and a job that is *still* ``interrupted`` is
    reported after about one poll instead of burning another full window.
    :func:`_try_reattach_job` then returns ``None``, and the request is
    submitted once -- at most once per invocation -- under a fresh job id with
    the caller's original ``config``. A fresh launch rewrites
    ``batch-receipt.json``, so the spent receipt is replaced with no extra
    bookkeeping.

    A ``batch_poll_timeout`` result is returned as is: that job may still be
    running on the fleet, so resubmitting it would bill the same work twice.
    """
    from .sim_batch import _run_batch_job, new_job_id

    if resume_job_id is not None:
        result = _try_reattach_job(
            lambda: _run_batch_job(
                config=_resume_attach_config(config, resume_grace_s),
                job_id=resume_job_id,
                attach=True,
                **job_kwargs,
            ),
            job_id=resume_job_id,
        )
        if result is not None:
            return result
    return _run_batch_job(
        config=config, job_id=new_job_id(), attach=False, **job_kwargs
    )
