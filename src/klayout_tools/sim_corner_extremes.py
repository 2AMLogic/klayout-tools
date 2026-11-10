"""Per-measurement corner-matrix helpers for ``sim._rollup_measurements``.

Split out of ``sim.py`` (issue #5221) so the rollup stays under the
complexity and file-size ratchets.
"""

from __future__ import annotations

from typing import Any

_ABSENT_STATUS = "absent"

CornerEntry = tuple[dict[str, Any], dict[str, Any]]


def corner_entries(name: str, corners: list[dict[str, Any]]) -> list[CornerEntry]:
    """``(corner, measurement)`` pairs for the first measurement called
    ``name`` in each corner (corners lacking it are skipped)."""
    entries: list[CornerEntry] = []
    for corner in corners:
        for m in corner["measurements"]:
            if m["name"] == name:
                entries.append((corner, m))
                break
    return entries


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
    )


def corner_extremes(entries: list[CornerEntry]) -> dict[str, Any]:
    """``{"min": ..., "max": ...}``: the ``{corner_id, value}`` of the true
    extremes over finite numeric, non-absent values (ties keep the first
    corner); ``None`` for both when no corner has one.

    ``worst_case`` is a margin pointer and, for a measurement with no
    ``limits`` (``margin`` always ``None``), is just the first corner --
    these are the actual extremes (issue #5221).
    """
    lo: dict[str, Any] | None = None
    hi: dict[str, Any] | None = None
    for corner, m in entries:
        value = m.get("value")
        if m["status"] == _ABSENT_STATUS or not _is_finite_number(value):
            continue
        point = {"corner_id": corner["corner_id"], "value": value}
        if lo is None or value < lo["value"]:
            lo = point
        if hi is None or value > hi["value"]:
            hi = point
    return {"min": lo, "max": hi}
