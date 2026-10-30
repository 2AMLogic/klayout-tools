"""What-if floorplan edits for ``klt economy --what-if`` (issue #4661).

Answers "if this instance moved, or this sub-cell were re-shaped to W x H,
what would the top bbox and utilization become?" without regenerating the
layout. The edits are applied to the **in-memory** ``klayout.db.Layout`` that
:func:`klayout_tools.economy.economy_report` already read; nothing is ever
written, no DRC runs, and the result is an area/overlap estimate only --
``legality`` is a fixed ``"not_checked"``.

``moves.json`` schema (both keys optional)::

    {
      "moves":   [{"instance": "<cell name | index>", "dx_um": 1.5, "dy_um": 0}],
      "reshape": [{"cell": "<name>", "width_um": 20, "height_um": 10}]
    }

``instance`` selects a **direct, non-array child** of the top cell: either the
child's cell name (it must match exactly one direct instance) or the integer
index of the instance in the top cell's instance order. Array instances,
nested instances, unknown names and ambiguous names are rejected rather than
guessed at.

``reshape`` replaces the named cell's content with an empty footprint
anchored at the cell's current lower-left corner (modelled as a box on the
reserved annotation layer, so it widens the bbox but contributes no drawn
area). The footprint's area is therefore **not invented** as drawn geometry:
the report's ``utilization_basis`` is ``"excludes_reshaped_footprints"``
whenever a reshape was applied.

Pure library; imports ``klayout.db`` lazily via the ``kdb`` argument so
argument parsing stays cheap (same convention as ``economy.py``).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ._annotation import CANONICAL_ANNOTATION_LAYER, is_reserved_annotation_layer
from ._layout import bbox_um_dict

_TOP_KEYS = {"moves", "reshape"}


def _number(value: Any, where: str, error_cls: type[Exception]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_cls(f"{where} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise error_cls(f"{where} must be finite, got {value!r}")
    return float(value)


def load_what_if_spec(path: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Read and validate a ``moves.json`` file; raise ``error_cls`` on any
    missing, unreadable, malformed or schema-violating input."""
    if not os.path.isfile(path):
        raise error_cls(f"what-if file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise error_cls(f"could not read what-if file '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise error_cls("what-if file must be a JSON object")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise error_cls(f"what-if file has unknown key(s): {', '.join(unknown)}")

    moves_in = data.get("moves", [])
    reshape_in = data.get("reshape", [])
    if not isinstance(moves_in, list) or not isinstance(reshape_in, list):
        raise error_cls("what-if 'moves' and 'reshape' must be lists")

    moves = []
    for i, entry in enumerate(moves_in):
        where = f"moves[{i}]"
        if not isinstance(entry, dict):
            raise error_cls(f"{where} must be an object")
        extra = sorted(set(entry) - {"instance", "dx_um", "dy_um"})
        if extra:
            raise error_cls(f"{where} has unknown key(s): {', '.join(extra)}")
        selector = entry.get("instance")
        if isinstance(selector, bool) or not isinstance(selector, (str, int)):
            raise error_cls(f"{where}.instance must be a cell name or index")
        moves.append(
            {
                "instance": selector,
                "dx_um": _number(entry.get("dx_um", 0.0), f"{where}.dx_um", error_cls),
                "dy_um": _number(entry.get("dy_um", 0.0), f"{where}.dy_um", error_cls),
            }
        )

    reshapes = []
    seen: set[str] = set()
    for i, entry in enumerate(reshape_in):
        where = f"reshape[{i}]"
        if not isinstance(entry, dict):
            raise error_cls(f"{where} must be an object")
        extra = sorted(set(entry) - {"cell", "width_um", "height_um"})
        if extra:
            raise error_cls(f"{where} has unknown key(s): {', '.join(extra)}")
        cell = entry.get("cell")
        if not isinstance(cell, str) or not cell:
            raise error_cls(f"{where}.cell must be a cell name")
        if cell in seen:
            raise error_cls(f"{where}: cell '{cell}' is reshaped more than once")
        seen.add(cell)
        for key in ("width_um", "height_um"):
            if key not in entry:
                raise error_cls(f"{where}.{key} is required")
        width = _number(entry["width_um"], f"{where}.width_um", error_cls)
        height = _number(entry["height_um"], f"{where}.height_um", error_cls)
        if width <= 0 or height <= 0:
            raise error_cls(f"{where} width_um/height_um must be positive")
        reshapes.append({"cell": cell, "width_um": width, "height_um": height})

    return {"moves": moves, "reshape": reshapes}


def _inst_id(index: int, cell_name: str) -> dict[str, Any]:
    return {"index": index, "cell": cell_name}


def _resolve_selector(
    selector: str | int, children: list[dict[str, Any]], error_cls: type[Exception]
) -> dict[str, Any]:
    if isinstance(selector, str) and selector.strip().lstrip("-").isdigit():
        selector = int(selector)
    if isinstance(selector, int):
        if not 0 <= selector < len(children):
            raise error_cls(
                f"instance index {selector} out of range "
                f"(top cell has {len(children)} direct instance(s))"
            )
        picked = children[selector]
    else:
        matches = [c for c in children if c["name"] == selector]
        if not matches:
            raise error_cls(f"instance not found in top cell: {selector}")
        if len(matches) > 1:
            indexes = ", ".join(str(c["index"]) for c in matches)
            raise error_cls(
                f"instance selector '{selector}' is ambiguous (matches indexes "
                f"{indexes}); use an index"
            )
        picked = matches[0]
    if picked["array"]:
        raise error_cls(
            f"instance {picked['index']} ({picked['name']}) is an array instance; "
            "moving array instances is not supported"
        )
    return picked


def _layer_regions(kdb: Any, layout: Any, cell: Any, layer_index: int) -> Any:
    return kdb.Region(cell.begin_shapes_rec(layer_index))


def _broken_contacts(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    child: dict[str, Any],
    before: Any,
    after: Any,
    dbu: float,
) -> list[dict[str, Any]]:
    """Top-cell-level shapes/labels that touched ``child``'s geometry on the
    same layer before the move and no longer do after it."""
    found: list[dict[str, Any]] = []
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if is_reserved_annotation_layer(info.layer, info.datatype):
            continue
        geometry = _layer_regions(kdb, layout, child["cell"], li)
        if geometry.is_empty():
            continue
        geo_before = geometry.transformed(before)
        geo_after = geometry.transformed(after)

        own = kdb.Region(top_cell.shapes(li)).merged()
        for poly in own.each():
            region = kdb.Region(poly)
            if region.interacting(geo_before).is_empty():
                continue
            if not region.interacting(geo_after).is_empty():
                continue
            found.append(
                {
                    "layer": info.layer,
                    "datatype": info.datatype,
                    "kind": "shape",
                    "text": None,
                    "instance": _inst_id(child["index"], child["name"]),
                    "bbox_um": bbox_um_dict(poly.bbox(), dbu),
                }
            )

        for shape in top_cell.shapes(li).each(kdb.Shapes.STexts):
            point = kdb.Point(shape.text.x, shape.text.y)
            touched_before = any(p.inside(point) for p in geo_before.each())
            touched_after = any(p.inside(point) for p in geo_after.each())
            if touched_before and not touched_after:
                found.append(
                    {
                        "layer": info.layer,
                        "datatype": info.datatype,
                        "kind": "text",
                        "text": shape.text.string,
                        "instance": _inst_id(child["index"], child["name"]),
                        "bbox_um": bbox_um_dict(kdb.Box(point, point), dbu),
                    }
                )
    return found


def _overlaps(
    kdb: Any, children: list[dict[str, Any]], dbu: float
) -> list[dict[str, Any]]:
    out = []
    for i, a in enumerate(children):
        for b in children[i + 1 :]:
            inter = a["bbox"] & b["bbox"]
            if inter.empty() or inter.width() <= 0 or inter.height() <= 0:
                continue
            out.append(
                {
                    "a": _inst_id(a["index"], a["name"]),
                    "b": _inst_id(b["index"], b["name"]),
                    "overlap_area_um2": inter.area() * dbu * dbu,
                    "overlap_bbox_um": bbox_um_dict(inter, dbu),
                }
            )
    return out


def apply_what_if(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    spec: dict[str, Any],
    error_cls: type[Exception],
) -> dict[str, Any]:
    """Apply ``spec`` (from :func:`load_what_if_spec`) to ``layout`` in place
    and return the report's ``what_if`` block.

    ``layout`` must be a throwaway in-memory copy; the caller never writes it.
    """
    dbu = layout.dbu

    # Snapshot the top cell's direct instances; every later step works from
    # this snapshot so instance handles invalidated by an edit never matter.
    children: list[dict[str, Any]] = []
    for index, inst in enumerate(top_cell.each_inst()):
        children.append(
            {
                "index": index,
                "name": inst.cell.name,
                "cell": inst.cell,
                "array": inst.is_regular_array(),
                "arr": inst.cell_inst,
                "prop_id": inst.prop_id,
                "trans": inst.cplx_trans,
                "shift": (0, 0),
            }
        )

    # Resolve everything up front so a bad entry fails before any edit.
    moves_applied = []
    for move in spec["moves"]:
        child = _resolve_selector(move["instance"], children, error_cls)
        dx = round(move["dx_um"] / dbu)
        dy = round(move["dy_um"] / dbu)
        sx, sy = child["shift"]
        child["shift"] = (sx + dx, sy + dy)
        moves_applied.append(
            {
                "instance": _inst_id(child["index"], child["name"]),
                "dx_um": dx * dbu,
                "dy_um": dy * dbu,
            }
        )

    reshape_plan = []
    for entry in spec["reshape"]:
        cell = layout.cell(entry["cell"])
        if cell is None:
            raise error_cls(f"reshape cell not found in stream: {entry['cell']}")
        if cell.cell_index() == top_cell.cell_index():
            raise error_cls("the top cell cannot be reshaped")
        bbox = cell.bbox()
        if bbox.empty():
            raise error_cls(f"reshape cell '{cell.name}' has no geometry")
        reshape_plan.append((cell, bbox, entry))

    # Broken contacts compare the same instance geometry before/after, on the
    # untouched top-level shapes -- compute against the pre-reshape layout.
    broken: list[dict[str, Any]] = []
    moved = [c for c in children if c["shift"] != (0, 0)]
    for child in moved:
        sx, sy = child["shift"]
        after = kdb.ICplxTrans(kdb.Trans(sx, sy)) * child["trans"]
        broken.extend(
            _broken_contacts(kdb, layout, top_cell, child, child["trans"], after, dbu)
        )

    # Apply moves by rebuilding the top cell's instances from the snapshot.
    if moved:
        top_cell.clear_insts()
        for child in children:
            arr = child["arr"]
            sx, sy = child["shift"]
            if (sx, sy) != (0, 0):
                arr = kdb.CellInstArray(
                    arr.cell_index,
                    kdb.ICplxTrans(kdb.Trans(sx, sy)) * child["trans"],
                )
            if child["prop_id"]:
                top_cell.insert(arr, child["prop_id"])
            else:
                top_cell.insert(arr)
            child["arr"] = arr

    reshaped_cells = []
    if reshape_plan:
        layer, datatype = CANONICAL_ANNOTATION_LAYER
        annotation = layout.layer(layer, datatype)
        for cell, bbox, entry in reshape_plan:
            w = round(entry["width_um"] / dbu)
            h = round(entry["height_um"] / dbu)
            cell.clear()
            cell.shapes(annotation).insert(
                kdb.Box(bbox.left, bbox.bottom, bbox.left + w, bbox.bottom + h)
            )
            reshaped_cells.append(
                {
                    "cell": cell.name,
                    "original_bbox_um": bbox_um_dict(bbox, dbu),
                    "width_um": w * dbu,
                    "height_um": h * dbu,
                    "anchor_um": {"x": bbox.left * dbu, "y": bbox.bottom * dbu},
                }
            )

    for child in children:
        child["bbox"] = child["arr"].bbox(layout)

    def sort_key(entry: dict[str, Any]) -> tuple:
        return (
            entry["layer"],
            entry["datatype"],
            entry["instance"]["index"],
            entry["kind"],
            entry["text"] or "",
            entry["bbox_um"]["left"],
            entry["bbox_um"]["bottom"],
        )

    return {
        "moves_applied": sorted(
            moves_applied,
            key=lambda m: (m["instance"]["index"], m["dx_um"], m["dy_um"]),
        ),
        "reshaped_cells": sorted(reshaped_cells, key=lambda r: r["cell"]),
        "overlaps": _overlaps(kdb, children, dbu),
        "broken_contacts": sorted(broken, key=sort_key),
        "utilization_basis": (
            "excludes_reshaped_footprints" if reshaped_cells else "measured"
        ),
        "legality": "not_checked",
    }
