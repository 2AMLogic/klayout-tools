"""Compare declared and measured block footprints in the composed coordinate frame."""

from __future__ import annotations

import math
from fractions import Fraction
from itertools import combinations
from typing import Any

from .gen_compose_base import GenComposeError

# Only measurements carry database-unit quantization error.
_BBOX_REALITY_TOLERANCE_UM = 1e-4


def _outside_edges(outer: dict[str, float], inner: dict[str, float]) -> set[str]:
    """Measured edges beyond the advisory's inclusive containment tolerance."""
    tol = _BBOX_REALITY_TOLERANCE_UM
    return {
        side
        for side, contained in (
            ("left", inner["x0"] >= outer["x0"] - tol),
            ("right", inner["x1"] <= outer["x1"] + tol),
            ("bottom", inner["y0"] >= outer["y0"] - tol),
            ("top", inner["y1"] <= outer["y1"] + tol),
        )
        if not contained
    }


def _bbox_contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return not _outside_edges(outer, inner)


def _move_box(box: dict[str, float], x: Fraction, y: Fraction) -> dict[str, float]:
    return {
        edge: float(Fraction(str(value)) + (x if edge.startswith("x") else y))
        for edge, value in box.items()
    }


def _placed_gate_footprints(
    order: list[str],
    declared: dict[str, dict[str, float]],
    measured: dict[str, dict[str, float]],
    offsets: dict[str, dict[str, float]],
    targets: dict[str, dict[str, float]] | None,
    row_spacing: float | None,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Translate both footprints from their local frames for the strict gate.

    JSON dimensions are decimal micrometres. Keep their specified edges exact
    while anchoring to a row neighbor or a bbox target. Rounding the derived
    offset before adding it back can turn an abutment into an intersection.
    Array placements check the base tile at its origin.
    """
    placed, drawn = {}, {}
    right = None
    for name in order:
        box = _require_gate_bbox(
            declared[name], f"declared footprint of block '{name}'"
        )
        if targets is not None:
            x = Fraction(str(targets[name]["x"])) - Fraction(str(box["x0"]))
            y = Fraction(str(targets[name]["y"])) - Fraction(str(box["y0"]))
        elif row_spacing is not None and right is not None:
            x = right + Fraction(str(row_spacing)) - Fraction(str(box["x0"]))
            y = Fraction(0)
        else:
            x = Fraction(str(offsets[name]["x"]))
            y = Fraction(str(offsets[name]["y"]))
        right = Fraction(str(box["x1"])) + x
        placed[name] = _move_box(box, x, y)
        if name in measured:
            drawn[name] = _move_box(measured[name], x, y)
    return placed, drawn


def check_composed_footprints(
    order: list[str],
    declared: dict[str, dict[str, float]],
    measured: dict[str, dict[str, float]],
    offsets: dict[str, dict[str, float]],
    targets: dict[str, dict[str, float]] | None,
    row_spacing: float | None,
) -> dict[str, dict[str, float]]:
    """Check the placement and return placed measurements for the advisory."""
    try:
        placed, drawn = _placed_gate_footprints(
            order, declared, measured, offsets, targets, row_spacing
        )
        failures = check_placement_overlap(placed, drawn)
    except (ValueError, OverflowError) as exc:
        raise GenComposeError(f"options.check_placement failed: {exc}") from exc
    if failures:
        raise GenComposeError("options.check_placement failed: " + "; ".join(failures))
    return drawn


def _require_gate_bbox(value: Any, where: str) -> dict[str, float]:
    """Validate one caller-supplied ``{x0, y0, x1, y1}`` box (um) for
    :func:`check_placement_overlap`, raising :class:`ValueError` on a
    malformed shape.

    The gate is a library entry point for merge scripts with no request
    document at all, so its input errors are plain ``ValueError`` s rather
    than :class:`GenComposeError`. Compose translates them at its gate boundary."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object with x0/y0/x1/y1 (um)")
    missing = [key for key in ("x0", "y0", "x1", "y1") if key not in value]
    if missing:
        raise ValueError(f"{where} is missing field(s): {', '.join(missing)}")
    box: dict[str, float] = {}
    for key in ("x0", "y0", "x1", "y1"):
        raw = value[key]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(raw)
        ):
            raise ValueError(f"{where}.{key} must be a finite number (um)")
        box[key] = float(raw)
    if box["x1"] < box["x0"] or box["y1"] < box["y0"]:
        raise ValueError(f"{where} must have x1 >= x0 and y1 >= y0")
    return box


def _format_coordinate(value: float) -> str:
    """Keep three decimals on grid coordinates and preserve finer differences."""
    rounded = f"{value:.3f}"
    return rounded if float(rounded) == value else repr(value)


def _format_bbox(box: dict[str, float]) -> str:
    x0, y0, x1, y1 = (_format_coordinate(box[key]) for key in ("x0", "y0", "x1", "y1"))
    return f"({x0}, {y0})-({x1}, {y1})"


def _edge_deltas(
    declared: dict[str, float], measured: dict[str, float]
) -> dict[str, float]:
    """Return signed outward movement of each measured edge in micrometres."""
    return {
        "left": declared["x0"] - measured["x0"],
        "right": measured["x1"] - declared["x1"],
        "bottom": declared["y0"] - measured["y0"],
        "top": measured["y1"] - declared["y1"],
    }


def _describe_deltas(deltas: dict[str, float]) -> str:
    parts = []
    for side, delta in deltas.items():
        # Keep a sub-grid displacement visible even when it rounds to zero at 1 nm.
        value = f"{delta:.12g}" if abs(delta) < 0.001 else f"{delta:.3f}"
        parts.append(f"{side} {value}um")
    return ", ".join(parts)


def _intersection(a: dict[str, float], b: dict[str, float]) -> dict[str, float] | None:
    overlap = {
        "x0": max(a["x0"], b["x0"]),
        "y0": max(a["y0"], b["y0"]),
        "x1": min(a["x1"], b["x1"]),
        "y1": min(a["y1"], b["y1"]),
    }
    if overlap["x0"] >= overlap["x1"] or overlap["y0"] >= overlap["y1"]:
        return None
    return overlap


def check_placement_overlap(
    declared_um: dict[str, dict[str, float]],
    measured_um: dict[str, dict[str, float]] | None = None,
) -> list[str]:
    """Return positive-area intersections and measured outward growth beyond tolerance.

    Both mappings use block IDs and x0/y0/x1/y1 rectangles in placed micrometres.
    A measured rectangle is optional for each declared block. Invalid input raises
    ValueError. Pair failures also list signed measured edge deltas for both blocks.
    """
    declared = {}
    for block_id, raw_box in declared_um.items():
        if not isinstance(block_id, str) or not block_id:
            raise ValueError("block ids must be non-empty strings")
        declared[block_id] = _require_gate_bbox(
            raw_box, f"declared footprint of block '{block_id}'"
        )

    pair_details = {}
    growth_failures = []
    for block_id, raw_box in (measured_um or {}).items():
        if block_id not in declared:
            raise ValueError(f"measured footprint given for unknown block '{block_id}'")
        measured = _require_gate_bbox(
            raw_box, f"measured footprint of block '{block_id}'"
        )
        box = declared[block_id]
        deltas = _edge_deltas(box, measured)
        changes = {side: delta for side, delta in deltas.items() if delta}
        if changes:
            pair_details[block_id] = (
                f"; '{block_id}' measured {_format_bbox(measured)} um, declared "
                f"{_format_bbox(box)} um, edge deltas: {_describe_deltas(changes)}"
            )
        outside = _outside_edges(box, measured)
        growth = {side: delta for side, delta in deltas.items() if side in outside}
        if growth:
            growth_failures.append(
                f"block '{block_id}' measured footprint {_format_bbox(measured)} um "
                f"exceeds its declared bbox_um {_format_bbox(box)} um -- grew past it "
                f"by {_describe_deltas(growth)}"
            )

    pair_failures = []
    for first, second in combinations(declared, 2):
        overlap = _intersection(declared[first], declared[second])
        if overlap is not None:
            pair_failures.append(
                f"blocks '{first}' and '{second}' overlap: declared footprints "
                f"intersect over {_format_bbox(overlap)} um"
                + pair_details.get(first, "")
                + pair_details.get(second, "")
            )
    return pair_failures + growth_failures
