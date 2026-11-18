"""Hierarchy-insensitive comparison for ``klt diff --flattened`` (issue #5729).

Answers "do these two streams hold the same drawn geometry, however it is
partitioned into cells?" -- the question the default cell-by-cell
:func:`klayout_tools.layout_diff.diff_report` deliberately cannot answer,
because it never ignores cell names.

Four independent checks, all taken on the instance-placed (flattened) content
under the comparison roots (``--top``, else every top cell):

* **per-layer XOR** -- each side's polygon-bearing shapes on a layer/datatype
  are merged into a ``Region``; the two regions are XOR-ed and the result's
  polygon count and area are reported (an empty XOR is ``0``, never absent);
* **drawn layer/datatype set** -- the pairs that carry polygon-bearing
  geometry on each side, reported as its own field so a layer present in one
  stream only is visible as itself and not merely as a non-empty XOR. The
  layer set is the *union* of both streams' pairs, never one side's;
* **flattened bounding box** of the drawn geometry;
* **labels** -- ``Region`` ignores text, so text is compared by its own walk,
  keyed by ``(layer, texttype)``, and never counts toward the drawn set.

Pure library; the CLI adapter is :mod:`klayout_tools.cli.diff_cmd`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ._layout import bbox_um_dict, load_layout
from .layout_diff import LayoutDiffError

LayerKey = tuple[int, int]


def _roots(layout: Any, top: str | None, path: str) -> list[Any]:
    if top is None:
        return list(layout.top_cells())
    cell = layout.cell(top)
    if cell is None:
        raise LayoutDiffError(f"top cell not found in stream: {top} ({path})")
    return [cell]


def _layer_indexes(layout: Any) -> dict[LayerKey, int]:
    indexes = {}
    for index in layout.layer_indexes():
        info = layout.get_info(index)
        indexes[(info.layer, info.datatype)] = index
    return indexes


def _label_key(text: Any) -> str:
    return f"{text.string}\t{text.x}\t{text.y}"


def _scan(path: str, top: str | None) -> dict[str, Any]:
    """Flattened drawn regions, bbox and labels of one stream, keyed by layer."""
    import klayout.db as kdb

    layout = load_layout(path, LayoutDiffError)
    roots = _roots(layout, top, path)
    regions: dict[LayerKey, Any] = {}
    shapes: dict[LayerKey, int] = {}
    labels: dict[LayerKey, Counter[str]] = {}
    bbox = kdb.Box()
    for key, index in _layer_indexes(layout).items():
        region = kdb.Region()
        counter: Counter[str] = Counter()
        for root in roots:
            region += kdb.Region(root.begin_shapes_rec(index))
            for text in kdb.Texts(root.begin_shapes_rec(index)).each():
                counter[_label_key(text)] += 1
        if counter:
            labels[key] = counter
        count = region.count()  # raw polygon count: text is not a Region member
        if count:
            regions[key] = region
            shapes[key] = count
            bbox += region.bbox()
    return {
        "file": path,
        # The regions are lazy views over the layout's shapes: the layout must
        # outlive them or they silently read as empty.
        "layout": layout,
        "dbu_um": layout.dbu,
        "regions": regions,
        "shapes": shapes,
        "labels": labels,
        "bbox": bbox,
    }


def _layer_entry(
    key: LayerKey, a: dict[str, Any], b: dict[str, Any], dbu_equal: bool
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "layer": key[0],
        "datatype": key[1],
        "shapes_a": a["shapes"].get(key, 0),
        "shapes_b": b["shapes"].get(key, 0),
    }
    if not dbu_equal:  # an XOR across different database units is not defined
        entry.update(xor_polygon_count=None, xor_area_dbu2=None, xor_area_um2=None)
        entry["equivalent"] = False
        return entry
    import klayout.db as kdb

    empty = kdb.Region()
    xor = a["regions"].get(key, empty).merged() ^ b["regions"].get(key, empty).merged()
    xor.merge()
    area = xor.area()
    entry.update(
        xor_polygon_count=xor.count(),
        xor_area_dbu2=area,
        xor_area_um2=area * a["dbu_um"] ** 2,
    )
    entry["equivalent"] = xor.is_empty()
    return entry


def _label_entry(
    key: LayerKey, a: dict[str, Any], b: dict[str, Any], limit: int
) -> tuple[dict[str, Any], bool] | None:
    left = a["labels"].get(key, Counter())
    right = b["labels"].get(key, Counter())
    only_a = sorted((left - right).elements())
    only_b = sorted((right - left).elements())
    if not only_a and not only_b:
        return None

    def items(keys: list[str]) -> list[dict[str, Any]]:
        out = []
        for k in keys[:limit]:
            # Split from the right: only the two numeric fields are peeled off,
            # so a tab inside the label string itself survives intact.
            text, x, y = k.rsplit("\t", 2)
            out.append({"text": text, "x": int(x), "y": int(y)})
        return out

    return (
        {
            "layer": key[0],
            "texttype": key[1],
            "only_in_a_count": len(only_a),
            "only_in_b_count": len(only_b),
            "only_in_a": items(only_a),
            "only_in_b": items(only_b),
        },
        len(only_a) > limit or len(only_b) > limit,
    )


def _side(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": info["file"],
        "dbu_um": info["dbu_um"],
        "drawn_layers": [
            {"layer": k[0], "datatype": k[1]} for k in sorted(info["shapes"])
        ],
        "flattened_shapes": sum(info["shapes"].values()),
        "bbox_um": bbox_um_dict(info["bbox"], info["dbu_um"]),
        "label_count": sum(sum(c.values()) for c in info["labels"].values()),
    }


def _bbox_tuple(info: dict[str, Any]) -> tuple[int, ...] | None:
    box = info["bbox"]
    return None if box.empty() else (box.left, box.bottom, box.right, box.top)


def flattened_report(
    file_a: str, file_b: str, *, top: str | None, max_items: int
) -> dict[str, Any]:
    """Compare two streams on their flattened geometry; see the module docstring."""
    a = _scan(file_a, top)
    b = _scan(file_b, top)
    dbu_equal = a["dbu_um"] == b["dbu_um"]

    keys_a, keys_b = set(a["shapes"]), set(b["shapes"])
    only_a = [{"layer": k[0], "datatype": k[1]} for k in sorted(keys_a - keys_b)]
    only_b = [{"layer": k[0], "datatype": k[1]} for k in sorted(keys_b - keys_a)]

    # Union, never one side's layers: a layer only the other stream drew must
    # still get an entry. Differing layers sort first so --max-items keeps them.
    union = sorted(keys_a | keys_b)
    entries = [_layer_entry(k, a, b, dbu_equal) for k in union]
    entries.sort(key=lambda e: e["equivalent"])
    different_layers = sum(1 for e in entries if not e["equivalent"])

    label_layers: list[dict[str, Any]] = []
    truncated = False
    for key in sorted(set(a["labels"]) | set(b["labels"])):
        found = _label_entry(key, a, b, max_items)
        if found is not None:
            label_layers.append(found[0])
            truncated = truncated or found[1]
    labels_equal = not label_layers

    bbox_equal = dbu_equal and _bbox_tuple(a) == _bbox_tuple(b)
    layers_equal = not only_a and not only_b
    equivalent = (
        dbu_equal
        and layers_equal
        and bbox_equal
        and labels_equal
        and different_layers == 0
    )
    truncated = truncated or len(entries) > max_items or len(label_layers) > max_items
    return {
        "schema_version": 1,
        "mode": "flattened",
        "top": top,
        "status": "equivalent" if equivalent else "different",
        "equivalent": equivalent,
        "a": _side(a),
        "b": _side(b),
        "dbu_equal": dbu_equal,
        "drawn_layers": {
            "equal": layers_equal,
            "only_in_a": only_a,
            "only_in_b": only_b,
        },
        "bbox": {"equal": bbox_equal},
        "layer_count": len(entries),
        "different_layer_count": different_layers,
        "layers": entries[:max_items],
        "labels": {
            "equal": labels_equal,
            "different_layer_count": len(label_layers),
            "layers": label_layers[:max_items],
        },
        "truncated": truncated,
        "max_items": max_items,
    }
