"""Lengthen instance-local ASAP7 M1 landings facing another landing (#7122)."""

from __future__ import annotations

from .place_and_route_via_tip import (
    _SHORT_MAX_NM,
    _TIP_RULES,
    _edge_classes,
    _nm_to_dbu_ceil,
)


def _landing_edges(kdb, top, metal, cut):
    """Index complete sides of rectangular VIA_* pads enclosing a V1 cut."""
    edges = {}
    for inst in top.each_inst():
        via = inst.cell
        if not via.name.startswith("VIA_") or via.shapes(cut).is_empty():
            continue
        cuts = kdb.Region(via.shapes(cut))
        for pad in kdb.Region(via.shapes(metal)).merged().each():
            if not pad.is_box() or cuts.inside(kdb.Region(pad)).count() != 1:
                continue
            for trans in inst.cell_inst.each_cplx_trans():
                placed = pad.transformed(trans)
                if not placed.is_box():
                    continue
                for edge in placed.each_edge():
                    edges[edge.bbox()] = placed.bbox()
    return edges


def _extend(kdb, box, vertical, target, bias):
    length = box.height() if vertical else box.width()
    extra = target - length
    low = (extra // 2, 0, extra)[bias]
    high = extra - low
    if vertical:
        return kdb.Box(box.left, box.bottom - low, box.right, box.top + high)
    return kdb.Box(box.left - low, box.bottom, box.right + high, box.top)


def _m1_spacing_clear(kdb, region, dbu, around):
    """Check M1.W.1 and S.1–S.6 around the edit using the packaged rule bounds."""
    if not region.width_check(_nm_to_dbu_ceil(18, dbu)).interacting(around).is_empty():
        return False
    corners = region.space_check(
        _nm_to_dbu_ceil(20, dbu),
        False,
        kdb.Region.Euclidian,
        90,
        None,
        _nm_to_dbu_ceil(0.25, dbu),
    )
    if not corners.interacting(around).is_empty():
        return False
    classes = _edge_classes(region.edges(), dbu)
    for _, one, two, nm in (("S.1", "long", "long", 18.0), *_TIP_RULES):
        distance = _nm_to_dbu_ceil(nm, dbu)
        a, b = classes[one], classes[two]
        if one == two:
            pairs = a.space_check(distance, False, kdb.Edges.Projection)
        else:
            pairs = a.separation_check(b, distance, False, kdb.Edges.Projection)
        if not pairs.interacting(around).is_empty():
            return False
    return True


def _repair_pair(kdb, pair, edges, metal, cuts, dbu):
    source = [edges.get(edge.bbox()) for edge in (pair.first, pair.second)]
    if any(box is None for box in source):
        return None
    vertical = pair.first.p1.x == pair.first.p2.x
    target = _nm_to_dbu_ceil(_SHORT_MAX_NM, dbu)
    # Each trial lengthens both facing edges. Preserve nearby polygons in full
    # so clipping cannot create an artificial tip at the window boundary.
    window = kdb.Region(pair.bbox().enlarged(2 * target))
    nearby = metal.interacting(kdb.Region(window.bbox().enlarged(target)))
    boundary = set(nearby.edges().each())
    if pair.first not in boundary or pair.second not in boundary:
        return None
    for bias in range(3):
        boxes = [_extend(kdb, box, vertical, target, bias) for box in source]
        if any(nearby.interacting(kdb.Region(box)).count() != 1 for box in boxes):
            continue
        extended = kdb.Region(boxes[0]) + kdb.Region(boxes[1])
        added = extended - nearby
        if added.is_empty() or not added.interacting(cuts).is_empty():
            continue
        if _m1_spacing_clear(kdb, (nearby + extended).merged(), dbu, window):
            return added
    return None


def repair_m1_landing_pairs(kdb, layout, top, lef_to_gds) -> int:
    """Repair only S.4 pairs with two actual V1 landing edges on ASAP7 M1."""
    # The merge supplies routing layers only. Require a physical ASAP7 V1
    # layer and respect an explicit cut mapping when the caller supplies one.
    if lef_to_gds.get("M1") != (19, 0) or lef_to_gds.get("V1", (21, 0)) != (21, 0):
        return 0
    metal_index, cut_index = layout.find_layer(19, 0), layout.find_layer(21, 0)
    if metal_index is None or cut_index is None:
        return 0
    metal = kdb.Region(top.begin_shapes_rec(metal_index)).merged()
    narrow = _edge_classes(metal.edges(), layout.dbu)["narrow"]
    pairs = narrow.space_check(
        _nm_to_dbu_ceil(31, layout.dbu), True, kdb.Edges.Projection
    )
    if pairs.is_empty():
        return 0
    edges = _landing_edges(kdb, top, metal_index, cut_index)
    cuts = kdb.Region(top.begin_shapes_rec(cut_index))
    v0 = layout.find_layer(18, 0)
    if v0 is not None:
        cuts += kdb.Region(top.begin_shapes_rec(v0))
    repaired = 0
    for pair in sorted(pairs.each(), key=lambda item: item.bbox()):
        extra = _repair_pair(kdb, pair, edges, metal, cuts, layout.dbu)
        if extra is not None:
            top.shapes(metal_index).insert(extra)
            metal = (metal + extra).merged()
            repaired += 1
    return repaired
