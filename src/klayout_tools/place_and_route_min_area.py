"""Post-route minimum-*area* repair pass for ``klt place-and-route`` (issue
#2139).

Split out of ``place_and_route_gds_merge.py`` as its own self-contained
subsystem, the same way that module was itself split out of
``place_and_route.py`` (#1820): the merge module owns *combining* the routed
DEF with the standard-cell/macro GDS views, this module owns the one
geometric repair applied to the result. Only :func:`repair_min_area` crosses
the boundary back -- called exactly once, from
:func:`~klayout_tools.place_and_route_gds_merge._merge_def_to_gds`, right
before it writes the merged GDS out.

Why a repair pass exists at all
-------------------------------

A routed design's metal on any one layer is the *merge* of everything the
router, the PDN generator and the PDK's own tech-LEF via cells drew there.
Two sources routinely contribute a shape that is legal in isolation but
lands below that layer's own minimum-**area** rule once it is the whole
merged polygon:

1. **Tech-LEF via enclosures instantiated with too little attached wire.**
   sky130's ``sky130_fd_sc_hd__nom.tlef`` defines ``VIA L1M1_PR_MR`` with a
   ``0.29 x 0.23 um`` (``0.0667 um^2``) met1 landing and ``VIA M2M3_PR``
   with a ``0.33 x 0.33 um`` (``0.1089 um^2``) met3 landing -- both below
   the same PDK's own ``m1.6`` (``0.083 um^2``) / ``m3.6``
   (``0.240 um^2``) minimum-area rules. That is legal in the LEF: the
   enclosure is expected to merge with the routing wire it terminates. It
   becomes a real violation only where the router leaves too little
   attached metal on that layer for the merged polygon to clear the rule.

2. **PDN via patches on a strap layer with a large area floor.** sky130's
   ``m5.4`` is ``4.0 um^2``, ~17x met3's; a ``1.42 x 1.60 um``
   (``2.272 um^2``) met5 via landing violates it by construction.

Neither geometry is this repo's to change (one is the foundry's tech LEF,
the other OpenROAD's ``pdngen``), so the fix is the same one PR #2075
already shipped on the analog side for ``klt gen-compose``'s via-drop
landing pads (``gen_compose_routing._landing_pad_side_um_for_layer``):
resolve the **landing layer's own** ``*.area.*`` rule out of the resolved
PDK family's curated deck -- the *same* ``DrcRule`` set ``klt drc`` judges
the geometry with, never a private hard-coded threshold -- and floor the
drawn metal against it. The difference is that ``gen-compose`` authors its
landing pads itself and can simply draw them bigger, whereas the digital
flow receives finished geometry: here the floor is applied afterwards, by
extending a violating merged polygon with an abutting patch.

Safety posture
--------------

A repair that traded a minimum-area violation for a *short* would be
strictly worse than the violation it fixes, so every candidate patch must
pass all of:

* it stays inside the DEF's own ``DIEAREA`` (when one was parseable);
* it does not overlap, and keeps at least the layer's own minimum-*spacing*
  rule away from, every other shape already drawn on that layer -- including
  patches this same pass drew earlier;
* it is at least the layer's own minimum-*width* rule thick, and its shared
  boundary with the polygon it repairs is at least that wide too, so the
  repair can never itself author a sliver;
* the polygon it produces really is one merged polygon of at least the
  required area (verified, not assumed).

A violation with no passing candidate is reported in ``unrepaired`` rather
than repaired unsafely -- the other half of this issue's requirement: the
response never claims clean geometry it did not produce. And, mirroring the
scoping trade-off ``_landing_pad_side_um_for_layer`` already documents for
``gen-compose``, this pass reasons about area, width and spacing only: a
patch narrower than the side it attaches to leaves a concave step, which is
a *notch* geometry no rule modelled here scores. That is strictly better
than the guaranteed area violation it replaces, and ``klt drc`` still sees
the final geometry either way.
"""

from __future__ import annotations

import math
from typing import Any

from .decks import UnknownDeckError, get_deck, get_nominal_dbu
from .pdk_families import pdk_variant_family

#: How many ``unrepaired`` entries the report names individually before it
#: stops listing them. The counts (``remaining``, and each rule's own
#: ``violations_after``) stay exact; this only bounds how much geometry
#: detail one response carries, mirroring the existing truncation convention
#: in ``_merge_def_to_gds``'s own missing-/orphan-cell messages.
_MAX_REPORTED_UNREPAIRED = 20

#: Candidate growth directions, tried in this order, as ``(dx, dy)`` unit
#: vectors. Horizontal before vertical: a routed wire's own preferred
#: direction is not recoverable from merged geometry alone, and every
#: candidate is safety-checked anyway, so this is a stable tie-break rather
#: than a routing-aware heuristic.
_GROWTH_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _plain_layer_limits(
    variant: str,
) -> tuple[dict[tuple[int, int], dict[str, Any]], float] | None:
    """``({(layer, datatype): {"area": (min_dbu2, rule_id), "width":
    min_dbu, "space": min_dbu}}, nominal_dbu_um)`` for every layer in
    ``variant``'s resolved family deck that carries a **plain**
    minimum-area rule, or ``None`` when the family's deck cannot be
    resolved at all.

    Mirrors ``gen_compose_routing._min_area_um2_for_layer`` /
    ``_min_width_um_for_layer`` (issues #2072 / #1501) -- same deck, same
    ``other_layer``/``derived_layer`` filters, same "largest matching
    threshold wins" rule -- but collects **every** layer in one pass and
    carries the width/spacing floors alongside, because this pass needs the
    whole set up front and needs all three limits per layer to size a patch
    safely. ``derived_layer`` rules are excluded deliberately: a layer's
    ``*.holes_area.*`` sibling checks the area of an interior *void* of the
    merged region, which drawing more metal can never satisfy.

    An unresolvable family degrades this pass to a no-op rather than
    raising, exactly like both helpers it mirrors.
    """
    try:
        family = pdk_variant_family(variant)
        deck_rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except UnknownDeckError:
        return None

    limits: dict[tuple[int, int], dict[str, Any]] = {}
    for rule in deck_rules:
        if rule.other_layer is not None or rule.derived_layer is not None:
            continue
        entry = limits.setdefault(rule.layer, {"area": None, "width": 0, "space": 0})
        if rule.check == "area" and rule.area_min_dbu2 is not None:
            if entry["area"] is None or rule.area_min_dbu2 > entry["area"][0]:
                entry["area"] = (rule.area_min_dbu2, rule.id)
        elif rule.check == "width":
            entry["width"] = max(entry["width"], rule.threshold_dbu)
        elif rule.check == "space":
            entry["space"] = max(entry["space"], rule.threshold_dbu)
    return (
        {layer: entry for layer, entry in limits.items() if entry["area"] is not None},
        nominal_dbu_um,
    )


def _side_probe(kdb: Any, bbox: Any, direction: tuple[int, int]) -> Any:
    """A one-database-unit-thick box hugging ``bbox``'s ``direction`` side
    from the inside -- the probe :func:`_candidate_patch` intersects with a
    polygon to find how much of that side the polygon's own boundary
    actually occupies."""
    dx, dy = direction
    if dx > 0:
        return kdb.Box(bbox.right - 1, bbox.bottom, bbox.right, bbox.top)
    if dx < 0:
        return kdb.Box(bbox.left, bbox.bottom, bbox.left + 1, bbox.top)
    if dy > 0:
        return kdb.Box(bbox.left, bbox.top - 1, bbox.right, bbox.top)
    return kdb.Box(bbox.left, bbox.bottom, bbox.right, bbox.bottom + 1)


def _candidate_patch(
    kdb: Any,
    *,
    polygon: Any,
    direction: tuple[int, int],
    shortfall_dbu2: int,
    min_width_dbu: int,
) -> Any | None:
    """A ``kdb.Box`` abutting ``polygon`` on its ``direction`` side, sized so
    the merged result clears the layer's area floor -- or ``None`` when that
    side offers too narrow a shared boundary to attach to.

    The patch spans exactly the extent of the polygon's own boundary on that
    side (so it really abuts drawn metal instead of dangling off a recessed
    edge of the bounding box), is at least ``min_width_dbu`` thick, and is
    deep enough that ``span * depth`` covers ``shortfall_dbu2``. It lies
    strictly outside the polygon's bounding box, so the merged area is
    exactly ``polygon.area() + span * depth`` -- no overlap to discount.
    """
    dx, dy = direction
    bbox = polygon.bbox()
    probe = _side_probe(kdb, bbox, direction)
    touch = (kdb.Region(polygon) & kdb.Region(probe)).bbox()
    span = touch.height() if dx else touch.width()
    if span < max(min_width_dbu, 1):
        return None

    depth = max(min_width_dbu, int(math.ceil(shortfall_dbu2 / span)), 1)
    if dx > 0:
        return kdb.Box(bbox.right, touch.bottom, bbox.right + depth, touch.top)
    if dx < 0:
        return kdb.Box(bbox.left - depth, touch.bottom, bbox.left, touch.top)
    if dy > 0:
        return kdb.Box(touch.left, bbox.top, touch.right, bbox.top + depth)
    return kdb.Box(touch.left, bbox.bottom - depth, touch.right, bbox.bottom)


def _patch_is_safe(
    kdb: Any,
    *,
    patch: Any,
    polygon: Any,
    top_cell: Any,
    layer_index: int,
    min_area_dbu2: int,
    min_space_dbu: int,
    die_box: Any | None,
) -> bool:
    """Whether ``patch`` may be drawn on ``layer_index``: inside the die,
    clear of every other shape already on that layer by at least
    ``min_space_dbu``, and producing -- together with ``polygon`` -- one
    merged polygon of at least ``min_area_dbu2``.

    Neighbour geometry is fetched with ``begin_shapes_rec_touching`` over a
    box bounded by the patch plus the spacing rule (the same cheap bounded
    region query ``_box_is_inside_drawn_geometry`` uses), so this stays a
    local test rather than a full-layer flatten per violation -- and so it
    automatically sees patches this pass drew for *earlier* violations,
    which are already in ``top_cell``'s shapes by then.

    The spacing test is deliberately conservative: *any* overlap with
    another polygon is rejected, even a same-net one that would merely have
    merged harmlessly, because the merged geometry carries no connectivity
    this pass can consult and authoring a short in exchange for clearing an
    area rule would be strictly worse than the violation being repaired.
    """
    if die_box is not None and not (
        die_box.contains(patch.p1) and die_box.contains(patch.p2)
    ):
        return False

    # Enlarge by the FULL spacing rule, not `rule - 1`: two regions that
    # merely touch intersect in zero area, which `Region#&` reports as
    # empty, so a box grown by exactly `min_space_dbu` intersects a
    # neighbour if and only if the gap to it is *strictly less than*
    # `min_space_dbu` -- precisely the rule's own predicate. Growing by
    # `min_space_dbu - 1` instead silently accepts a patch sitting one
    # database unit short of the rule, i.e. trades this layer's area
    # violation for a 1 nm spacing violation (measured against sky130's
    # `met1.space.1` = 140 dbu: a neighbour 139 dbu from the patch passed;
    # `test_repair_never_leaves_a_patch_one_dbu_short_of_minimum_spacing`
    # pins it).
    clearance = max(min_space_dbu, 0)
    search = patch.enlarged(clearance + 1, clearance + 1)
    nearby = kdb.Region(top_cell.begin_shapes_rec_touching(layer_index, search))
    neighbours = nearby.merged() - kdb.Region(polygon)
    if not neighbours.is_empty():
        grown = kdb.Region(patch.enlarged(clearance, clearance))
        if not (grown & neighbours).is_empty():
            return False

    merged = (kdb.Region(polygon) + kdb.Region(patch)).merged()
    return merged.count() == 1 and merged.area() >= min_area_dbu2


def _repair_one_layer(
    kdb: Any,
    *,
    top_cell: Any,
    layer_index: int,
    min_area_dbu2: int,
    min_width_dbu: int,
    min_space_dbu: int,
    die_box: Any | None,
) -> tuple[int, int, list[Any]]:
    """Repair every sub-minimum-area polygon on one layer of ``top_cell``.

    Returns ``(violations_before, patches_drawn, still_violating)``, where
    ``still_violating`` is re-measured from the layer's own geometry *after*
    the patches were drawn -- never inferred from which candidates were
    rejected, so the report can only ever describe geometry that is really
    there.

    Patches are inserted into ``top_cell``'s own shapes (never into a placed
    cell: a standard-cell or via-cell definition is shared by every instance
    of it, so a patch drawn there would appear at every other instance too).
    """
    violations = (
        kdb.Region(top_cell.begin_shapes_rec(layer_index))
        .merged()
        .with_area(min_area_dbu2, None, True)
    )
    before = violations.count()
    if not before:
        return 0, 0, []

    patches_drawn = 0
    for polygon in violations.each_merged():
        shortfall = min_area_dbu2 - int(polygon.area())
        for direction in _GROWTH_DIRECTIONS:
            candidate = _candidate_patch(
                kdb,
                polygon=polygon,
                direction=direction,
                shortfall_dbu2=shortfall,
                min_width_dbu=min_width_dbu,
            )
            if candidate is None:
                continue
            if _patch_is_safe(
                kdb,
                patch=candidate,
                polygon=polygon,
                top_cell=top_cell,
                layer_index=layer_index,
                min_area_dbu2=min_area_dbu2,
                min_space_dbu=min_space_dbu,
                die_box=die_box,
            ):
                top_cell.shapes(layer_index).insert(candidate)
                patches_drawn += 1
                break

    remaining = (
        kdb.Region(top_cell.begin_shapes_rec(layer_index))
        .merged()
        .with_area(min_area_dbu2, None, True)
    )
    return before, patches_drawn, list(remaining.each_merged())


def _die_box(kdb: Any, die_area_um: tuple[float, float, float, float], dbu: float):
    """``die_area_um`` as an inward-rounded ``kdb.Box`` in database units --
    inward so a patch that merely touches the rounded boundary is still
    strictly inside the DEF's own ``DIEAREA``."""
    x0, y0, x1, y1 = die_area_um
    return kdb.Box(
        math.ceil(x0 / dbu),
        math.ceil(y0 / dbu),
        math.floor(x1 / dbu),
        math.floor(y1 / dbu),
    )


def repair_min_area(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    variant: str,
    routing_layers: set[tuple[int, int]],
    die_area_um: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Floor every routed-metal polygon in ``top_cell`` against its own
    layer's minimum-area DRC rule, and report what was and was not repaired
    (issue #2139).

    ``routing_layers`` restricts the pass to the ``(layer, datatype)`` pairs
    this PDK's own KLayout LEF/DEF layer map names as *routed net* geometry
    (``place_and_route_gds_merge._lef_net_layer_gds_map``) -- i.e. exactly
    the layers the router, the PDN and the tech LEF's via cells draw on.
    Standard-cell internals on non-routing layers are the foundry's
    geometry, already signed off, and are never touched. The scope narrows
    further to layers the resolved family's curated deck actually carries a
    plain ``*.area.*`` rule for.

    Returns

    ``status``
        ``"clean"`` (no sub-minimum-area polygon remains on any checked
        layer), ``"violations"`` (at least one could not be repaired
        safely -- see ``unrepaired``), or ``"skipped"`` (the pass could not
        run at all; ``reason`` says why).
    ``reason``
        ``None`` unless ``status`` is ``"skipped"``.
    ``patches``
        How many repair patches were drawn.
    ``repaired`` / ``remaining``
        Violating polygons cleared / still violating after the pass, both
        measured against the layer's own post-repair geometry.
    ``rules``
        Per-rule detail (``rule``, ``layer``, ``threshold_um2``,
        ``violations_before``, ``violations_after``, ``patches``) for every
        checked layer that had at least one violation, sorted by rule id.
    ``unrepaired``
        Up to :data:`_MAX_REPORTED_UNREPAIRED` still-violating polygons
        (``rule``, ``layer``, ``area_um2``, ``threshold_um2``,
        ``bbox_um``), so a caller can act on real residual geometry rather
        than on a bare count.

    Never raises for an unresolvable PDK family, a deck with no area rule on
    any routing layer, or an absent layer map -- each is reported as
    ``"skipped"`` with a reason, matching this subsystem's
    "malformed/absent input never raises" convention.
    """
    report: dict[str, Any] = {
        "status": "skipped",
        "reason": None,
        "patches": 0,
        "repaired": 0,
        "remaining": 0,
        "rules": [],
        "unrepaired": [],
    }

    if not routing_layers:
        report["reason"] = (
            "no KLayout LEF/DEF layer map resolved, so the merged GDS's "
            "routed-net layers cannot be matched against the deck's own "
            "minimum-area rules"
        )
        return report

    resolved = _plain_layer_limits(variant)
    if resolved is None:
        report["reason"] = (
            f"PDK variant '{variant}' resolves to no curated deck, so no "
            "minimum-area rule is available to floor routed metal against"
        )
        return report
    limits, nominal_dbu_um = resolved

    checked = sorted(set(limits) & set(routing_layers))
    if not checked:
        report["reason"] = (
            f"the curated deck for PDK variant '{variant}' carries no plain "
            "minimum-area rule on any routed-net layer"
        )
        return report

    dbu_scale = nominal_dbu_um / layout.dbu
    area_um2_per_dbu2 = layout.dbu * layout.dbu
    die_box = None if die_area_um is None else _die_box(kdb, die_area_um, layout.dbu)

    for layer in checked:
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        entry = limits[layer]
        min_area_dbu2 = round(entry["area"][0] * dbu_scale * dbu_scale)
        before, patches, unrepaired = _repair_one_layer(
            kdb,
            top_cell=top_cell,
            layer_index=layer_index,
            min_area_dbu2=min_area_dbu2,
            min_width_dbu=round(entry["width"] * dbu_scale),
            min_space_dbu=round(entry["space"] * dbu_scale),
            die_box=die_box,
        )
        if not before:
            continue
        threshold_um2 = min_area_dbu2 * area_um2_per_dbu2
        report["patches"] += patches
        report["repaired"] += before - len(unrepaired)
        report["remaining"] += len(unrepaired)
        report["rules"].append(
            {
                "rule": entry["area"][1],
                "layer": [layer[0], layer[1]],
                "threshold_um2": threshold_um2,
                "violations_before": before,
                "violations_after": len(unrepaired),
                "patches": patches,
            }
        )
        for polygon in unrepaired:
            if len(report["unrepaired"]) >= _MAX_REPORTED_UNREPAIRED:
                break
            bbox_um = polygon.bbox().to_dtype(layout.dbu)
            report["unrepaired"].append(
                {
                    "rule": entry["area"][1],
                    "layer": [layer[0], layer[1]],
                    "area_um2": float(polygon.area()) * area_um2_per_dbu2,
                    "threshold_um2": threshold_um2,
                    "bbox_um": [
                        bbox_um.left,
                        bbox_um.bottom,
                        bbox_um.right,
                        bbox_um.top,
                    ],
                }
            )

    report["status"] = "violations" if report["remaining"] else "clean"
    report["rules"].sort(key=lambda item: item["rule"])
    return report
