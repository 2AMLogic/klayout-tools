"""Per-thread batch-job observer hook (issue #4971).

Split out of ``sim_batch`` (a file-size-ratchet monolith) so the hook adds no
lines to it. Dependency-free on purpose: ``sim_batch`` and ``sim_set`` both
import from here.
"""

from __future__ import annotations

import contextlib
import contextvars
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobObserver:
    """Who to tell, and what to call the request, when a batch job id exists.
    ``--batch-set`` installs one per member so the set manifest records the id
    at submit time and progress lines name the request."""

    label: str | None
    on_job: Callable[[str], None] | None = None
    #: Called as ``on_state(job_id, state)`` on each observed state change
    #: (issue #5014: lets ``--batch-set`` summarise per-shard history).
    on_state: Callable[[str, str], None] | None = None


_JOB_OBSERVER: contextvars.ContextVar[JobObserver | None] = contextvars.ContextVar(
    "klt_sim_batch_job_observer", default=None
)


@contextlib.contextmanager
def observe_batch_jobs(observer: JobObserver | None) -> Any:
    """Install ``observer`` for batch submissions made on this thread."""
    token = _JOB_OBSERVER.set(observer)
    try:
        yield
    finally:
        _JOB_OBSERVER.reset(token)


def current_job_observer() -> JobObserver | None:
    return _JOB_OBSERVER.get()


def report_progress(
    job_id: str, state: str, observer: JobObserver | None = None
) -> None:
    """One per-job progress line on stderr (stdout carries the JSON report).
    In set mode the request is named: ``batch job <id> (<request>): state``."""
    label = f" ({observer.label})" if observer is not None and observer.label else ""
    print(f"klt sim: batch job {job_id}{label}: {state}", file=sys.stderr, flush=True)


def report_submitted(job_id: str) -> None:
    """A job id now exists: tell this thread's observer, then report it."""
    observer = current_job_observer()
    if observer is not None and observer.on_job is not None:
        observer.on_job(job_id)
    report_progress(job_id, "submitted", observer)


def state_hook(job_id: str) -> Callable[[str], None]:
    """``on_state`` callback for the poller (bound to this thread's observer)."""
    observer = current_job_observer()

    def _hook(state: str) -> None:
        if observer is not None and observer.on_state is not None:
            observer.on_state(job_id, state)
        report_progress(job_id, state, observer)

    return _hook
