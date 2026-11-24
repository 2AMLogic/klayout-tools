"""Repair the ASAP7 V1.M1.EN.1 enclosure at an individual routed landing."""

from __future__ import annotations

from .place_and_route_via_tip import _nm_to_dbu_ceil
from .place_and_route_via_tip_repair import _m1_spacing_clear, _merge_repair


def _placed_landings(kdb, top, metal_index, cut_index):
    """A single rectangular V1 fully covered by one rectangular via-master pad."""
    masters = {}
    for inst in top.each_inst():
        via = inst.cell
        if not via.name.startswith("VIA_"):
            continue
        if via.cell_index() not in masters:
            cuts = kdb.Region(via.shapes(cut_index)).merged()
            pads = kdb.Region(via.shapes(metal_index)).merged()
            masters[via.cell_index()] = None
            if cuts.count() == pads.count() == 1 and (cuts - pads).is_empty():
                cut, pad = next(cuts.each()), next(pads.each())
                if cut.is_box() and pad.is_box():
                    masters[via.cell_index()] = (cut, pad)
        landing = masters[via.cell_index()]
        if landing is None:
            continue
        for trans in inst.cell_inst.each_cplx_trans():
            cut, pad = (shape.transformed(trans) for shape in landing)
            if cut.is_box() and pad.is_box():
                yield cut.bbox(), pad.bbox()


def _enclosure_boxes(kdb, cut, long_side, short_side):
    """One complete 5 nm side and its opposite 2 nm side along either axis."""
    for a, b in ((long_side, short_side), (short_side, long_side)):
        yield kdb.Box(cut.left - a, cut.bottom, cut.right + b, cut.top)
        yield kdb.Box(cut.left, cut.bottom - a, cut.right, cut.top + b)


def _protected(kdb, top, cut, cut_index, other_masks, label_index, extra):
    search = extra.bbox()
    for index in other_masks:
        shapes = kdb.Region(top.begin_shapes_rec_touching(index, search))
        if not extra.interacting(shapes).is_empty():
            return True
    vias = kdb.Region(top.begin_shapes_rec_touching(cut_index, search))
    if not extra.interacting(vias - kdb.Region(cut)).is_empty():
        return True
    if label_index is None:
        return False
    labels = kdb.Texts(top.begin_shapes_rec_touching(label_index, search))
    return not labels.interacting(extra).is_empty()


def _source(kdb, metal, cut):
    shapes = kdb.Region([s.polygon for s in metal.each_touching(cut)])
    return shapes.interacting(kdb.Region(cut))


def _missing_enclosures(kdb, cut, source, dbu):
    candidates = []
    for box in _enclosure_boxes(
        kdb, cut, _nm_to_dbu_ceil(5, dbu), _nm_to_dbu_ceil(2, dbu)
    ):
        extra = kdb.Region(box) - source
        if extra.is_empty():
            return []
        candidates.append((box, extra))
    return candidates


def _extension(kdb, cut, pad, source, candidates, metal, dbu, protected):
    # A bare via pad carries no evidence of attached cell or route metal.
    if source.count() != 1 or (source - kdb.Region(pad)).is_empty():
        return None
    # Include both ends of every possible spacing pair; retain complete
    # polygons because M1 spacing depends on the length of the facing edges.
    window = kdb.Region(cut.enlarged(_nm_to_dbu_ceil(75, dbu)))
    search = window.bbox().enlarged(_nm_to_dbu_ceil(40, dbu))
    nearby = kdb.Region([s.polygon for s in metal.each_touching(search)])
    for box, extra in candidates:
        trial = kdb.Region(box)
        if nearby.interacting(trial).count() != 1 or protected(cut, extra):
            continue
        if _m1_spacing_clear(kdb, (nearby + trial).merged(), dbu, window):
            return extra
    return None


def repair_m1_v1_enclosures(kdb, layout, top, lef_to_gds) -> tuple[int, int]:
    """Add only top-cell M1, preserving the shared masters and non-target cuts."""
    if lef_to_gds.get("M1") != (19, 0) or lef_to_gds.get("V1", (21, 0)) != (21, 0):
        return 0, 0
    metal_index, cut_index = layout.find_layer(19, 0), layout.find_layer(21, 0)
    if metal_index is None or cut_index is None:
        return 0, 0
    other_masks = [
        index
        for layer in (18, 112, 113)
        if (index := layout.find_layer(layer, 0)) is not None
    ]
    label_index = layout.find_layer(19, 251)

    def protected(cut, extra):
        return _protected(kdb, top, cut, cut_index, other_masks, label_index, extra)

    metal = kdb.Shapes()
    metal.insert(kdb.Region(top.begin_shapes_rec(metal_index)).merged())
    repaired = 0
    refused = []
    for cut, pad in _placed_landings(kdb, top, metal_index, cut_index):
        source = _source(kdb, metal, cut)
        candidates = _missing_enclosures(kdb, cut, source, layout.dbu)
        if not candidates:
            continue
        extra = _extension(
            kdb, cut, pad, source, candidates, metal, layout.dbu, protected
        )
        if extra is None:
            refused.append(cut)
            continue
        top.shapes(metal_index).insert(extra)
        _merge_repair(kdb, metal, extra)
        repaired += 1
    # Another placement may have added metal to the same conductor later.
    remaining = sum(
        bool(_missing_enclosures(kdb, cut, _source(kdb, metal, cut), layout.dbu))
        for cut in refused
    )
    return repaired, remaining
