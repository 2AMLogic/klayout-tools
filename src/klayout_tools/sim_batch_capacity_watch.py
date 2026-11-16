"""Fleet-wide capacity-wait / launch-progress view of a sharded batch run.

Split from :mod:`klayout_tools.sim_batch` (file-size ratchet, issue #5567);
``sim_batch._CapacityWatch`` re-exports it.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sim_batch_contract import BatchError


def _progress(line: str) -> None:
    """One progress line on stderr (never stdout: that carries the JSON
    envelope). Resolves ``sys.stderr`` at call time so it can be captured."""
    print(line, file=sys.stderr, flush=True)


def _heartbeat_interval_s() -> float:
    from .sim_batch import BATCH_HEARTBEAT_INTERVAL_S

    return BATCH_HEARTBEAT_INTERVAL_S


class _CapacityWatch:
    """Fleet-wide view of shards waiting out a capacity refusal (issue #4018).

    Each shard's :func:`launch_job` retries on its own thread; this folds
    their refusals into one stderr line -- ``K of M shards pending`` and the
    last refusal's code -- printed when the number of waiting shards changes
    and otherwise at most once per :data:`sim_batch.BATCH_HEARTBEAT_INTERVAL_S`.
    """

    def __init__(
        self,
        total: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._total = total
        self._monotonic = monotonic
        self._emit = emit if emit is not None else _progress
        self._lock = threading.Lock()
        self._waiting: set[int] = set()
        self._finished = 0
        self._last_emit: float | None = None
        self._last_waiting = 0
        self._states: dict[int, str] = {}
        self._launch_events = 0
        self._first_launch_at: float | None = None
        self._last_counts: tuple[int, int, int, int] = (0, 0, total, 0)
        self._last_progress: float | None = None
        self._final_emitted = False

    def seed(self, index: int, state: str | None) -> None:
        """Record the state a resumed shard starts in (no progress line)."""
        if state in ("launching", "launched", "collected"):
            with self._lock:
                self._states[index] = str(state)

    def phase(self, index: int, state: str) -> None:
        """Fold one shard lifecycle event into the launch-progress line
        (issue #4349): per-state counts and a launch-rate ETA on stderr,
        printed when the counts change, at most once per
        :data:`sim_batch.BATCH_HEARTBEAT_INTERVAL_S`, and once more when the last shard
        has launched. Single-shard runs print nothing."""
        with self._lock:
            if state == "launch_refused":
                state = "submitted"
            if state == "launched":
                self._launch_events += 1
                if self._first_launch_at is None:
                    self._first_launch_at = self._monotonic()
            self._states[index] = state
            self._progress_locked()

    def _counts(self) -> tuple[int, int, int, int]:
        values = list(self._states.values())
        launching = values.count("launching")
        launched = values.count("launched")
        collected = values.count("collected")
        pending = self._total - launching - launched - collected
        return launched, launching, pending, collected

    def _progress_locked(self) -> None:
        if self._total <= 1:
            return
        counts = self._counts()
        if counts == self._last_counts:
            return
        now = self._monotonic()
        final = counts[1] == 0 and counts[2] == 0 and not self._final_emitted
        if (
            not final
            and self._last_progress is not None
            and now - self._last_progress < _heartbeat_interval_s()
        ):
            return
        self._last_counts = counts
        self._last_progress = now
        self._final_emitted = self._final_emitted or final
        launched, launching, pending, collected = counts
        rate = self._rate_per_min(now)
        rate_text = "unknown" if rate is None else f"{rate:.1f}/min"
        self._emit(
            f"klt sim: shards {launched + collected}/{self._total} launched, "
            f"{launching} launching, {pending} pending, {collected} collected; "
            f"launch rate {rate_text}, ETA {self._eta_text(rate, pending + launching)}"
        )

    def _rate_per_min(self, now: float) -> float | None:
        if self._first_launch_at is None or self._launch_events < 2:
            return None
        elapsed = now - self._first_launch_at
        if elapsed <= 0:
            return None
        return (self._launch_events - 1) / elapsed * 60.0

    @staticmethod
    def _eta_text(rate: float | None, remaining: int) -> str:
        if remaining <= 0:
            return "0m"
        if not rate:
            return "unknown"
        hours, mins = divmod(max(1, round(remaining / rate)), 60)
        return f"{hours}h {mins}m" if hours else f"{mins}m"

    def refused(self, index: int, exc: BatchError) -> None:
        with self._lock:
            self._waiting.add(index)
            now = self._monotonic()
            if (
                self._last_emit is not None
                and len(self._waiting) == self._last_waiting
                and now - self._last_emit < _heartbeat_interval_s()
            ):
                return
            self._last_emit = now
            self._last_waiting = len(self._waiting)
            reason = exc.code or str(exc).splitlines()[0]
            self._emit(
                "klt sim: waiting for capacity: "
                f"{self._total - self._finished} of {self._total} shards "
                f"pending, {len(self._waiting)} waiting on a launch "
                f"(last refusal: {reason})"
            )

    def launched(self, index: int) -> None:
        with self._lock:
            self._waiting.discard(index)

    def finished(self, index: int) -> None:
        with self._lock:
            self._waiting.discard(index)
            self._finished += 1
            if self._states.get(index) == "launched":
                self._states[index] = "collected"
                self._progress_locked()
