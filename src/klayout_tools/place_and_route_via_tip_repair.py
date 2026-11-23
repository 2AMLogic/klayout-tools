"""Repair ASAP7 M1 tip spacing at placed V1 landings."""

from __future__ import annotations

from itertools import permutations

from .place_and_route_via_tip import (
    _SHORT_MAX_NM,
    _TIP_RULES,
    _edge_classes,
    _nm_to_dbu_ceil,
    _placed_pads,
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
            enclosed = cuts.inside(kdb.Region(pad))
            if not pad.is_box() or enclosed.count() != 1:
                continue
            cut_polygon = next(enclosed.each())
            for trans in inst.cell_inst.each_cplx_trans():
                placed = pad.transformed(trans)
                if not placed.is_box():
                    continue
                cut_sides = kdb.Region(cut_polygon.transformed(trans)).edges()
                for edge in placed.each_edge():
                    flush = not (kdb.Edges(edge) & cut_sides).is_empty()
                    edges[edge.bbox()] = (placed.bbox(), flush)
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


def _wire_end(kdb, edge, depth):
    """The minimum-width strip inside a Manhattan polygon's facing edge.

    KLayout orients polygon boundaries with metal to their right.
    """
    dx, dy = edge.p2.x - edge.p1.x, edge.p2.y - edge.p1.y
    across_x = depth if dy > 0 else -depth if dy < 0 else 0
    across_y = -depth if dx > 0 else depth if dx < 0 else 0
    face = edge.bbox()
    return face + face.moved(across_x, across_y)


def _trial_sources(kdb, pair, landings, pad_sides, nearby, dbu, allow_pad_pair):
    first, second = pair.first, pair.second
    a, b = landings.get(first.bbox()), landings.get(second.bbox())
    if a is not None and b is not None:
        if allow_pad_pair:
            yield (a[0], b[0]), False
        return
    landing, wire_edge = (a, second) if a is not None else (b, first)
    if landing is None or not landing[1]:
        return
    # A partial or wire-covered pad edge is not an independent wire face.
    if not (kdb.Edges(wire_edge) & pad_sides).is_empty():
        return
    yield (landing[0],), True
    wire = _wire_end(kdb, wire_edge, _nm_to_dbu_ceil(18, dbu))
    if (kdb.Region(wire) - nearby).is_empty():
        yield (landing[0], wire), True


def _try_extensions(kdb, source, mixed, vertical, target, nearby, cuts, dbu, window):
    biases = [(bias,) * len(source) for bias in range(3)]
    if mixed and len(source) == 2:
        biases.extend(permutations(range(3), 2))
    for directions in biases:
        boxes = [
            _extend(kdb, box, vertical, target, direction)
            for box, direction in zip(source, directions, strict=True)
        ]
        if any(nearby.interacting(kdb.Region(box)).count() != 1 for box in boxes):
            continue
        extended = kdb.Region()
        for box in boxes:
            extended.insert(box)
        added = extended - nearby
        if added.is_empty() or not added.interacting(cuts).is_empty():
            continue
        if _m1_spacing_clear(kdb, (nearby + extended).merged(), dbu, window):
            return added
    return None


def _repair_pair(kdb, pair, landings, pad_sides, metal, cuts, dbu, allow_pad_pair):
    if pair.first.bbox() not in landings and pair.second.bbox() not in landings:
        return None
    if not pair.first.is_parallel(pair.second):
        return None
    target = _nm_to_dbu_ceil(_SHORT_MAX_NM, dbu)
    window = kdb.Region(pair.bbox().enlarged(2 * target))
    nearby = metal.interacting(kdb.Region(window.bbox().enlarged(target)))
    boundary = set(nearby.edges().each())
    if pair.first not in boundary or pair.second not in boundary:
        return None
    vertical = pair.first.p1.x == pair.first.p2.x
    for source, mixed in _trial_sources(
        kdb, pair, landings, pad_sides, nearby, dbu, allow_pad_pair
    ):
        added = _try_extensions(
            kdb, source, mixed, vertical, target, nearby, cuts, dbu, window
        )
        if added is not None:
            return added, mixed
    return None


def _repair_m1_pairs(kdb, layout, top, lef_to_gds):
    # The merge supplies routing layers only. Require a physical ASAP7 V1
    # layer and respect an explicit cut mapping when the caller supplies one.
    if lef_to_gds.get("M1") != (19, 0) or lef_to_gds.get("V1", (21, 0)) != (21, 0):
        return 0, 0
    metal_index, cut_index = layout.find_layer(19, 0), layout.find_layer(21, 0)
    if metal_index is None or cut_index is None:
        return 0, 0
    metal = kdb.Region(top.begin_shapes_rec(metal_index)).merged()
    classes = _edge_classes(metal.edges(), layout.dbu)
    narrow, tip = classes["narrow"], classes["tip"]
    spacing = _nm_to_dbu_ceil(31, layout.dbu)
    narrow_pairs = narrow.space_check(spacing, True, kdb.Edges.Projection)
    tip_pairs = tip.separation_check(narrow, spacing, True, kdb.Edges.Projection)
    if narrow_pairs.is_empty() and tip_pairs.is_empty():
        return 0, 0
    landings = _landing_edges(kdb, top, metal_index, cut_index)
    if not landings:
        return 0, 0
    # Even an ineligible landing (e.g. nested, V0-only or multi-cut) is via
    # metal and cannot supply the routed-wire face of a mixed repair.
    pad_sides = _placed_pads(kdb, layout, top, metal_index).edges()
    cuts = kdb.Region(top.begin_shapes_rec(cut_index))
    v0 = layout.find_layer(18, 0)
    if v0 is not None:
        cuts += kdb.Region(top.begin_shapes_rec(v0))
    pad_count = wire_count = 0
    for pairs, allow_pad_pair in ((narrow_pairs, True), (tip_pairs, False)):
        for pair in sorted(pairs.each(), key=lambda item: item.bbox()):
            result = _repair_pair(
                kdb, pair, landings, pad_sides, metal, cuts, layout.dbu, allow_pad_pair
            )
            if result is not None:
                extra, mixed = result
                top.shapes(metal_index).insert(extra)
                metal = (metal + extra).merged()
                if mixed:
                    wire_count += 1
                else:
                    pad_count += 1
    return pad_count, wire_count


def repair_m1_landing_pairs(kdb, layout, top, lef_to_gds, *, report=None) -> int:
    """Return the pad–pad count and expose the separate pad–wire count in the report."""
    pads, wires = _repair_m1_pairs(kdb, layout, top, lef_to_gds)
    if report is not None:
        report["m1_landing_pad_wire_pairs_extended"] = wires
    return pads
