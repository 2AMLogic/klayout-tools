"""Per-PDK layer-rule resolution for ``klt gen-compose`` routing (#5426).

Relocated verbatim out of ``gen_compose_routing.py``, which sat exactly at
its per-file size ratchet ceiling with no headroom for routing work. This
cluster is a pure leaf: it resolves ``routing.layer_role`` to a
``(layer, datatype)`` pair and looks up the width / area / enclosure floors
and landing-pad sizes that pair implies, all out of the resolved PDK
family's own curated deck -- it calls nothing else in
``gen_compose_routing``, and five functions there call into it.

Same conventions as the module it came from (and as
``gen_compose_via_cuts.py``, the earlier split of the same file):
``gen_compose.py``'s own module-scope names (``GenComposeError``,
``_VIA_LANDING_SIZE_UM``, ``_VIA_DROP_SIZE_UM``) are imported *inside* the
functions that use them, so this module never has a load-time dependency
back on ``gen_compose.py``. ``gen_compose_routing`` re-imports every name
below at module scope, so each pre-existing
``from klayout_tools.gen_compose_routing import <name>`` path -- and
``gen_compose``'s own re-export of it -- keeps resolving unchanged.
"""

from __future__ import annotations

import math
from typing import Any

from .decks import UnknownDeckError, get_deck, get_nominal_dbu
from .gen import _PDK_ROLE_LAYERS, GenError, _pdk_family


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
    from .gen_compose import GenComposeError

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


def _min_width_um_for_layer(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    """Resolve the tightest applicable minimum-*width* DRC rule for ``layer``
    from the resolved PDK family's own curated deck -- the *same*
    ``ExtractionDeck``/``DrcRule`` set ``klt drc`` judges composed geometry
    with, never a private, hard-coded threshold (issue #1501).

    Mirrors ``compose()``'s own same-layer minimum-*spacing* lookup (issue
    #1386's ``_min_spacing_um_for_layer``), generalised to ``"width"`` rules
    and lifted to module scope so both the ``routing.width_um`` validation
    and the via-drop square sizing below can share it. Returns
    ``(threshold_um, rule_id)`` for the widest matching ``"width"`` rule on
    ``layer`` (a layer can carry more than one width rule from different DRM
    sections; the widest is the true floor), or ``None`` when ``layer`` is
    ``None``, the layer has no such rule in the resolved deck, or the PDK
    family/deck cannot be resolved at all -- an unresolvable family degrades
    this check to a no-op, exactly like #1386's spacing lookup does.
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
            rule.check == "width"
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
        ):
            threshold_um = rule.threshold_dbu * nominal_dbu_um
            if best is None or threshold_um > best[0]:
                best = (threshold_um, rule.id)
    return best


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
    size, never blocks routing.

    Same known, already-accepted scoping trade-off as ``via_drop_size_um``
    (issue #1501): this resolved size is threaded into *drawing*
    (:func:`~klayout_tools.gen_compose._write_composed_gds`) only, not into
    the router's own pre-route spacing/collision pre-checks (still sized
    off the fixed ``_VIA_LANDING_SIZE_UM`` baseline) -- so an oversized pad
    on a rule-heavy layer (met5's 2.0um side, up from 0.42um) is, in the
    rare case a route was accepted right at that pre-check's own margin,
    theoretically capable of surfacing a *new*, previously-unseen spacing
    violation against a neighbour `klt drc` would still catch. This is
    strictly better than the pre-#2072 guaranteed area violation it
    replaces, and mirrors the exact precedent #1501 already shipped and
    left unchanged for `via_drop_size_um`'s own via-square growth.
    """
    from .gen_compose import _VIA_LANDING_SIZE_UM

    area_floor = _min_area_um2_for_layer(variant, layer)
    if area_floor is None:
        return _VIA_LANDING_SIZE_UM
    needed_side_um = math.sqrt(area_floor[0])
    return max(_VIA_LANDING_SIZE_UM, needed_side_um)


def _resolve_landing_pad_sizes(
    routed_geometry: list[dict[str, Any]],
    variant: str,
    via_drop_size_um: dict[tuple[int, int], float],
) -> dict[tuple[tuple[int, int], tuple[int, int]], float]:
    """Landing-pad sides keyed by ``(landing_layer, via_layer)`` (#2074).

    Each side of each hop independently clears the fixed baseline, its
    metal's area floor (#2072), and the actual via side plus twice that
    pair's enclosure margin. Include the via in the key: the same metal
    can enclose different cuts with different margins on adjacent hops.
    Missing enclosure rules leave the baseline/area floor unchanged.

    Resolve once per distinct pair and pass the result into drawing; the
    router's existing spacing pre-checks keep their baseline footprint,
    as with the area and via-width floors described above.
    """
    from .gen_compose import _VIA_DROP_SIZE_UM

    landing_pad_size_um: dict[tuple[tuple[int, int], tuple[int, int]], float] = {}
    for route in routed_geometry:
        for drop in route.get("via_drops", []):
            via_layer = drop["via_layer"]
            for pad_layer in drop.get("landing_layers", ()):
                pair = (pad_layer, via_layer)
                if pair not in landing_pad_size_um:
                    side = _landing_pad_side_um_for_layer(variant, pad_layer)
                    enclosure = _min_enclosing_margin_um_for_layer(
                        variant, pad_layer, via_layer
                    )
                    if enclosure is not None:
                        via_side = via_drop_size_um.get(via_layer, _VIA_DROP_SIZE_UM)
                        side = max(side, via_side + 2 * enclosure[0])
                    landing_pad_size_um[pair] = side
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
