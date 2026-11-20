"""Via-drop landing-pad geometry helpers for ``klt gen-compose`` (issue #5803).

Landing-pad sizing lives in :mod:`.gen_compose_layer_rules` (the deck-floor
lookups and ``_resolve_landing_pad_sizes``); this module holds everything
those sizes feed on the *check* side of the router:

* ``_make_landing_pad_side_um`` -- the memoized per-pair size resolver
  ``compose()`` threads into its collision-check footprint builders, so a
  request-level ``routing.landing_pad_size_um`` override is checked at the
  same size it is drawn at (the #1197 "check and drawn output cannot
  disagree" rule) while a request without one keeps today's fixed-baseline
  footprints byte-for-byte; since #6058 the resolver also answers the
  per-pair floor of a ``routing.minimal_ladder`` request's *intermediate*
  landings, from the drop's own ``terminal_landing_layers`` marking;
* ``_landing_and_widen_pad_boxes`` -- every pad box one leg draws (via-drop
  landing pads plus stub-widen boxes), extracted verbatim from
  ``compose()``'s ``_leg_conflict`` (#1520's own-block self-notch loop) so
  the pad-vs-pitch rejection can reuse the same boxes;
* ``_drawn_leg_intermediate_pad_regions`` -- relocated verbatim from
  ``gen_compose_routing.py``, which sat exactly at its per-file size
  ratchet ceiling (the same move ``gen_compose_layer_rules.py`` made in
  #5426, and ``gen_compose_via_cuts.py`` before that). ``gen_compose_routing``
  re-imports the name at module scope, so every pre-existing import path
  keeps resolving unchanged.

The *message* cluster that turned a pad conflict into the named
pad-vs-pitch rejection (``_leg_conflict_message``,
``_landing_pad_conflict_reason``, ``_terminal_pitch_um``,
``_drawn_pad_side_um``, ``_pad_conflict_escape_note``,
``_pad_is_intermediate``) moved to
:mod:`.gen_compose_pad_conflicts` when #6058 grew it past this file's own
size-ratchet ceiling; the names are re-imported at module scope below so
every pre-existing ``gen_compose_landing_pads.<name>`` import path (the
test suite's included) keeps resolving unchanged.

Same conventions as the modules it was split from:
``gen_compose.py``/``gen_compose_routing.py`` names (``GenComposeError``,
``_VIA_LANDING_SIZE_UM``, ``_drop_array_extra_um``) are imported *inside*
the functions that use them, so this module never has a load-time
dependency back on them.
"""

from __future__ import annotations

from typing import Any

from .gen_compose_pad_conflicts import (  # noqa: F401 -- re-export (see above)
    _drawn_pad_side_um,
    _landing_pad_conflict_reason,
    _leg_conflict_message,
    _pad_conflict_escape_note,
    _pad_is_intermediate,
    _terminal_pitch_um,
)

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


class _LandingPadSideResolver:
    """Memoized ``((pad_layer, via_layer[, drop])) -> side_um`` resolver --
    what :func:`_make_landing_pad_side_um` returns.

    ``compose()`` builds one per request and threads it into every
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

    ``minimal_ladder`` (issue #6058) adds the optional third call argument,
    ``drop``: a landing that drop's own ``terminal_landing_layers`` marking
    (:func:`.gen_compose_routing._resolve_via_drops`) classifies as
    *intermediate* answers that hop's per-pair enclosure/area floor
    (:func:`.gen_compose_ladder_floors._intermediate_landing_pad_floor_um`)
    -- the drawn side without the contact-derived baseline -- instead of
    the baseline. A two-argument call (every pre-#6058 caller, and any drop
    without the marking) never sees the difference, so the mode-off
    footprints are byte-for-byte unchanged. The mode itself rides along as
    the ``minimal_ladder`` attribute so message paths can name the escape
    hatch that matches the pad's own policy.

    Via sides come from the same ``via_drop_cut_side_um`` deck lookup
    ``compose()`` resolves its ``via_drop_size_um`` dict from -- never a
    second, private threshold.
    """

    def __init__(
        self,
        variant: str,
        override_um: float | None = None,
        minimal_ladder: bool = False,
    ) -> None:
        from .gen_compose_via_cuts import via_drop_cut_side_um

        self.minimal_ladder = minimal_ladder
        self._variant = variant
        self._override_um = override_um
        self._pair_cache: dict[
            tuple[tuple[int, int], tuple[int, int], bool], float
        ] = {}
        self._via_cache: dict[tuple[int, int], float] = {}
        self._via_side_lookup = via_drop_cut_side_um

    def _via_side_um(self, via_layer: tuple[int, int]) -> float:
        if via_layer not in self._via_cache:
            self._via_cache[via_layer] = self._via_side_lookup(self._variant, via_layer)
        return self._via_cache[via_layer]

    def __call__(
        self,
        pad_layer: tuple[int, int],
        via_layer: tuple[int, int],
        drop: dict[str, Any] | None = None,
    ) -> float:
        from .gen_compose import _VIA_LANDING_SIZE_UM

        intermediate = self.minimal_ladder and _pad_is_intermediate(drop, pad_layer)
        key = (pad_layer, via_layer, intermediate)
        if key not in self._pair_cache:
            if intermediate:
                from .gen_compose_ladder_floors import (
                    _intermediate_landing_pad_floor_um,
                )

                floor_side, _floors = _intermediate_landing_pad_floor_um(
                    self._variant, pad_layer, via_layer, self._via_side_um(via_layer)
                )
                self._pair_cache[key] = floor_side
            elif self._override_um is None:
                self._pair_cache[key] = _VIA_LANDING_SIZE_UM
            else:
                from .gen_compose_layer_rules import _landing_pad_floor_um

                floor_side, _floors = _landing_pad_floor_um(
                    self._variant, pad_layer, via_layer, self._via_side_um(via_layer)
                )
                self._pair_cache[key] = max(self._override_um, floor_side)
        return self._pair_cache[key]


def _make_landing_pad_side_um(
    variant: str,
    override_um: float | None = None,
    minimal_ladder: bool = False,
) -> _LandingPadSideResolver:
    """Build the per-request landing-pad side resolver (issues #5803/#6058).

    See :class:`_LandingPadSideResolver` for the resolution contract; the
    factory keeps the pre-#6058 call shape (and every
    ``side_for_pair(pad, via)`` call site) unchanged.
    """
    return _LandingPadSideResolver(variant, override_um, minimal_ladder)


def _marked_via_drop_entry(
    pos: tuple[float, float],
    terminal_layers: tuple[tuple[int, int], tuple[int, int]],
    hop: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
    drop_array: dict[str, Any] | None,
    block_id: str,
) -> dict[str, Any]:
    """One hop's via_drops entry (``hop`` is :data:`._ViaDropHop`), with the
    #6058 terminal-landing marking.

    The two planes an endpoint's own ladder terminates on -- its port's own
    layer and the leg's effective route layer, passed as
    ``terminal_layers`` -- are *terminal* landings; every other level the
    ladder passes through is *intermediate*. Recorded per hop in
    ``terminal_landing_layers`` (a ``landing_layers`` entry can be terminal
    at this endpoint and intermediate at another -- a second net's ladder
    can pass through this net's own terminal plane) so pad sizing, the
    collision-check footprints and drawing all classify one landing the
    same way. ``routing.minimal_ladder`` is the only consumer that
    shrinks a pad below today's size, and it reads exactly this marking --
    absent (a caller-built drop), the pad keeps today's terminal sizing,
    unchanged. Everything else is the pre-#6058 entry
    (:func:`.gen_compose_routing._resolve_via_drops`) verbatim, including
    the #1520 ``block_id``: which endpoint's own block this drop's landing
    pad lands on -- lets a caller-side conflict check (compose()'s own
    ``_leg_conflict``) test the pad against *that* block's own drawn
    geometry, not just against other already-accepted nets.
    """
    via_layer, metal_a, metal_b = hop
    terminal = frozenset(terminal_layers)
    return {
        "x_um": pos[0],
        "y_um": pos[1],
        "via_layer": via_layer,
        "landing_layers": (metal_a, metal_b),
        "terminal_landing_layers": tuple(
            layer for layer in (metal_a, metal_b) if layer in terminal
        ),
        **({"array": drop_array} if drop_array else {}),
        "block_id": block_id,
    }


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
                side_for_pair(pad_layer, via_layer, drop)
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
    default (``None``) keeps that baseline, unchanged. Since #6058 the
    resolver accepts the drop itself as a third argument, so a
    ``routing.minimal_ladder`` request's intermediate pads are measured at
    their shrunk, per-pair floor side (checks match drawing); a two-
    argument call or an unmarked drop keeps the pre-#6058 answer.

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
                side_um = side_for_pair(pad_layer, drop["via_layer"], drop)
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
