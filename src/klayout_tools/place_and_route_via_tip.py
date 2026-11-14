"""ASAP7 post-route tip-spacing *flag* for the DEF -> GDS merge (issue #4495).

Why a flag and not another repair
---------------------------------

``place-and-route``'s ``route_drc_violation_count`` is TritonRoute's own
count, and TritonRoute's ASAP7 rule set does not carry the packaged deck's
length-conditioned metal tip spacings (Table 3.12.1 ``M1.S.2``-``M1.S.5``,
stated for ``M1`` and applying to ``M2``/``M3`` as well). A routed block can
therefore report ``route_drc_violation_count: 0`` while
``klt drc --deck asap7`` reports hundreds of tip-spacing violations against
the very geometry the router inserted -- the disagreement issue #4495 asks
this stage to surface.

:func:`~klayout_tools.place_and_route_via_pad.trim_via_pads` (the sibling
repair pass) can only act where acting is provably safe: it drops a landing
pad fully covered by routed wire, or shrinks a protruding one back to its own
via cut plus the deck's enclosure. Neither applies to a pad that is *already*
minimal -- exactly the shipped ``VIA_VIA12``/``VIA_VIA23``/``VIA_VIA34``
geometry, whose pads are drawn at cut + enclosure already. Those pads still
break the deck's tip rules against *neighbouring* metal, and nothing this
stage may safely change would fix it. So this pass measures and reports
instead of repairing: it never writes geometry, so the merged GDS is
byte-identical whether it runs or not, on every PDK including ASAP7.

What it measures
----------------

For each routing layer that carries the tip-rule family (``M1``, ``M2``,
``M3``), the merged layer region's own boundary edges are classified by
length exactly as the deck classifies them, and each rule's edge-pair check
is run with the deck's own metric (projection) and threshold:

======  =========================================  =========
Rule    Facing edges                               Threshold
======  =========================================  =========
S.2     one ``<= 36`` nm, one ``> 36`` nm (side)   25 nm
S.3     both ``24..36`` nm (tip to tip)            27 nm
S.4     both ``< 24`` nm (narrow tip to tip)       31 nm
S.5     one ``24..36`` nm, one ``< 24`` nm         31 nm
======  =========================================  =========

It is called by
:func:`~klayout_tools.place_and_route_via_pad.trim_via_pads` once that pass
has finished editing geometry, and its report rides out as that pass's own
``tip_check`` sub-object (``via_pad_trim.tip_check`` in the
``place-and-route`` response): the repair says what it changed, the flag says
what the deck still makes of the result.

Each rule's hits are reported twice: ``violations`` (every hit on the layer,
the deck-equivalent count) and ``via_pads`` (the subset whose edge pair
touches the M* landing pad of a ``VIA_*`` cell -- the router-inserted
geometry this issue is about). The two together say whether a non-zero deck
result is the router's landing geometry or the design's own routing.

This is a targeted measurement, not a deck run: it loads no ``.drc`` script
and needs no ``klayout`` *binary*, only the ``klayout.db`` module the merge
already depends on. It is deliberately narrower than ``klt drc --deck
asap7``, which stays the signoff verdict:

* only ``M1``-``M3`` are checked. The deck states the length-conditioned
  family for those three layers only; ``M4``-``M7`` get one unconditional
  40 nm spacing and ``M8``/``M9`` their own 80 nm-conditioned family.
* only the tip rules are checked -- not ``S.1`` (side to side), ``S.6``
  (corner to corner), widths, areas or enclosures.
* attribution is geometric: an edge pair is counted against via pads when it
  touches pad geometry, which cannot distinguish a pad's own tip edge from a
  routed wire edge abutting that pad.

Thresholds are declared here rather than resolved from a curated rule table
because ASAP7 has none (``get_deck("asap7")`` raises ``UnknownDeckError`` --
its DRC *is* the ``asap7.drc`` script, which is also why
``repair_min_area`` is a no-op on ASAP7). ``tests/test_place_and_route_via_tip.py``
keeps this table honest from both sides: it asserts every threshold here
matches the one ``decks/asap7.drc`` states, and runs the real deck through
the ``klayout`` binary over a generated layout to confirm the rule ids this
pass reports are the ones the deck reports.
"""

from __future__ import annotations

import math
from typing import Any

from .pdk_families import pdk_variant_family

_ASAP7_FAMILY = "asap7"
_VIA_PREFIX = "VIA_"

#: Routing layers the deck states the length-conditioned tip-spacing family
#: for (``decks/asap7.drc``, Table 3.12.1: "rules stated for M1 apply to M2
#: and M3 as well"). ``M4`` upwards carry a different, unconditional spacing
#: family and are deliberately not checked here.
_TIP_RULE_LEF_LAYERS = ("M1", "M2", "M3")

#: Edge-length class boundaries in nm, from Table 3.12.1: "> 36 nm" is a long
#: (side) edge, "<= 36 nm" a short one, "24..36 nm" a tip and "< 24 nm" a
#: narrow tip. 36.25 nm (not 36) is the deck's own bound, so that a 36 nm edge
#: counts as short at the deck's 0.25 nm nominal dbu.
_SHORT_MAX_NM = 36.25
_NARROW_MAX_NM = 24.0

#: ``(rule suffix, first edge class, second edge class, threshold nm)`` --
#: one entry per deck check in the Table 3.12.1 tip family. The class names
#: index :func:`_edge_classes`. A rule whose two classes are the *same* class
#: is the deck's single-collection ``space`` check; one with two different
#: classes is its two-collection ``separation`` check. The distinction is not
#: cosmetic: a ``separation`` of a collection against itself counts a
#: different number of edge pairs than ``space`` over that collection, so
#: using one primitive for both under-counts ``S.3``/``S.4`` against the
#: deck (measured: 1 vs 4 and 12 vs 25 on the in-repo ASAP7 canary block).
_TIP_RULES: tuple[tuple[str, str, str, float], ...] = (
    ("S.2", "short", "long", 25.0),
    ("S.3", "tip", "tip", 27.0),
    ("S.4", "narrow", "narrow", 31.0),
    ("S.5", "tip", "narrow", 31.0),
)


def _report(status: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "violations": 0,
        "via_pad_violations": 0,
        "rules": [],
    }


def _nm_to_dbu_ceil(nm: float, dbu_um: float) -> int:
    """``nm`` as an exclusive upper length bound in database units, rounded
    **up**: a layout coarser than the deck's own 0.25 nm dbu must still count
    a 36 nm edge as short against the deck's 36.25 nm bound."""
    return int(math.ceil(nm / (dbu_um * 1000.0)))


def _nm_to_dbu(nm: float, dbu_um: float) -> int:
    return int(round(nm / (dbu_um * 1000.0)))


def _edge_classes(edges: Any, dbu_um: float) -> dict[str, Any]:
    """The four Table 3.12.1 length classes of ``edges``.

    ``with_length`` takes a half-open ``[min, max)`` range in database units,
    which is why the bounds are the deck's own exclusive ones.
    """
    short_max = _nm_to_dbu_ceil(_SHORT_MAX_NM, dbu_um)
    narrow_max = _nm_to_dbu_ceil(_NARROW_MAX_NM, dbu_um)
    unbounded = edges.bbox().width() + edges.bbox().height() + 1
    return {
        "long": edges.with_length(short_max, unbounded, False),
        "short": edges.with_length(0, short_max, False),
        "tip": edges.with_length(narrow_max, short_max, False),
        "narrow": edges.with_length(0, narrow_max, False),
    }


def _placed_pads(kdb: Any, layout: Any, top_cell: Any, layer_index: int) -> Any:
    """Every ``VIA_*`` cell's shapes on ``layer_index``, in top-cell
    coordinates -- the router-inserted landing geometry this pass attributes
    violations to. Pads nested inside another cell are included too, via the
    recursive shape iterator restricted to those cells."""
    via_indices = [
        c.cell_index()
        for c in layout.each_cell()
        if c.name.startswith(_VIA_PREFIX) and c.cell_index() != top_cell.cell_index()
    ]
    if not via_indices:
        return None
    pads = kdb.Region()
    it = top_cell.begin_shapes_rec(layer_index)
    it.unselect_all_cells()
    for index in via_indices:
        it.select_cells([index])
    pads.insert(it)
    return pads.merged()


def _check_layer(
    kdb: Any,
    *,
    merged: Any,
    pads: Any,
    lef_layer: str,
    gds: tuple[int, int],
    dbu_um: float,
) -> list[dict[str, Any]]:
    """One routing layer's tip-rule entries, deck rule ids and all."""
    classes = _edge_classes(merged.edges(), dbu_um)
    entries: list[dict[str, Any]] = []
    for suffix, first, second, threshold_nm in _TIP_RULES:
        threshold = _nm_to_dbu(threshold_nm, dbu_um)
        if first == second:
            pairs = classes[first].space_check(threshold, False, kdb.Edges.Projection)
        else:
            pairs = classes[first].separation_check(
                classes[second], threshold, False, kdb.Edges.Projection
            )
        total = pairs.count()
        if not total:
            continue
        attributed = pairs.interacting(pads).count() if pads is not None else 0
        entries.append(
            {
                "rule": f"{lef_layer}.{suffix}",
                "layer": [gds[0], gds[1]],
                "threshold_nm": threshold_nm,
                "violations": total,
                "via_pads": attributed,
            }
        )
    return entries


def check_via_pad_tips(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    lef_to_gds: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Count the deck's ``M1``-``M3`` tip-spacing violations in the merged
    layout, and how many of them touch a ``VIA_*`` landing pad.

    Report-only: ``layout`` is never modified, so the merged GDS is
    byte-identical whether this pass runs or not.

    Returns ``status`` (``"violations"``: at least one hit; ``"clean"``: ran,
    none; ``"skipped"``: could not run, ``reason`` says why -- every
    non-ASAP7 PDK reports ``"skipped"``), ``reason``, ``violations`` and
    ``via_pad_violations`` (totals over every rule), and ``rules``: one entry
    per deck rule id with a non-zero count, carrying its ``layer``,
    ``threshold_nm``, ``violations`` and the ``via_pads`` subset.
    Never raises for absent input.
    """
    if pdk_variant_family(variant) != _ASAP7_FAMILY:
        return _report("skipped", f"PDK family of '{variant}' is not ASAP7; no-op")

    report = _report("clean")
    checked = 0
    for lef_layer in _TIP_RULE_LEF_LAYERS:
        gds = lef_to_gds.get(lef_layer)
        if gds is None:
            continue
        layer_index = layout.find_layer(*gds)
        if layer_index is None:
            continue
        checked += 1
        merged = kdb.Region(top_cell.begin_shapes_rec(layer_index)).merged()
        if merged.is_empty():
            continue
        pads = _placed_pads(kdb, layout, top_cell, layer_index)
        report["rules"].extend(
            _check_layer(
                kdb,
                merged=merged,
                pads=pads,
                lef_layer=lef_layer,
                gds=gds,
                dbu_um=layout.dbu,
            )
        )
    if not checked:
        return _report(
            "skipped",
            "no M1/M2/M3 routed-net layer in the KLayout layer map and layout",
        )
    report["violations"] = sum(entry["violations"] for entry in report["rules"])
    report["via_pad_violations"] = sum(entry["via_pads"] for entry in report["rules"])
    if report["violations"]:
        report["status"] = "violations"
    return report
