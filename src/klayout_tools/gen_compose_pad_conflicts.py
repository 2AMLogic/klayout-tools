"""Pad-vs-pitch conflict messaging for ``klt gen-compose`` (issues #5803/#6058).

The check side of the landing-pad work splits in two: sizes and boxes live
in :mod:`.gen_compose_landing_pads` (the memoized ``side_for_pair``
resolver, the #1520 pad-box loop, the #1913 intermediate-pad regions);
this module holds the *message* cluster that turns a route-vs-route
conflict whose geometry is a landing pad into the named pad-vs-pitch
rejection -- the pad's drawn side (deck floors, and the #6058
minimal-ladder intermediate floor, included), the terminal pitch, the
deck's spacing rule, and the escape hatch that matches the pad's own
sizing policy:

* ``_pad_is_intermediate`` -- whether a drop's own ``terminal_landing_layers``
  marking classifies one of its landings as a minimal-ladder intermediate
  (issue #6058);
* ``_drawn_pad_side_um`` -- the side the composed GDS will really carry;
* ``_pad_conflict_escape_note`` -- the named fix, per the pad's policy;
* ``_conductive_contact_message`` -- the #7024/#7221 shared-edge or
  shared-corner short between a candidate and an accepted net;
* ``_leg_conflict_message`` -- the #1057 overlap, #7024/#7221 contact and
  #1386 spacing verdicts, upgraded to consult the pad rejection first;
* ``_landing_pad_conflict_reason`` -- the rejection itself;
* ``_terminal_pitch_um`` -- centre-to-centre pitch of a pad vs a region;
* ``_pad_other_block_contact_reason`` -- the #7325 pad-vs-other-block
  electrical-contact rejection.

``gen_compose_landing_pads`` re-imports ``_pad_is_intermediate`` (its
resolver needs it) and re-exports the message entry points, so every
pre-existing ``from klayout_tools.gen_compose_landing_pads import <name>``
path keeps resolving unchanged -- the same convention the earlier splits
of this family (``gen_compose_layer_rules``, ``gen_compose_via_cuts``,
``gen_compose_landing_pads`` itself) established.

The resolver and conflict checks share the landing-pad and via-cut geometry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .gen_compose_base import _VIA_LANDING_SIZE_UM, _drop_array_extra_um
from .gen_compose_pad_ports import PortLocations, contacted_port
from .gen_compose_plane_change import RouteConflict


def leg_pad_notch_conflict(
    region: Any,
    layer: tuple[int, int] | None,
    spacing: tuple[float, str] | None,
    dbu: float,
) -> RouteConflict | None:
    """Check the primary layer's drawn pad and backbone on the output grid."""
    if spacing is None:
        return None
    required_um, rule = spacing
    spacing_dbu = round(required_um / dbu)
    if spacing_dbu <= 0:
        return None
    gaps = region.merged().notch_check(spacing_dbu)
    if gaps.is_empty():
        return None
    closest_um = min(pair.distance() for pair in gaps) * dbu
    return RouteConflict(
        f"this leg's landing pad and backbone form a {closest_um:.4g}um notch "
        f"on layer {layer}. '{rule}' requires at least {required_um:.4g}um "
        "of spacing. Move the turn farther from the landing pad."
    )


def _pad_is_intermediate(
    drop: dict[str, Any] | None, pad_layer: tuple[int, int]
) -> bool:
    """Whether ``drop`` draws ``pad_layer`` as a minimal-ladder intermediate.

    ``True`` only when the drop carries the ``terminal_landing_layers``
    marking (issue #6058: :func:`.gen_compose_routing._resolve_via_drops`
    records which of a hop's two ``landing_layers`` are this endpoint's own
    terminal planes) and ``pad_layer`` is not one of them. A drop without
    the marking (a caller-built drop, the ``klt route`` path, or any
    pre-#6058 entry) is never intermediate -- it keeps today's sizing.
    """
    terminal_layers = (drop or {}).get("terminal_landing_layers")
    return terminal_layers is not None and pad_layer not in terminal_layers


def _conductive_contact_message(
    cand_region: Any, other_region: Any, other_net: str
) -> str | None:
    """The short verdict for a shared edge or corner (#7024, #7221), or ``None``.

    A shared edge -- or a single shared corner point -- conducts even when
    the polygon intersection has no area. ``kdb.Region.interacting`` is
    "shares at least one point": the rule ``kdb.LayoutToNetlist`` connects
    on, so `klt extract` would read the two nets as one node. A positive
    gap never interacts, and neither does a pair whose *bounding boxes*
    merely meet. The caller has already ruled out a positive-area overlap.
    """
    if cand_region.interacting(other_region).is_empty():
        return None
    if (cand_region.edges() & other_region.edges()).is_empty():
        return f"shares a conductive corner with already-routed net '{other_net}'"
    return f"shares a conductive edge with already-routed net '{other_net}'"


def _leg_conflict_message(
    variant: str,
    via_drops: list[dict[str, Any]],
    cand_layer: tuple[int, int] | None,
    other_net: str,
    cand_region: Any,
    other_region: Any,
    dbu: float,
    side_for_pair: Any,
    spacing_lookup: Any,
    check_spacing: bool,
) -> str | None:
    """The overlap-or-spacing verdict for one (candidate, accepted) pair.

    Relocated verbatim from ``compose()``'s ``_leg_conflict`` loop body
    (issue #5803, beside the pad-vs-pitch rejection it now folds in): the
    #1057 overlap test; the electrical-contact test (#7024, #7221) -- a
    shared edge or corner, on the primary entry and the #1913
    intermediate/far pad entries alike, whatever the deck's space rules
    say; then the #1386 spacing test wherever the caller enables
    ``check_spacing``. The overlap and spacing tests consult
    :func:`_landing_pad_conflict_reason` first so a conflict at a landing
    pad names pad-vs-pitch instead of a generic backbone crossing. Returns
    ``None`` when no test fires -- the caller keeps looping.

    The #1386 spacing test grows the candidate by the deck's own same-layer
    ``"space"`` threshold (``kdb.Region.sized`` -- the standard "distance < d"
    Minkowski-sum test, symmetric regardless of which side grows) and
    intersects that with the other region. A layer with no known ``"space"``
    rule keeps the overlap and contact tests only (#6961).

    The overlap and spacing messages (and their thresholds) are
    byte-identical to the pre-#5803 wording. What they say changes only
    for a pad-involving conflict and -- #7221 -- for a touching pair on
    the primary entry, which now reports the contact rather than the
    spacing miss it used to be caught as.
    """
    if not (cand_region & other_region).is_empty():
        if not check_spacing:
            reason = _landing_pad_conflict_reason(
                variant,
                via_drops,
                cand_layer,
                other_net,
                other_region,
                dbu,
                side_for_pair,
                spacing_lookup,
            )
            if reason is not None:
                return reason
        return f"crosses already-routed net '{other_net}'"
    # The contact test runs on both entries and ahead of the spacing branch:
    # an electrical contact is a short whether or not the deck has a space
    # rule for the layer.
    contact = _conductive_contact_message(cand_region, other_region, other_net)
    if contact is not None:
        return contact
    if not check_spacing:
        return None
    cand_spacing = spacing_lookup(cand_layer)
    if cand_spacing is None:
        return None
    cand_spacing_um, cand_rule_id = cand_spacing
    cand_spacing_dbu = int(round(cand_spacing_um / dbu))
    if cand_spacing_dbu <= 0:
        return None
    if not (cand_region.sized(cand_spacing_dbu) & other_region).is_empty():
        reason = _landing_pad_conflict_reason(
            variant,
            via_drops,
            cand_layer,
            other_net,
            other_region,
            dbu,
            side_for_pair,
            spacing_lookup,
            spacing_um=cand_spacing_um,
        )
        if reason is not None:
            return reason
        return (
            f"comes within {cand_spacing_um:.4g}um of "
            f"already-routed net '{other_net}' -- closer "
            "than the resolved deck's own "
            f"'{cand_rule_id}' minimum same-layer spacing "
            "rule (no literal overlap, but still a real "
            "`klt drc` violation on the composed layout)"
        )
    return None


def _landing_pad_conflict_reason(
    variant: str,
    via_drops: list[dict[str, Any]],
    pad_layer: tuple[int, int] | None,
    other_net: str,
    other_region: Any,
    dbu: float,
    side_for_pair: Any,
    spacing_lookup: Any,
    spacing_um: float | None = None,
) -> str | None:
    """The named pad-vs-pitch rejection (issue #5803), or ``None``.

    Called from ``compose()``'s ``_leg_conflict`` when a candidate leg's
    conflict against an already-accepted net involves a landing pad this
    leg draws on ``pad_layer`` -- either a literal overlap (the
    intermediate/far pad entries #1913 compare overlap-only, on a plane
    neither net's primary layer names) or a spacing-rule fire on the
    primary entry whose region mixes the backbone with a route-layer pad.
    Today both surface as a generic "crosses/comes within already-routed
    net ..." message that reads as a *backbone* problem, sending the caller
    to fix width/waypoints when the binding constraint is the pad
    footprint vs the neighbouring terminal's pitch -- the compatibility
    wall #5803 records.

    ``spacing_um`` selects the mode: ``None`` (overlap) requires the pad
    itself to literally intersect ``other_region``; a spacing threshold
    requires the pad grown by that threshold to -- the same test the
    caller just fired on its whole-footprint region, restricted to the
    pad so a backbone-only conflict keeps the generic (accurate) message.

    Returns ``None`` (caller falls back to the generic message unchanged)
    when no via_drops entry contributes a pad on ``pad_layer``, or the
    conflict does not involve one -- i.e. when it is genuinely
    backbone-only.

    The message names the three things the caller needs to reconcile the
    conflict: the pad footprint (the effective drawn side for this
    ``(landing, via)`` pair), the terminal pitch (centre-to-centre, see
    :func:`_terminal_pitch_um`; 0 for an outright overlap), and the
    resolved deck's own spacing rule when the layer carries one -- plus the
    ``routing.landing_pad_size_um`` override as the escape hatch where the
    deck's floors allow a smaller pad.
    """
    import klayout.db as kdb

    if pad_layer is None:
        return None  # unknown candidate layer -- no pair-keyed pad to name
    pad_drops = [d for d in via_drops if pad_layer in (d.get("landing_layers") or ())]
    if not pad_drops:
        return None  # backbone-only conflict -- generic message is accurate

    def drawn_side_um(drop: dict[str, Any]) -> float:
        return _drawn_pad_side_um(variant, side_for_pair, pad_layer, drop)

    pad_region = kdb.Region()
    for drop in pad_drops:
        half_dbu = int(round(drawn_side_um(drop) / 2.0 / dbu))
        cx = int(round(drop["x_um"] / dbu))
        cy = int(round(drop["y_um"] / dbu))
        pad_region.insert(
            kdb.Box(cx - half_dbu, cy - half_dbu, cx + half_dbu, cy + half_dbu)
        )
    if spacing_um is None:
        if (pad_region & other_region).is_empty():
            return None  # the backbone conflicts, the pad does not
    else:
        spacing_dbu = int(round(spacing_um / dbu))
        if (
            spacing_dbu <= 0
            or (pad_region.sized(spacing_dbu) & other_region).is_empty()
        ):
            return None  # the backbone is the near side, the pad is not
    drop = pad_drops[0]
    side_um = drawn_side_um(drop)
    px, py = float(drop["x_um"]), float(drop["y_um"])
    pitch_um = _terminal_pitch_um(pad_region, other_region, px, py, side_um, dbu)
    spacing = spacing_lookup(pad_layer)
    spacing_note = ""
    if spacing is not None:
        spacing_um_rule, rule_id = spacing
        spacing_note = (
            f"; '{rule_id}' additionally requires >= {spacing_um_rule:.4g}um "
            "same-layer edge-to-edge clearance to a neighbouring shape"
        )
    need_um = side_um + (spacing[0] if spacing is not None else 0.0)
    if pitch_um <= 1e-9:
        conflict = (
            f"overlaps already-routed net '{other_net}' on that plane -- "
            "the two terminals' pads touch outright (pitch 0um)"
        )
    else:
        conflict = (
            f"sits {pitch_um:.4g}um (terminal pitch, centre-to-centre) "
            f"from already-routed net '{other_net}' on that plane"
        )
    # Issue #6058: the escape hatch named here must match the pad's own
    # sizing policy (already-minimal, mode-off intermediate, or terminal).
    escape = _pad_conflict_escape_note(side_for_pair, drop, pad_layer)
    return (
        f"via-drop landing pad ({side_um:.4g}um square at ({px:.4g}, {py:.4g}) "
        f"on layer {pad_layer}) {conflict} -- a {side_um:.4g}um landing-pad "
        f"footprint needs >= {need_um:.4g}um of terminal pitch to clear a "
        f"neighbouring terminal{spacing_note} -- a generator whose terminal "
        f"pitch is below the landing-pad footprint cannot be landed on "
        f"independently: {escape}"
    )


def _drawn_pad_side_um(
    variant: str,
    side_for_pair: Any,
    pad_layer: tuple[int, int],
    drop: dict[str, Any],
) -> float:
    """The side drawing will actually insert for this drop's pair on
    ``pad_layer`` (the message path of issue #5803's pad-vs-pitch
    rejection).

    Report the same area and enclosure floors used by drawing. A
    ``minimal_ladder`` intermediate landing (issue #6058) is the one pad
    whose drawn side *excludes* the fixed baseline: the resolver built
    with the mode answers its per-pair floor directly, so report that
    rather than re-max-ing the baseline over it.
    """
    from .gen_compose_ladder_floors import _intermediate_landing_pad_floor_um
    from .gen_compose_layer_rules import _landing_pad_floor_um
    from .gen_compose_via_cuts import via_drop_cut_side_um

    if getattr(side_for_pair, "minimal_ladder", False) and _pad_is_intermediate(
        drop, pad_layer
    ):
        floor_side, _floors = _intermediate_landing_pad_floor_um(
            variant,
            pad_layer,
            drop["via_layer"],
            via_drop_cut_side_um(variant, drop["via_layer"]),
        )
        return floor_side + _drop_array_extra_um(drop)
    pair_side = side_for_pair(pad_layer, drop["via_layer"])
    floor_side, _floors = _landing_pad_floor_um(
        variant,
        pad_layer,
        drop["via_layer"],
        via_drop_cut_side_um(variant, drop["via_layer"]),
    )
    return max(pair_side, _VIA_LANDING_SIZE_UM, floor_side) + _drop_array_extra_um(drop)


def _pad_conflict_escape_note(side_for_pair: Any, drop: dict[str, Any], pad_layer):
    """The named fix for a pad-vs-pitch rejection, per the pad's own policy.

    Issue #6058: the escape hatch named here must match the pad's own
    sizing policy. A minimal-ladder intermediate pad is already at its
    per-pair floor, and ``routing.landing_pad_size_um`` is rejected
    together with the mode -- so for that pad the only fixes left are a
    wider pitch or another plane. A mode-off intermediate pad, conversely,
    can gain clearance just by enabling the mode.
    """
    minimal_mode = getattr(side_for_pair, "minimal_ladder", False)
    if minimal_mode and _pad_is_intermediate(drop, pad_layer):
        return (
            "this pad is already at its routing.minimal_ladder (issue #6058) "
            "per-pair enclosure floor -- widen the pitch, or land one of "
            "the two nets on another plane"
        )
    if not minimal_mode and _pad_is_intermediate(drop, pad_layer):
        return (
            "request routing.minimal_ladder (issue #6058) to size this "
            "intermediate ladder pad at its own per-pair enclosure floor, "
            "or routing.landing_pad_size_um (issue #5803) where the deck's "
            "floors allow a smaller pad, or land one of the two nets on "
            "another plane"
        )
    return (
        "request routing.landing_pad_size_um (issue #5803) "
        "where the deck's floors allow a smaller pad, or land one of "
        "the two nets on another plane"
    )


def _terminal_pitch_um(
    pad_region: Any, other_region: Any, px: float, py: float, side_um: float, dbu: float
) -> float:
    """Centre-to-centre terminal pitch of a pad vs ``other_region``, in um.

    0 on an outright overlap; otherwise the pad centre's distance to the
    nearest point of ``other_region`` plus half the pad side -- exact for a
    like-sized square neighbour, and commensurable with the ``side +
    spacing`` requirement. ``Edge.distance(point)`` is the *signed* distance
    to the edge's infinite line, so some edge of any region the point sits
    outside always scores <= 0 and a ``min`` over ``max(0, ...)`` would
    report 0 here -- use the true point-to-segment distance instead.
    """
    import klayout.db as kdb

    if not (pad_region & other_region).is_empty():
        return 0.0
    centre = kdb.Point(int(round(px / dbu)), int(round(py / dbu)))
    nearest = min(
        (edge.euclidian_distance(centre) for edge in other_region.edges()), default=0
    )
    return (int(nearest) + int(round(side_um / 2.0 / dbu))) * dbu


def _pad_other_block_contact_reason(
    pad_boxes: list[Any],
    net_label: str,
    block_ids: list[str],
    endpoints: dict[str, PortLocations],
    geometry_for: Callable[[str, tuple[int, int], float | None], dict[str, Any] | None],
    drawing_dbu: float,
) -> str | None:
    """The pad-vs-other-block electrical-contact rejection (issue #7325).

    Called from ``compose()``'s ``_leg_conflict`` after the #1520 own-block
    notch loop: every pad a candidate leg draws
    (:func:`.gen_compose_landing_pads._landing_and_widen_pad_boxes` -- a
    via-drop's landings on *every* plane of its ladder, intermediate and
    terminal alike, plus stub-widen boxes) is tested against every placed
    block hosting **no** pin of the net being routed, on the pad's own
    layer. A pad touching such a block's own drawn geometry merges with it
    electrically, so both nets can compose ``routed: true`` while `klt
    extract` reads one node -- the silent short #7325 records, invisible
    to `klt drc` (two touching shapes on one layer are one polygon, never
    a spacing violation) and to the route-vs-route half of the check
    (block geometry never enters ``accepted_route_regions``).

    The predicate is ``kdb.Region.interacting`` -- shares at least one
    point: overlap, shared edge *or* shared corner -- the same rule
    extraction resolves layer connectivity with, so a corner-only touch is
    rejected exactly like the edge share of #7325's reproducer (#7024
    applied the same predicate to already-routed *route* geometry).
    Contact-only by design: a mere near miss against another block's metal
    stays a plain `klt drc` finding on the composed GDS, the same honest
    backstop the #1913 intermediate entries leave in place.

    On an endpoint block, a pad is allowed to join its own terminal. Reject
    contact with a different component when another logical net has a declared
    port on that same-layer polygon. Unlabelled components on an endpoint do
    not establish ownership. Bystanders retain the whole-block contact check.
    The separate #1520 self-notch check still checks spacing on the same wire.

    Returns the rejection wording naming the pad, the block and the net --
    carrying a ``_CLIMBABLE_REASON_MARKERS`` fragment so the #6920 plane
    climb can lift the leg to a plane that clears the block -- or ``None``
    when every pad keeps clear of every other block.
    """
    import klayout.db as kdb

    for pad_layer, _own_block_id, box, drop in pad_boxes:
        scale = drawing_dbu if drop is not None else None
        for other_block in block_ids:
            endpoint = endpoints.get(other_block)
            if endpoint is not None and pad_layer not in endpoint:
                continue
            geometry = geometry_for(other_block, pad_layer, scale)
            if geometry is None:
                continue
            dbu = geometry["dbu"]
            pad_region = kdb.Region(
                kdb.Box(
                    int(round(box[0] / dbu)),
                    int(round(box[1] / dbu)),
                    int(round(box[2] / dbu)),
                    int(round(box[3] / dbu)),
                )
            )
            if endpoint is not None:
                hit = geometry["region"].interacting(pad_region)
                port = contacted_port(hit, dbu, endpoint[pad_layer])
                if port is None:
                    continue
                return (
                    f"pad on layer {pad_layer} of net '{net_label}' "
                    f"touches foreign port '{other_block}.{port}' on its endpoint "
                    "block. Move the pad or route on a different plane."
                )
            if pad_region.interacting(geometry["region"]).is_empty():
                continue
            return (
                f"draws a pad ({box[0]:.4g}, {box[1]:.4g}) - "
                f"({box[2]:.4g}, {box[3]:.4g}) on layer {pad_layer} that "
                f"touches block '{other_block}'s own drawn geometry on "
                "that layer -- a pad touches a block hosting no pin of "
                f"its net, so `klt extract` reads net '{net_label}' and "
                "that block's metal as one node: a silent short the "
                "'routed' verdict would otherwise hide (issue #7325). "
                "Separate the blocks (placement.origins_um), reach this "
                "net's pin from a clear side (waypoints_um), or route "
                "this net on another plane (connectivity[].layer_role, "
                "or routing.cross_block_layer_role)"
            )
    return None
