"""ASAP7 post-route ``VIA_*`` metal landing-pad trim for the DEF -> GDS merge
(issue #4495).

OpenROAD's ``VIA_*`` cells carry a metal landing pad on each of the two
layers they join, sized for TritonRoute's own spacing rules, which the
packaged ASAP7 deck's length-conditioned tip rules (Table 3.12.1
``M<n>.S.2``-``M<n>.S.5``) reject; ``route_drc_violation_count`` stays 0
because the router's rule set does not carry them. This pass drops a pad
polygon only when every placed instance of its via cell has that polygon
fully covered by routed/cell metal on the same layer *outside* any ``VIA_*``
cell, so connectivity is preserved by construction (overlap is same-net or
already a short). It is a documented no-op for every other PDK family -- the
GDS is not touched at all.

A pad that protrudes past the wire is shrunk to the via cut(s) it lands plus
``_CUT_ENCLOSURE_UM`` on every side, which is what changes the merged metal
region. A shrink is kept only when, in every placement, the shrunk pad still
touches the wire wherever the original pad did; otherwise the pad is left
alone. Without a cut layer in the layer map, or with no cut under the pad,
nothing is shrunk.

The returned report carries a ``tip_check`` sub-object
(:mod:`~klayout_tools.place_and_route_via_tip`): what the packaged deck's own
``M1``-``M3`` tip rules make of the merged geometry this pass leaves behind,
including how much of it is attributable to router-inserted landing pads.
That is the half ``route_drc_violation_count`` cannot report, and it is why
"trimmed" never has to be read as "deck-clean".

Layer scope (issue #4495's follow-up report: the same router-vs-deck
disagreement is not specific to the V1/M1 landing pad)
------------------------------------------------------------------------

``M1``-``M3`` over ``V1``-``V3``, which is exactly the span where both
halves of the rule set this pass is answerable to are uniform:

* the deck states the length-conditioned tip-spacing family for ``M1`` and
  applies it to ``M2``/``M3`` ("Section 3.12: rules stated for M1 apply to M2
  and M3 as well"). ``M4``-``M7`` instead get one unconditional 40 nm
  spacing, and ``M8``/``M9`` their own 80 nm-conditioned family, so a pad
  trim there would not be answering the same rule.
* every via enclosure the deck requires of those metals over those cuts is
  5 nm (``V1.M1.EN.1`` "5 & 2", ``V2.M2.EN.1``, ``V3.M3.EN.1`` note 2, and
  the ``V(x).M(x+1).EN.2`` "(5 & 5) | (5 & 0)" family), so one conservative
  all-four-sides enclosure is correct for the whole span. It stops at
  ``M4``: ``V3.M4.EN.2`` is 11 nm and ``V4``/``V5``/``V6``/``V7`` are 11 nm
  on both metals, so shrinking an ``M4``-and-above pad to a 5 nm enclosure
  would trade a tip violation for an enclosure violation.
"""

from __future__ import annotations

from typing import Any

from .pdk_families import pdk_variant_family
from .place_and_route_via_tip import check_via_pad_tips

_ASAP7_FAMILY = "asap7"

#: Routing layers whose ``VIA_*`` landing pads this pass may edit, and the
#: via cut layers whose enclosure bounds the shrink -- see the layer-scope
#: section above for why the span stops below ``M4``/``V4``.
_PAD_LEF_LAYERS = ("M1", "M2", "M3")
_CUT_LEF_LAYERS = ("V1", "V2", "V3")

#: Enclosure of a via cut by the metal above or below it, in microns: the
#: deck's 5 nm bound for every (``V1``-``V3``, ``M1``-``M3``) pair, applied
#: conservatively to all four sides (the DRM states it "on at least two
#: opposite sides", so requiring it on four keeps strictly more metal than
#: the rule does).
_CUT_ENCLOSURE_UM = 0.005
_VIA_PREFIX = "VIA_"


def _report(status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "cells_touched": 0,
        "pads_trimmed": 0,
        "pads_shrunk": 0,
        "pads_left": 0,
        "layers": [],
    }


def _wire_region(
    kdb: Any, top_cell: Any, metal_index: int, via_indices: set[int]
) -> Any:
    """Wire on one metal layer = every shape on it reachable from the top cell
    except through a ``VIA_*`` cell (router wires, PDN, standard-cell
    pin/obstruction metal)."""
    it = top_cell.begin_shapes_rec(metal_index)
    it.unselect_cells(sorted(via_indices))
    return kdb.Region(it).merged()


def _top_level_placements(top_cell: Any, via: Any) -> list[Any]:
    """Placements of ``via`` in the top cell; empty when it is also nested in
    another cell (conservative: the caller then leaves its pads alone)."""
    if any(p != top_cell.cell_index() for p in via.each_parent_cell()):
        return []
    placements: list[Any] = []
    for inst in top_cell.each_inst():
        if inst.cell_index == via.cell_index():
            placements.extend(inst.cell_inst.each_cplx_trans())
    return placements


def _shrunk_pad(
    kdb: Any, pad: Any, cuts: Any, enclosure: int, placements: list[Any], wire: Any
) -> Any:
    """``pad`` cut back to the via cut(s) it lands grown by ``enclosure``, or
    ``None`` when that is not a strict, connectivity-preserving reduction."""
    if cuts is None:
        return None
    single = kdb.Region(pad)
    landed = cuts.interacting(single)
    if landed.is_empty():
        return None
    shrunk = single & landed.sized(enclosure)
    if shrunk.is_empty() or (single - shrunk).is_empty():
        return None
    for t in placements:
        if not single.transformed(t).interacting(wire).is_empty() and (
            shrunk.transformed(t).interacting(wire).is_empty()
        ):
            return None
    return shrunk


def _split_pads(
    kdb: Any,
    pads: Any,
    placements: list[Any],
    wire: Any,
    cuts: Any = None,
    enclosure: int = 0,
) -> tuple[Any, int, int]:
    """Return ``(kept pads, dropped count, shrunk count)``: a pad is dropped
    only when wire fully covers it in every placement, else shrunk to its own
    cut's enclosure zone when that keeps it connected to the wire."""
    keep = kdb.Region()
    trimmed = 0
    shrunk_count = 0
    for pad in pads.each():
        single = kdb.Region(pad)
        if all((single.transformed(t) - wire).is_empty() for t in placements):
            trimmed += 1
            continue
        shrunk = _shrunk_pad(kdb, pad, cuts, enclosure, placements, wire)
        if shrunk is None:
            keep.insert(pad)
        else:
            keep.insert(shrunk)
            shrunk_count += 1
    return keep, trimmed, shrunk_count


def _cut_region(kdb: Any, via: Any, cut_indices: list[int]) -> Any:
    """Every via cut drawn inside ``via``, merged across cut layers, or
    ``None`` when it draws none. Merging the cut layers together is safe for
    the shrink: a cell that draws two cut layers (a stacked via) then keeps
    the union of both enclosure zones, which is strictly more metal than
    either cut alone requires."""
    cuts = kdb.Region()
    for index in cut_indices:
        cuts.insert(kdb.Region(via.shapes(index)))
    cuts = cuts.merged()
    return None if cuts.is_empty() else cuts


def _trim_one_layer(
    kdb: Any,
    *,
    top_cell: Any,
    via_cells: list[Any],
    via_indices: set[int],
    metal_index: int,
    placements: dict[int, list[Any]],
    cuts: dict[int, Any],
    enclosure: int,
    touched: set[int],
) -> dict[str, int]:
    """Trim every via cell's pads on one routing layer, editing the cells in
    place and recording in ``touched`` which were edited (so the caller counts
    a cell once however many of its layers it edits)."""
    wire = _wire_region(kdb, top_cell, metal_index, via_indices)
    counts = {"pads_trimmed": 0, "pads_shrunk": 0, "pads_left": 0}
    for via in via_cells:
        pads = kdb.Region(via.shapes(metal_index)).merged()
        if pads.is_empty():
            continue
        index = via.cell_index()
        if not placements[index]:
            counts["pads_left"] += pads.count()
            continue
        keep, trimmed, shrunk = _split_pads(
            kdb, pads, placements[index], wire, cuts[index], enclosure
        )
        counts["pads_trimmed"] += trimmed
        counts["pads_shrunk"] += shrunk
        counts["pads_left"] += pads.count() - trimmed - shrunk
        if trimmed or shrunk:
            via.shapes(metal_index).clear()
            via.shapes(metal_index).insert(keep)
            touched.add(index)
    return counts


def _trim(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Trim ASAP7 ``VIA_*`` landing pads on every routing layer in
    :data:`_PAD_LEF_LAYERS`: drop those redundant with routed wire on the same
    layer and shrink protruding ones to their via cut's enclosure.

    Returns ``status`` (``"trimmed"``: at least one pad dropped or shrunk; ``"clean"``:
    ran, nothing to drop; ``"skipped"``: could not run, ``reason`` says why --
    non-ASAP7 PDKs report ``"skipped"`` and leave the layout untouched),
    ``reason``, ``cells_touched`` (distinct via cells edited, counted once
    however many of their layers were), ``pads_trimmed``, ``pads_shrunk``
    (protruding pads cut back to the cut enclosure), ``pads_left`` (pad
    polygons left unchanged) and ``layers``: the same counts per routing layer
    that was checked -- one entry for each of :data:`_PAD_LEF_LAYERS` present
    in both the layer map and the layout, so a layer that was checked and had
    nothing to do is distinguishable from one that was never looked at.
    Never raises for absent input.
    """
    if pdk_variant_family(variant) != _ASAP7_FAMILY:
        return _report("skipped", f"PDK family of '{variant}' is not ASAP7; no-op")
    mapped = [name for name in _PAD_LEF_LAYERS if lef_to_gds.get(name) is not None]
    if not mapped:
        return _report(
            "skipped",
            "no M1/M2/M3 routed-net layer in the KLayout layer map",
        )
    metals = [
        (name, index)
        for name, index in (
            (name, layout.find_layer(*lef_to_gds[name])) for name in mapped
        )
        if index is not None
    ]
    if not metals:
        return _report("clean")

    via_cells = [
        c
        for c in layout.each_cell()
        if c.name.startswith(_VIA_PREFIX) and c.cell_index() != top_cell.cell_index()
    ]
    if not via_cells:
        return _report("clean")
    cut_indices = [
        index
        for index in (
            layout.find_layer(*lef_to_gds[name])
            for name in _CUT_LEF_LAYERS
            if lef_to_gds.get(name) is not None
        )
        if index is not None
    ]
    enclosure = round(_CUT_ENCLOSURE_UM / layout.dbu)
    via_indices = {c.cell_index() for c in via_cells}
    placements = {c.cell_index(): _top_level_placements(top_cell, c) for c in via_cells}
    cuts = {c.cell_index(): _cut_region(kdb, c, cut_indices) for c in via_cells}

    report = _report("clean")
    touched: set[int] = set()
    for name, metal_index in metals:
        counts = _trim_one_layer(
            kdb,
            top_cell=top_cell,
            via_cells=via_cells,
            via_indices=via_indices,
            metal_index=metal_index,
            placements=placements,
            cuts=cuts,
            enclosure=enclosure,
            touched=touched,
        )
        for key in ("pads_trimmed", "pads_shrunk", "pads_left"):
            report[key] += counts[key]
        report["layers"].append(
            {"layer_name": name, "layer": list(lef_to_gds[name]), **counts}
        )
    report["cells_touched"] = len(touched)
    if report["pads_trimmed"] or report["pads_shrunk"]:
        report["status"] = "trimmed"
    return report


def trim_via_pads(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """:func:`_trim`'s report plus the ``tip_check`` sub-report: what the
    deck's own tip rules say about the merged geometry this pass leaves
    behind (:func:`~klayout_tools.place_and_route_via_tip.check_via_pad_tips`,
    issue #4495).

    The two belong together because neither answers the caller's actual
    question alone. ``"trimmed"`` says pads were edited, not that the deck
    agrees; and the deck's tip rules are exactly what ``route_drc_violation_count``
    cannot report. So the repair runs first, and the flag then measures the
    geometry that is actually written -- which is also why it must run after,
    never before, the trim.
    """
    report = _trim(
        kdb,
        layout=layout,
        top_cell=top_cell,
        variant=variant,
        lef_to_gds=lef_to_gds,
    )
    report["tip_check"] = check_via_pad_tips(
        kdb,
        layout=layout,
        top_cell=top_cell,
        variant=variant,
        lef_to_gds=lef_to_gds,
    )
    return report
