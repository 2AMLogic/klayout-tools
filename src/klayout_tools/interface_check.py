"""Check that two separately generated blocks *mate*: the labelled pins of
block A land on the pads of block B at a declared placement (issue #4024).

Pure library: :func:`run_interface_check` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints, mirroring ``socket_check.py``.

Headless invariant: uses only the pip ``klayout`` package's batch database API
(``klayout.db``) -- no GUI, no Qt.

Model: each stream contributes text labels whose string fully matches the
``--pins`` regex. A label's *pad* is the merged polygon on the pin layer that
contains the label point (edge-touching counts), or the nearest one by
bounding-box distance when none contains it. Both streams are flattened; B is
translated by ``(dx, dy)`` micrometres into A's frame in memory -- no assembled
layout is ever written. Labels are paired by identical string; a pin is
``ok`` when the two pads' bounding boxes overlap or sit within ``tol_um`` of
each other, ``misaligned`` when they do not, and ``no_partner`` when the
label exists in only one stream or has no pad geometry on its pin layer.

Positions and sizes are reported in micrometres in A's frame. A and B may use
different database units.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ._layout import load_layout, resolve_top_cell

#: Pin statuses, in the order the summary counts them.
STATUSES: tuple[str, ...] = ("ok", "misaligned", "no_partner")

# Rounding applied to every reported micrometre value: far below any real dbu
# yet removes float noise from dbu * float products.
_UM_DIGITS = 6


class InterfaceCheckError(Exception):
    """Raised when the interface cannot be checked: unreadable stream, bad
    ``--place`` / ``--pins`` / layer argument, or no matching pin in either
    stream. The CLI turns this into a clean stderr message + exit code 1."""


def parse_place(spec: str) -> tuple[float, float]:
    """Parse ``"dx,dy"`` (micrometres; optional surrounding parentheses)."""
    text = spec.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    parts = [part.strip() for part in text.split(",")]
    try:
        if len(parts) != 2:
            raise ValueError
        dx, dy = float(parts[0]), float(parts[1])
    except ValueError:
        raise InterfaceCheckError(
            f"--place must look like 'dx,dy' (micrometres), got: {spec}"
        ) from None
    if not (math.isfinite(dx) and math.isfinite(dy)):
        raise InterfaceCheckError(f"--place must be finite, got: {spec}")
    return dx, dy


def parse_layer(spec: str, flag: str) -> tuple[int, int]:
    """Parse ``"L/D"`` into a ``(layer, datatype)`` pair."""
    try:
        layer, datatype = spec.split("/")
        return int(layer), int(datatype)
    except ValueError:
        raise InterfaceCheckError(f"{flag} must look like L/D, got: {spec}") from None


def run_interface_check(
    a_path: str,
    b_path: str,
    place: tuple[float, float],
    pins: str,
    *,
    pin_layer_a: tuple[int, int] | None = None,
    pin_layer_b: tuple[int, int] | None = None,
    tol_um: float = 0.0,
    top_a: str | None = None,
    top_b: str | None = None,
) -> dict[str, Any]:
    """Check that the pins of ``a_path`` mate with those of ``b_path`` when B
    is placed at ``place`` (micrometres, in A's frame).

    ``pins`` is a regex that must fully match a label string. ``pin_layer_a``
    / ``pin_layer_b`` give the layer the pads are drawn on; when ``None`` a
    label's own layer is used (labels drawn on the pad layer itself).

    Returns a dict::

        {
            "schema_version": 1,
            "a": <a_path>, "b": <b_path>,
            "place_um": [dx, dy],
            "pins_pattern": <pins>,
            "tolerance_um": <tol_um>,
            "dbu_um": {"a": <float>, "b": <float>},
            "status": "pass" | "fail",
            "pin_count": <int>, "ok_count": ..., "misaligned_count": ...,
            "no_partner_count": ...,
            "pins": [
                {"name", "status", "offset_um": [dx, dy] | None,
                 "gap_um": <float> | None,
                 "a": {"layer", "label_um", "pad_bbox_um"} | None,
                 "b": {...} | None, "reason": <str> | None},
                ...
            ],
        }

    A pin name that occurs several times in one stream is paired by
    occurrence, ordered bottom-to-top then left-to-right in A's frame; the
    occurrence index is appended as ``name#k`` (k >= 1) only for such names.
    """
    if tol_um < 0 or not math.isfinite(tol_um):
        raise InterfaceCheckError(f"--tol-um must be >= 0, got: {tol_um}")
    try:
        pattern = re.compile(pins)
    except re.error as exc:
        raise InterfaceCheckError(f"--pins is not a valid regex: {exc}") from None

    layout_a = load_layout(a_path, InterfaceCheckError)
    layout_b = load_layout(b_path, InterfaceCheckError)
    cell_a = resolve_top_cell(layout_a, top_a, InterfaceCheckError, path=a_path)
    cell_b = resolve_top_cell(layout_b, top_b, InterfaceCheckError, path=b_path)

    side_a = _collect_side(layout_a, cell_a, pattern, pin_layer_a, (0.0, 0.0))
    side_b = _collect_side(layout_b, cell_b, pattern, pin_layer_b, place)

    if not side_a and not side_b:
        raise InterfaceCheckError(f"--pins {pins!r} matches no label in either stream")

    entries = [
        _pin_entry(name, side_a.get(name), side_b.get(name), tol_um)
        for name in sorted(set(side_a) | set(side_b), key=_natural_key)
    ]
    counts = {
        status: sum(1 for e in entries if e["status"] == status) for status in STATUSES
    }
    return {
        "schema_version": 1,
        "a": a_path,
        "b": b_path,
        "place_um": [place[0], place[1]],
        "pins_pattern": pins,
        "tolerance_um": tol_um,
        "dbu_um": {"a": layout_a.dbu, "b": layout_b.dbu},
        "status": "pass" if counts["ok"] == len(entries) else "fail",
        "pin_count": len(entries),
        "ok_count": counts["ok"],
        "misaligned_count": counts["misaligned"],
        "no_partner_count": counts["no_partner"],
        "pins": entries,
    }


def _natural_key(name: str) -> list[Any]:
    """Sort ``OUT[2]`` before ``OUT[10]``."""
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", name)]


def _round(value: float) -> float:
    return round(value, _UM_DIGITS) + 0.0  # + 0.0 turns -0.0 into 0.0


def _collect_side(
    layout: Any,
    cell: Any,
    pattern: re.Pattern[str],
    pin_layer: tuple[int, int] | None,
    shift: tuple[float, float],
) -> dict[str, dict[str, Any]]:
    """``{pin name: {"layer", "label", "pad"}}`` for one stream, in A's frame.

    ``label`` is ``(x, y)`` um; ``pad`` is the pad's ``(l, b, r, t)`` bbox um
    or ``None`` when the pin layer carries no geometry.
    """
    import klayout.db as kdb

    dbu = layout.dbu
    labels: list[tuple[str, tuple[int, int], float, float]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        for text in kdb.Texts(cell.begin_shapes_rec(layer_index)).each():
            if pattern.fullmatch(text.string):
                labels.append(
                    (
                        text.string,
                        (info.layer, info.datatype),
                        text.x * dbu + shift[0],
                        text.y * dbu + shift[1],
                    )
                )

    pads_by_layer: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}

    def pads(layer: tuple[int, int]) -> list[tuple[float, float, float, float]]:
        if layer not in pads_by_layer:
            layer_index = layout.find_layer(*layer)
            region = (
                kdb.Region(cell.begin_shapes_rec(layer_index)).merged()
                if layer_index is not None
                else kdb.Region()
            )
            pads_by_layer[layer] = [
                (
                    poly.bbox().left * dbu + shift[0],
                    poly.bbox().bottom * dbu + shift[1],
                    poly.bbox().right * dbu + shift[0],
                    poly.bbox().top * dbu + shift[1],
                )
                for poly in region.each()
            ]
        return pads_by_layer[layer]

    by_name: dict[str, list[dict[str, Any]]] = {}
    for name, label_layer, x, y in sorted(labels, key=lambda t: (t[3], t[2], t[0])):
        layer = pin_layer if pin_layer is not None else label_layer
        by_name.setdefault(name, []).append(
            {"layer": layer, "label": (x, y), "pad": _pick_pad(pads(layer), x, y)}
        )

    side: dict[str, dict[str, Any]] = {}
    for name, items in by_name.items():
        for index, item in enumerate(items, start=1):
            side[name if len(items) == 1 else f"{name}#{index}"] = item
    return side


def _box_distance(box: tuple[float, float, float, float], x: float, y: float) -> float:
    left, bottom, right, top = box
    return math.hypot(max(left - x, 0.0, x - right), max(bottom - y, 0.0, y - top))


def _pick_pad(
    pad_boxes: list[tuple[float, float, float, float]], x: float, y: float
) -> tuple[float, float, float, float] | None:
    """The pad containing ``(x, y)`` (edge counts), else the nearest by
    bounding-box distance; ``None`` when the layer has no geometry. Ties go to
    the lowest-left pad, so the result is deterministic."""
    if not pad_boxes:
        return None
    eps = 1e-9
    best = min(
        pad_boxes,
        key=lambda box: (round(_box_distance(box, x, y) / eps), box[1], box[0]),
    )
    return best


def _side_report(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    pad = item["pad"]
    return {
        "layer": f"{item['layer'][0]}/{item['layer'][1]}",
        "label_um": [_round(item["label"][0]), _round(item["label"][1])],
        "pad_bbox_um": None if pad is None else [_round(v) for v in pad],
    }


def _pin_entry(
    name: str,
    a: dict[str, Any] | None,
    b: dict[str, Any] | None,
    tol_um: float,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "status": "no_partner",
        "offset_um": None,
        "gap_um": None,
        "a": _side_report(a),
        "b": _side_report(b),
        "reason": None,
    }
    if a is None or b is None:
        entry["reason"] = "label only in stream " + ("B" if a is None else "A")
        return entry
    if a["pad"] is None or b["pad"] is None:
        missing = [s for s, item in (("A", a), ("B", b)) if item["pad"] is None]
        entry["reason"] = "no pad geometry on the pin layer in stream " + "+".join(
            missing
        )
        return entry

    la, ba, ra, ta = a["pad"]
    lb, bb, rb, tb = b["pad"]
    gap = math.hypot(max(lb - ra, la - rb, 0.0), max(bb - ta, ba - tb, 0.0))
    entry["offset_um"] = [
        _round((lb + rb) / 2 - (la + ra) / 2),
        _round((bb + tb) / 2 - (ba + ta) / 2),
    ]
    entry["gap_um"] = _round(gap)
    # Compare unrounded gap with a sub-dbu epsilon so a gap of exactly tol
    # (e.g. 0.1 + float noise) is not flagged.
    entry["status"] = "ok" if gap <= tol_um + 1e-9 else "misaligned"
    return entry
