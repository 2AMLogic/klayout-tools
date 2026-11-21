"""Windowed drawn-area tiling for ``klt stats --density-window``.

Split out of :mod:`klayout_tools.stats` (issue #5551) to keep that module
under the repository's per-file line limit; the tiling behaviour is unchanged.

Headless invariant: pure ``klayout.db`` geometry via the ``kdb`` handle the
caller already imported -- no GUI, no Qt.
"""

from __future__ import annotations

from typing import Any

from ._layout import bbox_um_dict

#: Upper bound on windows per layer for ``--density-window``; a finer grid is
#: refused rather than emitting unbounded JSON.
MAX_DENSITY_WINDOWS = 10_000


def density_windows(
    region: Any, bbox: Any, window_dbu: int, dbu: float, kdb: Any
) -> dict[str, Any]:
    """Tile ``bbox`` with ``window_dbu`` squares and report drawn fraction.

    Windows start at the bbox left/bottom; edge windows are clipped to the
    bbox and judged against their clipped area. Ordered by ``(iy, ix)``.
    """
    windows: list[dict[str, Any]] = []
    iy = 0
    y = bbox.bottom
    while y < bbox.top:
        y2 = min(y + window_dbu, bbox.top)
        ix = 0
        x = bbox.left
        while x < bbox.right:
            x2 = min(x + window_dbu, bbox.right)
            box = kdb.Box(x, y, x2, y2)
            area_dbu2 = (region & kdb.Region(box)).area()
            windows.append(
                {
                    "ix": ix,
                    "iy": iy,
                    "bbox_um": bbox_um_dict(box, dbu),
                    "area_um2": area_dbu2 * dbu * dbu,
                    "fraction": area_dbu2 / (box.width() * box.height()),
                }
            )
            ix += 1
            x = x2
        iy += 1
        y = y2
    fractions = [w["fraction"] for w in windows]
    low = min(windows, key=lambda w: w["fraction"])
    high = max(windows, key=lambda w: w["fraction"])
    return {
        "windows": windows,
        "summary": {
            "min": low["fraction"],
            "max": high["fraction"],
            "mean": sum(fractions) / len(fractions),
            "min_window": {"ix": low["ix"], "iy": low["iy"]},
            "max_window": {"ix": high["ix"], "iy": high["iy"]},
        },
    }
