"""Validation of the optional ``region_um`` / ``order`` keys on instance groups.

Split out of ``place_and_route_request`` to keep that module under its
per-file size ceiling.  The request module passes its own error class in so
this module does not import it (which would be circular).
"""

from __future__ import annotations

import math
from typing import Any

_GROUP_KEYS = frozenset({"name", "instances", "max_extent_um", "region_um", "order"})


def _is_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _check_region_um(region: Any, where: str, error: type[Exception]) -> None:
    if not (
        isinstance(region, list)
        and len(region) == 4
        and all(_is_finite_number(v) for v in region)
        and region[0] < region[2]
        and region[1] < region[3]
    ):
        raise error(
            f"{where}.region_um must be [x0, y0, x1, y1] finite numbers "
            "with x0 < x1 and y0 < y1"
        )


def _check_group_order(
    order: Any, instances: list[str], where: str, error: type[Exception]
) -> dict[str, Any]:
    if not isinstance(order, dict):
        raise error(f"{where}.order must be a JSON object")
    unknown = sorted(set(order) - {"axis", "pitch_um"})
    if unknown:
        raise error(f"{where}.order has unknown key(s): {', '.join(unknown)}")
    axis = order.get("axis")
    if axis not in ("x", "y"):
        raise error(f"{where}.order.axis must be 'x' or 'y'")
    result: dict[str, Any] = {"axis": axis}
    pitch = order.get("pitch_um")
    if pitch is not None:
        if not _is_finite_number(pitch) or pitch <= 0:
            raise error(f"{where}.order.pitch_um must be a number > 0")
        result["pitch_um"] = pitch
    if any(ch in name for name in instances for ch in "*?"):
        raise error(f"{where}.order requires exact instance names (no glob patterns)")
    return result


def validate_group_geometry(
    entry: dict[str, Any], instances: list[str], where: str, error: type[Exception]
) -> dict[str, Any]:
    """Return the validated ``region_um`` / ``order`` items present in *entry*."""
    item: dict[str, Any] = {}
    region = entry.get("region_um")
    if region is not None:
        _check_region_um(region, where, error)
        item["region_um"] = list(region)
    order = entry.get("order")
    if order is not None:
        item["order"] = _check_group_order(order, instances, where, error)
    return item


def reject_unknown_group_keys(
    entry: dict[str, Any], where: str, error: type[Exception]
) -> None:
    """Raise *error* when *entry* carries a key outside the group vocabulary."""
    unknown = sorted(set(entry) - _GROUP_KEYS)
    if unknown:
        raise error(f"{where} has unknown key(s): {', '.join(unknown)}")
