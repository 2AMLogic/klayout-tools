"""Best-effort per-unit engine peak RSS for ``klt sim`` (issue #4867).

``RUSAGE_CHILDREN`` only exposes a process-wide *maximum*, so a unit's peak
is attributable only when it raised that high-water mark. A unit that peaked
below an earlier one (or ran beside a larger sibling) reports nothing and the
``peak_rss_bytes`` report field is omitted rather than guessed.
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
    after = children_max_rss_bytes()
    if before is None or after is None or after <= before:
        return None
    return after


class RunWatch:
    """Wall-clock and peak-RSS baseline taken when a unit's engine launches."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._rss_before = children_max_rss_bytes()
        self._peak: int | None = None

    def elapsed(self) -> float:
        """Seconds since launch; also snapshots the peak RSS at this point."""
        self._peak = peak_rss_since(self._rss_before)
        return time.monotonic() - self._started

    def report_field(self) -> dict[str, Any]:
        """``{"peak_rss_bytes": n}`` when attributable, else ``{}``."""
        return {"peak_rss_bytes": self._peak} if self._peak else {}
