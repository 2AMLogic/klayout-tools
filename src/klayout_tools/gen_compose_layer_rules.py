"""Per-PDK layer-rule resolution for ``klt gen-compose`` routing (#5426).

Relocated verbatim out of ``gen_compose_routing.py``, which sat exactly at
its per-file size ratchet ceiling with no headroom for routing work. This
cluster is a pure leaf: it resolves ``routing.layer_role`` to a
``(layer, datatype)`` pair and looks up the width / area / enclosure floors
and landing-pad sizes that pair implies, all out of the resolved PDK
family's own curated deck -- it calls nothing else in
``gen_compose_routing``, and five functions there call into it.

The router and composition API re-export these rule lookups for existing callers.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from . import gen_compose_top_metal_floors as _top_metal_floors
from .decks import UnknownDeckError, get_deck, get_nominal_dbu
from .decks.plain_rules import largest_plain_rule_um
from .gen import _PDK_ROLE_LAYERS, GenError, _pdk_family
from .gen_compose_base import _VIA_DROP_SIZE_UM, _VIA_LANDING_SIZE_UM, GenComposeError


def _resolve_route_layer(variant: str, layer_role: str) -> tuple[int, int]:
    """Resolve ``routing.layer_role`` to a ``(layer, datatype)`` pair.

    Routing goes through the *same* per-PDK-family role-layer table every
    ``klt gen`` generator already uses (:data:`klayout_tools.gen._PDK_ROLE_LAYERS`)
    -- never a raw ``{layer, datatype}`` pair from the request, and never a
    second, private layer map (spike section 2, ``routing.layer_role``).

    Raises :class:`GenComposeError` when the variant is unsupported, the role
    is not a known role, or the resolved family's curated deck has no layer
    for the role (``None`` entry).
    """
    try:
        family = _pdk_family(variant)
    except GenError as exc:
        raise GenComposeError(str(exc)) from exc

    family_roles = _PDK_ROLE_LAYERS[family]
    if layer_role not in family_roles:
        available = ", ".join(sorted(family_roles))
        raise GenComposeError(
            f"routing.layer_role '{layer_role}' is not a known layer role for "
            f"PDK family '{family}' -- available: {available}"
        )
    pair = family_roles[layer_role]
    if pair is None:
        raise GenComposeError(
            f"routing.layer_role '{layer_role}' has no layer in PDK family "
            f"'{family}''s curated deck -- cannot route on it"
        )
    return pair


# Native GF180 metaltop.rb selects the 9K/11K MT.1 and MT.2a rules on
# these physical planes. Variant A uses a separate 30K rule table.
def _is_gf180_9k_11k_top(variant: str | None, layer: tuple[int, int] | None) -> bool:
    return (variant, layer) in {
        ("gf180mcuB", (46, 0)),
        ("gf180mcuC", (81, 0)),
        ("gf180mcuD", (81, 0)),
    }


@lru_cache(maxsize=256)
def _route_spacing_rule(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    if _is_gf180_9k_11k_top(variant, layer):
        return (0.46, "MT.2a")
    return _min_rule_um_for_layer(variant, layer, "space")


def _min_width_um_for_layer(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    return _min_rule_um_for_layer(variant, layer, "width")


def _min_rule_um_for_layer(
    variant: str, layer: tuple[int, int] | None, check: str
) -> tuple[float, str] | None:
    """Resolve the variant before looking up its shared plain-layer rule."""
    if layer is None:
        return None
    try:
        family = _pdk_family(variant)
    except GenError:
        return None
    return largest_plain_rule_um(family, layer, check)


def _min_area_um2_for_layer(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    """Resolve the tightest applicable minimum-*area* DRC rule for ``layer``
    from the resolved PDK family's own curated deck -- the *same*
    ``ExtractionDeck``/``DrcRule`` set ``klt drc`` judges composed geometry
    with, never a private, hard-coded threshold (issue #2072).

    Mirrors :func:`_min_width_um_for_layer` above, generalised to ``"area"``
    rules -- used by :func:`_landing_pad_side_um_for_layer` below to floor a
    via-drop's landing-pad square against whichever metal level it actually
    lands on. Returns ``(threshold_um2, rule_id)`` for the *largest*
    matching plain ``"area"`` rule on ``layer`` (``other_layer`` and
    ``derived_layer`` both unset -- this deliberately excludes a layer's
    ``*.holes_area.*`` sibling rule, which checks the area of an *interior
    void* of the merged region, not the polygon itself, and would never be
    satisfied merely by drawing a bigger solid square), or ``None`` when
    ``layer`` is ``None``, the layer has no such rule in the resolved deck,
    or the PDK family/deck cannot be resolved at all -- an unresolvable
    family degrades this check to a no-op, exactly like
    :func:`_min_width_um_for_layer` does.
    """
    if layer is None:
        return None
    try:
        family = _pdk_family(variant)
        deck_rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return None
    best: tuple[float, str] | None = None
    for rule in deck_rules:
        if (
            rule.check == "area"
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
            and rule.area_min_dbu2 is not None
        ):
            threshold_um2 = rule.area_min_dbu2 * (nominal_dbu_um**2)
            if best is None or threshold_um2 > best[0]:
                best = (threshold_um2, rule.id)
    return best


def _min_enclosing_margin_um_for_layer(
    variant: str,
    layer: tuple[int, int] | None,
    other_layer: tuple[int, int] | None,
) -> tuple[float, str] | None:
    """Tightest plain enclosure rule for the ordered metal/via layer pair.

    Like the width/area lookups above, convert the resolved family's deck
    threshold from its nominal dbu to um, and return ``None`` for a missing
    rule or unresolvable family/deck. Derived-region rules do not describe
    a plain landing-pad square and are deliberately excluded.
    """
    if layer is None or other_layer is None:
        return None
    try:
        family = _pdk_family(variant)
        deck_rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return None
    candidates = (
        (rule.threshold_dbu * nominal_dbu_um, rule.id)
        for rule in deck_rules
        if rule.check == "enclosing"
        and rule.layer == layer
        and rule.other_layer == other_layer
        and rule.derived_layer is None
    )
    return max(candidates, key=lambda entry: entry[0], default=None)


def _top_metal_width_floor_um(
    variant: str, layer: tuple[int, int] | None
) -> float | None:
    """The variant-keyed native top-metal width floor (um) on ``layer``,
    or ``None`` when the variant has none or ``layer`` is not that
    family's own ``top_metal`` role layer (#6612).

    The single lookup both pad-size resolvers compose as a ``max``: the
    drawing-path floor :func:`_landing_pad_floor_um` (whose result
    :func:`_resolve_landing_pad_sizes` actually draws) and the search
    predictor :func:`_landing_pad_side_um_for_layer` -- one table, so the
    drawn pads and the landing search can never disagree on it again.
    The exact-variant dict lookup gates the (unraisable-for-these-
    variants) family resolution, so an unknown variant still degrades to
    ``None``, never raises.
    """
    width_floor_um = _top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_UM.get(variant)
    if width_floor_um is None:
        return None
    if layer != _PDK_ROLE_LAYERS[_pdk_family(variant)]["top_metal"]:
        return None
    return width_floor_um


def _top_metal_area_floor_side_um(
    variant: str, layer: tuple[int, int] | None
) -> float | None:
    """The variant-keyed native top-metal area floor expressed as the
    square side (um) it requires on ``layer``, or ``None`` when the
    variant has none or ``layer`` is not that family's own ``top_metal``
    role layer (#6645).

    Same exact-variant dict + top-metal role check as
    :func:`_top_metal_width_floor_um` above -- the native ``MT.4``
    minimum *area* expressed as the side of the smallest square that
    clears it (``sqrt``), so both pad-size resolvers can compose it as a
    plain ``max`` beside every other floor.
    """
    area_floor_um2 = _top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_UM2.get(variant)
    if area_floor_um2 is None:
        return None
    if layer != _PDK_ROLE_LAYERS[_pdk_family(variant)]["top_metal"]:
        return None
    return math.sqrt(area_floor_um2)


def _landing_pad_side_um_for_layer(variant: str, layer: tuple[int, int]) -> float:
    """A landing pad's baseline/area side length (um) on ``layer`` (#2072).

    :func:`_resolve_landing_pad_sizes` additionally applies each hop's
    via-enclosure floor before passing the final size into drawing (#2074).

    A via-drop's landing pad (``_VIA_LANDING_SIZE_UM``, sized from the PDK's
    contact-enclosure convention) is drawn on *every* hop of a multi-level
    via ladder (:func:`_resolve_via_drop_layer`) -- including an
    intermediate metal level the ladder merely passes through on its way to
    a pin several levels up (e.g. a route reaching a `bond_pad`'s
    ``top_metal`` port through sky130's li1->met1->met2->met3->met4->met5
    stack). ``_VIA_LANDING_SIZE_UM`` (0.42um square, 0.1764um^2) clears
    sky130's own met1/met2 minimum-area rules (0.083/0.0676um^2) but falls
    short of met3/met4's (0.240um^2 each) and met5's (4.0um^2) -- so an
    intermediate hop landing pad that never merges with a larger shape on
    that layer (nothing else the composition draws is anchored there) is a
    real, isolated sub-minimum-area polygon: exactly the `met3.area.1`/
    `met4.area.1` violations issue #2072 reproduced against a real `klt
    gen-compose` + `bond_pad` output.

    Returns ``max(_VIA_LANDING_SIZE_UM, sqrt(area_floor))`` when ``layer``
    carries a plain minimum-area rule in the resolved deck (a landing pad
    already merged into a larger drawn shape on that layer stays clean
    regardless of the extra size), or ``_VIA_LANDING_SIZE_UM`` unchanged
    when the layer has no such rule (or the family/deck cannot be
    resolved) -- an unresolvable family degrades to the pre-#2072 fixed
    size, never blocks routing. A variant's native top-metal width floor
    (:data:`.gen_compose_top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_UM`,
    #6612) and area floor
    (:data:`.gen_compose_top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_UM2`,
    #6645) additionally raise
    the result on that variant's own ``top_metal`` layer only, each as a
    ``max`` -- a larger area-driven side still wins. Those floors are the
    same :func:`_top_metal_width_floor_um` /
    :func:`_top_metal_area_floor_side_um` lookups the drawing-path floor
    :func:`_landing_pad_floor_um` composes, so this search footprint and
    the pad ``compose()`` actually draws cannot disagree on them.

    Drawing and the own-block self-notch check use the resolved side. The
    router’s other spacing checks retain their search footprint (#6469).
    """
    area_floor = _min_area_um2_for_layer(variant, layer)
    if area_floor is None:
        side_um = _VIA_LANDING_SIZE_UM
    else:
        side_um = max(_VIA_LANDING_SIZE_UM, math.sqrt(area_floor[0]))
    # #6612: the variant-keyed native top-metal width floor -- the same
    # lookup the drawing-path floor below composes.
    top_metal_floor = _top_metal_width_floor_um(variant, layer)
    if top_metal_floor is not None:
        side_um = max(side_um, top_metal_floor)
    # #6645: the variant-keyed native top-metal area floor, expressed as
    # the square side it requires -- same composition, same layer gate.
    top_metal_area_side = _top_metal_area_floor_side_um(variant, layer)
    if top_metal_area_side is not None:
        side_um = max(side_um, top_metal_area_side)
    return side_um


def _min_enclosing_adjacent_margin_um_for_layer(
    variant: str,
    layer: tuple[int, int] | None,
    other_layer: tuple[int, int] | None,
) -> tuple[float, str] | None:
    """Tightest two-adjacent-edges enclosure rule for the layer pair.

    Same shape as :func:`_min_enclosing_margin_um_for_layer` over the
    ``"enclosing_adjacent"`` check family (sky130's ``via.5a``/``m2.5``
    rules, #3316): the threshold a *centered* square pad must leave on
    every side -- a centered square's four margins are equal, so the rule
    fires (two adjacent edges both under it) unless the margin clears the
    threshold outright. Used by :func:`_intermediate_landing_pad_floor_um`
    (issue #6058) to floor a minimal-ladder intermediate pad; terminal
    pads never shrink below the fixed baseline, so they never need it.
    """
    if layer is None or other_layer is None:
        return None
    try:
        family = _pdk_family(variant)
        deck_rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return None
    candidates = (
        (rule.threshold_dbu * nominal_dbu_um, rule.id)
        for rule in deck_rules
        if rule.check == "enclosing_adjacent"
        and rule.layer == layer
        and rule.other_layer == other_layer
        and rule.derived_layer is None
    )
    return max(candidates, key=lambda entry: entry[0], default=None)


def _landing_pad_floor_um(
    variant: str,
    pad_layer: tuple[int, int],
    via_layer: tuple[int, int],
    via_side_um: float,
) -> tuple[float, list[tuple[str, float]]]:
    """The deck floor one ``(landing, via)`` landing-pad pair must clear.

    Returns ``(floor_side_um, floors)``: ``floor_side_um`` is the smallest
    pad side the resolved deck's own rules permit on this pair -- the larger
    of the pad layer's minimum-area side (issue #2072) and via side plus
    twice the pair's minimum enclosing-margin (issue #2074), additionally
    raised on a 5LM gf180mcu variant's own top metal by the native-runset
    ``MT.1`` width floor of issue #6612 and ``MT.4`` area floor of issue
    #6645 (variant-keyed because the curated deck cannot express either) --
    and ``floors`` is one ``(rule_id, required_side_um)`` entry per rule
    that shapes it, so a rejection can name the binding rule rather than a
    bare threshold. Both are empty when
    the layer carries no such rule (or the family/deck cannot be resolved):
    anything the caller asks for is then legal.

    Issue #5803: this is the floor ``routing.landing_pad_size_um``'s
    override is validated against -- a pad the deck forbids is an
    application error naming the rule, not a silently grown one. The #6612
    width floor participates in that validation: an override below
    0.44um on gf180mcuC/D Metal5 is rejected naming ``MT.1``, exactly as
    a below-deck-floor override on any other layer is. So does the #6645
    area floor: an override below 0.75um on the same pair is rejected
    naming ``MT.4`` -- the larger floor, and therefore the binding rule
    an override there actually violates.
    """
    floor_side = 0.0
    floors: list[tuple[str, float]] = []
    area_floor = _min_area_um2_for_layer(variant, pad_layer)
    if area_floor is not None:
        needed = math.sqrt(area_floor[0])
        floors.append((area_floor[1], needed))
        floor_side = max(floor_side, needed)
    enclosure = _min_enclosing_margin_um_for_layer(variant, pad_layer, via_layer)
    if enclosure is not None:
        needed = via_side_um + 2 * enclosure[0]
        floors.append((enclosure[1], needed))
        floor_side = max(floor_side, needed)
    width_floor = _top_metal_width_floor_um(variant, pad_layer)
    if width_floor is not None:
        floors.append(
            (_top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID, width_floor)
        )
        floor_side = max(floor_side, width_floor)
    area_side = _top_metal_area_floor_side_um(variant, pad_layer)
    if area_side is not None:
        floors.append(
            (_top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_RULE_ID, area_side)
        )
        floor_side = max(floor_side, area_side)
    return floor_side, floors


def _landing_pad_side_um_for_pair(
    variant: str,
    pad_layer: tuple[int, int],
    via_layer: tuple[int, int],
    via_side: float,
) -> float:
    """Default drawn side of one landing pad, before via-array growth."""
    floor, _ = _landing_pad_floor_um(variant, pad_layer, via_layer, via_side)
    return max(_VIA_LANDING_SIZE_UM, floor)


def _resolve_landing_pad_sizes(
    routed_geometry: list[dict[str, Any]],
    variant: str,
    via_drop_size_um: dict[tuple[int, int], float],
    override_um: float | None = None,
) -> dict[tuple[tuple[int, int], tuple[int, int]], float]:
    """Landing-pad sides keyed by ``(landing_layer, via_layer)`` (#2074).

    Each side of each hop independently clears the fixed baseline, its
    metal's area floor (#2072), the actual via side plus twice that
    pair's enclosure margin, and -- on a 5LM gf180mcu variant's own top
    metal -- the native ``MT.1`` width floor (#6612) and ``MT.4`` area
    floor (#6645). Include the via in
    the key: the same metal can enclose different cuts with different
    margins on adjacent hops. Missing enclosure rules leave the
    baseline/area floor unchanged.

    ``override_um`` (issue #5803) is ``routing.landing_pad_size_um``: a
    caller-set pad side replacing the fixed baseline. A pair whose deck
    floor exceeds it is an application error naming the binding rule --
    silently growing the pad back would defeat the override's whole purpose
    (reconciling a generator's terminal pitch with the pad footprint),
    exactly the #1548 silently-dropped-intent failure mode. A pair with no
    shaping rule draws at exactly the override; the default (``None``) is
    byte-for-byte the pre-#5803 resolution.

    Resolve once per distinct pair for drawing. Route conflict checks use
    the same floors and round the pad onto the output grid.
    """
    landing_pad_size_um: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}
    for route in routed_geometry:
        for drop in route.get("via_drops", []):
            via_layer = drop["via_layer"]
            for pad_layer in drop.get("landing_layers", ()):
                pair = (pad_layer, via_layer)
                if pair in landing_pad_size_um:
                    continue
                via_side = via_drop_size_um.get(via_layer, _VIA_DROP_SIZE_UM)
                if override_um is None:
                    landing_pad_size_um[pair] = _landing_pad_side_um_for_pair(
                        variant, pad_layer, via_layer, via_side
                    )
                    continue
                floor_side, floors = _landing_pad_floor_um(
                    variant, pad_layer, via_layer, via_side
                )
                if override_um < floor_side - 1e-9:
                    rule_id, needed = max(floors, key=lambda f: f[1])
                    source = (
                        "native runset"
                        if rule_id
                        in (
                            _top_metal_floors._TOP_METAL_LANDING_WIDTH_FLOOR_RULE_ID,
                            _top_metal_floors._TOP_METAL_LANDING_AREA_FLOOR_RULE_ID,
                        )
                        else "resolved PDK deck"
                    )
                    raise GenComposeError(
                        f"request.routing.landing_pad_size_um ({override_um:.4g}um) "
                        f"is below the {source} floor for layer {pad_layer} over "
                        f"via layer {via_layer}. '{rule_id}' requires "
                        f">= {needed:.4g}um. "
                        f"Please request at least {floor_side:.4g}um or omit "
                        "routing.landing_pad_size_um to keep the default pads"
                    )
                landing_pad_size_um[pair] = override_um
    return landing_pad_size_um


def _port_own_layer(port: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(layer, datatype)`` a port's own reported ``layer{layer,
    datatype}`` geometry names, or ``None`` when it is missing/malformed.

    Distinct from :func:`_port_has_geometry` (which also requires
    ``x_um``/``y_um``) -- callers of this helper already know the port has a
    usable position and only need its physical layer, e.g. to decide whether
    a via-drop is needed (issue #454, see :func:`_resolve_via_drop_layer`).
    """
    layer = port.get("layer")
    if not isinstance(layer, dict):
        return None
    layer_num, datatype = layer.get("layer"), layer.get("datatype")
    if (
        isinstance(layer_num, int)
        and not isinstance(layer_num, bool)
        and isinstance(datatype, int)
        and not isinstance(datatype, bool)
    ):
        return (layer_num, datatype)
    return None
