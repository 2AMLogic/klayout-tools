"""Minimal-ladder intermediate landing-pad floors for ``klt gen-compose`` (#6058).

The per-pair deck floors a ``routing.minimal_ladder`` intermediate hop pad is
sized to, and the resolver that maps a request's ``via_drops`` onto them --
kept out of ``gen_compose_layer_rules.py``, which stands at 430 lines after
the #6612 top-metal floor: all of this PR's additions there would pass its
500-line per-file size ratchet. The terminal-pad resolvers these are the
counterpart of (``_landing_pad_floor_um``, ``_resolve_landing_pad_sizes``)
and the deck lookups they build on (width / area / plain and
two-adjacent-edges enclosure) stay in ``gen_compose_layer_rules``, which this
module imports at module scope; that module never imports this one.

The composition API re-exports ``_resolve_intermediate_landing_pad_sizes``.
"""

from __future__ import annotations

import math
from typing import Any

from .gen_compose_base import _VIA_DROP_SIZE_UM, _VIA_LANDING_SIZE_UM
from .gen_compose_layer_rules import (
    _min_area_um2_for_layer,
    _min_enclosing_adjacent_margin_um_for_layer,
    _min_enclosing_margin_um_for_layer,
    _min_width_um_for_layer,
)


def _intermediate_landing_pad_floor_um(
    variant: str,
    pad_layer: tuple[int, int],
    via_layer: tuple[int, int],
    via_side_um: float,
) -> tuple[float, list[tuple[str, float]]]:
    """The deck floor a *minimal-ladder* intermediate pad must clear (#6058).

    Returns ``(floor_side_um, floors)`` in :func:`_landing_pad_floor_um`'s
    own shape, but without the fixed contact-derived baseline: an
    intermediate hop pad is sized by what its own deck rules actually
    require -- the pad layer's minimum-area side, its minimum-width rule
    (a square's side is its width), the via side plus twice the pair's
    plain enclosure margin, and, new here, the via side plus twice the
    pair's two-adjacent-edges enclosure margin (a centered square's four
    margins are equal, so satisfying ``enclosing_adjacent`` takes
    ``via_side + 2 * threshold``, exactly the plain-enclosure shape; on
    sky130 that is what floors a met1 pad over via1 at 0.32um rather than
    ``met1.enclosing.via.1``'s 0.26um).

    When the resolved deck declares *neither* enclosure rule for the pair,
    the floor falls back to the fixed ``_VIA_LANDING_SIZE_UM`` baseline:
    the baseline exists precisely to guarantee contact-class enclosure, so
    a deck that says nothing about this pair gives no information that
    would justify shrinking below it (issue #6058's "keep a conservative
    existing size where the deck provides insufficient information to
    justify shrinking"). Same for an unresolvable family/deck -- the
    mode-off size is the conservative answer there too.
    """

    floor_side = 0.0
    floors: list[tuple[str, float]] = []
    area_floor = _min_area_um2_for_layer(variant, pad_layer)
    if area_floor is not None:
        needed = math.sqrt(area_floor[0])
        floors.append((area_floor[1], needed))
        floor_side = max(floor_side, needed)
    width_floor = _min_width_um_for_layer(variant, pad_layer)
    if width_floor is not None:
        floors.append((width_floor[1], width_floor[0]))
        floor_side = max(floor_side, width_floor[0])
    enclosure = _min_enclosing_margin_um_for_layer(variant, pad_layer, via_layer)
    if enclosure is not None:
        needed = via_side_um + 2 * enclosure[0]
        floors.append((enclosure[1], needed))
        floor_side = max(floor_side, needed)
    adjacent = _min_enclosing_adjacent_margin_um_for_layer(
        variant, pad_layer, via_layer
    )
    if adjacent is not None:
        needed = via_side_um + 2 * adjacent[0]
        floors.append((adjacent[1], needed))
        floor_side = max(floor_side, needed)
    if enclosure is None and adjacent is None:
        floor_side = max(floor_side, _VIA_LANDING_SIZE_UM)
    return floor_side, floors


def _resolve_intermediate_landing_pad_sizes(
    routed_geometry: list[dict[str, Any]],
    variant: str,
    via_drop_size_um: dict[tuple[int, int], float],
) -> dict[tuple[tuple[int, int], tuple[int, int]], float]:
    """Landing-pad sides for **intermediate** hops only, keyed by pair (#6058).

    ``routing.minimal_ladder``'s drawing-side resolver: for every via_drops
    entry that carries the ``terminal_landing_layers`` marking
    (:func:`._gen_compose_routing._resolve_via_drops`), each of its
    ``landing_layers`` *not* in that marking is an intermediate plane of
    this endpoint's ladder, sized to :func:`_intermediate_landing_pad_floor_um`
    -- the per-pair enclosure/area/width floors, without the contact-derived
    baseline. Terminal landings (and drops without the marking -- a
    caller-built drop, or the whole ``klt route`` path) are not this
    function's business: they keep :func:`_resolve_landing_pad_sizes`'s own
    sizing, unchanged.

    A pair absent from the returned dict keeps its terminal size -- the
    conservative (never smaller) answer for any pair the marking did not
    cover. On a plane two hops share, each hop's own square is sized for
    its own cut; the concentric squares merge in the drawn GDS, and the
    union encloses both cuts.
    """

    sizes: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}
    for route in routed_geometry:
        for drop in route.get("via_drops", []):
            terminal_layers = drop.get("terminal_landing_layers")
            if terminal_layers is None:
                continue  # unmarked (caller-built) drop -- no intermediate claim
            via_layer = drop["via_layer"]
            via_side = via_drop_size_um.get(via_layer, _VIA_DROP_SIZE_UM)
            for pad_layer in drop.get("landing_layers", ()):
                if pad_layer in terminal_layers:
                    continue  # terminal landing -- keeps today's sizing
                pair = (pad_layer, via_layer)
                if pair in sizes:
                    continue
                floor_side, _floors = _intermediate_landing_pad_floor_um(
                    variant, pad_layer, via_layer, via_side
                )
                sizes[pair] = floor_side
    return sizes
