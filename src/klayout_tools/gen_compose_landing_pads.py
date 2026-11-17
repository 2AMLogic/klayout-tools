"""Via-drop landing-pad geometry helpers for ``klt gen-compose`` (issue #5803).

Landing-pad sizing lives in :mod:`.gen_compose_layer_rules` (the deck-floor
lookups and ``_resolve_landing_pad_sizes``); this module holds everything
those sizes feed on the *check* side of the router:

* ``_make_landing_pad_side_um`` -- the memoized per-pair size resolver
  ``compose()`` threads into its collision-check footprint builders, so a
  request-level ``routing.landing_pad_size_um`` override is checked at the
  same size it is drawn at (the #1197 "check and drawn output cannot
  disagree" rule) while a request without one keeps today's fixed-baseline
  footprints byte-for-byte;
* ``_landing_and_widen_pad_boxes`` -- every pad box one leg draws (via-drop
  landing pads plus stub-widen boxes), extracted verbatim from
  ``compose()``'s ``_leg_conflict`` (#1520's own-block self-notch loop) so
  the pad-vs-pitch rejection can reuse the same boxes;
* ``_landing_pad_conflict_reason`` -- the named pad-vs-pitch rejection
  (issue #5803): a conflict whose geometry is a landing pad is reported as
  the pad footprint vs terminal pitch conflict it is, with the deck's
  spacing rule and the ``routing.landing_pad_size_um`` escape hatch, not
  as a generic backbone crossing;
* ``_drawn_leg_intermediate_pad_regions`` -- relocated verbatim from
  ``gen_compose_routing.py``, which sat exactly at its per-file size
  ratchet ceiling (the same move ``gen_compose_layer_rules.py`` made in
  #5426, and ``gen_compose_via_cuts.py`` before that). ``gen_compose_routing``
  re-imports the name at module scope, so every pre-existing import path
  keeps resolving unchanged.

Same conventions as the modules it was split from:
``gen_compose.py``/``gen_compose_routing.py`` names (``GenComposeError``,
``_VIA_LANDING_SIZE_UM``, ``_drop_array_extra_um``) are imported *inside*
the functions that use them, so this module never has a load-time
dependency back on them.
"""

from __future__ import annotations

from typing import Any

#: One pad box a leg draws: ``(pad_layer, block_id, (x0, y0, x1, y1)_um,
#: drop)``. ``drop`` is the via_drops entry the pad came from (its
#: ``via_layer`` and position name the pair and terminal the message
#: path reports) -- ``None`` for a stub-widen box, which is not a
#: landing pad.
_LegPadBox = tuple[
    tuple[int, int],
    str,
    tuple[float, float, float, float],
    dict[str, Any] | None,
]


def _parse_landing_pad_override(raw: Any, where: str) -> float | None:
    """Type-check an optional ``routing.landing_pad_size_um`` (issue #5803).

    ``None``/absent means "keep the default pads" (``_VIA_LANDING_SIZE_UM``
    plus the per-pair area/enclosure floors, unchanged). Any other value
    must be a positive number of um. The *deck floor* is not checked here --
    the resolved PDK variant (and the actually-drawn ``(landing, via)``
    pairs) are not in scope at request-parse time; the floor rejection
    naming the binding rule lives in :func:`_resolve_landing_pad_sizes`,
    which raises once the router has resolved which pairs this request
    really draws.
    """
    from .gen_compose import GenComposeError

    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise GenComposeError(f"{where} must be a positive number when given")
    return float(raw)


def _make_landing_pad_side_um(variant: str, override_um: float | None = None) -> Any:
    """Memoized ``((pad_layer, via_layer)) -> side_um`` resolver (issue #5803).

    ``compose()`` builds one resolver per request and threads it into every
    collision-check footprint builder (``_drawn_leg_footprint_region``,
    :func:`_drawn_leg_intermediate_pad_regions`, ``_leg_conflict``'s #1520
    pad-box loop and its own-block escape checks), so the checked footprint
    matches what drawing will insert for the same pair.

    Without an override (the default) the resolver answers the fixed
    ``_VIA_LANDING_SIZE_UM`` baseline for every pair -- the exact footprint
    those builders assumed before #5803, byte-for-byte unchanged. With
    ``routing.landing_pad_size_um`` set it answers the drawn side,
    ``max(override, deck floor)``: the floor growth mirrors drawing
    (:func:`.gen_compose_layer_rules._resolve_landing_pad_sizes` rejects the
    request outright before drawing when the override is below the floor,
    so the grown value only ever reaches the checks for pairs whose floor
    is legal), keeping the two from disagreeing mid-flight.

    Via sides come from the same ``via_drop_cut_side_um`` deck lookup
    ``compose()`` resolves its ``via_drop_size_um`` dict from -- never a
    second, private threshold.
    """
    from .gen_compose import _VIA_LANDING_SIZE_UM
    from .gen_compose_via_cuts import via_drop_cut_side_um

    pair_cache: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}
    via_cache: dict[tuple[int, int], float] = {}

    def side_um(pad_layer: tuple[int, int], via_layer: tuple[int, int]) -> float:
        pair = (pad_layer, via_layer)
        if pair not in pair_cache:
            if override_um is None:
                pair_cache[pair] = _VIA_LANDING_SIZE_UM
            else:
                if via_layer not in via_cache:
                    via_cache[via_layer] = via_drop_cut_side_um(variant, via_layer)
                from .gen_compose_layer_rules import _landing_pad_floor_um

                floor_side, _floors = _landing_pad_floor_um(
                    variant, pad_layer, via_layer, via_cache[via_layer]
                )
                pair_cache[pair] = max(override_um, floor_side)
        return pair_cache[pair]

    return side_um


def _landing_and_widen_pad_boxes(
    via_drops: list[dict[str, Any]],
    stub_widen: list[dict[str, Any]],
    layer: tuple[int, int] | None,
    side_for_pair: Any,
) -> list[_LegPadBox]:
    """Every pad box one routed leg draws, sized for its own pair (#1520).

    Extracted verbatim from ``compose()``'s ``_leg_conflict`` (issue #1520's
    own-block pad self-notch loop grew there; issue #5803's message path
    needs the same boxes, so the loop moved here beside it instead of being
    duplicated). ``side_for_pair`` (issue #5803) is
    :func:`_make_landing_pad_side_um`'s resolver -- a landing pad is sized
    for its own ``(landing, via)`` pair rather than the fixed baseline, so
    a request-level override is checked at the size drawing will insert.
    Stub-widen boxes draw on the leg's own ``layer`` at their own width,
    exactly as before (they are not pair-sized; ``drop`` is ``None``).

    Returns ``[(pad_layer, block_id, (x0, y0, x1, y1)_um, drop)]``. A
    pre-#1520 caller-built entry (``block_id`` absent) is skipped, not an
    error -- unchanged behaviour.
    """
    from .gen_compose import _VIA_LANDING_SIZE_UM
    from .gen_compose_routing import _drop_array_extra_um

    boxes: list[_LegPadBox] = []
    for drop in via_drops:
        block_id = drop.get("block_id")
        if block_id is None:
            continue  # pre-#1520 caller-built via_drops -- skip, not an error
        via_layer = drop["via_layer"]
        cx, cy = drop["x_um"], drop["y_um"]
        # Each hop's landing pad is drawn on *both* of its own
        # ``landing_layers`` (see _write_composed_gds) -- issue #1567
        # generalized this from a single hop's fixed (port_layer, this leg's
        # own route layer) pair to whatever pair *that hop* actually lands
        # on, so a multi-level ladder's intermediate pads are checked here
        # too, not just the one nearest the backbone or the one nearest the
        # pin. dict.fromkeys dedupes the common case where both layers of a
        # hop happen to be identical.
        for pad_layer in dict.fromkeys(drop.get("landing_layers", ())):
            side_um = (
                side_for_pair(pad_layer, via_layer)
                if side_for_pair is not None
                else _VIA_LANDING_SIZE_UM
            )
            half_um = (side_um + _drop_array_extra_um(drop)) / 2.0
            boxes.append(
                (
                    pad_layer,
                    block_id,
                    (cx - half_um, cy - half_um, cx + half_um, cy + half_um),
                    drop,
                )
            )
    for widen in stub_widen:
        block_id = widen.get("block_id")
        if block_id is None or layer is None:
            continue  # pre-#1520 caller-built stub_widen -- skip
        cx, cy = widen["x_um"], widen["y_um"]
        half_um = widen["width_um"] / 2.0
        length_um = widen["length_um"]
        if widen["direction_deg"] == 90:
            box = (cx - half_um, cy, cx + half_um, cy + length_um)
        else:  # 270
            box = (cx - half_um, cy - length_um, cx + half_um, cy)
        boxes.append((layer, block_id, box, None))
    return boxes


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
    #1057 overlap test, the #1386 spacing-aware test on the primary entry
    only (``check_spacing`` -- the #1913 intermediate/far pad entries are
    compared overlap-only, #1680), each upgraded to consult
    :func:`_landing_pad_conflict_reason` first so a conflict at a landing
    pad names pad-vs-pitch instead of a generic backbone crossing. Returns
    ``None`` when neither test fires -- the caller keeps looping.

    Messages (and their thresholds) are byte-identical to the pre-#5803
    wording; only the pad-involving conflicts can change what they say.
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
    ``(landing, via)`` pair), the terminal pitch (centre-to-nearest-edge
    of the other net's already-accepted metal on that plane, 0 for an
    outright overlap), and the resolved deck's own spacing rule when the
    layer carries one -- plus the ``routing.landing_pad_size_um`` override
    as the escape hatch where the deck's floors allow a smaller pad.
    """
    import klayout.db as kdb

    from .gen_compose import _VIA_LANDING_SIZE_UM
    from .gen_compose_layer_rules import _landing_pad_floor_um
    from .gen_compose_routing import _drop_array_extra_um
    from .gen_compose_via_cuts import via_drop_cut_side_um

    if pad_layer is None:
        return None  # unknown candidate layer -- no pair-keyed pad to name
    pad_drops = [d for d in via_drops if pad_layer in (d.get("landing_layers") or ())]
    if not pad_drops:
        return None  # backbone-only conflict -- generic message is accurate

    def drawn_side_um(drop: dict[str, Any]) -> float:
        """The side drawing will actually insert for this drop's pair.

        The collision *checks* keep the fixed-baseline footprint when the
        request sets no ``routing.landing_pad_size_um`` (the #2072/#1501
        scoping trade-off, deliberately unchanged here) -- but the message
        must report the size the composed GDS will really carry, area and
        enclosure floors included, or it understates the conflict it
        names.
        """
        pair_side = side_for_pair(pad_layer, drop["via_layer"])
        floor_side, _floors = _landing_pad_floor_um(
            variant,
            pad_layer,
            drop["via_layer"],
            via_drop_cut_side_um(variant, drop["via_layer"]),
        )
        return max(pair_side, _VIA_LANDING_SIZE_UM, floor_side) + _drop_array_extra_um(
            drop
        )

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
    centre = kdb.Point(int(round(px / dbu)), int(round(py / dbu)))
    pitch_dbu = int(
        min(
            (max(0, edge.distance(centre)) for edge in other_region.edges()),
            default=0,
        )
    )
    pitch_um = pitch_dbu * dbu
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
            f"comes within {pitch_um:.4g}um of already-routed net "
            f"'{other_net}' on that plane"
        )
    return (
        f"via-drop landing pad ({side_um:.4g}um square at ({px:.4g}, {py:.4g}) "
        f"on layer {pad_layer}) {conflict} -- a {side_um:.4g}um landing-pad "
        f"footprint needs >= {need_um:.4g}um of terminal pitch to clear a "
        f"neighbouring terminal{spacing_note} -- a generator whose terminal "
        "pitch is below the landing-pad footprint cannot be landed on "
        "independently: request routing.landing_pad_size_um (issue #5803) "
        "where the deck's floors allow a smaller pad, or land one of the "
        "two nets on another plane"
    )


def _drawn_leg_intermediate_pad_regions(
    via_drops: list[dict[str, Any]],
    dbu: float,
    route_layer: tuple[int, int] | None,
    side_for_pair: Any = None,
) -> dict[tuple[int, int], Any]:
    """Every via-drop landing-pad box this leg draws on a physical layer
    *other than* its own primary ``route_layer``, grouped by that pad's own
    layer (issue #1913).

    :func:`~.gen_compose_routing._drawn_leg_footprint_region` deliberately
    excludes a multi-hop via-drop ladder's intermediate/far landing pads --
    they are not part of the leg's footprint *on route_layer* -- and the
    module docstring for that function pointed at `klt drc` as the backstop
    for a cross-net short that lives entirely on one of those excluded
    planes. That pointer is wrong: two shapes that overlap on one layer
    *merge* into a single polygon in the composed GDS, which is a short,
    not a spacing violation, so no rule deck has anything to flag.

    This is the complement of
    :func:`~.gen_compose_routing._drawn_leg_footprint_region`: the
    landing-pad geometry it leaves out, keyed by the pad's own layer so the
    route-vs-route collision check in :func:`compose` can compare it against
    another already-accepted net's footprint on that *same* plane -- even
    though neither net's own primary routing layer names it.

    ``side_for_pair`` (issue #5803) sizes each pad for its own
    ``(landing, via)`` pair -- the drawn side -- instead of the fixed
    ``_VIA_LANDING_SIZE_UM`` baseline every pad assumed before #5803; the
    default (``None``) keeps that baseline, unchanged.

    Returns ``{}`` when ``route_layer`` is ``None`` -- a caller that
    genuinely does not know its own leg's layer already gets every via-drop
    pad folded into the single conservative region
    :func:`~.gen_compose_routing._drawn_leg_footprint_region` returns for
    that case, so there is nothing left here to separate out.
    """
    import klayout.db as kdb

    from .gen_compose import _VIA_LANDING_SIZE_UM
    from .gen_compose_routing import _drop_array_extra_um

    regions: dict[tuple[int, int], Any] = {}
    if route_layer is None:
        return regions

    for drop in via_drops:
        landing_layers = drop.get("landing_layers")
        if not landing_layers:
            continue
        cx = int(round(drop["x_um"] / dbu))
        cy = int(round(drop["y_um"] / dbu))
        # dict.fromkeys dedupes the common case where both layers of a hop
        # happen to be identical (mirrors the same idiom in
        # `_leg_conflict`'s own-block pad self-notch loop).
        for pad_layer in dict.fromkeys(landing_layers):
            if pad_layer == route_layer:
                continue  # already part of _drawn_leg_footprint_region
            if side_for_pair is not None:
                side_um = side_for_pair(pad_layer, drop["via_layer"])
            else:
                side_um = _VIA_LANDING_SIZE_UM
            landing_half_dbu = int(
                round((side_um + _drop_array_extra_um(drop)) / 2.0 / dbu)
            )
            box = kdb.Box(
                cx - landing_half_dbu,
                cy - landing_half_dbu,
                cx + landing_half_dbu,
                cy + landing_half_dbu,
            )
            region = regions.get(pad_layer)
            if region is None:
                region = kdb.Region()
                regions[pad_layer] = region
            region.insert(box)

    return regions
