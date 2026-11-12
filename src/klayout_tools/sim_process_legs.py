"""Identical-process-leg check and per-corner value table (issue #5385).

Split out of ``sim.py`` so the report assembly stays under the complexity
and file-size ratchets.

``process_leg_check`` answers "did the other ``corners.process`` legs change
anything?": legs whose finite measurement values are bit-identical at every
shared (supply, temperature, params) point may never have been applied. It is
a warning, never a failure -- some legs legitimately coincide.
"""

from __future__ import annotations

import json
import re
from typing import Any

PROCESS_LEG_CHECK_CODE = "process_leg_check"

_ABSENT_STATUS = "absent"
_MC_SUFFIX = re.compile(r"/mc(\d+)$")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


def _point_key(corner: dict[str, Any]) -> str:
    """Everything that identifies a corner except its process leg. A Monte
    Carlo ``/mcN`` sample groups with the same-index sample of other legs."""
    match = _MC_SUFFIX.search(str(corner.get("corner_id", "")))
    return json.dumps(
        [
            sorted((corner.get("supply_v") or {}).items()),
            corner.get("temperature_c"),
            sorted((corner.get("params") or {}).items()),
            match.group(1) if match else None,
        ],
        sort_keys=True,
        default=str,
    )


def _signature(corner: dict[str, Any]) -> tuple[tuple[str, float], ...]:
    return tuple(
        sorted(
            (m["name"], m["value"])
            for m in corner.get("measurements") or []
            if m.get("status") != _ABSENT_STATUS and _is_finite_number(m.get("value"))
        )
    )


def process_leg_check(corners: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A warning-severity diagnostic naming process legs with identical
    results, or ``None`` (one leg, legs differ, or nothing finite to compare)."""
    by_leg: dict[str, dict[str, tuple[tuple[str, float], ...]]] = {}
    for corner in corners:
        leg = corner.get("process")
        if leg is None:
            continue
        by_leg.setdefault(leg, {})[_point_key(corner)] = _signature(corner)
    if len(by_leg) < 2:
        return None

    groups: dict[tuple[Any, ...], list[str]] = {}
    for leg, points in by_leg.items():
        if not any(points.values()):
            continue  # no finite values: nothing to compare
        groups.setdefault(tuple(sorted(points.items())), []).append(leg)
    identical = [sorted(legs) for legs in groups.values() if len(legs) > 1]
    if not identical:
        return None
    identical.sort()
    described = "; ".join(", ".join(legs) for legs in identical)
    return {
        "code": PROCESS_LEG_CHECK_CODE,
        "severity": "warning",
        "message": (
            f"process legs produced bit-identical values for every finite "
            f"measurement at each supply/temperature: {described}. They may "
            "not have been applied (check each leg's sections/libs); ignore "
            "if the legs legitimately coincide."
        ),
        "legs": identical,
    }


def value_table(
    measurement: str, corners: list[dict[str, Any]]
) -> list[tuple[str, Any]]:
    """``(corner_id, value)`` rows for one measurement, in corner order."""
    rows: list[tuple[str, Any]] = []
    for corner in corners:
        for m in corner.get("measurements") or []:
            if m.get("name") == measurement:
                rows.append((corner["corner_id"], m.get("value")))
                break
    return rows


def attach_process_leg_check(
    report: dict[str, Any], corners: list[dict[str, Any]]
) -> None:
    """Append the ``process_leg_check`` warning to ``report["diagnostics"]``."""
    diag = process_leg_check(corners)
    if diag:
        report.setdefault("diagnostics", []).append(diag)


def print_value_table(measurement: str, corners: list[dict[str, Any]]) -> None:
    """Text-mode per-corner value rows (multi-corner runs only)."""
    if len(corners) > 1:
        for cid, val in value_table(measurement, corners):
            print(f"    {cid}: {val!r}")
