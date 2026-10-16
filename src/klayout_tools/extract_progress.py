"""Per-stage wall-clock timing and stderr progress for ``klt extract`` (issue #3181).

A large routed block can spend tens of minutes in ``klt extract`` with no
output, which a caller cannot tell from a hang. :class:`StageTimer` records
monotonic wall seconds per named stage (the additive ``timing`` object on the
response envelope) and, when enabled, writes stage start/finish lines plus a
periodic heartbeat to **stderr** -- stdout stays the pure envelope.

``run_extract`` installs a timer with :func:`active_timer`; code deeper in the
call stack opens stages through :func:`stage`, which is a no-op when no timer is
installed so library callers that never ask for timing pay nothing.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, TextIO

# Seconds between "still running" lines while one stage is open.
HEARTBEAT_INTERVAL_S = 30.0

_active: StageTimer | None = None


class StageTimer:
    """Accumulates per-stage wall seconds; optionally narrates to a stream."""

    def __init__(
        self,
        *,
        progress: bool | str = False,
        stream: TextIO | None = None,
        heartbeat_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        # `True`: stage start/finish lines plus heartbeat. `"heartbeat"`: the
        # heartbeat only, so a short run (and any stderr error envelope that
        # follows it) stays byte-clean while a long one still shows life.
        self._progress = bool(progress)
        self._narrate_stages = progress is True
        self._stream = stream if stream is not None else sys.stderr
        self._heartbeat_s = heartbeat_s
        self._t0 = time.monotonic()
        self._stages: dict[str, float] = {}
        self._current: str | None = None
        self._current_t0 = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _emit(self, message: str) -> None:
        if not self._progress:
            return
        try:
            print(f"klt extract: {message}", file=self._stream, flush=True)
        except (OSError, ValueError):
            # A closed/broken stderr must never fail an extraction.
            pass

    def _elapsed(self) -> float:
        return time.monotonic() - self._t0

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_s):
            name = self._current
            if name is not None:
                self._emit(
                    f"{name} still running "
                    f"({time.monotonic() - self._current_t0:.0f}s in stage, "
                    f"{self._elapsed():.0f}s total)"
                )

    def start(self) -> None:
        if self._progress and self._thread is None:
            self._thread = threading.Thread(
                target=self._heartbeat, name="klt-extract-heartbeat", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        previous, previous_t0 = self._current, self._current_t0
        self._current, self._current_t0 = name, time.monotonic()
        if self._narrate_stages:
            self._emit(f"{name} started ({self._elapsed():.0f}s total)")
        try:
            yield
        finally:
            took = time.monotonic() - self._current_t0
            self._stages[name] = self._stages.get(name, 0.0) + took
            if self._narrate_stages:
                self._emit(
                    f"{name} finished in {took:.1f}s ({self._elapsed():.0f}s total)"
                )
            self._current, self._current_t0 = previous, previous_t0

    def report(self) -> dict[str, Any]:
        """The envelope's ``timing`` object.

        ``stages`` is per-stage wall seconds, ``total_seconds`` the whole
        run, and ``unattributed_seconds`` the remainder no named stage covers
        (report assembly, parasitics injection and the like) so the three
        always reconcile.
        """
        total = self._elapsed()
        return {
            "stages": {name: round(sec, 3) for name, sec in self._stages.items()},
            "total_seconds": round(total, 3),
            "unattributed_seconds": round(
                max(total - sum(self._stages.values()), 0.0), 3
            ),
        }


@contextlib.contextmanager
def active_timer(timer: StageTimer) -> Iterator[StageTimer]:
    """Install ``timer`` as the process-wide active timer for the block."""
    global _active
    previous = _active
    _active = timer
    timer.start()
    try:
        yield timer
    finally:
        timer.close()
        _active = previous


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Open stage ``name`` on the active timer; a no-op without one."""
    timer = _active
    if timer is None:
        yield
        return
    with timer.stage(name):
        yield
