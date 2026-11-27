"""Best-effort per-unit engine peak RSS for ``klt sim`` (issue #4867).

``RUSAGE_CHILDREN`` exposes a process-wide maximum. A unit reports
``peak_rss_bytes`` only when it runs in isolation and raises that mark.
"""

from __future__ import annotations

import sys
import time
from typing import Any


def children_max_rss_bytes() -> int | None:
    """High-water RSS of all waited-for child processes, in bytes, or
    ``None`` where :mod:`resource` is unavailable."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (ImportError, OSError, ValueError):
        return None
    # ru_maxrss is KiB on Linux, bytes on macOS.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def peak_rss_since(before: int | None) -> int | None:
    """This unit's engine peak RSS, or ``None`` when not attributable."""
    if before is None:
        return None
    after = children_max_rss_bytes()
    if after is None or after <= before:
        return None
    return after


class RunWatch:
    """Wall-clock and peak-RSS baseline taken when a unit's engine launches."""

    def __init__(self, *, measure_peak_rss: bool = True) -> None:
        self._started = time.monotonic()
        self._rss_before = children_max_rss_bytes() if measure_peak_rss else None
        self._peak: int | None = None

    def elapsed(self) -> float:
        """Seconds since launch; also snapshots the peak RSS at this point."""
        self._peak = peak_rss_since(self._rss_before)
        return time.monotonic() - self._started

    def report_field(self) -> dict[str, Any]:
        """``{"peak_rss_bytes": n}`` when attributable, else ``{}``."""
        return {"peak_rss_bytes": self._peak} if self._peak else {}
