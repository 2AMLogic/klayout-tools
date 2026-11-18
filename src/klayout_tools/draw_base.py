"""Copy a base layout and apply erase windows for `klt draw`."""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_int, is_number
from ._layout import load_layout, resolve_top_cell
from .draw_validation import DrawError, _reject_unknown_keys

_ALLOWED_ERASE_KEYS = frozenset({"rect_um", "layer"})


def _parse_base_params(
    params: dict[str, Any],
) -> tuple[str | None, str | None, list, bool]:
    """Return the base path, selected cell, erase windows, and hierarchy mode."""
    base_gds = params.get("base_gds")
    base_cell = params.get("base_cell")
    keep_hierarchy = params.get("keep_hierarchy", False)
    if not isinstance(keep_hierarchy, bool):
        raise DrawError("params.keep_hierarchy must be a boolean")
    if base_gds is None:
        for key in ("base_cell", "erase_um", "keep_hierarchy"):
            if params.get(key) is not None:
                raise DrawError(f"params.{key} requires params.base_gds")
        return None, None, [], False
    if not isinstance(base_gds, str) or not base_gds:
        raise DrawError("params.base_gds must be a non-empty string")
    if base_cell is not None and (not isinstance(base_cell, str) or not base_cell):
        raise DrawError("params.base_cell must be a non-empty string")
    erase_windows = _parse_erase(params.get("erase_um"))
    if keep_hierarchy and erase_windows:
        raise DrawError("params.keep_hierarchy requires params.erase_um to be empty")
    return base_gds, base_cell, erase_windows, keep_hierarchy


def _seed_layout(
    layout, cell_name, dbu_um, params, base_gds, base_cell, keep_hierarchy
):
    """Create the output top cell; return ``(top, base_cell_name, dbu_um)``.

    Without a base this is an empty cell at ``dbu_um`` (``base_cell_name`` is
    ``None``). With a base, the selected cell tree is copied into the output and
    the base's dbu wins (an explicit, differing ``params.dbu_um`` is an error).
    The base cell's *name* is returned, not the cell: the base layout dies with
    this call.
    """
    if base_gds is None:
        layout.dbu = dbu_um
        return layout.create_cell(cell_name), None, dbu_um
    base = load_layout(base_gds, DrawError)
    if params.get("dbu_um") is not None and abs(base.dbu - dbu_um) > 1e-12:
        raise DrawError(
            f"params.dbu_um ({dbu_um}) differs from the base layout's "
            f"dbu ({base.dbu}) -- omit it to use the base's"
        )
    base_top = resolve_top_cell(base, base_cell, DrawError, path=base_gds)
    layout.dbu = base.dbu
    top = layout.create_cell(cell_name)
    top.copy_tree(base_top)
    if not keep_hierarchy:
        top.flatten(True)
    return top, base_top.name, base.dbu


def _erase_base(
    top, layout, erase_windows, to_dbu, kdb, layer_order, layer_counts, layer_names
) -> tuple[int, int]:
    """Apply every erase window, then seed the per-layer report from what remains.

    Returns ``(erased_shapes, erased_labels)``. A no-op (``(0, 0)``) when there
    is no base -- the top cell is empty -- so the caller needs no branch.
    """
    erased_shapes = 0
    erased_labels = 0
    for window, scope in erase_windows:
        n_shapes, n_labels = _erase(top, layout, window, scope, to_dbu, kdb)
        erased_shapes += n_shapes
        erased_labels += n_labels
    for idx in layout.layer_indexes():
        info = layout.get_info(idx)
        count = sum(1 for sh in top.shapes(idx).each() if not sh.is_text())
        if count:
            pair = (info.layer, info.datatype)
            layer_order.append(pair)
            layer_counts[pair] = count
            layer_names[pair] = info.name or None
    return erased_shapes, erased_labels


def _base_report_fields(base_gds, base_name, erased_shapes, erased_labels) -> dict:
    """Additive response fields for a base run; ``{}`` when no base was used."""
    if base_gds is None:
        return {}
    return {
        "base_gds": base_gds,
        "base_cell": base_name,
        "erased_shape_count": erased_shapes,
        "erased_label_count": erased_labels,
    }


def _parse_erase(raw: Any) -> list[tuple[tuple[float, float, float, float], Any]]:
    """Validate ``params.erase_um`` into ``[((x0, y0, x1, y1), layer_or_None)]``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DrawError("params.erase_um must be a list")
    windows = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise DrawError(f"erase_um[{i}] must be a JSON object")
        _reject_unknown_keys(entry, _ALLOWED_ERASE_KEYS, f"erase_um[{i}]")
        rect = entry.get("rect_um")
        if (
            not isinstance(rect, list)
            or len(rect) != 4
            or any(not is_number(v) for v in rect)
        ):
            raise DrawError(
                f"erase_um[{i}].rect_um must be [x0, y0, x1, y1] of numbers"
            )
        layer = entry.get("layer")
        if layer is not None and (
            not isinstance(layer, list)
            or len(layer) != 2
            or any(not is_int(v) or v < 0 for v in layer)
        ):
            raise DrawError(
                f"erase_um[{i}].layer must be a [layer, datatype] pair of "
                "non-negative integers when present"
            )
        x0, x1 = sorted((rect[0], rect[2]))
        y0, y1 = sorted((rect[1], rect[3]))
        windows.append(((x0, y0, x1, y1), None if layer is None else tuple(layer)))
    return windows


def _erase(top, layout, window, scope, to_dbu, kdb) -> tuple[int, int]:
    """Subtract one ``window`` (microns) from ``top``; return (shapes, labels) hit.

    Only shapes whose interior overlaps the window are touched: each is replaced
    by its boolean difference with the window (a straddling polygon is clipped,
    not dropped). Texts whose anchor lies inside the closed window are removed.
    ``scope`` is a ``(layer, datatype)`` pair or ``None`` for all layers.
    """
    box = kdb.Box(*(to_dbu(v) for v in window))
    win = kdb.Region(box)
    erased_shapes = 0
    erased_labels = 0
    for idx in layout.layer_indexes():
        info = layout.get_info(idx)
        if scope is not None and (info.layer, info.datatype) != scope:
            continue
        shapes = top.shapes(idx)
        for shape in list(shapes.each_touching(box)):
            if shape.is_text():
                if box.contains(shape.text_trans.disp.to_p()):
                    shapes.erase(shape)
                    erased_labels += 1
                continue
            region = kdb.Region()
            region.insert(shape.polygon)
            if (region & win).is_empty():
                continue
            shapes.erase(shape)
            for poly in (region - win).each():
                shapes.insert(poly)
            erased_shapes += 1
    return erased_shapes, erased_labels
