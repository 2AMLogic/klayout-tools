"""Assert structural regularity of a cell array: mirror/translation symmetry
layer by layer, and abutment of the unit-cell placements at a declared pitch.

Pure library: :func:`run_array_check` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring
``keepout_check.py``. Serialisation and human-readable formatting live in the
CLI command module (``cli/array_check_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database API
(``klayout.db``) -- ``Region``'s native boolean/sizing primitives.

Why a distinct verb: ``klt diff`` compares two streams and ``klt stats``
reports a bounding box; neither can assert that one cell is its own mirror
image, or that N placements of a unit cell tile a line with no gap or overlap.
Like ``klt keepout-check`` this emits the shared ``violations[]`` shape.

Semantics:

- **Symmetry** flattens each checked root (``cell`` if given, else every top
  cell) per layer, transforms the merged region (mirror about a line, or a
  translation) and XORs it with the original. Each residual polygon is a
  violation. ``mirror_axis="x"`` mirrors about the *vertical* line ``x = at_um``
  (x -> 2*at - x); ``"y"`` about the horizontal line ``y = at_um``. For a
  translation the XOR is taken only inside the overlap of the root's bounding
  box and its translated copy, since nothing can be compared outside it.
  ``tolerance_um`` ignores residual features narrower than ``2 * tolerance_um``.
- **Abutment** collects the bounding box of every placement of the unit
  ``cell`` under each top cell (placement transforms applied), sorts them along
  ``direction`` and checks consecutive pairs touch exactly (no gap, no overlap),
  advance by ``pitch_um``, that there are ``count`` of them, and that the union
  extent equals ``count * pitch_um``. The pitch is rounded to the database grid.
"""

from __future__ import annotations

import math
from typing import Any

from ._layout import bbox_um_dict, load_layout, select_top_cells

#: Stable rule ids carried by the violations this module emits.
RULE_SYMMETRY = "array.symmetry_residual"
RULE_ABUTMENT = "array.abutment"

CHECK_SYMMETRY = "symmetry"
CHECK_ABUTMENT = "abutment"


class ArrayCheckError(Exception):
    """Raised when an array check cannot even be attempted: a missing or
    unreadable layout, a request selecting no check, an empty layer set, a bad
    pitch/count/tolerance, or an unknown cell. The CLI turns this into a clean
    stderr message plus exit code 1. Distinct from a documented
    ``status: "violated"`` response, which is a trustworthy verdict."""


def _finite(value: Any, flag: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArrayCheckError(f"{flag} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ArrayCheckError(f"{flag} must be finite, got {value!r}")
    return float(value)


def _validate_symmetry(
    layers: list[tuple[int, int]] | None,
    mirror_axis: str | None,
    at_um: float | None,
    translate_um: tuple[float, float] | None,
) -> None:
    if mirror_axis is not None and translate_um is not None:
        raise ArrayCheckError("--mirror-axis and --translate are exclusive")
    if not layers:
        raise ArrayCheckError("layer set is empty: at least one layer required")
    if mirror_axis is None:
        if at_um is not None:
            raise ArrayCheckError("--at is only valid with --mirror-axis")
        dx, dy = translate_um  # type: ignore[misc]
        _finite(dx, "--translate")
        _finite(dy, "--translate")
        if dx == 0 and dy == 0:
            raise ArrayCheckError("--translate must be a non-zero vector")
        return
    if mirror_axis not in ("x", "y"):
        raise ArrayCheckError(f"--mirror-axis must be 'x' or 'y', got {mirror_axis!r}")
    if at_um is None:
        raise ArrayCheckError("--mirror-axis requires --at <um>")
    _finite(at_um, "--at")


def _validate_abutment(
    pitch_um: float | None, count: int | None, direction: str, cell: str | None
) -> None:
    if pitch_um is None or count is None:
        raise ArrayCheckError("abutment needs both --pitch and --count")
    if _finite(pitch_um, "--pitch") <= 0:
        raise ArrayCheckError(f"--pitch must be > 0 um, got {pitch_um}")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ArrayCheckError(f"--count must be an integer >= 1, got {count!r}")
    if direction not in ("x", "y"):
        raise ArrayCheckError(f"--direction must be 'x' or 'y', got {direction!r}")
    if not cell:
        raise ArrayCheckError("abutment needs --cell (the unit cell)")


def _validate_request(
    layers: list[tuple[int, int]] | None,
    mirror_axis: str | None,
    at_um: float | None,
    translate_um: tuple[float, float] | None,
    tolerance_um: float,
    pitch_um: float | None,
    count: int | None,
    direction: str,
    cell: str | None,
) -> None:
    symmetry = mirror_axis is not None or translate_um is not None
    abutment = pitch_um is not None or count is not None
    if not symmetry and not abutment:
        raise ArrayCheckError(
            "no check requested: pass --mirror-axis/--at, --translate and/or "
            "--pitch/--count"
        )
    if _finite(tolerance_um, "--tolerance") < 0:
        raise ArrayCheckError(f"--tolerance must be >= 0 um, got {tolerance_um}")
    if symmetry:
        _validate_symmetry(layers, mirror_axis, at_um, translate_um)
    if abutment:
        _validate_abutment(pitch_um, count, direction, cell)


def run_array_check(
    layout_path: str,
    layers: list[tuple[int, int]] | None = None,
    *,
    top: str | None = None,
    cell: str | None = None,
    mirror_axis: str | None = None,
    at_um: float | None = None,
    translate_um: tuple[float, float] | None = None,
    tolerance_um: float = 0.0,
    pitch_um: float | None = None,
    count: int | None = None,
    direction: str = "x",
) -> dict[str, Any]:
    """Run the symmetry and/or abutment checks selected by the arguments.

    Returns a dict matching ``docs/cli/array-check.md``: the request echoed
    back, ``dbu_um``, ``status`` (``"clean"`` | ``"violated"``),
    ``violation_count`` and ``violations`` (sorted deterministically).
    """
    _validate_request(
        layers,
        mirror_axis,
        at_um,
        translate_um,
        tolerance_um,
        pitch_um,
        count,
        direction,
        cell,
    )
    layout = load_layout(layout_path, ArrayCheckError)
    if cell is not None and layout.cell(cell) is None:
        raise ArrayCheckError(f"unknown cell in --cell: {cell!r}")

    import klayout.db as kdb

    dbu = layout.dbu
    tops = select_top_cells(layout, top, ArrayCheckError)
    violations: list[dict[str, Any]] = []

    if mirror_axis is not None or translate_um is not None:
        roots = [layout.cell(cell)] if cell is not None else tops
        for root in roots:
            violations.extend(
                _symmetry_violations(
                    kdb,
                    layout,
                    root,
                    layers or [],
                    mirror_axis,
                    at_um,
                    translate_um,
                    tolerance_um,
                )
            )
    if pitch_um is not None:
        violations.extend(
            _abutment_violations(
                kdb, layout, tops, cell, pitch_um, count, direction, tolerance_um
            )
        )

    violations.sort(
        key=lambda v: (
            v["check"],
            v["cell"],
            v["layer"] or "",
            v["kind"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )
    return {
        "schema_version": 1,
        "file": layout_path,
        "layers": [list(p) for p in layers] if layers else None,
        "cell": cell,
        "mirror_axis": mirror_axis,
        "at_um": at_um,
        "translate_um": list(translate_um) if translate_um is not None else None,
        "tolerance_um": tolerance_um,
        "pitch_um": pitch_um,
        "count": count,
        "direction": direction if pitch_um is not None else None,
        "dbu_um": dbu,
        "status": "violated" if violations else "clean",
        "violation_count": len(violations),
        "violations": violations,
    }


def _symmetry_violations(
    kdb: Any,
    layout: Any,
    root: Any,
    layers: list[tuple[int, int]],
    mirror_axis: str | None,
    at_um: float | None,
    translate_um: tuple[float, float] | None,
    tolerance_um: float,
) -> list[dict[str, Any]]:
    dbu = layout.dbu
    tol = round(tolerance_um / dbu)
    if mirror_axis == "x":
        # x -> 2a - x : mirror at the y axis (M90) then shift by 2a.
        trans = kdb.Trans(kdb.Trans.M90, round(2 * at_um / dbu), 0)  # type: ignore[operator]
        kind, what = "mirror", f"mirror about x = {at_um} um"
    elif mirror_axis == "y":
        trans = kdb.Trans(kdb.Trans.M0, 0, round(2 * at_um / dbu))  # type: ignore[operator]
        kind, what = "mirror", f"mirror about y = {at_um} um"
    else:
        dx, dy = translate_um  # type: ignore[misc]
        trans = kdb.Trans(round(dx / dbu), round(dy / dbu))
        kind, what = "translation", f"translation by ({dx}, {dy}) um"

    window = None
    if kind == "translation":
        box = root.bbox()
        window = kdb.Region(box & trans.trans(box))

    out: list[dict[str, Any]] = []
    for layer, datatype in layers:
        layer_index = layout.find_layer(layer, datatype)
        if layer_index is None:
            continue
        original = kdb.Region(layout.begin_shapes(root, layer_index))
        original.merge()
        moved = original.transformed(trans)
        if window is not None:
            original = original & window
            moved = moved & window
        residual = original ^ moved
        if tol > 0:
            residual = residual.sized(-tol).sized(tol)
        residual.merge()
        for piece in residual.each():
            out.append(
                {
                    "rule": RULE_SYMMETRY,
                    "description": f"layer {layer}/{datatype} differs under {what}",
                    "check": CHECK_SYMMETRY,
                    "kind": kind,
                    "layer": f"{layer}/{datatype}",
                    "cell": root.name,
                    "bbox": bbox_um_dict(piece.bbox(), dbu),
                }
            )
    return out


def _placement_boxes(kdb: Any, layout: Any, root: Any, name: str) -> list[Any]:
    """Root-coordinate bbox of every placement of cell ``name`` under ``root``."""
    boxes: list[Any] = []

    def walk(cell: Any, trans: Any) -> None:
        for inst in cell.each_inst():
            child = layout.cell(inst.cell_index)
            for t in inst.cell_inst.each_cplx_trans():
                combined = trans * t
                if child.name == name and not child.bbox().empty():
                    boxes.append(combined.trans(child.bbox()))
                walk(child, combined)

    walk(root, kdb.ICplxTrans())
    return boxes


def _lo(b: Any, horizontal: bool) -> int:
    return b.left if horizontal else b.bottom


def _hi(b: Any, horizontal: bool) -> int:
    return b.right if horizontal else b.top


def _pair_box(kdb: Any, kind: str, prev: Any, nxt: Any, horizontal: bool) -> Any:
    """Box to report for a pair violation: the gap/overlap strip spanning both
    placements across the array axis, or their union for a pitch error."""
    if kind == "pitch":
        return prev + nxt
    a, b = (
        (_hi(prev, horizontal), _lo(nxt, horizontal))
        if kind == "gap"
        else (_lo(nxt, horizontal), _hi(prev, horizontal))
    )
    if horizontal:
        return kdb.Box(a, min(prev.bottom, nxt.bottom), b, max(prev.top, nxt.top))
    return kdb.Box(min(prev.left, nxt.left), a, max(prev.right, nxt.right), b)


_PAIR_TEXT = {
    "gap": "{} um gap between consecutive placements",
    "overlap": "{} um overlap between consecutive placements",
    "pitch": "placement advance {} um != pitch",
}


def _classify_pair(gap: int, step: int, pitch: int, tol: int) -> tuple[str, int] | None:
    """Classify two consecutive placements (all in dbu): ``(kind, size)`` or
    ``None`` when they abut and advance by exactly the pitch (within ``tol``)."""
    if gap > tol:
        return "gap", gap
    if gap < -tol:
        return "overlap", -gap
    if abs(step - pitch) > tol:
        return "pitch", step
    return None


def _abutment_for_top(
    kdb: Any,
    layout: Any,
    top: Any,
    boxes: list[Any],
    unit: str,
    pitch_um: float,
    count: int,
    direction: str,
    tolerance_um: float,
) -> list[dict[str, Any]]:
    dbu = layout.dbu
    pitch = round(pitch_um / dbu)
    tol = round(tolerance_um / dbu)
    horizontal = direction == "x"

    def lo(b: Any) -> int:
        return _lo(b, horizontal)

    def hi(b: Any) -> int:
        return _hi(b, horizontal)

    def um(value: int) -> float:
        return round(value * dbu, 9)

    out: list[dict[str, Any]] = []

    def add(kind: str, desc: str, box: Any, measured: float, expected: float) -> None:
        out.append(
            {
                "rule": RULE_ABUTMENT,
                "description": desc,
                "check": CHECK_ABUTMENT,
                "kind": kind,
                "layer": None,
                "cell": top.name,
                "bbox": bbox_um_dict(box, dbu),
                "measured_um": measured,
                "expected_um": expected,
            }
        )

    boxes = sorted(boxes, key=lo)
    if len(boxes) != count:
        add(
            "count",
            f"{len(boxes)} placement(s) of {unit} found, expected {count}",
            boxes[0],
            float(len(boxes)),
            float(count),
        )
    for prev, nxt in zip(boxes, boxes[1:], strict=False):
        found = _classify_pair(lo(nxt) - hi(prev), lo(nxt) - lo(prev), pitch, tol)
        if found is None:
            continue
        kind, size = found
        box = _pair_box(kdb, kind, prev, nxt, horizontal)
        expected = um(pitch) if kind == "pitch" else 0.0
        add(kind, _PAIR_TEXT[kind].format(um(size)), box, um(size), expected)
    extent = max(hi(b) for b in boxes) - lo(boxes[0])
    expected = count * pitch
    if abs(extent - expected) > tol:
        union = boxes[0]
        for b in boxes[1:]:
            union = union + b
        desc = f"array length {um(extent)} um != count x pitch {um(expected)} um"
        add("length", desc, union, um(extent), um(expected))
    return out


def _abutment_violations(
    kdb: Any,
    layout: Any,
    tops: list[Any],
    unit: str | None,
    pitch_um: float,
    count: int | None,
    direction: str,
    tolerance_um: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    found = False
    for top in tops:
        boxes = _placement_boxes(kdb, layout, top, unit)  # type: ignore[arg-type]
        if not boxes:
            continue
        found = True
        out.extend(
            _abutment_for_top(
                kdb,
                layout,
                top,
                boxes,
                unit,  # type: ignore[arg-type]
                pitch_um,
                count,  # type: ignore[arg-type]
                direction,
                tolerance_um,
            )
        )
    if not found:
        raise ArrayCheckError(
            f"cell {unit!r} has no placed geometry under the checked top cell(s)"
        )
    return out
