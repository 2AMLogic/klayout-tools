"""Compare two GDSII/OASIS streams for equal geometry, labels and hierarchy.

Pure library: :func:`diff_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints. Serialisation and text
formatting live in :mod:`klayout_tools.cli.diff_cmd`.

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only -- no GUI, no Qt.

Normalisation contract (documented for consumers in ``docs/cli/diff.md``):

* **Ignored** -- stream timestamps (BGNLIB/BGNSTR), the order of shapes and
  instances inside a cell, trailing record padding, the library name, and
  user properties.
* **Never ignored** -- shape coordinates (in database units), path width and
  end extensions, text string/position/orientation, instance transform and
  array parameters, the database unit, cell names, layer/datatype numbers.

Each cell is reduced to a multiset of canonical, JSON-able items (shapes keyed
by layer/datatype, plus instances). The report is the multiset difference of
those items per cell; ``canonical_hash`` is a SHA-256 over the sorted items, so
two streams that differ only in ignored detail share a hash while their raw
``stream_sha256`` values differ.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from ._layout import cells_in_hierarchy, load_layout

#: Default cap on listed items per (cell, layer) / per cell instance list and
#: on listed differing cells.
DEFAULT_MAX_ITEMS = 50


class LayoutDiffError(Exception):
    """Raised when a layout cannot be read or an option is invalid.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _points(pts: Any) -> list[list[int]]:
    return [[p.x, p.y] for p in pts]


def _rotate_to_min(pts: list[list[int]]) -> list[list[int]]:
    """Rotate a closed contour so it starts at its smallest point."""
    if not pts:
        return pts
    start = pts.index(min(pts))
    return pts[start:] + pts[:start]


def _shape_item(shape: Any) -> dict[str, Any]:
    if shape.is_box():
        b = shape.box
        return {"type": "box", "bbox": [b.left, b.bottom, b.right, b.top]}
    if shape.is_polygon():
        poly = shape.polygon
        hull = _rotate_to_min(_points(poly.each_point_hull()))
        holes = sorted(
            _rotate_to_min(_points(poly.each_point_hole(i)))
            for i in range(poly.holes())
        )
        return {"type": "polygon", "hull": hull, "holes": holes}
    if shape.is_path():
        path = shape.path
        return {
            "type": "path",
            "width": path.width,
            "bgn_ext": path.bgn_ext,
            "end_ext": path.end_ext,
            "round": bool(path.round),
            "points": _points(path.each_point()),
        }
    if shape.is_text():
        text = shape.text
        trans = text.trans
        return {
            "type": "text",
            "string": text.string,
            "x": trans.disp.x,
            "y": trans.disp.y,
            "rotation": trans.rot,
            "mirror": bool(trans.is_mirror()),
        }
    return {"type": "other", "repr": str(shape)}


def _instance_item(inst: Any) -> dict[str, Any]:
    trans = inst.cplx_trans
    item: dict[str, Any] = {
        "cell": inst.cell.name,
        "x": round(trans.disp.x),
        "y": round(trans.disp.y),
        "angle": trans.angle,
        "mirror": bool(trans.is_mirror()),
        "mag": trans.mag,
    }
    if inst.is_regular_array():
        item["array"] = {
            "na": inst.na,
            "nb": inst.nb,
            "a": [inst.a.x, inst.a.y],
            "b": [inst.b.x, inst.b.y],
        }
    return item


def _key(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def _cell_content(layout: Any, cell: Any) -> dict[str, Any]:
    """Canonical content of one cell: per-(layer, datatype) and instance multisets."""
    layers: dict[tuple[int, int], Counter[str]] = {}
    for layer_index in layout.layer_indexes():
        shapes = cell.shapes(layer_index)
        if shapes.is_empty():
            continue
        info = layout.get_info(layer_index)
        counter = layers.setdefault((info.layer, info.datatype), Counter())
        for shape in shapes.each():
            counter[_key(_shape_item(shape))] += 1
    instances: Counter[str] = Counter()
    for inst in cell.each_inst():
        instances[_key(_instance_item(inst))] += 1
    return {"layers": layers, "instances": instances}


def _scope_cells(layout: Any, top: str | None, path: str) -> list[Any]:
    if top is None:
        return list(layout.each_cell())
    cell = layout.cell(top)
    if cell is None:
        raise LayoutDiffError(f"top cell not found in stream: {top} ({path})")
    return cells_in_hierarchy(layout, cell)


def _load(path: str, top: str | None) -> dict[str, Any]:
    layout = load_layout(path, LayoutDiffError)
    cells = {
        cell.name: _cell_content(layout, cell)
        for cell in _scope_cells(layout, top, path)
    }
    digest = hashlib.sha256()
    digest.update(f"dbu={layout.dbu!r}\n".encode())
    for name in sorted(cells):
        digest.update(f"cell {name}\n".encode())
        content = cells[name]
        for layer_datatype in sorted(content["layers"]):
            digest.update(f"layer {layer_datatype[0]}/{layer_datatype[1]}\n".encode())
            for key in sorted(content["layers"][layer_datatype].elements()):
                digest.update(key.encode() + b"\n")
        digest.update(b"instances\n")
        for key in sorted(content["instances"].elements()):
            digest.update(key.encode() + b"\n")
    with open(path, "rb") as handle:
        stream_sha256 = hashlib.sha256(handle.read()).hexdigest()
    return {
        "file": path,
        "dbu_um": layout.dbu,
        "cells": cells,
        "canonical_hash": digest.hexdigest(),
        "stream_sha256": stream_sha256,
    }


def _only(left: Counter[str], right: Counter[str]) -> list[str]:
    """Sorted canonical keys in ``left`` but not (or fewer times) in ``right``."""
    return sorted((left - right).elements())


def _capped(keys: list[str], limit: int) -> tuple[list[Any], bool]:
    return [json.loads(k) for k in keys[:limit]], len(keys) > limit


def _diff_cell(
    name: str, a: dict[str, Any], b: dict[str, Any], limit: int
) -> dict | None:
    truncated = False
    layers_out: list[dict[str, Any]] = []
    for layer_datatype in sorted(set(a["layers"]) | set(b["layers"])):
        left = a["layers"].get(layer_datatype, Counter())
        right = b["layers"].get(layer_datatype, Counter())
        only_a = _only(left, right)
        only_b = _only(right, left)
        if not only_a and not only_b:
            continue
        items_a, trunc_a = _capped(only_a, limit)
        items_b, trunc_b = _capped(only_b, limit)
        truncated = truncated or trunc_a or trunc_b
        layers_out.append(
            {
                "layer": layer_datatype[0],
                "datatype": layer_datatype[1],
                "only_in_a_count": len(only_a),
                "only_in_b_count": len(only_b),
                "only_in_a": items_a,
                "only_in_b": items_b,
            }
        )
    inst_a = _only(a["instances"], b["instances"])
    inst_b = _only(b["instances"], a["instances"])
    if not layers_out and not inst_a and not inst_b:
        return None
    items_a, trunc_a = _capped(inst_a, limit)
    items_b, trunc_b = _capped(inst_b, limit)
    return {
        "cell": name,
        "layers": layers_out,
        "instances": {
            "only_in_a_count": len(inst_a),
            "only_in_b_count": len(inst_b),
            "only_in_a": items_a,
            "only_in_b": items_b,
        },
        "truncated": truncated or trunc_a or trunc_b,
    }


def diff_report(
    file_a: str,
    file_b: str,
    *,
    top: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    """Compare two layout streams and return a deterministic report dict.

    ``top`` scopes both streams to that cell's hierarchy (it must exist in
    both). ``max_items`` caps every listed difference list; the ``*_count``
    fields always carry the true totals.

    Raises :class:`LayoutDiffError` for an unreadable file, an unknown
    ``top`` cell, or a non-positive ``max_items``.
    """
    if max_items < 1:
        raise LayoutDiffError(f"max-items must be >= 1, got {max_items}")
    a = _load(file_a, top)
    b = _load(file_b, top)

    names_a = set(a["cells"])
    names_b = set(b["cells"])
    only_a = sorted(names_a - names_b)
    only_b = sorted(names_b - names_a)

    cell_diffs = []
    for name in sorted(names_a & names_b):
        diff = _diff_cell(name, a["cells"][name], b["cells"][name], max_items)
        if diff is not None:
            cell_diffs.append(diff)

    dbu_equal = a["dbu_um"] == b["dbu_um"]
    equivalent = dbu_equal and not only_a and not only_b and not cell_diffs
    listed = cell_diffs[:max_items]
    truncated = len(cell_diffs) > max_items or any(d["truncated"] for d in listed)

    def side(info: dict[str, Any]) -> dict[str, Any]:
        return {
            "file": info["file"],
            "stream_sha256": info["stream_sha256"],
            "canonical_hash": info["canonical_hash"],
            "dbu_um": info["dbu_um"],
            "cell_count": len(info["cells"]),
        }

    return {
        "schema_version": 1,
        "top": top,
        "status": "equivalent" if equivalent else "different",
        "equivalent": equivalent,
        "stream_identical": a["stream_sha256"] == b["stream_sha256"],
        "a": side(a),
        "b": side(b),
        "dbu_equal": dbu_equal,
        "cells_only_in_a": only_a,
        "cells_only_in_b": only_b,
        "different_cell_count": len(cell_diffs),
        "differences": listed,
        "truncated": truncated,
        "max_items": max_items,
    }
