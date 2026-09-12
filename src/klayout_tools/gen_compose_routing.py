"""Two- and multi-pin Manhattan routing subsystem for ``klt gen-compose``.

Split out of ``gen_compose.py`` (issue #1708) as the routing half of that
module -- layer resolution (:func:`_resolve_route_layer` and friends)
through the two-pin (:func:`route_two_pin`) and bundle/N-pin
(:func:`route_bundle`) routers, ending at :func:`_declare_only_bundle_result`
(the ``route_bundle``-shaped stand-in :func:`~klayout_tools.gen_compose.compose`
uses for a declare-only net). This mirrors the shape of the earlier
``extract.py``/``extract_abstract.py`` (#1303) and ``gen.py``/``gen_pcells``
(#1698) splits: a self-contained, single-purpose subsystem relocated
verbatim out of a file that had grown past the point one module should hold
placement/parsing *and* routing.

``promote_composed_ports`` (port-promotion for the composed cell) and
:func:`~klayout_tools.gen_compose.compose` (top-level orchestration) are
*not* part of this split -- they stay in ``gen_compose.py``, calling into
this module's public routing entry points (``route_two_pin``,
``route_bundle``, ``manhattan_backbone``, ``read_block_layer_geometry``, and
a handful of the layer-resolution/geometry helpers below) exactly as they
called the same functions when this was one file.

Dependency surface is intentionally narrow and mostly one-directional:
``gen_compose.py``'s own module-scope constants/helpers this module needs
(``GenComposeError``, ``_DIRECTION_VECTORS``, ``_ORIENTATION_KDB_ARGS``,
``_VIA_LANDING_SIZE_UM``, ``_load_block_cell``, ``_port_has_geometry``) are
imported *inside* the handful of functions that use them (the same deferred-
import discipline ``gen_pcells/*.py`` uses to depend on ``gen.py``) rather
than at module scope, so this module never has a load-time dependency back
on ``gen_compose.py`` -- only ``gen_compose.py`` depends on this module at
import time. Two of those same functions (``_endpoint_stub_widen_um``,
``_self_net_cross_layer_lane_waypoints_um``) are *also* looked up this way
even though :func:`route_two_pin` (defined in this module) could otherwise
call them as ordinary same-module names -- the test suite monkeypatches
both by their ``klayout_tools.gen_compose`` re-export (predating this
split), and only a call routed back through ``gen_compose`` at call time
still observes that patch. ``gen_compose.py`` in turn imports this module's
entire routing API back (module scope, no cycle -- this module never
imports ``gen_compose`` at its own module scope) purely to preserve
``klayout_tools.gen_compose.<name>`` as a working import path for every
name the test suite/callers used before this split, plus the one name it
calls directly itself (``_ring_port_side``, for ``_validate_block_port``'s
ring-gap-port check).
"""

from __future__ import annotations

import math
from typing import Any

from .decks import (
    ExtractionDeck,
    UnknownDeckError,
    get_deck,
    get_extraction_deck,
    get_nominal_dbu,
)
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


#: ``direction_deg`` values a *stub-widen* (#496) applies to -- a port's own
#: outward-facing north/south stub, never an east/west one. Scoped this
#: narrowly per the issue: an east/west-facing port's horizontal stub is left
#: byte-for-byte unchanged.
_STUB_WIDEN_DIRECTIONS = (90, 270)


def _endpoint_stub_widen_um(
    port: dict[str, Any],
    pos: tuple[float, float],
    direction_deg: int,
    stub_len_um: float,
    width_um: float,
    route_layer: tuple[int, int] | None,
) -> dict[str, Any] | None:
    """Whether ``port``'s own stub (issue #496) needs widening past
    ``width_um``, and if so, the entry :func:`_write_composed_gds` draws for
    it.

    A north/south-facing port's *drawn* pad can be wider than the route's own
    ``width_um`` -- :func:`manhattan_backbone`'s stub still leaves the pad at
    ``width_um``, so the pad's edge outside the stub's narrow footprint (but
    inside the pad's own footprint) can sit closer to the perpendicular jog
    above/below it than the target deck's same-layer spacing rule allows (a
    real DRC violation, not a routability question -- see the issue). Mirrors
    the via-drop landing pad's own precedent (``_VIA_LANDING_SIZE_UM``, sized
    independent of ``width_um`` for the same enclosure reason): widen just the
    stub segment leaving the pad -- from the port out to where the
    un-widened stub already ends (``stub_len_um``, the caller's own
    ``stub_a_um``/``stub_b_um``) -- to the *port's own reported* ``width_um``,
    rather than the whole backbone.

    Only fires when: the port faces north/south (``_STUB_WIDEN_DIRECTIONS`` --
    an east/west-facing port's horizontal stub is untouched, see the issue);
    the port's own reported ``width_um`` actually exceeds the route's
    ``width_um`` (an equal or narrower pad is already a no-op); and the pad is
    actually drawn on ``route_layer`` (a port whose own layer differs needs a
    via-drop instead -- see :func:`_resolve_via_drop_layer` -- and its real
    pad lives on a different layer, where this trace-width mismatch does not
    apply). Not keyed on any port-name convention (``U*_G``, ``TAP_*``, ...)
    -- purely geometric, so it generalizes to any generator's north/south
    port, not just a gate contact's.
    """
    if direction_deg not in _STUB_WIDEN_DIRECTIONS:
        return None
    if route_layer is None or _port_own_layer(port) != route_layer:
        return None
    pad_width_um = port.get("width_um")
    if not isinstance(pad_width_um, (int, float)) or isinstance(pad_width_um, bool):
        return None
    pad_width_um = float(pad_width_um)
    if pad_width_um <= width_um:
        return None
    return {
        "x_um": pos[0],
        "y_um": pos[1],
        "direction_deg": direction_deg,
        "length_um": stub_len_um,
        "width_um": pad_width_um,
    }


#: One via-drop hop, as resolved by :func:`_resolve_via_drop_layer`:
#: ``(via_layer, metal_a, metal_b)`` -- the via square's own layer, plus the
#: two ``deck.metals`` levels it lands a pad on, in ascending stack order
#: (issue #1567).
_ViaDropHop = tuple[tuple[int, int], tuple[int, int], tuple[int, int]]


def _resolve_via_drop_layer(
    deck: ExtractionDeck,
    route_layer: tuple[int, int],
    port_layer: tuple[int, int],
) -> tuple[tuple[_ViaDropHop, ...] | None, str | None]:
    """Resolve whether a route drawn on ``route_layer`` needs a via-drop
    *ladder* to reach a target pin drawn on ``port_layer`` (issue #454,
    re-raising #433's Ask options 1/2: a ``metal2``/via role pair plus router
    support for actually using it; generalized from a single hop to an
    arbitrary number of them by issue #1567).

    Looks the two layers up in the resolved PDK family's own
    :class:`~klayout_tools.decks.ExtractionDeck` ``metals``/``vias`` stack
    (:func:`~klayout_tools.decks.get_extraction_deck`) -- the *same*
    connectivity data ``klt extract``'s per-layer connectivity loop already
    walks (``connect(metals[i], vias[i])`` / ``connect(vias[i],
    metals[i + 1])``), never a second, private via table.

    Returns ``(ladder, error)``:

    * ``(None, None)`` -- no drop needed. Either ``port_layer`` already *is*
      ``route_layer`` (the pre-#454 single-metal routing path, unchanged), or
      ``route_layer`` itself is not a member of ``deck.metals`` (a
      ``"poly"``/``"tap"``-role backbone has no metals stack to walk, so it
      draws directly exactly as it always has).
    * ``(ladder, None)`` -- a drop is needed and fully resolved. ``ladder`` is
      a non-empty, ordered tuple of :data:`_ViaDropHop` entries -- one per via
      the backbone must drop through between ``route_layer`` and
      ``port_layer``, walking ``deck.metals`` one level at a time (in
      ascending stack order, regardless of which end is ``route_layer``/
      ``port_layer``). Before issue #1567 this tuple could only ever have
      exactly one entry -- any greater metals-stack distance was rejected
      below instead. The caller draws a via square on each hop's own
      ``via_layer`` plus a landing pad on each of that hop's ``metal_a``/
      ``metal_b``, all at the pin's own position, exactly as the single-hop
      case always has; consecutive hops share a landing pad at the
      intermediate level they have in common (harmless redundancy, not a
      second, larger pad).
    * ``(None, reason)`` -- a drop is needed but not resolvable (the deck
      declares no via for one of the hops the ladder needs, or the pin sits
      on the deck's bare ``poly`` layer, which no via in the metals stack
      reaches) -- the caller reports the net unroutable rather than drawing a
      disconnected short.

    That last case is issue #492: before it, a metal-role backbone ending on
    a bare-poly gate port fell into the ``(None, None)`` "unrelated role,
    nothing to do" branch and was drawn anyway -- ``"routed": true``, no
    note, and a metal stub sitting *over* the gate poly with no contact
    joining the two. That is an open net (or, where the stub crosses other
    geometry, a short) that only a later ``klt drc``/``klt extract``/``klt
    lvs`` run would surface, with nothing pointing back at the cause. It is
    now an explicit rejection naming the fix.
    """
    if route_layer == port_layer:
        return None, None
    try:
        route_idx = deck.metals.index(route_layer)
    except ValueError:
        # The backbone itself is not on a declared routing-metal level (e.g.
        # routing.layer_role "tap"): there is no metals stack to drop through,
        # so draw directly on route_layer exactly as before #454.
        return None, None
    try:
        port_idx = deck.metals.index(port_layer)
    except ValueError:
        if port_layer == deck.poly:
            return None, (
                "this pin is a bare-poly gate -- gen_compose draws no poly "
                "contact, so the backbone would end as an uncontacted metal "
                "stub over the gate rather than connecting to it. Re-run this "
                "block's generator with params.gate_contact=true so the gate "
                "reports a contacted metal landing pad (issue #492), or name "
                "the gate with pins[] instead of routing to it"
            )
        # Any other non-metals-stack role (e.g. a guard ring's active/tap
        # port, which the ring's own metal already covers at that position)
        # keeps the pre-#454 behavior: drawn directly on route_layer, no via.
        return None, None
    lo, hi = (route_idx, port_idx) if route_idx < port_idx else (port_idx, route_idx)
    ladder: list[_ViaDropHop] = []
    for via_index in range(lo, hi):
        if via_index >= len(deck.vias):
            return None, (
                "the resolved PDK's extraction deck declares no via "
                f"connecting deck metals[{via_index}] and "
                f"metals[{via_index + 1}] -- needed for a via-drop ladder "
                f"between routing.layer_role's metal (deck "
                f"metals[{route_idx}]) and this pin's own layer (deck "
                f"metals[{port_idx}])"
            )
        ladder.append(
            (
                deck.vias[via_index],
                deck.metals[via_index],
                deck.metals[via_index + 1],
            )
        )
    return tuple(ladder), None


def _resolve_cross_block_route_layer(
    variant: str, layer_role: str, cross_layer_role: str
) -> tuple[tuple[int, int], tuple[tuple[int, int], ...]]:
    """Resolve ``routing.cross_block_layer_role`` to ``(cross_route_layer,
    via_layers)`` (issue #1168).

    ``routing.layer_role`` resolves to exactly one ``(layer, datatype)`` pair
    for the whole composition (:func:`_resolve_route_layer`) -- a net whose
    backbone must cross an *intermediate* block's own same-layer pin (the
    canonical case: a same-block self-net leg bussing two of a matched
    array's terminals together across a third terminal sitting between them,
    see :func:`route_two_pin`'s checks 3/4) has no escape route: the single
    global layer either shorts to that pad or the whole composition has to
    move to a second layer, even for nets that never needed it. This resolves
    an optional *second*, higher metal role -- the one :func:`route_two_pin`
    retries a same-layer-short leg on instead of rejecting it outright -- via
    the *same* ``_PDK_ROLE_LAYERS`` table :func:`_resolve_route_layer` reads
    (never a second, private layer map).

    Reuses :func:`_resolve_via_drop_layer` verbatim to confirm the two
    resolved layers are connectable by a via-drop ladder in the resolved PDK
    family's own ``ExtractionDeck.metals``/``.vias`` stack, and to resolve
    which via(s) connect them -- the identical hop-resolution logic
    :func:`route_two_pin`'s check 6 (via-drop) already relies on, called here
    with the roles reversed (``route_layer`` slot = the cross-block layer,
    ``port_layer`` slot = the primary ``layer_role``) rather than
    reimplemented a second time. ``via_layers`` is returned for
    completeness/testability only -- the actual via-drop(s) a leg falling
    back to this cross layer needs are re-resolved per pin by check 6 itself
    (:func:`route_two_pin`), since that is also where a pin's own reported
    layer, not just the two routing layers, enters the picture.

    Raises :class:`GenComposeError` when either role does not resolve (the
    same errors :func:`_resolve_route_layer` raises), when the two roles
    resolve to the identical layer, or when they are not connectable by any
    via-drop ladder at all (one/both roles outside the metals stack
    entirely, or the deck declares no via for one of the hops between them --
    issue #1567 removed the earlier single-hop-only restriction here, the
    same as it did for :func:`_resolve_via_drop_layer` itself).
    """
    from .gen_compose import GenComposeError

    route_layer = _resolve_route_layer(variant, layer_role)
    try:
        cross_layer = _resolve_route_layer(variant, cross_layer_role)
    except GenComposeError as exc:
        # _resolve_route_layer's own message names the field "routing.
        # layer_role" regardless of which caller-facing field is actually
        # invalid -- re-labelled here so a bad cross_block_layer_role points
        # a caller at the right request field, not the primary one.
        raise GenComposeError(
            str(exc).replace("routing.layer_role", "routing.cross_block_layer_role")
        ) from exc
    if cross_layer == route_layer:
        raise GenComposeError(
            f"routing.cross_block_layer_role '{cross_layer_role}' resolves to "
            f"the same layer as routing.layer_role '{layer_role}' -- a "
            "cross-block bus layer must be a distinct metal"
        )
    deck = get_extraction_deck(_pdk_family(variant))
    ladder, error = _resolve_via_drop_layer(deck, cross_layer, route_layer)
    if ladder is None:
        reason = error or (
            f"'{cross_layer_role}' and '{layer_role}' are not both members of "
            "the PDK's metals/via stack"
        )
        raise GenComposeError(
            f"routing.cross_block_layer_role '{cross_layer_role}' cannot be "
            f"connected to routing.layer_role '{layer_role}' by any via-drop "
            f"ladder: {reason}"
        )
    via_layers = tuple(hop[0] for hop in ladder)
    return cross_layer, via_layers


def _resolve_label_layer(
    variant: str, draw_layer: tuple[int, int]
) -> tuple[int, int] | None:
    """Resolve the PDK-family net-label layer/datatype that pairs with
    ``draw_layer``, for naming a net on that drawn layer.

    Mirrors `klt extract`'s own label-recognition convention exactly --
    :class:`klayout_tools.decks.ExtractionDeck`'s ``metals[i]`` <->
    ``metal_labels[i]`` correspondence (see ``_extract_netlist()`` in
    ``extract.py``, which only promotes a net to a named ``.SUBCKT`` pin
    when a ``kdb.Text`` on ``metal_labels[i]`` touches a shape on
    ``metals[i]``), plus the ``poly`` <-> ``poly_label`` correspondence the
    deck uses to name a bare-poly gate node that has no metal landing pad
    (#210, ``l2n.connect(poly, poly_label)``). This is the *same*
    per-PDK-family :class:`~klayout_tools.decks.ExtractionDeck` every `klt
    extract` call already resolves via ``get_extraction_deck`` -- never a
    second, private label-layer table.

    ``draw_layer`` is the drawn ``(layer, datatype)`` a shape lives on: a
    ``routing.layer_role``-resolved metal for a routed ``connectivity[]`` net,
    or a ``pins[]`` port's own reported layer (which may be metal *or* poly).

    Returns ``None`` when ``draw_layer`` is neither a ``metals[]`` entry with
    a paired ``metal_labels[]`` layer nor the deck's ``poly`` layer with a
    ``poly_label`` -- the shape is still drawn, just without a net label for
    `klt extract` to promote into a pin (partial success, not an error).
    """
    family = _pdk_family(variant)
    deck = get_extraction_deck(family)
    try:
        index = deck.metals.index(draw_layer)
    except ValueError:
        index = None
    if index is not None:
        if index >= len(deck.metal_labels):
            return None
        return deck.metal_labels[index]
    if draw_layer == deck.poly:
        return deck.poly_label
    return None


def _polyline_midpoint_um(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    """A point strictly along ``points``' own drawn path, at half its total
    arc length.

    Used to place a routed net's label away from both endpoints (each
    endpoint sits at, or just inside, a block's own port -- see
    :func:`route_two_pin`'s obstacle-overlap check, which already guarantees
    the backbone stays clear of every block's interior beyond a pin's own
    small edge-approach margin). The arc-length midpoint is the point on the
    backbone farthest, in the routing sense, from either endpoint, minimising
    the chance the label lands over a neighbouring block's own metal on the
    same layer (which would misattach the label to that block's net instead
    of this one). Falls back to the first point for a degenerate
    (zero-length) route.
    """
    total = _polyline_length_um(points)
    if total <= 0.0:
        return points[0]

    target = total / 2.0
    accumulated = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        seg_len = abs(x1 - x0) + abs(y1 - y0)
        if accumulated + seg_len >= target:
            if seg_len <= 0.0:
                return (x0, y0)
            frac = (target - accumulated) / seg_len
            return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac)
        accumulated += seg_len
    return points[-1]


def _cleanup_points(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Drop consecutive duplicate and collinear waypoints from a backbone.

    A raw backbone can contain zero-length hops (a stub that lands on top of
    the next waypoint) and three collinear points (a straight run split by a
    degenerate jog); both are geometrically harmless but produce a
    ``pya.Path`` with redundant vertices. Collapsing them keeps the drawn
    path -- and its reported ``route_length_um`` -- minimal and stable.
    """
    deduped: list[tuple[float, float]] = []
    for p in points:
        if not deduped or (
            abs(p[0] - deduped[-1][0]) > 1e-9 or abs(p[1] - deduped[-1][1]) > 1e-9
        ):
            deduped.append(p)

    if len(deduped) <= 2:
        return deduped

    cleaned: list[tuple[float, float]] = [deduped[0]]
    for i in range(1, len(deduped) - 1):
        prev, cur, nxt = cleaned[-1], deduped[i], deduped[i + 1]
        # Collinear (same x for both segments, or same y for both segments)?
        same_x = abs(prev[0] - cur[0]) < 1e-9 and abs(cur[0] - nxt[0]) < 1e-9
        same_y = abs(prev[1] - cur[1]) < 1e-9 and abs(cur[1] - nxt[1]) < 1e-9
        if same_x or same_y:
            continue  # cur adds nothing; skip it
        cleaned.append(cur)
    cleaned.append(deduped[-1])
    return cleaned


def _polyline_length_um(points: list[tuple[float, float]]) -> float:
    """Total length of an orthogonal polyline (sum of ``|dx| + |dy|``)."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        total += abs(x1 - x0) + abs(y1 - y0)
    return total


def manhattan_backbone(
    a: tuple[float, float],
    dir_a_deg: int,
    b: tuple[float, float],
    dir_b_deg: int,
    stub_um: float,
    *,
    stub_a_um: float | None = None,
    stub_b_um: float | None = None,
    waypoints: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Generate a two-pin Manhattan backbone from port ``a`` to port ``b``.

    Reimplements gdsfactory's ``route_single`` *algorithm* (spike section 1)
    natively: leave port ``a`` along its outward ``dir_a_deg`` for a short
    ``stub_um`` stub, leave port ``b`` along ``dir_b_deg`` likewise, then
    connect the two stub ends with right-angle-only segments:

    * both ports facing along **x** (the common row-placement case): a single
      vertical jog at the midpoint x between the stub ends (degenerates to a
      straight line when the ports share a y);
    * both facing along **y**: a single horizontal jog at the midpoint y;
    * mixed (one x, one y): a single corner (an "L").

    ``stub_a_um``/``stub_b_um`` override ``stub_um`` for one port when a
    longer stub is needed to lift the connecting jog clear of a block the
    port sits inside (e.g. a gate landing pad recessed below its block's top
    edge, issue #461); each defaults to ``stub_um``.

    ``waypoints`` (#634) is an optional caller-supplied ordered list of
    ``(x, y)`` points the backbone is forced through, between port ``a``'s
    stub and port ``b``'s stub -- the fixed-shape jog logic above is skipped
    entirely when it is given. This is the escape hatch for a pair the
    fixed shape can never route (most notably two ports facing the *same*
    absolute direction in a row placement, where the one-jog shape's jog
    lands inside the upstream block's own bbox): the caller supplies the
    routing knowledge the fixed shape lacks (e.g. a point above the row's
    shared bbox top), and every consecutive pair in ``[sa, *waypoints, sb]``
    that does not already share an x or a y gets exactly one elbow corner
    inserted between them (move in x first, then y -- the same corner
    convention the mixed-orientation case above uses), so the result stays a
    strictly axis-aligned polyline like every other backbone this function
    returns. The caller is responsible for choosing waypoints that actually
    clear whatever obstacle motivated them -- :func:`route_two_pin`'s
    existing routability checks (including the obstacle-overlap check) still
    run against the resulting path and reject it exactly as they would any
    other backbone if it does not.

    Returns the cleaned ordered ``(x, y)`` waypoint list (um). ``pya.Path``
    renders each interior corner as a square miter that fully fills the bend,
    so no separate bend-insertion pass is needed -- the corner *is* the bend.
    """
    from .gen_compose import _DIRECTION_VECTORS

    ax, ay = a
    bx, by = b
    va = _DIRECTION_VECTORS[dir_a_deg]
    vb = _DIRECTION_VECTORS[dir_b_deg]
    sa_len = stub_um if stub_a_um is None else stub_a_um
    sb_len = stub_um if stub_b_um is None else stub_b_um
    sa = (ax + va[0] * sa_len, ay + va[1] * sa_len)
    sb = (bx + vb[0] * sb_len, by + vb[1] * sb_len)

    if waypoints:
        chain = [sa, *waypoints, sb]
        points: list[tuple[float, float]] = [a, chain[0]]
        for p, q in zip(chain, chain[1:], strict=False):
            same_x = abs(p[0] - q[0]) < 1e-9
            same_y = abs(p[1] - q[1]) < 1e-9
            if not (same_x or same_y):
                points.append((q[0], p[1]))  # elbow: x first, then y
            points.append(q)
        points.append(b)
        return _cleanup_points(points)

    a_horizontal = va[1] == 0  # port a faces +/-x
    b_horizontal = vb[1] == 0  # port b faces +/-x

    mid: list[tuple[float, float]] = []
    if a_horizontal and b_horizontal:
        mx = (sa[0] + sb[0]) / 2.0
        mid = [(mx, sa[1]), (mx, sb[1])]
    elif not a_horizontal and not b_horizontal:
        my = (sa[1] + sb[1]) / 2.0
        mid = [(sa[0], my), (sb[0], my)]
    else:
        # One port faces x, the other y -> a single corner joins the stubs.
        if a_horizontal:
            mid = [(sb[0], sa[1])]
        else:
            mid = [(sa[0], sb[1])]

    return _cleanup_points([a, sa, *mid, sb, b])


def _block_gap_um(
    bbox_a: dict[str, float], bbox_b: dict[str, float], axis: str
) -> float:
    """Signed gap between two placed bboxes along ``axis`` (``"x"``/``"y"``).

    Positive when the boxes are disjoint along that axis (the value is the
    channel width available between them); ``<= 0`` when they touch or overlap
    (a route jog has room regardless). ``axis`` is the axis the channel spans,
    i.e. for a *vertical* jog the relevant channel is the horizontal (``"x"``)
    gap between the two blocks.
    """
    if axis == "x":
        lo, hi = sorted((bbox_a, bbox_b), key=lambda bb: bb["x0"])
        return hi["x0"] - lo["x1"]
    lo, hi = sorted((bbox_a, bbox_b), key=lambda bb: bb["y0"])
    return hi["y0"] - lo["y1"]


#: How far past a bbox edge a detour lane (:func:`_detour_lane_waypoints_um`)
#: is placed, as a multiple of the route width. The obstacle-overlap check
#: itself only demands ``width_um / 2`` (half the drawn conductor), so a lane
#: at ``2 * width_um`` leaves ``1.5 * width_um`` of clear space between the
#: drawn metal's edge and the block it routes around -- clear by a comfortable
#: margin rather than legal-by-a-hair, which is what keeps ``klt drc``'s
#: same-layer spacing rule satisfied against whatever that block draws at its
#: own bbox edge.
_DETOUR_CLEARANCE_WIDTHS = 2.0

#: How many alternate lanes the bounded detour search tries before reporting
#: the net unroutable (#1167). Two -- one on each side of the obstacles -- is
#: the "1-2 alternate jog points" bound the issue asks for, and is what keeps
#: this a *bounded* search rather than a general maze router: there is no
#: recursion (a lane is never itself detoured around) and no per-obstacle
#: growth (see :func:`_detour_lane_waypoints_um`). Shared by both callers of
#: :func:`_detour_lane_waypoints_um` in :func:`route_two_pin` -- the original
#: third-party-block obstacle retry (#1167) and the same-block cross-layer
#: conflict retry (#1393) -- so a block with more same-block self-nets
#: needing ``cross_block_layer_role``'s fallback than this bound can give
#: each a distinct lane still has the same "reported unroutable, not a
#: silent short" failure mode as any other exhausted bounded search.
_MAX_DETOUR_LANES = 2


def _detour_escape_um(
    pos: tuple[float, float],
    vec: tuple[int, int],
    bbox_um: dict[str, float],
    width_um: float,
    axis: str,
) -> float:
    """Where one endpoint leaves its own block on its way to a detour lane.

    ``axis`` is the axis the *lane* runs along (``"x"`` for a horizontal lane
    over/under a row, ``"y"`` for a vertical lane left/right of a column), so
    this returns the coordinate of the leg that carries the route from the
    port out to that lane -- the lane's entry column for a horizontal lane,
    its entry row for a vertical one.

    A port facing *along* the lane's own axis leaves sideways past its own
    block's bbox edge, by ``_DETOUR_CLEARANCE_WIDTHS * width_um`` (at least as
    far as its own stub end). Clearing the **bbox** rather than the stub is
    what keeps the turn DRC-clean: a port's own pad is routinely wider than
    ``width_um`` (a ``resistor_strip`` end pad is 0.42um for a 0.17um route),
    so a leg turning at the stub end leaves a slot between the pad's far
    corner and the leg -- same net, but still a ``li1.space``-class spacing
    violation, exactly the sub-spacing slit issue #496 fixes for a north/
    south-facing stub. A port facing *across* the lane leaves straight out
    along its own facing direction instead (its stub already points at the
    lane), where the #496 stub-widen handles the same pad-vs-trace step.

    **Same-block self-net caveat (#1179, investigated and confirmed safe.)**
    For a same-block self-net (``bbox_a is bbox_b`` at the call site), if the
    two ports face *opposite* ways along the lane's axis, each one's escape
    is pushed toward the *opposite* edge of that one shared bbox -- e.g. a
    port facing ``+x`` pushes toward the bbox's east edge regardless of
    which side of the block it actually sits on. The resulting entry/exit
    leg (drawn at the port's own coordinate on the lane's cross-axis, from
    the stub straight out to that far escape point) can then run differently
    from the short hop past the near edge a caller would expect, depending on
    which way the two ports face relative to each other:

    - **Facing each other** (e.g. the west port facing ``+x``, the east port
      facing ``-x``) -- each escape point is pushed *past* the other port,
      so the entry/exit leg runs back across whatever sits between the two
      ports at their own level. This leg is always a superset of the region
      the original (rejected) straight backbone ran through, so any obstacle
      that made the unrouted backbone crossable is also crossed by the
      malformed leg, and :func:`route_two_pin`'s obstacle-overlap check (run
      in full against the detour's actual drawn points, exactly as for any
      other candidate path) rejects it the same way -- "reported unroutable"
      is the accepted terminal behaviour for this sub-case. Verified with a
      targeted fixture
      (``test_route_two_pin_self_net_opposite_facing_detour_rejects_cleanly``
      in ``tests/test_gen_compose.py``) and a 30k-trial randomized sweep
      restricted to this orientation that found zero wrongly-accepted paths.
    - **Facing away from each other** (e.g. the west port facing ``-x``, the
      east port facing ``+x``) -- each escape point is instead pushed *away*
      from the other port, past the bbox's own far edge on its own side, so
      neither entry/exit leg overlaps the original backbone's range at all
      (disjoint, not a superset). The detour lane then draws a full loop
      around the entire block rather than being rejected -- a valid,
      non-crossing (if very indirect) path, not a silently bad one. Verified
      with a targeted fixture
      (``test_route_two_pin_self_net_away_facing_detour_loops_around_cleanly``
      in ``tests/test_gen_compose.py``) and a ~2.8k-trial randomized sweep
      restricted to this orientation that found zero wrongly-crossing paths,
      with about a quarter of trials routing successfully via the loop-around
      rather than being rejected.

    Both outcomes were suspected to still fail safe rather than draw a
    silently bad path, and that has been confirmed for each: no fix is
    needed here, but "reported unroutable" is *not* the terminal behaviour
    for every same-block opposite-facing self-net -- only for the
    facing-each-other sub-case above.
    """
    if axis == "x":
        if vec[0] > 0:
            return max(
                pos[0] + width_um, bbox_um["x1"] + _DETOUR_CLEARANCE_WIDTHS * width_um
            )
        if vec[0] < 0:
            return min(
                pos[0] - width_um, bbox_um["x0"] - _DETOUR_CLEARANCE_WIDTHS * width_um
            )
        return pos[0]
    if vec[1] > 0:
        return max(
            pos[1] + width_um, bbox_um["y1"] + _DETOUR_CLEARANCE_WIDTHS * width_um
        )
    if vec[1] < 0:
        return min(
            pos[1] - width_um, bbox_um["y0"] - _DETOUR_CLEARANCE_WIDTHS * width_um
        )
    return pos[1]


def _detour_lane_waypoints_um(
    end_a: tuple[tuple[float, float], tuple[int, int], dict[str, float]],
    end_b: tuple[tuple[float, float], tuple[int, int], dict[str, float]],
    obstacle_bboxes_um: list[dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
) -> list[list[tuple[float, float]]]:
    """Alternate waypoint paths that route *around* one or more obstacles.

    ``end_a``/``end_b`` are the two endpoints as ``(position, direction
    vector, own placed bbox)``, and ``obstacle_bboxes_um`` are the placed
    bboxes the fixed-shape backbone was rejected for crossing. Each returned
    candidate is a ``waypoints`` list for :func:`manhattan_backbone`
    describing one **lane**: a single straight run, perpendicular to the axis
    the two endpoints are mostly separated along, placed clear of every block
    in the way, reached from each port by one leg out to it
    (:func:`_detour_escape_um`). So the drawn path is ``a -> stub -> out to
    the lane -> along the lane -> back in -> b`` -- every consecutive pair of
    points shares an x or a y, so ``manhattan_backbone`` inserts no elbows of
    its own and the result is exactly the hand-supplied ``waypoints_um``
    shape a caller writes today for the same job (#634).

    The lane is placed past the extreme edge of **every** block whose extent
    the run spans (plus the obstacles themselves, which is what makes the
    result actually clear them), offset by ``_DETOUR_CLEARANCE_WIDTHS *
    width_um``. That single union is why the number of obstacles between the
    pins costs nothing: one lane over the top of the row clears two blocks
    exactly as it clears one. What *is* bounded is the number of candidates --
    at most ``_MAX_DETOUR_LANES``, the two sides of the row -- ordered
    shortest-detour first, with a stable tie-break (over before under, right
    before left) so the composed GDS stays byte-reproducible (#320).

    Returns candidates in try-order; the caller re-runs every routability
    check against each drawn path and keeps the first that passes.
    """
    (a, va, bbox_a), (b, vb, bbox_b) = end_a, end_b
    clearance_um = _DETOUR_CLEARANCE_WIDTHS * width_um
    sa = (a[0] + va[0] * width_um, a[1] + va[1] * width_um)
    sb = (b[0] + vb[0] * width_um, b[1] + vb[1] * width_um)

    if abs(sb[0] - sa[0]) >= abs(sb[1] - sa[1]):
        # Endpoints mostly separated in x -> the connecting run travels in x,
        # so the two ways around are a horizontal lane over or under the row.
        enter = _detour_escape_um(a, va, bbox_a, width_um, "x")
        leave = _detour_escape_um(b, vb, bbox_b, width_um, "x")
        lo, hi = sorted((enter, leave))
        boxes = [
            *obstacle_bboxes_um,
            *(
                bbox
                for bbox in placed_bboxes_um.values()
                if bbox["x0"] <= hi and bbox["x1"] >= lo
            ),
        ]
        over_y = max(bbox["y1"] for bbox in boxes) + clearance_um
        under_y = min(bbox["y0"] for bbox in boxes) - clearance_um
        lanes = [
            [
                (enter, sa[1]),
                (enter, lane_y),
                (leave, lane_y),
                (leave, sb[1]),
            ]
            for lane_y in (over_y, under_y)
        ]
        lanes.sort(key=lambda wp: abs(wp[1][1] - sa[1]) + abs(wp[2][1] - sb[1]))
    else:
        enter = _detour_escape_um(a, va, bbox_a, width_um, "y")
        leave = _detour_escape_um(b, vb, bbox_b, width_um, "y")
        lo, hi = sorted((enter, leave))
        boxes = [
            *obstacle_bboxes_um,
            *(
                bbox
                for bbox in placed_bboxes_um.values()
                if bbox["y0"] <= hi and bbox["y1"] >= lo
            ),
        ]
        right_x = max(bbox["x1"] for bbox in boxes) + clearance_um
        left_x = min(bbox["x0"] for bbox in boxes) - clearance_um
        lanes = [
            [
                (sa[0], enter),
                (lane_x, enter),
                (lane_x, leave),
                (sb[0], leave),
            ]
            for lane_x in (right_x, left_x)
        ]
        lanes.sort(key=lambda wp: abs(wp[1][0] - sa[0]) + abs(wp[2][0] - sb[0]))
    return lanes[:_MAX_DETOUR_LANES]


#: How many alternate corner loops :func:`_self_net_cross_layer_lane_waypoints_um`
#: (issue #1393) tries before giving up -- the block's four corners (top/
#: bottom x left/right), still a fixed, bounded set with no recursion and no
#: per-obstacle growth, same as :func:`_detour_lane_waypoints_um`'s own bound
#: just above. Deliberately a *different* constant from ``_MAX_DETOUR_LANES``
#: rather than reusing it: that search's "two ways around" (over/under, or
#: left/right) is inherent to a *channel* between two obstacles, whereas this
#: retry loops fully *around* one block's own bbox, where there are
#: genuinely four independent corners to try, not two.
_MAX_SAME_BLOCK_CROSS_LAYER_LANES = 4


def _self_net_cross_layer_lane_waypoints_um(
    a: tuple[float, float],
    b: tuple[float, float],
    bbox_um: dict[str, float],
    width_um: float,
) -> list[list[tuple[float, float]]]:
    """Alternate waypoint loops for the same-block cross-layer conflict retry
    (issue #1393, see :func:`route_two_pin`'s own docstring for when this is
    used).

    Unlike :func:`_detour_lane_waypoints_um` -- which places its lane using
    each port's own natural entry/exit column (fine for an inter-block
    obstacle, since a third block's bbox has no relationship to either
    port's own coordinates) -- a *same-block* self-net's two ports sit
    *inside* the very bbox this retry must clear, and a different same-block
    self-net's own ports can sit at those exact same coordinates: the
    ``diff_pair``/``splits: 2`` reproduction this issue fixes has its two
    self-nets' ports at the identical pair of columns, swapped between the
    two nets (a common-centroid cross-quad checkerboard). Routing straight
    up (or down) either port's own column into an over/under lane -- the
    shape :func:`_detour_lane_waypoints_um` would produce -- runs straight
    through the *other* self-net's own approach stub at that same column
    even when the horizontal jog itself clears it (this was verified as the
    actual failure mode during this issue's implementation: shifting only
    the jog's height, the first fix attempted, still collided for exactly
    this reason).

    So this instead routes fully **around the outside of the block's own
    bbox**: each candidate is a single corner point strictly outside
    ``bbox_um`` on both axes (one of the four corners, each pushed out by
    :func:`_detour_lane_waypoints_um`'s own ``_DETOUR_CLEARANCE_WIDTHS *
    width_um`` clearance) -- :func:`manhattan_backbone`'s own waypoint
    handling (``waypoints_um``, issue #634) already inserts the two elbows
    needed to reach it and return from it (leave ``a``'s stub, sideways to
    the corner's column *at the port's own row*, then along that column
    clear of the block, then sideways to ``b``'s column *at the corner's
    row*, then down/up into ``b``'s stub) -- so no bespoke elbow-insertion
    logic is needed here, only the four corner points themselves. Because
    the loop leaves each block edge at the *other* port's own row/column
    rather than at either port's shared native one, the two nets' own
    approach stubs no longer share a coordinate to collide on.

    Returns up to :data:`_MAX_SAME_BLOCK_CROSS_LAYER_LANES` single-waypoint
    candidates, ordered shortest total detour first (ties broken over
    before under, right before left -- the same stable convention
    :func:`_detour_lane_waypoints_um` uses, keeping the composed GDS
    byte-reproducible, #320). The caller re-runs every routability check
    against each drawn path and keeps the first that passes -- exactly the
    same "propose candidates, let the checks decide" contract
    :func:`_detour_lane_waypoints_um` follows.
    """
    clearance_um = _DETOUR_CLEARANCE_WIDTHS * width_um
    over_y = bbox_um["y1"] + clearance_um
    under_y = bbox_um["y0"] - clearance_um
    right_x = bbox_um["x1"] + clearance_um
    left_x = bbox_um["x0"] - clearance_um
    corners = [
        (side_x, lane_y) for lane_y in (over_y, under_y) for side_x in (right_x, left_x)
    ]
    lanes = [[corner] for corner in corners]
    lanes.sort(
        key=lambda wp: (
            abs(wp[0][0] - a[0])
            + abs(wp[0][1] - a[1])
            + abs(wp[0][0] - b[0])
            + abs(wp[0][1] - b[1])
        )
    )
    return lanes[:_MAX_SAME_BLOCK_CROSS_LAYER_LANES]


#: Port-name prefixes a ``klt gen`` generator uses for a guard/collector ring's
#: own tap ports (``diff_pair``'s ``add_guard_ring``, ``bjt_array``'s
#: ``add_collector_ring``, and the standalone ``guard_ring`` generator itself
#: -- see ``gen.py``'s ``_diff_pair_describe``/``_bjt_array_describe``/
#: ``_guard_ring_describe``). A block reporting any port with one of these
#: prefixes has a ring drawn *around* its other ports -- any route touching a
#: non-tap port on that block necessarily crosses the ring's own metal loop on
#: its way in or out (#199 case 2).
_RING_TAP_PORT_PREFIXES = ("TAP_", "COLL_")


#: Port-name prefix a ``klt gen`` generator uses to report a *ring opening*
#: -- the routing gap ``params.ring_gap_side`` cuts through one side of a
#: guard/collector ring (#434, see ``gen.py``'s ``_ring_ports``). A
#: ``GAP_<side>`` entry is a marker, not a conductor: ``x_um``/``y_um`` is the
#: opening's centre on the ring's own centre line, ``width_um`` is how long
#: the opening is along that side, and ``direction_deg`` is the side's outward
#: normal. It is the one place a route may cross the ring without merging with
#: the ring's tap net.
_RING_GAP_PORT_PREFIX = "GAP_"

#: The four sides of a ring, as named by ``TAP_``/``COLL_``/``GAP_`` ports.
_RING_SIDES = ("N", "S", "E", "W")


def _block_has_ring_taps(block: dict[str, Any]) -> bool:
    """Whether ``block`` reports a guard/collector ring (any tap port)."""
    return any(name.startswith(_RING_TAP_PORT_PREFIXES) for name in block["port_names"])


def _ring_port_side(name: str) -> str | None:
    """The ring side a ``TAP_<side>``/``COLL_<side>``/``GAP_<side>`` port name
    refers to, or ``None`` for any other port name."""
    for prefix in (*_RING_TAP_PORT_PREFIXES, _RING_GAP_PORT_PREFIX):
        if name.startswith(prefix):
            side = name[len(prefix) :]
            if side in _RING_SIDES:
                return side
    return None


def _ring_gap_ports(block: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The ring openings ``block`` declares, keyed by side (#434).

    Reads every ``GAP_<side>`` port with usable geometry into
    ``{"x_um", "y_um", "opening_um"}`` (block-local coordinates). An empty
    dict means the block's ring is a closed loop, which is what the
    guard/collector-ring check (#199 case 2) requires it to reject routes
    into.
    """
    gaps: dict[str, dict[str, float]] = {}
    for name, port in block["ports"].items():
        if not name.startswith(_RING_GAP_PORT_PREFIX) or not isinstance(port, dict):
            continue
        side = name[len(_RING_GAP_PORT_PREFIX) :]
        if side not in _RING_SIDES:
            continue
        values: list[float] = []
        for key in ("x_um", "y_um", "width_um"):
            value = port.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                break
            values.append(float(value))
        if len(values) != 3 or values[2] <= 0.0:
            continue  # a gap with no usable geometry cannot be routed through
        gaps[side] = {"x_um": values[0], "y_um": values[1], "opening_um": values[2]}
    return gaps


def _ring_side_lines(block: dict[str, Any]) -> dict[str, float]:
    """Each ring side's own centre-line coordinate in the block's local frame
    (an ``x`` for ``E``/``W``, a ``y`` for ``N``/``S``), keyed by side.

    Read off the ring's own reported ports: a ``TAP_``/``COLL_`` tap port sits
    at the midpoint of its side, and a ``GAP_`` port sits on the same centre
    line, so between them a ring reports where all four of its sides run --
    which is what :func:`_ring_gap_route_conflict` needs to tell a route that
    passes through a declared opening from one that cuts the ring's metal
    somewhere else.
    """
    lines: dict[str, float] = {}
    for name, port in block["ports"].items():
        side = _ring_port_side(name)
        if side is None or not isinstance(port, dict):
            continue
        value = port.get("x_um" if side in ("E", "W") else "y_um")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        lines[side] = float(value)
    return lines


def _ring_gap_route_conflict(
    points: list[tuple[float, float]],
    block: dict[str, Any],
    offset_um: dict[str, float],
    bbox_um: dict[str, float],
    width_um: float,
) -> str | None:
    """Why ``points`` may not cross ``block``'s (gapped) guard/collector ring,
    or ``None`` when every crossing passes cleanly through a declared opening.

    Applied only to a block that declares at least one ``GAP_<side>`` opening
    (#434) -- a closed ring is still rejected outright by the name-based
    guard/collector-ring check in :func:`route_two_pin`. Every segment of the
    backbone is tested against all four of the ring's own side centre lines
    (:func:`_ring_side_lines`), inside the block's placed bbox:

    * a crossing on a side with no declared opening merges the net with the
      ring's tap net exactly as before;
    * a crossing on the gapped side must clear the opening's edges by half
      the route width plus the block's own reported ``min_spacing_um``, so the
      drawn wire fits *through* the opening rather than shorting to either cut
      end of the ring;
    * a segment running *along* a ring side lies on the ring's metal for its
      whole length, which is a short however wide the opening is.

    A ring that does not report where all four of its sides run cannot be
    checked this way, and is rejected rather than assumed clear.
    """
    eps = 1e-9
    gaps = _ring_gap_ports(block)
    lines = _ring_side_lines(block)
    missing = [side for side in _RING_SIDES if side not in lines]
    if missing:
        return (
            f"block '{block['id']}' declares a ring opening but reports no "
            f"port locating its ring's {'/'.join(missing)} side(s) -- the "
            "route cannot be shown to pass through the opening rather than "
            "across the ring's metal elsewhere"
        )

    clearance_um = width_um / 2.0 + block.get("min_spacing_um", 0.0)
    for side, local in lines.items():
        vertical = side in ("E", "W")
        line = local + (offset_um["x"] if vertical else offset_um["y"])
        if vertical:
            extent = (bbox_um["y0"], bbox_um["y1"])
        else:
            extent = (bbox_um["x0"], bbox_um["x1"])

        window: tuple[float, float] | None = None
        gap = gaps.get(side)
        if gap is not None:
            centre = (
                gap["y_um"] + offset_um["y"]
                if vertical
                else gap["x_um"] + offset_um["x"]
            )
            half = gap["opening_um"] / 2.0
            window = (centre - half, centre + half)

        for p0, p1 in zip(points, points[1:], strict=False):
            # "across" is the coordinate the side's centre line is fixed in;
            # "along" is the coordinate that runs down the side.
            across0, across1 = (p0[0], p1[0]) if vertical else (p0[1], p1[1])
            along0, along1 = (p0[1], p1[1]) if vertical else (p0[0], p1[0])

            if abs(across0 - line) <= eps and abs(across1 - line) <= eps:
                seg_lo, seg_hi = sorted((along0, along1))
                if min(seg_hi, extent[1]) - max(seg_lo, extent[0]) > eps:
                    return (
                        f"backbone runs along block '{block['id']}''s ring on "
                        f"its {side} side -- a route laid on the ring's own "
                        "metal merges this net with the ring's tap net, "
                        "whatever opening the ring declares"
                    )
                continue

            if (across0 - line) * (across1 - line) >= 0.0:
                continue  # this segment does not cross the side's centre line

            t = (line - across0) / (across1 - across0)
            at = along0 + t * (along1 - along0)
            if not (extent[0] - eps <= at <= extent[1] + eps):
                continue  # crosses the line beyond the block -- clear of the ring

            if window is None:
                return (
                    f"backbone crosses block '{block['id']}''s ring on its "
                    f"{side} side, which declares no opening -- the route "
                    "would merge this net with the ring's tap net; cut the "
                    f"opening on that side (params.ring_gap_side: '{side}') "
                    "or route through the side that already has one"
                )
            if (
                at - clearance_um < window[0] - eps
                or at + clearance_um > window[1] + eps
            ):
                return (
                    f"backbone crosses block '{block['id']}''s ring on its "
                    f"{side} side at {at:.4g}um, outside the "
                    f"[{window[0]:.4g}, {window[1]:.4g}]um opening it declares "
                    f"(a {width_um}um-wide route needs {clearance_um:.4g}um of "
                    "clearance inside the opening) -- widen the opening "
                    "(params.ring_gap_um), move it (params.ring_gap_offset_um), "
                    "or place the blocks so the route lines up with it"
                )

    return None


def _port_edge_margin_um(
    port_xy: tuple[float, float], direction_deg: int, bbox_um: dict[str, float]
) -> float:
    """Distance from a port to its own placed block's bbox edge it faces.

    A ``klt gen`` port need not sit exactly on its own block's bbox boundary
    (e.g. ``mos_array``/``diff_pair`` inset every port ~0.2um from the edge
    regardless of ``add_guard_ring``) -- this is the amount of a route's
    approach into that port that is *always* inside the block's own bbox,
    regardless of where the route comes from, and therefore not evidence of
    anything wrong. Used as the routability check's per-block allowance
    (:func:`route_two_pin`) -- an approach that crosses *more* than this
    margin through the block's interior is crossing something else inside it
    (e.g. another device's pin, or -- when the extra margin matches a ring's
    width -- the guard/collector ring, though that specific case is instead
    caught directly by :func:`_block_has_ring_taps`, since the margin here is
    identical whether or not a ring sits in that space).
    """
    x, y = port_xy
    if direction_deg == 0:
        return bbox_um["x1"] - x
    if direction_deg == 180:
        return x - bbox_um["x0"]
    if direction_deg == 90:
        return bbox_um["y1"] - y
    if direction_deg == 270:
        return y - bbox_um["y0"]
    return 0.0  # unreachable -- direction_deg is validated before this is called


def _segment_bbox_interior_overlap_um(
    p0: tuple[float, float], p1: tuple[float, float], bbox_um: dict[str, float]
) -> float:
    """Length of axis-aligned segment ``p0``->``p1`` inside ``bbox_um``'s
    *strict interior* (a segment that only touches the boundary, or lies
    fully outside, returns ``0.0``).

    Every :func:`manhattan_backbone` segment is horizontal or vertical by
    construction, so only those two cases are handled; a (unreachable)
    diagonal or zero-length segment reports no overlap rather than raising.
    """
    eps = 1e-9
    bx0, by0 = bbox_um["x0"] + eps, bbox_um["y0"] + eps
    bx1, by1 = bbox_um["x1"] - eps, bbox_um["y1"] - eps
    if bx0 >= bx1 or by0 >= by1:
        return 0.0  # degenerate (zero-area) bbox

    x0, y0 = p0
    x1, y1 = p1
    horizontal = abs(y0 - y1) < 1e-9
    vertical = abs(x0 - x1) < 1e-9
    if horizontal and not vertical:
        if not (by0 < y0 < by1):
            return 0.0
        lo, hi = sorted((x0, x1))
        return max(0.0, min(hi, bx1) - max(lo, bx0))
    if vertical and not horizontal:
        if not (bx0 < x0 < bx1):
            return 0.0
        lo, hi = sorted((y0, y1))
        return max(0.0, min(hi, by1) - max(lo, by0))
    return 0.0


def read_block_layer_geometry(
    block_id: str,
    block: dict[str, Any],
    offset_um: dict[str, float],
    layer: tuple[int, int],
) -> dict[str, Any] | None:
    """Read ``block``'s **drawn** shapes on ``layer`` into the composed frame.

    Returns ``{"region": kdb.Region, "dbu": float}`` -- the block's own GDS
    geometry on the ``(layer, datatype)`` pair the route is drawn on, mirrored/
    rotated per the block's own ``orientation`` (#1166) and translated by the
    block's placed ``offset_um``, in integer database units -- or ``None``
    when the block draws nothing there. The same ``kdb.Trans`` (mirror-then-
    translate) is applied here as :func:`_write_composed_gds` applies to this
    block's actual cell instance, so this obstacle geometry always agrees
    with what the composed GDS actually draws.

    This is the one place this module looks at a block's *shapes* rather than
    at its ``generator_report``. It exists because a ``klt gen`` port's
    reported ``width_um`` is the port's **contact/access** size, not the
    extent of the pad metal drawn around it (e.g. a ``bjt_array`` base tie
    reports ``width_um: 0.22`` -- ``CONTACT_SIZE_UM`` -- for a pad whose drawn
    local metal is 0.42um x 0.68um, see ``gen.py``'s ``_bjt_unit_layout``), so
    the reported-geometry pad model :func:`route_two_pin`'s self-net check
    (check 3, ``_segment_bbox_interior_overlap_um``) starts from systematically
    *under*-estimates what a route can short to (#453/#469). Placement math is
    still never re-derived from the stream -- this reads obstacle geometry
    only, and only for a self-net's own block.
    """
    import klayout.db as kdb

    from .gen_compose import _ORIENTATION_KDB_ARGS, _load_block_cell

    src_layout, src_cell = _load_block_cell(block_id, block)

    dbu = src_layout.dbu
    layer_index = src_layout.find_layer(layer[0], layer[1])
    if layer_index is None:
        return None

    region = kdb.Region(src_cell.begin_shapes_rec(layer_index))
    region.merge()
    if region.is_empty():
        return None
    rot, mirrx = _ORIENTATION_KDB_ARGS[block.get("orientation", "none")]
    region.transform(
        kdb.Trans(
            rot,
            mirrx,
            int(round(offset_um["x"] / dbu)),
            int(round(offset_um["y"] / dbu)),
        )
    )
    return {"region": region, "dbu": dbu}


def _drawn_route_region(
    points_um: list[tuple[float, float]], width_um: float, dbu: float
):
    """The metal a routed backbone actually draws, as a ``kdb.Region``.

    Built from the *same* ``kdb.Path(points, width)`` construction
    :func:`_write_composed_gds` inserts into the composed cell, so this check
    and the drawn output cannot disagree about the route's footprint.
    """
    import klayout.db as kdb

    path = kdb.Path(
        [kdb.Point(int(round(x / dbu)), int(round(y / dbu))) for (x, y) in points_um],
        int(round(width_um / dbu)),
    )
    return kdb.Region(path.polygon())


def _drawn_leg_footprint_region(
    points_um: list[tuple[float, float]],
    width_um: float,
    via_drops: list[dict[str, Any]],
    stub_widen: list[dict[str, Any]],
    dbu: float,
    route_layer: tuple[int, int] | None = None,
):
    """The full metal one routed leg draws on its own route layer, as a
    ``kdb.Region`` -- the bare backbone (:func:`_drawn_route_region`) plus
    every via-drop landing pad and stub-widen box :func:`_write_composed_gds`
    *also* draws on that same layer (issue #1197).

    ``route_layer`` (issue #1567) is the leg's own effective drawing layer --
    the one the returned region represents. A single-hop via-drop's landing
    pad always lands on that layer (one of its two ``landing_layers`` always
    *is* the backbone's own layer, by construction), so this included every
    resolved drop unconditionally before #1567. A multi-hop ladder's
    intermediate/far hops land on layers *other* than ``route_layer`` --
    those are excluded here (they are not part of this leg's footprint *on
    route_layer*), leaving only the hop(s) whose ``landing_layers`` actually
    include it. ``route_layer=None`` (a caller predating #1386/#1567, or one
    that genuinely does not know the leg's layer) keeps every drop, exactly
    the pre-#1567 behavior -- the conservative choice when the layer is
    unknown.

    The route-vs-route collision check (#1057) originally built its
    comparison region from the bare backbone path alone. That misses two
    kinds of metal :func:`_write_composed_gds` draws wider than the
    backbone itself, on the identical layer:

    * a via-drop's landing pad (``_VIA_LANDING_SIZE_UM``, sized from the
      PDK's own contact-enclosure convention, independent of the route's own
      ``width_um``) -- drawn centered on the leg's own endpoint whenever
      that endpoint's pin sits on a different physical layer than the route;
    * a stub-widen box (:func:`_endpoint_stub_widen_um`, #496) -- drawn
      re-covering the backbone's own first segment at the pin's wider pad
      width, when that pad is wider than the route.

    Either can extend past the bare backbone far enough to land on a
    *different* net's already-accepted route while the two backbones
    themselves never touch -- exactly what two same-block self-nets whose
    via-drop landing pads cross each other's backbones produce: each leg
    individually passes every one of :func:`route_two_pin`'s own checks
    (which only ever compare a leg against its *own* block's geometry), and
    the bare-backbone-only version of this check found no overlap either, so
    both compose ``routed: true`` while `klt extract` merges the two nets
    onto one node through the landing pad's silent overlap. Building this
    region from the *same* drawing primitives :func:`_write_composed_gds`
    actually inserts -- just as :func:`_drawn_route_region` already does for
    the bare path -- is what keeps this check and the composed output from
    ever disagreeing about a leg's real footprint.
    """
    import klayout.db as kdb

    from .gen_compose import _VIA_LANDING_SIZE_UM

    region = _drawn_route_region(points_um, width_um, dbu)

    landing_half_dbu = int(round((_VIA_LANDING_SIZE_UM / 2.0) / dbu))
    for drop in via_drops:
        landing_layers = drop.get("landing_layers")
        if (
            route_layer is not None
            and landing_layers is not None
            and route_layer not in landing_layers
        ):
            continue  # this hop's pads are on a different layer than route_layer
        cx = int(round(drop["x_um"] / dbu))
        cy = int(round(drop["y_um"] / dbu))
        region.insert(
            kdb.Box(
                cx - landing_half_dbu,
                cy - landing_half_dbu,
                cx + landing_half_dbu,
                cy + landing_half_dbu,
            )
        )

    for widen in stub_widen:
        cx = int(round(widen["x_um"] / dbu))
        cy = int(round(widen["y_um"] / dbu))
        half_dbu = int(round((widen["width_um"] / 2.0) / dbu))
        length_dbu = int(round(widen["length_um"] / dbu))
        if widen["direction_deg"] == 90:
            region.insert(kdb.Box(cx - half_dbu, cy, cx + half_dbu, cy + length_dbu))
        else:  # 270
            region.insert(kdb.Box(cx - half_dbu, cy - length_dbu, cx + half_dbu, cy))

    return region


def _pad_self_notch_violation_um(
    pad_box_um: tuple[float, float, float, float],
    geometry: dict[str, Any],
    spacing_um: float,
) -> float | None:
    """Whether a newly drawn pad box (a via-drop landing pad or a
    stub-widen box -- both wider than the route's own backbone, drawn
    independent of ``routing.width_um``) comes within ``spacing_um`` of the
    *same block's* own other drawn shapes on the pad's own layer, once
    merged with them (issue #1520).

    ``geometry`` is :func:`read_block_layer_geometry`'s result for the pad's
    owning block, on the pad's own layer. The pad is *meant* to land on --
    and merge with -- the block's own wire at the declared port; that touch
    is never a violation. But the merged shape can still have a self-notch
    elsewhere the caller never chose: e.g. a perpendicular leg of the very
    same wire, near a corner the declared port happens to sit close to. A
    rule-deck same-layer ``"space"`` check (e.g. sky130's ``li1.space.1``) is
    net-agnostic, so that notch is a real ``klt drc`` violation even though
    both shapes are the same electrical node -- exactly the class of mistake
    #1520 reports gen-compose drawing silently.

    ``kdb.Region.notch_check`` is the tool for this: a same-*polygon*
    self-space check, as opposed to :func:`_leg_conflict`'s own
    inflate-and-intersect test (used against a *different* net's
    already-accepted region) or :func:`_self_net_drawn_short`'s
    "exclude the shape the route lands on" test -- both of which report
    nothing here, since the pad and the wire it lands on are, after
    ``region.merge()``, literally the same polygon; the violation is a
    self-notch of *that* polygon, not a conflict between two separate ones.

    Returns the closest found spacing (um), or ``None`` when nothing in the
    merged shape violates ``spacing_um`` (including when ``spacing_um`` is
    non-positive, or the pad does not even reach the block's own geometry at
    the resolution ``geometry["dbu"]`` provides).
    """
    import klayout.db as kdb

    dbu = geometry["dbu"]
    spacing_dbu = int(round(spacing_um / dbu))
    if spacing_dbu <= 0:
        return None

    x0_um, y0_um, x1_um, y1_um = pad_box_um
    combined = kdb.Region()
    combined.insert(geometry["region"])
    combined.insert(
        kdb.Box(
            int(round(x0_um / dbu)),
            int(round(y0_um / dbu)),
            int(round(x1_um / dbu)),
            int(round(y1_um / dbu)),
        )
    )
    combined.merge()
    violations = combined.notch_check(spacing_dbu)
    if violations.is_empty():
        return None
    return min(edge_pair.distance() for edge_pair in violations) * dbu


def _self_net_drawn_short(
    points_um: list[tuple[float, float]],
    geometry: dict[str, Any],
    a: tuple[float, float],
    b: tuple[float, float],
    width_um: float,
    own_ports: dict[str, Any],
    own_offset: dict[str, float],
) -> tuple[float, list[str]] | None:
    """Whether a leg's drawn metal lands on a placed block's *other* drawn
    shapes on the route layer.

    ``geometry`` is :func:`read_block_layer_geometry`'s result for the block
    to check against -- both a same-block self-net's shared block (``a``/
    ``b`` its two endpoints, check 4, #453/#469) and, since issue #1527, a
    single inter-block leg endpoint's own block (``a``/``b`` both that one
    endpoint's own landing point -- see the "own-block escape check" caller).
    Every merged shape of ``geometry``'s block on the route layer is an
    obstacle **except** whatever shape(s) ``a``/``b`` land on -- the route is
    meant to terminate there. Any remaining shape the drawn metal actually
    overlaps (positive area; a mere edge touch is a spacing question for
    ``klt drc``, not a short) is a silent short.

    Returns ``(overlap_um2, crossed_port_names)`` for the first such shape
    set, or ``None`` when the route only lands on shapes at ``a``/``b``.
    """
    import klayout.db as kdb

    from .gen_compose import _port_has_geometry

    region = geometry["region"]
    dbu = geometry["dbu"]

    def _probe(x_um: float, y_um: float):
        px = int(round(x_um / dbu))
        py = int(round(y_um / dbu))
        return kdb.Box(px - 1, py - 1, px + 1, py + 1)

    endpoints = kdb.Region()
    for x_um, y_um in (a, b):
        endpoints.insert(_probe(x_um, y_um))

    # Everything on this block's route layer that is *not* one of the two
    # shapes the route is meant to terminate on.
    obstacles = region.not_interacting(endpoints)
    if obstacles.is_empty():
        return None

    overlap = obstacles & _drawn_route_region(points_um, width_um, dbu)
    if overlap.is_empty():
        return None

    overlap_um2 = overlap.area() * dbu * dbu
    hit = obstacles.interacting(overlap)
    crossed: list[str] = []
    for name, port in own_ports.items():
        if not _port_has_geometry(port):
            continue
        probe = kdb.Region(
            _probe(
                float(port["x_um"]) + own_offset["x"],
                float(port["y_um"]) + own_offset["y"],
            )
        )
        if not hit.interacting(probe).is_empty():
            crossed.append(name)
    return overlap_um2, crossed


def route_two_pin(
    pin_a: dict[str, Any],
    pin_b: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
    route_layer: tuple[int, int] | None = None,
    extraction_deck: ExtractionDeck | None = None,
    block_geometry: dict[str, dict[str, Any] | None] | None = None,
    waypoints_um: list[tuple[float, float]] | None = None,
    cross_block_route_layer: tuple[int, int] | None = None,
    cross_block_geometry: dict[str, dict[str, Any] | None] | None = None,
    cross_block_width_um: float | None = None,
    leg_conflict: Any = None,
    block_geometry_for: Any = None,
    cross_block_geometry_for: Any = None,
) -> dict[str, Any]:
    """Route one two-pin net and report the result.

    Resolves each pin's port position into the composed coordinate frame
    (port ``x_um``/``y_um`` translated by its block's ``offset_um``),
    generates a Manhattan backbone (:func:`manhattan_backbone`), and applies
    five routability checks before reporting success -- all diagnostic
    heuristics against the composition's own already-known geometry
    (``bbox_um``/``ports[]``) or, for check 4, the block's own drawn shapes
    (:func:`read_block_layer_geometry`) -- not a DRC check (``klt drc``
    remains authoritative):

    1. **Channel-width check** (original, phase 2): when the backbone
       requires a jog *across* the channel between the two pins' blocks and
       that channel is narrower than ``width_um``, the wire cannot fit
       without overlapping a block. Evaluated from the raw port positions
       and the inter-block gap -- i.e. it is a property of the *fixed*
       one-jog shape, not of the drawn path -- so it is skipped entirely
       when ``waypoints_um`` is supplied (#634); see below.
    2. **Guard/collector-ring check** (#199 case 2): a block reporting a
       guard or collector ring (any ``TAP_*``/``COLL_*`` port -- see
       :func:`_block_has_ring_taps`) has that ring drawn *around* its other
       ports; a route touching one of those non-tap ports necessarily
       crosses the ring's own metal loop on its way in or out, merging the
       net with the ring's own tap net.
    3. **Self-net pad-crossing check** (#433): a same-block net's backbone is
       always inside its own block's bbox, so the obstacle-overlap check
       below exempts it entirely -- but nothing else was checking whether
       that backbone runs straight over one of the block's *other* pads
       (e.g. bussing a bjt_array's emitters together necessarily crosses the
       base pads sitting between them). Every other port on the same block
       that shares the route's own ``route_layer`` is treated as a square
       pad footprint (side length its reported ``width_um``); a backbone
       overlapping that footprint's interior is rejected rather than drawn
       as a silent short. A same-direction degenerate-jog variant of this
       same check (#453's conservative fallback) additionally rejects any
       other same-facing port on the same row/column between the two pins
       outright, regardless of its reported ``width_um`` -- see the
       ``conservative_same_dir`` block below. That degenerate-jog fallback is
       likewise a property of the fixed shape rather than of the drawn path,
       so it too is skipped when ``waypoints_um`` is supplied (#634); the
       pad-footprint test itself still runs against the drawn ``points``.
    4. **Self-net drawn-metal check** (#453/#469): check 3's reported-
       ``width_um`` pad model, even with its same-direction fallback, is
       still built from *reported* geometry and can miss a short whenever
       the pins sit on different rows/columns or the route is wide enough to
       reach a pad the modelled square under-states. When ``block_geometry``
       supplies the block's actual drawn shapes on ``route_layer``
       (:func:`read_block_layer_geometry`), the route's own drawn metal
       (:func:`_drawn_route_region`, the same ``kdb.Path`` the composed cell
       gets) is intersected with them: overlapping any merged shape other
       than the two the endpoints sit on (positive area only -- an edge
       touch is a ``klt drc`` spacing question, not a short) is a silent
       short. This check is independent of, and composes with, check 3 --
       it catches cases (different rows, wider routes) check 3's reported-
       geometry model cannot see.

       **Own-block escape check** (issue #1527): checks 3/4 above only ever
       ran for a same-block self-net (``pin_a``/``pin_b`` share a block) --
       an inter-block leg (the far more common case) skipped both entirely,
       even though it still draws an approach stub inside *each* endpoint's
       own block on its way out, exactly the same as a self-net's backbone
       does. Nothing checked that stub against the block's other drawn
       geometry: check 5 below only ever modelled a pin's own block by its
       bbox, with an unavoidable margin (:func:`_port_edge_margin_um`)
       exempting the approach stub from being flagged at all, so a
       coordinate-tapped ``blocks[].cell`` port whose escape direction
       happened to run across a *different* net already drawn inside that
       same block composed ``routed: true`` and DRC-clean while silently
       shorting the two nets together. When ``block_geometry`` is supplied,
       :func:`_self_net_drawn_short` is now also run once per endpoint whose
       own block is a ``blocks[].cell`` block (independently of
       ``same_block_self_net`` -- scoped to ``cell`` blocks only, not every
       ``generator_report`` block too, since a generator can legitimately
       draw real, unreported geometry this check cannot tell apart from an
       obstacle, e.g. ``mos_array``'s own dummy matching columns, already
       excluded from the netlist by ``klt extract``'s own dummy-suppression
       convention), comparing that endpoint's drawn leg against its *own*
       block's other drawn shapes on ``route_layer`` -- excluding only the
       shape its own port lands on (not the far endpoint's port, which is
       not on this block at all).
    5. **Obstacle-overlap check** (#199 case 1): after each port's own
       unavoidable "sit set back from my own block's edge" margin is
       excluded (:func:`_port_edge_margin_um`), the drawn backbone must not
       cross the *interior* of any block's bbox -- including the two pins'
       own blocks (a same-facing port pair forces the backbone to plow
       through the destination block's full width to reach a port on its far
       side, e.g. crossing that block's own opposite-facing pin) and any
       third block's bbox the straight-line/single-jog backbone happens to
       cross in a longer row. Every bbox tested here is inflated by
       ``width_um / 2`` on every side first (#999), so a centerline that
       merely runs close enough alongside a block's edge -- closer than half
       the route's own drawn width -- is caught too, not just one that
       crosses the zero-width centerline through the block's true interior;
       each own-pin's allowance is bumped by the same ``width_um / 2`` so a
       normal approach into that pin's own block is not penalized by the
       inflation. A block that draws no shapes at all on ``effective_route_
       layer`` is exempted from this check entirely (#1656): a route on
       metal2 cannot short to a block whose own drawn geometry has no
       metal2 shapes, so a bare bbox crossing there is a false positive,
       not a real obstacle -- see ``block_geometry_for``/
       ``cross_block_geometry_for`` below. A block that *does* draw there is
       exempted too whenever this leg's own drawn metal
       (:func:`_drawn_route_region`) does not actually overlap any of those
       shapes (#1681): a bbox is a solid proxy for a shape that need not be
       solid, so a route crossing only the *hollow interior* of a
       ``guard_ring``-style enclosure block -- the natural shape for a
       composition that places a region's ring and the content it encloses
       as two separate ``blocks[]`` entries -- was being rejected for
       "crossing" metal it never comes near. Both exemptions remove false
       positives only: a leg that does reach a block's real drawn shapes is
       still measured, and rejected or detoured, exactly as before. A
       crossing this check finds against a block neither exemption clears is
       not automatically
       fatal (#1167): when the *only* blocks crossed are ones neither pin
       sits on, the net is retried around them first -- see "Bounded detour
       search" below -- and reported unroutable only if no alternate lane
       clears them either.
    6. **Via-drop resolution** (#454; multi-hop ladder since #1567): when
       ``route_layer`` and a pin's own reported layer differ,
       :func:`_resolve_via_drop_layer` looks up whether ``extraction_deck``
       connects the two -- via however many via hops the ``deck.metals``
       stack puts between them (e.g. ``routing.layer_role: "metal2"``
       backbone reaching a ``"metal"``-role li1 pad via sky130's ``mcon``, or
       ``routing.layer_role: "metal3"`` reaching that same li1 pad via a
       two-hop ``mcon`` + ``via`` ladder). A pin whose own layer *is*
       ``route_layer`` needs no drop; a pin on an unrelated role a
       route-layer shape already covers (e.g. a guard ring's active/tap port)
       is left exactly as before #454 (drawn directly on ``route_layer``, no
       via). A pin whose layer is a *different* ``deck.metals`` level than
       ``route_layer`` but the deck declares no via for one of the hops
       between them, **or** a pin on the deck's bare ``poly`` layer (a gate
       drawn without ``params.gate_contact``, issue #492), is rejected here
       -- reported unroutable rather than drawing a disconnected short or an
       uncontacted stub.

    Checks 1-5 report the net unroutable (spike section 2,
    ``unrouted_nets[]``) rather than silently drawing a short; check 6 does
    the same for a route it cannot connect end to end. ``route_layer`` (the
    ``(layer, datatype)`` pair ``routing.layer_role`` resolved to, see
    :func:`_resolve_route_layer`) is optional only for callers that predate
    #433/don't care about check 3 (e.g. direct unit tests) -- ``compose()``
    always passes it when ``connectivity[]`` is non-empty, since that's the
    only time a route is actually drawn. ``block_geometry`` (block ``id`` ->
    :func:`read_block_layer_geometry` result, ``None`` for a block that draws
    nothing on the route layer) is likewise optional and likewise always
    supplied by ``compose()`` -- for both endpoint blocks of every leg since
    issue #1527, not only a same-block self-net's shared one; without it
    check 4 and the own-block escape check above are both skipped and only
    check 3's weaker reported-geometry pad model applies (same-block self-net
    legs only -- an inter-block leg has no check 3 fallback, since that
    check's reported-``width_um`` pad model only ever compared against
    *other ports on the same block*, which an inter-block leg's own block
    need not report at all). ``extraction_deck`` is likewise optional and
    only consulted
    (check 6) when both it and ``route_layer`` are given -- omitting it (e.g.
    a pre-#454 caller) draws every pin directly on ``route_layer`` with no
    via-drop, exactly as before that issue. ``waypoints_um`` (#634) is
    likewise optional -- ``None``/omitted preserves today's fixed-shape
    backbone exactly (:func:`manhattan_backbone`'s default one-jog/corner
    shape); when given, it is threaded straight into
    :func:`manhattan_backbone`, whose own docstring covers how the path is
    built from it. Every check that measures the *drawn path* runs against
    whatever ``points`` results either way -- a waypoint-supplied path is not
    exempt from the obstacle-overlap check (5), the ring-opening check
    (#434), the pad-footprint half of check 3, check 4, or check 6 just
    because the caller supplied it. What supplying waypoints *does* switch
    off is the handful of heuristics that never look at ``points`` at all
    because they are predictions about the fixed one-jog shape: the
    channel-width check (1), check 3's ``conservative_same_dir``
    degenerate-jog fallback, and the #461 recessed-stub lift. Each of those
    reasons from the raw port positions and block bboxes on the assumption
    that ``manhattan_backbone`` will draw its default shape; once the caller
    supplies a path, that shape is not drawn, so the prediction describes a
    route that does not exist and would reject perfectly good route-arounds
    (the tightly-packed-row case -- inter-block gap narrower than
    ``width_um`` -- being exactly the one waypoints exist to solve). The
    closed guard/collector-ring check (2) is *not* in that group and still
    runs: a closed ring encloses its block completely, so no choice of
    waypoints can reach an interior port without crossing it.

    ``cross_block_route_layer``/``cross_block_geometry`` (issue #1168) name an
    optional *second*, higher metal ``(layer, datatype)`` pair (resolved by
    :func:`_resolve_cross_block_route_layer` from ``routing.
    cross_block_layer_role``) and its own drawn-geometry cache
    (:func:`read_block_layer_geometry` result per block, mirroring
    ``block_geometry`` but for the cross layer) -- both optional and both
    ``None`` unless the caller configured a cross-block bus layer. When
    checks 3/4 above reject a leg because it crosses another same-block port
    or drawn pad *on the primary ``route_layer``*, and a cross-block layer is
    configured, the identical pair of checks is retried against
    ``cross_block_route_layer``/``cross_block_geometry`` instead of failing
    outright; a leg that resolves on the cross layer draws there for its
    *entire* length (not just the crossing segment), with checks 5/6 and the
    stub-widen pass all re-run against that same effective layer, including
    an endpoint via-drop (check 6) wherever a pin's own reported layer is the
    primary ``route_layer`` rather than the cross layer itself -- exactly the
    same via-drop resolution :func:`_resolve_via_drop_layer` already performs
    for a caller-selected ``routing.layer_role: "metal2"``, just decided
    per-leg instead of for the whole composition. A leg that never trips
    checks 3/4 on the primary layer, or that trips them with no cross-block
    layer configured, is entirely unaffected -- this is an additive fallback,
    not a change to any existing single-layer behaviour.

    ``cross_block_width_um`` (issue #1620) is the width a leg draws at once
    it actually falls back to ``cross_block_route_layer`` -- checks 3/4's
    retry above, the obstacle-overlap inflation (check 1 below), the
    detour-lane clearance, and the stub-widen pass are all re-evaluated
    against this width instead of ``width_um`` once the cross layer wins,
    so a wider cross-block plane's own footprint is judged at the width it
    is actually drawn with, not the (generally narrower) primary plane's
    width. ``None`` (the default) reuses ``width_um`` for a cross-layer leg
    too, reproducing this function's pre-#1620 behaviour exactly. The
    returned result's own ``width_um`` field (see below) reports whichever
    width actually won.

    ``block_geometry_for``/``cross_block_geometry_for`` (issue #1656) are the
    same lazy per-block geometry caches ``compose()`` builds around
    :func:`read_block_layer_geometry` for ``block_geometry``/
    ``cross_block_geometry`` above (a callable that, given a ``block_id``,
    returns the shared ``{block_id: geometry-or-None}`` cache dict with that
    entry populated) -- but reachable here for *any* block in
    ``placed_bboxes_um``, not only the leg's own two endpoint blocks. The
    obstacle-overlap check (5) below uses them to test whether a block it is
    about to reject a crossing against actually draws anything on
    ``effective_route_layer``: a block whose own geometry has nothing there
    cannot be shorted to by this route, no matter how much its *bbox*
    overlaps the drawn path, so it is exempted from that check entirely.
    Since issue #1681 they are also used for the stronger test the same
    cached ``region`` makes possible: a block that *does* draw on that layer
    is exempted too when this leg's own drawn metal does not overlap any of
    its shapes, so an enclosure block (a ``guard_ring``) whose hollow
    interior the route merely passes through stops reading as an obstacle.
    Both are optional and ``None`` by default; when omitted (e.g. a direct
    unit-test caller, or ``route_layer is None``), the obstacle check falls
    back to its pre-#1656 bbox-only behaviour and every placed block stays a
    potential obstacle regardless of what it draws.

    **Bounded detour search (#1167).** Rejecting every backbone that crosses
    a third block's bbox means only *immediately adjacent* blocks in a row
    can ever be wired -- a real block's netlist is not a Hamiltonian path
    over its devices, so most of its nets never had a chance (issue #1164
    measured 0/8, 0/9 and 0/9 nets routed across three real gf180mcu
    blocks). So when check 5 rejects a path **solely** because of blocks
    neither pin sits on, this function retries the same pin pair around them
    before giving up: :func:`_detour_lane_waypoints_um` proposes up to
    ``_MAX_DETOUR_LANES`` alternate *lanes* (a straight run over/under, or
    left/right of, every block in the way, ordered shortest-detour first),
    and each is routed by a recursive call to this same function with the
    lane as ``waypoints_um``. That is the whole mechanism -- a detour is
    just a route this function generated for itself instead of the caller
    generating it, so **every check above applies to it unchanged**; nothing
    is waived to make a detour fit, and a lane that would cross a block's
    drawn geometry, a ring, or another pad is rejected exactly as a
    caller-supplied path would be. The search is bounded on both sides: the
    retry supplies ``waypoints_um``, and a waypoint-supplied path is never
    itself detoured (one level of recursion, at most two extra attempts per
    net); and the lane is placed clear of *every* block it spans, so two
    obstacles between the pins cost exactly one lane, not two nested
    detours. What is deliberately **not** detoured is a backbone crossing
    one of its own two pins' blocks (the same-facing port pair): that is a
    statement about which way the ports face, and its remedy stays the
    caller-supplied ``waypoints_um`` of #634. When no lane works, the
    reported ``reason`` is check 5's original wording plus a note that the
    detour was tried, so a caller can tell "there was no way around" from
    "no attempt was made".

    **Same-block cross-layer conflict retry (issue #1393).** ``leg_conflict``
    is an optional callable with the same signature as
    :func:`route_bundle`'s own ``leg_conflict`` parameter (``(points_um,
    via_drops, stub_widen, route_layer) -> str | None``) -- ``compose()``
    threads its route-vs-route collision check (#1057/#1386) through
    unchanged. It has no effect unless **all** of the following hold, so
    every other leg is entirely unaffected -- a same-block self-net that
    resolved on the *primary* ``route_layer``, an inter-block net, and a
    caller-supplied ``waypoints_um`` path all behave exactly as before this
    issue:

    - this leg is a same-block self-net (``pin_a``/``pin_b`` share a block);
    - this call is the *fixed*-shape attempt, not itself a caller- or
      detour-supplied path (``waypoints_um`` is ``None`` -- bounds the
      search to one level of recursion, mirroring #1167);
    - the leg actually resolved on ``cross_block_route_layer`` -- i.e. the
      checks 3/4 same-layer-short retry above fired, so this leg exists
      *because of* ``routing.cross_block_layer_role`` in the first place.

    When all three hold and ``leg_conflict`` rejects the fixed-shape path --
    the case #1168's own fallback has no visibility into: a *different*
    same-block self-net that also needed the cross layer and already claimed
    the identical lane (:func:`route_bundle`'s route-vs-route check, #1057,
    correctly flags the resulting literal overlap or spacing violation) --
    this function generalises the same *pattern* of bounded detour search
    used just above -- but not the same lane shape, which does not work for
    this case (see :func:`_self_net_cross_layer_lane_waypoints_um`'s own
    docstring: a straight up/down lane at either port's native column can run
    straight through a *different* same-block self-net's own approach stub,
    since a same-block self-net's ports can share coordinates with another
    same-block self-net's ports -- verified as the actual failure mode this
    issue's own reproduction hits). Instead,
    :func:`_self_net_cross_layer_lane_waypoints_um` proposes up to
    ``_MAX_SAME_BLOCK_CROSS_LAYER_LANES`` single-corner waypoints that route
    fully *around the outside* of the block both pins sit on (there is no
    third-party obstacle bbox here, unlike the #1167 case -- the collision is
    with another net's *drawn metal*, not a placed block), and each is routed
    by a recursive call to this same function with the corner as its sole
    ``waypoints_um`` entry (without threading ``leg_conflict`` further --
    the one-level recursion bound is enforced the same way #1167's own
    retry is, by the ``waypoints_um is None`` gate above); ``leg_conflict``
    is then re-checked against *that* lane's actual drawn points before it is
    accepted, so a lane that clears the block but still collides with the
    other net is rejected and the next lane is tried. Unlike #1167's own
    scope guard (just above, which deliberately excludes a pin's own block
    from that retry), this is a distinct case with its own gating -- not a
    relaxation of that guard: #1167 retries around a *third*, unrelated
    block's bbox; this retries around an *already-accepted leg of a
    different self-net on the same block*. When no lane clears the conflict
    either, the reported ``reason`` is ``leg_conflict``'s own wording plus a
    note that the detour was tried.

    A retried lane is **not** pinned to ``cross_block_route_layer``: the
    recursive call re-runs the *whole* layer resolution above against the
    lane's own drawn path, so a lane that loops clear of the block no longer
    crosses the pads that forced the fallback in the first place and
    naturally resolves back onto the primary ``route_layer``. That is the
    intended outcome, not an escape -- it is the same checks reaching a
    different verdict for a genuinely different path, and it leaves the
    cross layer free for the *first* self-net that still needs it. What the
    gating above guarantees is only that this retry is *reachable* solely
    from a leg that needed the cross layer; it makes no promise about which
    layer the accepted lane ends up on.

    Returns ``{"routed": bool, "route_length_um": float | None,
    "points_um": list | None, "via_drops": list, "stub_widen": list,
    "route_layer": tuple[int, int] | None, "width_um": float, "reason":
    str | None}`` (a rejected leg omits ``width_um``, nothing having been
    drawn for it).
    ``via_drops`` is a list of ``{"x_um", "y_um", "via_layer",
    "landing_layers", "block_id"}`` entries (empty unless a drop was
    resolved; issue #1567: one entry per via *hop* -- a multi-level drop
    produces more than one entry at the identical ``(x_um, y_um)``, each with
    its own ``via_layer``/``landing_layers`` pair), consumed by
    :func:`_write_composed_gds` to draw each hop's via + landing pads.
    ``stub_widen`` (issue #496, see :func:`_endpoint_stub_widen_um`) is a list
    of ``{"x_um", "y_um", "direction_deg", "length_um", "width_um"}`` entries
    -- one per endpoint whose own reported pad is wider than ``width_um`` and
    faces north/south -- consumed by :func:`_write_composed_gds` to re-draw
    that endpoint's own first backbone segment at the pad's width, closing
    the sub-spacing gap a narrower stub would otherwise leave beside it.
    ``route_layer`` (issue #1168) is the *effective* drawing layer this
    result's geometry actually landed on -- ``route_layer`` (the parameter)
    unless the cross-block retry above fired, in which case it is
    ``cross_block_route_layer`` -- so a caller drawing multiple legs on
    different layers within one net (:func:`route_bundle`) or one composition
    (``compose()``) knows which layer each leg's ``points_um``/``via_drops``
    belong on. ``width_um`` (issue #1620) is, correspondingly, the *effective*
    drawn width -- ``width_um`` (the parameter) unless the cross-block retry
    fired, in which case it is ``cross_block_width_um`` (or ``width_um``
    again when that parameter was left ``None``) -- so the same caller knows
    which width to draw each leg's geometry at.
    """
    # `_endpoint_stub_widen_um`/`_self_net_cross_layer_lane_waypoints_um` are
    # looked up dynamically (rather than called as this module's own
    # same-name functions, defined above) so a caller that monkeypatches
    # `klayout_tools.gen_compose.<name>` (the test suite's own re-export,
    # preserved from before issue #1708's split) still takes effect here,
    # exactly as before the split.
    from .gen_compose import (
        _DIRECTION_VECTORS,
        _endpoint_stub_widen_um,
        _port_has_geometry,
        _self_net_cross_layer_lane_waypoints_um,
    )

    block_a = blocks[pin_a["block"]]
    block_b = blocks[pin_b["block"]]
    port_a = block_a["ports"].get(pin_a["port"])
    port_b = block_b["ports"].get(pin_b["port"])

    # A block with no reported ports[] (a hand-crafted generator_report) can't
    # supply a routable position -- treat as unroutable rather than crashing.
    if not isinstance(port_a, dict) or not isinstance(port_b, dict):
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": "one or both ports report no position (empty ports[])",
        }

    off_a = offsets_um[pin_a["block"]]
    off_b = offsets_um[pin_b["block"]]
    a = (
        float(port_a["x_um"]) + off_a["x"],
        float(port_a["y_um"]) + off_a["y"],
    )
    b = (
        float(port_b["x_um"]) + off_b["x"],
        float(port_b["y_um"]) + off_b["y"],
    )
    dir_a = int(port_a.get("direction_deg", 0)) % 360
    dir_b = int(port_b.get("direction_deg", 0)) % 360
    if dir_a not in _DIRECTION_VECTORS or dir_b not in _DIRECTION_VECTORS:
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": "a port reports a non-orthogonal direction_deg",
        }

    va = _DIRECTION_VECTORS[dir_a]
    vb = _DIRECTION_VECTORS[dir_b]
    stub_um = width_um

    # Guard/collector-ring check (#199 case 2): a block with a *closed* ring
    # drawn around it (any TAP_*/COLL_* port reported, no GAP_* opening)
    # cannot be reached at any other port without the route crossing the
    # ring's own metal loop -- on the way in for a destination pin, or on the
    # way out for a source pin, since the ring fully encloses the block either
    # way. Only meaningful for distinct blocks: a self-net's two ports already
    # both sit inside the same ring, so it draws no *additional* ring crossing.
    #
    # A block whose ring declares an opening (params.ring_gap_side, #434) is
    # not rejected here: it is collected instead, and the drawn backbone is
    # checked against the ring's own geometry below, once `points` exists --
    # the route is allowed only if it actually passes through that opening.
    ring_pins: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if pin_a["block"] != pin_b["block"]:
        for pin, block in ((pin_a, block_a), (pin_b, block_b)):
            if not _block_has_ring_taps(block) or pin["port"].startswith(
                _RING_TAP_PORT_PREFIXES
            ):
                continue
            if _ring_gap_ports(block):
                ring_pins.append((pin, block))
                continue
            return {
                "routed": False,
                "route_length_um": None,
                "points_um": None,
                "reason": (
                    f"block '{pin['block']}' has a closed guard/collector ring "
                    f"(reports a TAP_*/COLL_* port and no GAP_* opening) -- a "
                    f"route to its non-tap port '{pin['port']}' would cross the "
                    "ring's own metal loop and merge this net with the ring's "
                    "tap net; route to the ring's own tap port instead, "
                    "regenerate the block with a routing opening in the ring "
                    "(params.ring_gap_side/ring_gap_um), or regenerate it with "
                    "add_guard_ring/add_collector_ring: false"
                ),
            }

    # Routability heuristic: a jog perpendicular to the ports' facing axis has
    # to squeeze through the channel between the two blocks. When both ports
    # face x and their y differs (a vertical jog is required), the channel is
    # the horizontal gap; symmetric for both-y. Only meaningful for distinct
    # blocks (a self-net has no inter-block channel).
    #
    # Skipped entirely when the caller supplies waypoints_um (#634), for the
    # same reason the #461 stub lift below is: this heuristic is a statement
    # about the *fixed one-jog shape*, computed from the raw port positions
    # and the inter-block gap rather than from the path actually drawn. Once
    # waypoints are supplied that jog is never drawn at all -- manhattan_
    # backbone routes through the caller's own points -- so a narrow channel
    # the caller's path deliberately avoids (routing over the row, say) is
    # not a reason to reject the net. Tightly packed rows, where the gap is
    # narrower than the route width, are exactly the case a caller most needs
    # a route-around for, so leaving this check unconditional silently
    # defeated waypoints_um precisely where it is most useful. The path that
    # does get drawn is still fully checked below: the obstacle-overlap check
    # (#199 case 1) rejects any backbone that actually crosses a block's
    # interior, including through this same channel.
    if waypoints_um is None and pin_a["block"] != pin_b["block"]:
        bbox_a = placed_bboxes_um[pin_a["block"]]
        bbox_b = placed_bboxes_um[pin_b["block"]]
        if va[1] == 0 and vb[1] == 0 and abs(a[1] - b[1]) > 1e-9:
            gap = _block_gap_um(bbox_a, bbox_b, axis="x")
            if 0.0 <= gap < width_um:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"vertical jog needs a channel >= width {width_um}um "
                        f"but the gap between blocks is only {gap:.4g}um"
                    ),
                }
        elif va[0] == 0 and vb[0] == 0 and abs(a[0] - b[0]) > 1e-9:
            gap = _block_gap_um(bbox_a, bbox_b, axis="y")
            if 0.0 <= gap < width_um:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"horizontal jog needs a channel >= width {width_um}um "
                        f"but the gap between blocks is only {gap:.4g}um"
                    ),
                }

    # Recessed same-direction vertical ports (#461): a gate landing pad now
    # sits *above* its own gate port, so a port facing +y no longer sits on
    # its block's top edge -- it is inset by the pad's own height. Two such
    # ports bussed across a row connect through a horizontal jog; left at the
    # default one-width stub that jog runs at pad height, straight through
    # both blocks' poly. Lift each stub so the jog clears both blocks' tops
    # (or bottoms, for -y). Scoped to ring-free blocks: a ringed block routes
    # through its declared gap at port level, never over its own top. For the
    # pre-#461 geometry (gate port exactly on the block edge) this reduces to
    # the default stub, so no existing route changes. Skipped entirely when
    # the caller supplies waypoints_um (#634): the automatic jog this lift
    # exists to clear is not drawn at all in that case (manhattan_backbone
    # routes through the caller's own points instead), so stretching the
    # stub here would just move the endpoint the caller's waypoints are
    # relative to, out from under them.
    stub_a_um = stub_b_um = stub_um
    if (
        waypoints_um is None
        and pin_a["block"] != pin_b["block"]
        and dir_a == dir_b
        and va[0] == 0
        and not _block_has_ring_taps(block_a)
        and not _block_has_ring_taps(block_b)
    ):
        bbox_a = placed_bboxes_um[pin_a["block"]]
        bbox_b = placed_bboxes_um[pin_b["block"]]
        if dir_a == 90:
            jog_y = max(bbox_a["y1"], bbox_b["y1"]) + stub_um
            stub_a_um = max(stub_um, jog_y - a[1])
            stub_b_um = max(stub_um, jog_y - b[1])
        else:  # dir 270
            jog_y = min(bbox_a["y0"], bbox_b["y0"]) - stub_um
            stub_a_um = max(stub_um, a[1] - jog_y)
            stub_b_um = max(stub_um, b[1] - jog_y)

    points = manhattan_backbone(
        a,
        dir_a,
        b,
        dir_b,
        stub_um,
        stub_a_um=stub_a_um,
        stub_b_um=stub_b_um,
        waypoints=waypoints_um,
    )
    margin_eps_um = 1e-6

    # Ring-opening check (#434): for every pin on a block whose ring declares
    # an opening, the drawn backbone must reach that pin *through* the opening
    # -- every other crossing of the ring's metal is the same short the
    # closed-ring check above rejects.
    for pin, block in ring_pins:
        conflict = _ring_gap_route_conflict(
            points,
            block,
            offsets_um[pin["block"]],
            placed_bboxes_um[pin["block"]],
            width_um,
        )
        if conflict is not None:
            return {
                "routed": False,
                "route_length_um": None,
                "points_um": None,
                "reason": conflict,
            }

    # Obstacle-overlap check (#199 case 1): sum how much of the drawn backbone
    # lies inside each block's bbox *interior* (a boundary touch doesn't
    # count -- see _segment_bbox_interior_overlap_um), then compare each
    # block's total against how much crossing is unavoidable there. A pin's
    # own block always gets an allowance equal to that port's own edge margin
    # (_port_edge_margin_um) -- crossing exactly that much is just "the route
    # reached the pin", not a fault. Crossing *more* than that (own blocks) or
    # *any* amount (every other block) means the backbone plowed through a
    # block it shouldn't have -- e.g. a same-facing port pair forcing the
    # route through the destination's full width to reach a pin on its far
    # side, crossing that block's own other pins on the way.
    #
    # width_um-aware inflation (#999): the check above treats the backbone as
    # a zero-width centerline, but the wire actually drawn is `width_um` wide
    # -- it extends `width_um / 2` past the centerline on every side, the
    # same Minkowski-sum inflation the self-net pad-crossing check (#433,
    # below) and the ring-opening check (`_ring_gap_route_conflict`) already
    # apply. A centerline that clears a block's bbox edge by less than
    # `width_um / 2` therefore still draws metal on top of that block even
    # though the old zero-width test reports no crossing at all -- most
    # commonly a same-facing port pair's connecting jog running parallel to,
    # and just outside, its own origin block's edge. Every bbox this check
    # tests against is inflated by `width_um / 2` on every side before the
    # interior-overlap test runs (see `obstacle_bboxes_um` below); each own-
    # pin's allowance is bumped by the same `width_um / 2` to compensate --
    # without it, the inflated bbox would eat into the own-pin's normal
    # approach margin and reject routes that were always fine (the approach
    # stub's own dip into its own block, `_port_edge_margin_um`, is a
    # statement about *insertion depth*, not about the wire's width).
    own_a, own_b = pin_a["block"], pin_b["block"]
    same_block_self_net = own_a == own_b
    # obstacle_half_um/obstacle_bboxes_um/allowances_um (#999) are computed
    # further below, *after* the same-layer-short retry decides
    # effective_route_layer/effective_width_um (#1620) -- they must be sized
    # from whichever width the leg actually ends up drawn at, not
    # unconditionally from the primary plane's own width_um, or a wider
    # cross-block leg's real footprint would be checked against too small an
    # inflation.

    # Self-net pad-crossing check (#433) and self-net drawn-metal check
    # (#453/#469), factored into a closure parameterised on which layer (and,
    # since issue #1620, which width) is under test: issue #1168 retries
    # these two checks (and only these two -- the ones the checks' own
    # rejection text below points a caller at a "layer_role with a metal2/via
    # stack") against an optional ``cross_block_route_layer`` when the
    # primary ``route_layer`` rejects, so a same-block bus that must cross an
    # intermediate pad on the same drawing layer can escape to the configured
    # second metal instead of failing outright. See the retry drive code just
    # below this def.
    def _same_layer_short_reason(
        effective_route_layer: tuple[int, int] | None,
        effective_block_geometry: dict[str, dict[str, Any] | None] | None,
        effective_width_um: float,
    ) -> str | None:
        # Self-net pad-crossing check (#433): the whole-block bbox check below
        # skips a self-net's own block entirely (a same-block net's backbone
        # is, by construction, always inside its own block's bbox -- that
        # check would otherwise reject every self-net). But skipping it also
        # means nothing else was checking whether the backbone runs straight
        # over one of that block's *other* pads -- exactly what happens
        # bussing an array's unit devices (e.g. chaining a bjt_array's
        # emitters): a same-layer pad in the backbone's path shorts to it,
        # silently, since a self-net was never compared against the block's
        # own ports[] at all. Approximate each other port on this block as a
        # square pad footprint (side length its own reported ``width_um``,
        # centered on its position), *inflated* by the route's own trace
        # half-width on every side -- ``_segment_bbox_interior_overlap_um``
        # treats a backbone segment as a zero-width centerline, but the wire
        # actually drawn is ``width_um`` wide, so a centerline that merely
        # passes within ``width_um / 2`` of a pad's edge still draws metal on
        # top of it. This Minkowski-sum inflation is what makes the check
        # actually catch pads much narrower than the route (e.g. a bjt_array
        # base contact's reported ``width_um`` alone is too small to reach a
        # jog stubbed out by the route's own width -- only their sum is).
        # Reject the route if the backbone overlaps this inflated footprint's
        # interior -- mirroring the bbox-interior accounting above, just
        # against a pad instead of a whole block.
        #
        # Same-direction degenerate-jog check (#453): the inflated-footprint
        # test above models each other port as a square of side its
        # *reported* ``width_um``. For an array unit's own pad that badly
        # *under*-estimates the real drawn metal in the port's facing
        # direction -- e.g. a bjt_array base-tie tap draws li1 metal several
        # times taller than its reported ``width_um`` (the reported width is
        # roughly the contact size, not the pad extent). When both pins face
        # the *same* direction and share the coordinate along that facing
        # axis (same row for a vertical facing, same column for a horizontal
        # one), ``manhattan_backbone()`` collapses to a single straight jog
        # lifted just one stub width (``width_um``) to the ports' outward
        # side -- so a route wide enough that the jog clears the under-sized
        # reported square still plows straight through the real pad of any
        # intervening same-facing port. That is exactly the reproduction in
        # #453 (bussing two same-row north-facing bjt_array emitters across
        # the intervening unit's north-facing base-tie pad composed
        # ``routed: true`` and DRC-clean while extraction showed the whole
        # array's shared base node absorbed into the emitter net). Treat any
        # other same-layer port that faces the same direction and sits
        # strictly between the two pins along the perpendicular axis (on the
        # same row/column) as crossed: its pad opens toward the jog, so
        # bussing across it draws a silent short regardless of how small its
        # reported ``width_um`` is.
        if same_block_self_net:
            own_ports = blocks[own_a].get("ports") or {}
            own_offset = offsets_um[own_a]
            route_half_um = effective_width_um / 2.0
            skip_port_names = {pin_a["port"], pin_b["port"]}
            facing_vertical = va[1] != 0
            # A degenerate single-jog backbone only forms when both pins face
            # the same direction *and* share their facing-axis coordinate
            # (same row for a vertical facing, same column for a horizontal
            # one).
            same_line = (
                abs(a[1] - b[1]) < 1e-9 if facing_vertical else abs(a[0] - b[0]) < 1e-9
            )
            # Like the channel-width heuristic and the #461 stub lift above,
            # this conservative fallback is a statement about the
            # *degenerate single-jog shape* -- it rejects on port positions
            # alone, without consulting `points`, precisely because that
            # fixed shape is known to run straight along the ports' own
            # row/column. Supplying waypoints_um (#634) replaces that shape
            # with the caller's own path, so the premise no longer holds and
            # the fallback would reject route-arounds that never go near the
            # intervening pad. The waypoint-drawn path is still checked
            # against the same pads by the inflated-footprint test below and
            # by the drawn-metal check (#453/#469) -- both of which measure
            # the actual `points`, so a caller whose waypoints really do
            # cross a pad is still rejected.
            conservative_same_dir = (
                waypoints_um is None and dir_a == dir_b and same_line
            )
            for other_name, other_port in own_ports.items():
                if other_name in skip_port_names or not _port_has_geometry(other_port):
                    continue
                other_layer = (
                    other_port["layer"]["layer"],
                    other_port["layer"]["datatype"],
                )
                if (
                    effective_route_layer is not None
                    and other_layer != effective_route_layer
                ):
                    continue  # a pad on a different physical layer can't short
                px = float(other_port["x_um"]) + own_offset["x"]
                py = float(other_port["y_um"]) + own_offset["y"]

                # #453: an intervening same-facing pad on the jog's own
                # row/column is crossed no matter how small its reported
                # footprint is.
                other_dir = int(other_port.get("direction_deg", 0)) % 360
                if conservative_same_dir and other_dir == dir_a:
                    if facing_vertical:
                        between = (
                            min(a[0], b[0]) < px < max(a[0], b[0])
                            and abs(py - a[1]) < 1e-9
                        )
                    else:
                        between = (
                            min(a[1], b[1]) < py < max(a[1], b[1])
                            and abs(px - a[0]) < 1e-9
                        )
                    if between:
                        axis = "row" if facing_vertical else "column"
                        return (
                            f"self-net between two same-facing ports on the "
                            f"same {axis} jogs directly over block "
                            f"'{own_a}''s own port '{other_name}' (same "
                            "facing direction, same drawing layer) -- "
                            "bussing this net across the block would draw a "
                            "silent short to that pad's real drawn metal, "
                            "which extends past its reported width_um "
                            "footprint in its facing direction; route to a "
                            "layer_role with a metal2/via stack instead "
                            "(or configure routing.cross_block_layer_role, "
                            "issue #1168), or wire this net externally"
                        )

                pad_w = other_port.get("width_um")
                if (
                    not isinstance(pad_w, (int, float))
                    or isinstance(pad_w, bool)
                    or (pad_w <= 0)
                ):
                    # no reported pad size -- fall back to trace width
                    pad_w = effective_width_um
                half = float(pad_w) / 2.0 + route_half_um
                pad_bbox_um = {
                    "x0": px - half,
                    "y0": py - half,
                    "x1": px + half,
                    "y1": py + half,
                }
                crossed_um = sum(
                    _segment_bbox_interior_overlap_um(seg_p0, seg_p1, pad_bbox_um)
                    for seg_p0, seg_p1 in zip(points, points[1:], strict=False)
                )
                if crossed_um > margin_eps_um:
                    return (
                        f"self-net backbone crosses {crossed_um:.4g}um "
                        f"through block '{own_a}''s own port '{other_name}' "
                        "on the same drawing layer -- bussing this net "
                        "across the block would draw a silent short to that "
                        "pad; route to a layer_role with a metal2/via stack "
                        "instead (or configure "
                        "routing.cross_block_layer_role, issue #1168), or "
                        "wire this net externally"
                    )

        # Self-net drawn-metal check (#453/#469): check 3 above models each
        # other port as a square built from its *reported* ``width_um``,
        # which is a port's contact/access size -- not the extent of the pad
        # metal drawn around it. A bjt_array base tie, for instance, reports
        # ``width_um: 0.22`` (``CONTACT_SIZE_UM``) for a pad whose drawn
        # local metal is 0.42um x 0.68um. So the modelled square (and even
        # its same-direction same-row/column fallback, which only fires for a
        # degenerate single-jog backbone) systematically under-states the
        # real obstacle and misses any short outside that narrow shape --
        # notably a same-facing pair on *different* rows/columns, or a route
        # wide enough to reach an adjacent row's pad. Compare the route's
        # *drawn* metal against the block's own *drawn* shapes on the route
        # layer instead: every merged shape except the two the endpoints land
        # on is an obstacle, and overlapping one (positive area -- an edge
        # touch is a spacing question for `klt drc`, not a short) is the same
        # silent short, just measured against geometry the reported port
        # model cannot see. This is independent of, and composes with, check
        # 3: either can catch a case the other misses.
        if same_block_self_net and effective_block_geometry is not None:
            geometry = effective_block_geometry.get(own_a)
            if geometry is not None:
                drawn = _self_net_drawn_short(
                    points,
                    geometry,
                    a,
                    b,
                    effective_width_um,
                    blocks[own_a].get("ports") or {},
                    offsets_um[own_a],
                )
                if drawn is not None:
                    overlap_um2, crossed_names = drawn
                    if not crossed_names:
                        where = "drawn geometry (no port of its own sits on it)"
                    else:
                        noun = "port" if len(crossed_names) == 1 else "ports"
                        where = f"{noun} " + ", ".join(f"'{n}'" for n in crossed_names)
                    return (
                        f"self-net's drawn {effective_width_um}um metal overlaps "
                        f"{overlap_um2:.4g}um^2 of block '{own_a}''s own "
                        f"drawn pad metal on the route layer ({where}) -- "
                        "bussing this net across the block would draw a "
                        "silent short to that pad (its drawn metal is "
                        "larger than the contact size its port reports); "
                        "route to a layer_role with a metal2/via stack "
                        "instead (or configure "
                        "routing.cross_block_layer_role, issue #1168), or "
                        "wire this net externally"
                    )

        # Own-block escape check (issue #1527): the two checks above only
        # ever ran for a same-block self-net -- a leg whose two pins sit on
        # *different* blocks skipped both entirely. But such a leg still
        # draws an approach stub inside each endpoint's own block on its way
        # out (the whole-block bbox check further below deliberately exempts
        # exactly that stub, via each own-pin's `_port_edge_margin_um`
        # allowance, so a normal approach is never rejected for "crossing my
        # own block") -- and nothing checked whether that stub crosses a
        # *different* net already drawn inside that same block. That is
        # precisely the gap a coordinate-tapped `blocks[].cell` port opens:
        # the tap sits on one of the block's own internal wires, so the one
        # region a leg is guaranteed to cross is the one region no obstacle
        # model covered.
        #
        # Scoped to a `blocks[].cell` endpoint only (`block.get("source") ==
        # "cell"`), deliberately *not* every block. A `cell` block's every
        # `ports[]` entry is hand-declared by the caller directly onto the
        # stream's own geometry (`_parse_cell_block`) -- so, for that block
        # kind, "every merged shape my own port does not land on is a
        # genuinely different net" is a sound assumption. It is not sound for
        # a `generator_report` block: e.g. `mos_array`'s own `dummy` columns
        # (added by default) draw real, unreported metal pads flanking the
        # array purely for layout matching, which `klt extract`'s own
        # dummy-suppression convention (#295/#462) already excludes from the
        # netlist -- this check cannot tell that apart from an actual
        # obstacle, and a first attempt at this issue that ran unconditionally
        # rejected several previously-`routed: true` fixtures that route
        # straight over such dummy geometry (verified against this repo's own
        # `mos_array`/`bjt_array` composition tests) for a "short" `klt
        # extract` would never actually see. A generator that reports
        # everything it draws as either a port or a suppressed dummy has no
        # such gap to begin with, so it is left to the coarser whole-block
        # bbox check (#199) below, unchanged. Compare each endpoint's own
        # drawn leg against its *own* block's other drawn shapes on the route
        # layer, excluding only the shape that endpoint's own port lands on
        # -- unlike the same-block check above, the *other* endpoint is not
        # on this block at all, so nothing else needs excluding here.
        if not same_block_self_net and effective_block_geometry is not None:
            for own_id, pin, point, block in (
                (own_a, pin_a, a, block_a),
                (own_b, pin_b, b, block_b),
            ):
                if block.get("source") != "cell":
                    continue
                geometry = effective_block_geometry.get(own_id)
                if geometry is None:
                    continue
                drawn = _self_net_drawn_short(
                    points,
                    geometry,
                    point,
                    point,
                    effective_width_um,
                    block.get("ports") or {},
                    offsets_um[own_id],
                )
                if drawn is not None:
                    overlap_um2, crossed_names = drawn
                    if not crossed_names:
                        where = "drawn geometry (no port of its own sits on it)"
                    else:
                        noun = "port" if len(crossed_names) == 1 else "ports"
                        where = f"{noun} " + ", ".join(f"'{n}'" for n in crossed_names)
                    return (
                        f"leg's drawn {effective_width_um}um metal overlaps "
                        f"{overlap_um2:.4g}um^2 of block '{own_id}''s own "
                        f"drawn geometry on the route layer ({where}) near "
                        f"its own port '{pin['port']}' -- the escape from "
                        f"this port would draw a silent short to another net "
                        f"already present inside block '{own_id}' (issue "
                        "#1527); tap a different point on the intended net, "
                        "supply waypoints_um that route the approach clear of "
                        "the obstruction, or route to a layer_role with a "
                        "metal2/via stack instead (or configure "
                        "routing.cross_block_layer_role, issue #1168)"
                    )
        return None

    # Drive the retry (#1168): try the primary route_layer first -- the
    # overwhelmingly common case (no configured cross-block layer, or a leg
    # that never crosses another same-layer pad) resolves here with no
    # behaviour change from before this issue. Only when the primary layer's
    # checks object AND a cross_block_route_layer is configured is the
    # *identical* pair of checks retried against that second layer; if the
    # crossed pad sits on the primary layer (the overwhelmingly common case),
    # the `effective_route_layer != other_layer` guard inside the closure
    # above naturally exempts it on the retry, since a pad on `route_layer`
    # cannot short a backbone now drawn on `cross_block_route_layer`. Any
    # *other* obstacle that also happens to sit on the cross layer is still
    # caught by the same checks, run again in full -- this is not a bypass,
    # it is the identical short-detection logic evaluated against a different
    # drawing layer. Everything downstream (obstacle-overlap check 5,
    # via-drop check 6, stub-widen) uses whichever layer wins here.
    effective_route_layer = route_layer
    short_reason = _same_layer_short_reason(route_layer, block_geometry, width_um)
    if short_reason is not None and cross_block_route_layer is not None:
        cross_effective_width_um = (
            cross_block_width_um if cross_block_width_um is not None else width_um
        )
        cross_short_reason = _same_layer_short_reason(
            cross_block_route_layer, cross_block_geometry, cross_effective_width_um
        )
        if cross_short_reason is None:
            effective_route_layer = cross_block_route_layer
            short_reason = None
    if short_reason is not None:
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": short_reason,
        }

    # effective_width_um (#1620): the width this leg actually draws at --
    # cross_block_width_um once the retry above switched effective_route_layer
    # to cross_block_route_layer (falling back to width_um when the caller
    # left cross_block_width_um unset), width_um otherwise. Everything below
    # that sizes drawn/checked geometry (obstacle-overlap inflation, the
    # detour-lane clearance, stub-widen) uses this, not the bare width_um
    # parameter, so a wider cross-block leg's own footprint is judged and
    # drawn consistently.
    effective_width_um = (
        (cross_block_width_um if cross_block_width_um is not None else width_um)
        if cross_block_route_layer is not None
        and effective_route_layer == cross_block_route_layer
        else width_um
    )

    # obstacle_half_um/obstacle_bboxes_um/allowances_um (#999, relocated here
    # by #1620): every bbox this check tests against is inflated by
    # `effective_width_um / 2` on every side, sized from whichever layer this
    # leg actually landed on above -- see the check's own docstring section
    # further up this function for the full rationale.
    obstacle_half_um = effective_width_um / 2.0
    obstacle_bboxes_um = {
        block_id: {
            "x0": bbox["x0"] - obstacle_half_um,
            "y0": bbox["y0"] - obstacle_half_um,
            "x1": bbox["x1"] + obstacle_half_um,
            "y1": bbox["y1"] + obstacle_half_um,
        }
        for block_id, bbox in placed_bboxes_um.items()
    }
    allowances_um: dict[str, float] = {}
    if not same_block_self_net:
        allowances_um[own_a] = (
            max(0.0, _port_edge_margin_um(a, dir_a, placed_bboxes_um[own_a]))
            + obstacle_half_um
        )
        allowances_um[own_b] = (
            max(0.0, _port_edge_margin_um(b, dir_b, placed_bboxes_um[own_b]))
            + obstacle_half_um
        )

    # #1656: pick whichever lazy per-block geometry cache (if any) matches
    # the layer this leg actually landed on above -- `block_geometry_for`
    # reads `route_layer`, `cross_block_geometry_for` reads
    # `cross_block_route_layer` (see #1620's same-layer-short retry just
    # above for how `effective_route_layer` can end up being either one).
    # `None` when the caller supplied neither cache for this layer (e.g. a
    # direct unit-test caller, or `route_layer is None`) -- the obstacle
    # check below then falls back to its pre-#1656 bbox-only behaviour.
    if effective_route_layer == route_layer:
        layer_geometry_for = block_geometry_for
    elif (
        cross_block_route_layer is not None
        and effective_route_layer == cross_block_route_layer
    ):
        layer_geometry_for = cross_block_geometry_for
    else:
        layer_geometry_for = None

    # #1681: the leg's own drawn metal, as the composed cell would draw it --
    # the same `_drawn_route_region` construction `_self_net_drawn_short`
    # tests a self-net's backbone against. Built at most once per distinct
    # `dbu` (every block read from a PDK-family layout shares one in
    # practice, so this is one region per leg) and only when some block
    # actually reaches the geometry test below.
    drawn_route_by_dbu: dict[float, Any] = {}

    def _route_touches_drawn_geometry(geometry: dict[str, Any]) -> bool:
        """Whether this leg's drawn metal actually overlaps ``geometry``'s
        own drawn shapes (positive area -- a bare edge touch is a ``klt drc``
        spacing question, not a short, the same convention
        :func:`_self_net_drawn_short` applies)."""
        dbu = geometry["dbu"]
        drawn = drawn_route_by_dbu.get(dbu)
        if drawn is None:
            drawn = _drawn_route_region(points, effective_width_um, dbu)
            drawn_route_by_dbu[dbu] = drawn
        return not (geometry["region"] & drawn).is_empty()

    # Per-block exemptions, decided once per block rather than per segment --
    # both are properties of the whole leg against one block, not of a single
    # segment against it.
    exempt_block_ids: set[str] = set()
    if layer_geometry_for is not None:
        for other_id in obstacle_bboxes_um:
            if same_block_self_net and other_id == own_a:
                continue
            geometry = layer_geometry_for(other_id).get(other_id)
            if geometry is None:
                # #1656: this block draws nothing on effective_route_layer,
                # so the route cannot short to it -- a bbox crossing here is
                # a false positive, not a real obstacle.
                exempt_block_ids.add(other_id)
            elif not _route_touches_drawn_geometry(geometry):
                # #1681: this block *does* draw on effective_route_layer, but
                # not anywhere this leg's own drawn metal actually reaches --
                # the classic case being a `guard_ring` block whose hollow
                # interior holds the content block a route legitimately
                # starts inside. A bbox is a solid proxy for a shape that is
                # not solid, so the crossing it reports here is a false
                # positive exactly as #1656's "draws nothing" one was.
                exempt_block_ids.add(other_id)

    overlap_by_block_um: dict[str, float] = {}
    for seg_p0, seg_p1 in zip(points, points[1:], strict=False):
        for other_id, other_bbox in obstacle_bboxes_um.items():
            if same_block_self_net and other_id == own_a:
                continue  # a self-net is expected to cross its own block
            if other_id in exempt_block_ids:
                continue
            length = _segment_bbox_interior_overlap_um(seg_p0, seg_p1, other_bbox)
            if length > 0.0:
                overlap_by_block_um[other_id] = (
                    overlap_by_block_um.get(other_id, 0.0) + length
                )

    blocking_um = {
        other_id: crossed_um
        for other_id, crossed_um in overlap_by_block_um.items()
        if crossed_um > allowances_um.get(other_id, 0.0) + margin_eps_um
    }

    # Bounded detour search (#1167): a backbone rejected *only* for crossing
    # blocks neither of its two pins sits on is not a routing failure -- it is
    # the fixed one-jog shape being the wrong shape. Before reporting the net
    # unroutable, retry it around those obstacles on up to _MAX_DETOUR_LANES
    # alternate lanes (_detour_lane_waypoints_um), shortest first, and keep the
    # first that passes. Each retry goes back through *this same function* with
    # the lane as waypoints_um, so every routability check above -- rings, pad
    # crossings, drawn-metal shorts, this very obstacle check, via drops --
    # applies to the detour exactly as it does to any other path; nothing is
    # waived to make a detour fit. Recursion is bounded at one level by
    # construction: the retry supplies waypoints_um, and a waypoint-supplied
    # path is never itself detoured (a caller who supplies a path owns it --
    # #634 -- and the detour retry is such a caller).
    #
    # Scoped to *unrelated* blocks on purpose. A backbone that plows through
    # one of its own two pins' blocks (the same-facing port pair) is a
    # statement about which port faces which way, not about something sitting
    # in the channel; the remedy there stays the caller-supplied waypoints_um
    # of #634, whose routability the caller -- not this heuristic -- vouches
    # for.
    detour_note = ""
    if blocking_um and waypoints_um is None and not ({own_a, own_b} & set(blocking_um)):
        lanes = _detour_lane_waypoints_um(
            (a, va, placed_bboxes_um[own_a]),
            (b, vb, placed_bboxes_um[own_b]),
            [placed_bboxes_um[other_id] for other_id in blocking_um],
            placed_bboxes_um,
            effective_width_um,
        )
        for lane in lanes:
            retry = route_two_pin(
                pin_a,
                pin_b,
                blocks,
                offsets_um,
                placed_bboxes_um,
                width_um,
                route_layer,
                extraction_deck,
                block_geometry,
                waypoints_um=lane,
                cross_block_route_layer=cross_block_route_layer,
                cross_block_geometry=cross_block_geometry,
                cross_block_width_um=cross_block_width_um,
                block_geometry_for=block_geometry_for,
                cross_block_geometry_for=cross_block_geometry_for,
            )
            if retry["routed"]:
                return retry
        if lanes:
            detour_note = (
                f" -- a bounded detour around it ({len(lanes)} alternate "
                f"lane{'' if len(lanes) == 1 else 's'} placed clear of every "
                "block between the two pins) was tried first, and each one "
                "still crossed a placed block"
            )

    for other_id, crossed_um in blocking_um.items():
        allowed_um = allowances_um.get(other_id, 0.0)
        if other_id in (own_a, own_b):
            reason = (
                f"backbone's {effective_width_um}um-wide drawn path crosses "
                f"{crossed_um:.4g}um through its own pin's block '{other_id}' "
                f"-- more than that pin's own {allowed_um:.4g}um edge margin "
                f"(including {obstacle_half_um:.4g}um for the route's own "
                "half-width), so the route plows through, or clips the edge "
                "of, the block's interior (e.g. a same-facing port pair "
                "reaching a pin on the block's far side, crossing another "
                "pin on the way, or a connecting jog running close enough "
                "alongside the block's own edge that the drawn wire's width "
                "still overlaps it) rather than approaching the pin cleanly"
            )
        else:
            reason = (
                f"backbone's {effective_width_um}um-wide drawn path crosses "
                f"{crossed_um:.4g}um through unrelated block '{other_id}''s "
                "bbox (including its own edge, within half the route's "
                "width) -- the route is not point-to-point between only the "
                "two connected blocks"
            )
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": reason + detour_note,
        }

    # Via-drop resolution (#454, check 5 -- see docstring; generalized to a
    # multi-hop ladder by issue #1567): only consulted when both a
    # route_layer and an extraction_deck are given (pre-#454 callers that
    # pass neither draw exactly as before, no via-drop). For each endpoint
    # whose own reported layer differs from route_layer, either resolve the
    # connecting via-drop ladder (drop needed and available -- possibly more
    # than one via, issue #1567), find nothing to do (not a metals-stack
    # level -- an unrelated role, unchanged legacy behavior), or reject the
    # whole net as unroutable (a drop is needed but not resolvable). Uses
    # ``effective_route_layer`` (#1168): when the same-layer-short retry
    # above switched this leg to ``cross_block_route_layer``, every endpoint
    # whose own pad sits on the *primary* ``route_layer`` now needs exactly
    # the drop this loop already knows how to resolve -- no separate
    # cross-block via-drop mechanism.
    via_drops: list[dict[str, Any]] = []
    if effective_route_layer is not None and extraction_deck is not None:
        for pin, port, pos in ((pin_a, port_a, a), (pin_b, port_b, b)):
            port_layer = _port_own_layer(port)
            if port_layer is None:
                continue  # no reported layer -- draw directly, legacy behavior
            ladder, drop_error = _resolve_via_drop_layer(
                extraction_deck, effective_route_layer, port_layer
            )
            if drop_error is not None:
                return {
                    "routed": False,
                    "route_length_um": None,
                    "points_um": None,
                    "reason": (
                        f"pin '{pin['port']}' on block '{pin['block']}' is drawn "
                        f"on layer {port_layer}, which routing.layer_role's "
                        f"{effective_route_layer} cannot reach: {drop_error}"
                    ),
                }
            if ladder is not None:
                # One via_drops entry per hop (issue #1567): a single-hop
                # ladder (the pre-#1567 case) produces exactly the one entry
                # it always did; a multi-hop ladder produces one entry per
                # via, each with its own pair of landing-pad layers
                # (``landing_layers``) -- :func:`_write_composed_gds` draws a
                # via square plus a landing pad on each of those two layers
                # per entry, so a multi-level drop is just N single-level
                # drops stacked at the identical (x, y).
                for via_layer, metal_a, metal_b in ladder:
                    via_drops.append(
                        {
                            "x_um": pos[0],
                            "y_um": pos[1],
                            "via_layer": via_layer,
                            "landing_layers": (metal_a, metal_b),
                            # block_id (#1520): which endpoint's own block
                            # this drop's landing pad lands on -- lets a
                            # caller-side conflict check (compose()'s own
                            # _leg_conflict) test the pad against *that*
                            # block's own drawn geometry, not just against
                            # other already-accepted nets.
                            "block_id": pin["block"],
                        }
                    )

    # Stub-widen (#496): see _endpoint_stub_widen_um's own docstring. Computed
    # independently of the via-drop loop above (it needs no ExtractionDeck --
    # only route_layer, to compare against each port's own reported layer),
    # for both endpoints.
    stub_widen: list[dict[str, Any]] = []
    for pin, port, pos, direction, stub_len_um in (
        (pin_a, port_a, a, dir_a, stub_a_um),
        (pin_b, port_b, b, dir_b, stub_b_um),
    ):
        widen = _endpoint_stub_widen_um(
            port, pos, direction, stub_len_um, effective_width_um, effective_route_layer
        )
        if widen is not None:
            # block_id (#1520): same purpose as via_drops' own block_id above.
            widen["block_id"] = pin["block"]
            stub_widen.append(widen)

    # Same-block cross-layer conflict retry (#1393, see docstring): only
    # armed when leg_conflict was supplied AND this leg is a same-block
    # self-net's fixed-shape attempt that resolved on cross_block_route_layer
    # -- every other leg (an inter-block net, a same-block self-net that
    # never needed the cross-layer fallback, or a caller-/detour-supplied
    # waypoints_um path) skips this block entirely and returns below exactly
    # as before this issue, so route_bundle's own leg_conflict check (#1057/
    # #1386) remains the sole, unchanged decision-maker for every one of
    # those cases.
    retry_eligible = (
        leg_conflict is not None
        and same_block_self_net
        and waypoints_um is None
        and cross_block_route_layer is not None
        and effective_route_layer == cross_block_route_layer
    )
    conflict_reason = (
        leg_conflict(points, via_drops, stub_widen, effective_route_layer)
        if retry_eligible
        else None
    )
    if conflict_reason is not None:
        lanes = _self_net_cross_layer_lane_waypoints_um(
            a, b, placed_bboxes_um[own_a], effective_width_um
        )
        for lane in lanes:
            retry = route_two_pin(
                pin_a,
                pin_b,
                blocks,
                offsets_um,
                placed_bboxes_um,
                width_um,
                route_layer,
                extraction_deck,
                block_geometry,
                waypoints_um=lane,
                cross_block_route_layer=cross_block_route_layer,
                cross_block_geometry=cross_block_geometry,
                cross_block_width_um=cross_block_width_um,
                block_geometry_for=block_geometry_for,
                cross_block_geometry_for=cross_block_geometry_for,
            )
            if not retry["routed"]:
                continue
            lane_conflict = leg_conflict(
                retry["points_um"],
                retry.get("via_drops", []),
                retry.get("stub_widen", []),
                retry.get("route_layer"),
            )
            if lane_conflict is None:
                return retry
        return {
            "routed": False,
            "route_length_um": None,
            "points_um": None,
            "reason": (
                conflict_reason + " -- a bounded same-block cross-layer detour "
                f"({len(lanes)} alternate lane{'' if len(lanes) == 1 else 's'} "
                "looped clear of the block's own bbox) was tried first, and "
                "each one still conflicted"
            ),
        }

    return {
        "routed": True,
        "route_length_um": _polyline_length_um(points),
        "points_um": points,
        "via_drops": via_drops,
        "stub_widen": stub_widen,
        "route_layer": effective_route_layer,
        "width_um": effective_width_um,
        "reason": None,
    }


def _pin_ref(pin: dict[str, Any]) -> str:
    """``"block.port"`` -- the compact form router diagnostics name a pin by."""
    return f"{pin['block']}.{pin['port']}"


def _pin_position_um(
    pin: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
) -> tuple[float, float] | None:
    """A pin's port position in the *composed* frame, or ``None``.

    ``None`` when the block reported no ``ports[]`` entry for it, or the entry
    carries no usable ``x_um``/``y_um`` -- exactly the case
    :func:`route_two_pin` already reports as "one or both ports report no
    position (empty ports[])". Used only to *order* candidate legs by
    Manhattan distance (:func:`route_bundle`); the routability verdict itself
    always comes from :func:`route_two_pin`.
    """
    block = blocks.get(pin["block"])
    if block is None:
        return None
    port = (block.get("ports") or {}).get(pin["port"])
    if not isinstance(port, dict):
        return None
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if isinstance(x_um, bool) or not isinstance(x_um, (int, float)):
        return None
    if isinstance(y_um, bool) or not isinstance(y_um, (int, float)):
        return None
    offset = offsets_um[pin["block"]]
    return (float(x_um) + offset["x"], float(y_um) + offset["y"])


def route_bundle(
    pins: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
    placed_bboxes_um: dict[str, dict[str, float]],
    width_um: float,
    route_layer: tuple[int, int] | None = None,
    extraction_deck: ExtractionDeck | None = None,
    block_geometry_for: Any = None,
    leg_conflict: Any = None,
    waypoints_um: list[tuple[float, float]] | None = None,
    cross_block_route_layer: tuple[int, int] | None = None,
    cross_block_geometry_for: Any = None,
    cross_block_width_um: float | None = None,
    explicit_legs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Route one ``connectivity[]`` net of *any* pin count (issue #1073).

    This is the N-pin generalisation the spike's section 5 item 2 named as
    "the natural next increment once two-pin routing is proven against a real
    block" -- a shared supply/ground rail or a fanout node (a bias line, a
    clock, an inverter chain's tap) touches one port on every block it spans,
    so a two-pin-only router cannot wire the *majority* of a real circuit's
    connectivity at all.

    **Algorithm: a routed minimum spanning tree over the net's pins.** Every
    unordered pin pair is a candidate *leg*; candidates are tried in
    increasing Manhattan distance (ties broken by pin index, so the result is
    deterministic and the composed GDS stays byte-reproducible, #320), and a
    leg is accepted only when it joins two so-far-disconnected parts of the
    net (Kruskal). The net is routed when every pin ends up in one component.
    For the canonical rail case -- one north-facing supply port per block
    across a placement row -- the nearest-first ordering yields exactly the
    trunk-and-stub shape the issue asks for: a chain of adjacent-block legs,
    each one an ordinary Manhattan backbone.

    **Every leg is routed by :func:`route_two_pin`**, unchanged. That is the
    point of decomposing into legs rather than inventing a second geometry
    routine: all six of its routability checks (channel width, guard/collector
    ring, self-net pad crossing, self-net drawn-metal short, obstacle overlap,
    via-drop resolution) apply per leg, for free, with no second
    implementation to keep in sync. A leg a check rejects is simply not
    accepted, and the *next* candidate that would join the same two components
    is tried -- so an N-pin net routes around an individually unroutable pair
    whenever another spanning tree exists, rather than failing outright.

    **Partial routing when no spanning tree exists (issue #1169).** A net
    whose pins cannot all be joined into one component still gets every leg
    the spanning-tree search *did* accept drawn -- only the pins left
    stranded (and any candidate leg the router tried and rejected on the way
    to reaching them) are left undrawn. The caller gets the net back in
    ``unrouted_nets[]`` (it is still not *fully* connected) but is not left
    building routable geometry around a fully-blank net -- discarding metal
    that already passed every one of :func:`route_two_pin`'s per-leg checks
    (plus the route-vs-route collision check, #1057) made a real composition
    failure harder to debug than necessary, for no benefit: an individually
    routable leg's metal never introduces a short or a DRC violation just
    because a *different* leg of the same net failed. (Before this, the net
    was all-or-nothing: any spanning-tree failure reset every leg's
    ``routed`` back to ``false``, discarding metal for legs that were
    individually routable -- see issue #1073's original rationale, now
    superseded.) Every returned leg still reports ``routed: true`` **iff its
    metal is drawn** -- so no leg is ever reported as routed when nothing was
    drawn for it -- and the top-level ``status`` field (below) tells the
    caller whether the net ended up fully routed, partially routed, or not
    routed at all, so partial routing is never mistaken for full routing (or
    vice versa) by counting ``legs[]`` alone.

    ``block_geometry_for`` is an optional callable taking a block ``id`` and
    returning the ``{block_id: geometry}`` mapping :func:`route_two_pin`
    takes as ``block_geometry`` (``compose()`` passes its lazily-populated
    per-block cache); it is also consulted for an *inter*-block leg's own
    ``blocks[].cell`` endpoint(s) (issue #1527, not only when both pins sit
    on the same block) -- a leg whose two pins sit on different blocks still
    draws its approach stub inside each endpoint's own block, so a ``cell``
    endpoint (an existing GDS/OASIS stream this command did not generate,
    whose ``ports[]`` are hand-declared directly on the stream's own
    geometry) needs its own block's drawn geometry to check that stub
    against (see :func:`route_two_pin`'s own "own-block escape check"). A
    ``generator_report`` endpoint is deliberately left out of this fetch --
    see that same docstring for why. ``block_geometry_for``/
    ``cross_block_geometry_for`` are *also* forwarded to :func:`route_two_pin`
    verbatim (issue #1656) so its obstacle-overlap check (5) can look up any
    third-party obstacle block's own geometry on demand -- not only the
    ``cell``-endpoint fetch just described, which remains scoped to a leg's
    own two blocks. ``leg_conflict`` is an optional callable taking a
    candidate leg's drawn ``points_um``, ``via_drops``, ``stub_widen``, and
    (issue #1386) its effective ``route_layer`` (the same four fields
    :func:`route_two_pin` returns for it -- issue #1197 widened this from
    ``points_um`` alone, since a leg's via-drop landing pad or stub-widen box
    can extend past its bare backbone far enough to collide with another net
    even when the two backbones never touch; #1386 added the layer so the
    callback can tell two legs on genuinely different physical layers apart
    and, when it has a same-layer minimum-spacing rule to consult, reject a
    leg that comes too *close* to an already-accepted one, not only one that
    literally overlaps it) and returning a rejection reason (or ``None`` to
    accept) -- ``compose()`` passes its route-vs-route collision check
    (#1057) here, so a leg colliding with an *already-routed other net* is
    rejected as a candidate and an alternative leg is tried, rather than
    failing the whole net. The identical callable is also threaded straight
    into every candidate leg's own :func:`route_two_pin` call (issue #1393):
    that function's own "same-block cross-layer conflict retry" (see its
    docstring) uses it to detect -- and retry around, with an alternate
    ``cross_block_layer_role`` lane -- the narrow case this bundle-level
    check alone cannot recover from: a same-block self-net whose *only*
    candidate leg collides with a *different* same-block self-net's
    already-accepted route on the shared cross layer, where there is no
    other candidate pair left to fall back to.

    **Route-vs-route cross-layer retry (issue #1680).** The two cases above
    -- #1057's plain rejection and #1393's same-block cross-layer retry --
    both leave one gap: a leg that ``route_two_pin`` itself already accepted
    on the *primary* ``route_layer`` (no same-block short, #1393's own gate
    never engaged) can still be rejected by this function's own post-hoc
    ``leg_conflict`` call, once its footprint is compared against a
    *different* net's already-accepted route -- and at that point
    ``route_two_pin`` is no longer on the call stack to retry itself onto
    ``cross_block_route_layer`` the way checks 3/4 do. When that happens and
    ``cross_block_route_layer`` is configured, this function re-invokes
    :func:`route_two_pin` for the identical pin pair with the primary/cross
    layer roles swapped (``cross_block_route_layer`` as its ``route_layer``,
    this leg's own cross-layer geometry cache as its ``block_geometry``) --
    the same checks (including via-drop resolution for a port whose reported
    layer differs from the new drawing layer) run exactly as they would for
    a leg drawn on the cross layer from the start; ``leg_conflict`` is
    re-checked against *that* result before it is accepted, so a leg that
    conflicts on both layers still fails the whole net rather than drawing a
    silent short. This retry is scoped to a *route-vs-route* rejection only
    (a genuine same-layer short with no ``cross_block_route_layer``
    configured is unaffected and still rejects exactly as before #1680) and,
    like every other detour search in this module, offers no further
    fallback beyond that one retried attempt.

    ``cross_block_route_layer``/``cross_block_geometry_for`` (issue #1168)
    mirror ``route_layer``/``block_geometry_for`` for an optional second,
    higher metal :func:`route_two_pin` retries a same-block self-net leg on
    when it would otherwise short across another same-layer pad on the
    primary ``route_layer`` -- threaded straight through to every candidate
    leg's own :func:`route_two_pin` call; see that function's docstring for
    the retry mechanics. Both default to ``None`` (no cross-block layer
    configured), which reproduces this function's pre-#1168 behaviour
    exactly. ``cross_block_width_um`` (issue #1620) is the width a leg
    draws at once it actually falls back to ``cross_block_route_layer`` --
    distinct from ``width_um`` so a caller is never forced to widen the
    primary plane's own routing just to satisfy the cross layer's own
    (typically stricter) deck minimum; ``None`` (the default) reuses
    ``width_um`` for a cross-layer leg too, reproducing this function's
    pre-#1620 behaviour exactly. Threaded straight through to every
    candidate leg's own :func:`route_two_pin` call, and each accepted leg's
    own effective width -- ``cross_block_width_um`` or ``width_um``,
    whichever layer it actually landed on -- is reported back in its own
    ``legs[]`` entry as ``width_um`` (see the return-value doc below), since
    a partially-routed net's drawn legs may not all share one width any more
    than they all share one layer.

    ``waypoints_um`` (#634) applies to the single backbone of a **2-pin** net;
    supplying it for a >2-pin net raises :class:`GenComposeError` (there is no
    unambiguous leg for a caller-supplied path to belong to -- see
    :func:`_parse_connectivity`, which rejects that combination at request-parse
    time).

    ``explicit_legs`` (issue #1529) is the bundle-net counterpart:
    ``[{"from_pin": {block, port}, "to_pin": {block, port}, "waypoints_um":
    [...] | None}, ...]``, each pair already validated (by
    :func:`_parse_connectivity`/:func:`_parse_legs`) to be members of this
    net's own ``pins``. Every explicit leg is routed and unioned into the
    spanning tree **before** the automatic nearest-first search runs (seeded
    first, not merely preferred) -- so a caller can hand-route one or more
    legs of a large bundle net (steering around pre-existing geometry, or
    just forcing a specific pair together) while every pin the explicit legs
    don't cover still completes automatically, exactly as it would with no
    ``explicit_legs`` at all. Because they are committed to this call's own
    ``legs`` list before ``compose()`` ever records this net's regions in
    ``accepted_route_regions``, two explicit legs of the same net are never
    compared against each other by the route-vs-route collision check
    (``leg_conflict``) -- unlike the old workaround of splitting one bundle
    net into several 2-pin ``connectivity[]`` entries, which had to keep
    every pair pin-adjacent to get that same exemption. Mutually exclusive
    with ``waypoints_um`` (only meaningful for a 2-pin net in the first
    place); supplying both raises :class:`GenComposeError`.

    An explicit leg that is *rejected* (unroutable path, or a
    ``leg_conflict``) does not fail the net -- the automatic search still
    runs and may connect those pins another way -- but, unlike a rejected
    auto-selected candidate, it is **kept in the returned** ``legs[]`` even
    for a fully-routed net, with ``routed: False`` and its own ``reason``.
    A named leg is caller intent, not a router guess: silently dropping it
    would let ``status: "routed"`` hide the fact that the requested path
    was never drawn.

    Returns ``{"routed": bool, "route_length_um": float | None, "legs": [...],
    "reason": str | None, "status": str}``, where ``routed`` is ``True`` only
    when *every* pin joined one component (unchanged from before #1169),
    ``route_length_um`` is the summed length of every *drawn* leg (``None``
    only when zero legs were drawn), ``status`` is one of ``"routed"`` (every
    pin connected), ``"partial"`` (at least one leg drawn, but the net is not
    fully connected), or ``"unrouted"`` (no leg was ever accepted) -- and each
    ``legs[]`` entry is ``{"pins": [pin_a, pin_b], "routed": bool,
    "route_length_um": float | None, "reason": str | None, "points_um": list
    | None, "via_drops": list, "stub_widen": list, "route_layer":
    tuple[int, int] | None, "width_um": float}`` (a rejected leg omits
    ``width_um``, nothing having been drawn for it) --
    ``points_um``/``via_drops``/``stub_widen``/``route_layer``/``width_um``
    being the same per-leg drawing payload :func:`route_two_pin` returns,
    consumed by ``compose()`` (``route_layer`` is the *effective* layer that
    leg actually drew on, #1168; ``width_um`` (#1620) is the width it drew
    at on that layer -- and both are per *leg*, not per net, so a
    partially-routed net's drawn legs may sit on different layers at
    different widths). A leg that came from ``explicit_legs`` also carries
    ``"explicit": True`` -- an internal marker for the retention rule above,
    not part of the ``klt gen-compose`` response (``compose()`` projects
    every leg down to ``pins``/``routed``/``route_length_um``/``reason``).
    """
    from .gen_compose import GenComposeError

    if len(pins) < 2:
        raise GenComposeError("route_bundle() needs at least 2 pins")
    if waypoints_um is not None and len(pins) != 2:
        raise GenComposeError(
            "waypoints_um applies to a 2-pin net's single backbone -- it cannot "
            f"be attributed to any one leg of a {len(pins)}-pin net"
        )
    if waypoints_um is not None and explicit_legs:
        raise GenComposeError(
            "waypoints_um and explicit_legs are mutually exclusive -- "
            "waypoints_um steers a 2-pin net's single backbone, "
            "explicit_legs steers one or more named legs"
        )

    pin_count = len(pins)
    parent = list(range(pin_count))

    def _find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(left: int, right: int) -> bool:
        root_left, root_right = _find(left), _find(right)
        if root_left == root_right:
            return False
        parent[max(root_left, root_right)] = min(root_left, root_right)
        return True

    components = pin_count

    # A pin listed twice (the same block+port) is the same physical point, so
    # it needs no leg -- pre-union it rather than routing a zero-length route
    # between a port and itself.
    first_seen: dict[tuple[str, str], int] = {}
    for index, pin in enumerate(pins):
        key = (pin["block"], pin["port"])
        if key in first_seen:
            if _union(first_seen[key], index):
                components -= 1
        else:
            first_seen[key] = index

    positions = [_pin_position_um(pin, blocks, offsets_um) for pin in pins]

    def _candidate_distance_um(pair: tuple[int, int]) -> float:
        pos_i, pos_j = positions[pair[0]], positions[pair[1]]
        if pos_i is None or pos_j is None:
            return math.inf
        return abs(pos_i[0] - pos_j[0]) + abs(pos_i[1] - pos_j[1])

    candidates = sorted(
        ((i, j) for i in range(pin_count) for j in range(i + 1, pin_count)),
        key=lambda pair: (_candidate_distance_um(pair), pair[0], pair[1]),
    )

    # Own-block geometry (issue #1527): for an *inter*-block leg (unlike a
    # same-block self-net, always fetched below), also fetched for whichever
    # endpoint's own block is a `blocks[].cell` block -- an existing
    # GDS/OASIS stream this command did not generate, whose `ports[]` are
    # hand-declared by the caller directly on the stream's own internal
    # geometry (see `_parse_cell_block`). That is the one block kind where
    # "every merged shape not touching my own declared port is a genuinely
    # different net" actually holds: a `generator_report` block can
    # legitimately carry drawn geometry no `ports[]` entry names (e.g.
    # `mos_array`'s own unreported dummy matching columns, deliberately
    # excluded from `klt extract`'s netlist via its own dummy-suppression
    # convention, #295/#462) that this check cannot tell apart from a real
    # obstacle -- so it is scoped to `cell` blocks only, see
    # route_two_pin()'s own "own-block escape check" docstring.
    # block_geometry_for()/cross_block_geometry_for() each return the *same*
    # growing {block_id: geometry} cache object regardless of which block id
    # is requested (populating it lazily, once per block), so the two
    # assignments below always end up pointing at one dict either way.
    # Shared by both the explicit-legs loop and the automatic candidate loop
    # below (#1529) -- previously duplicated inline in the candidate loop
    # only, since explicit legs did not exist yet.
    def _leg_geometry(i: int, j: int) -> tuple[Any, Any]:
        geometry = None
        cross_geometry = None
        if block_geometry_for is not None:
            if pins[i]["block"] == pins[j]["block"]:
                geometry = block_geometry_for(pins[i]["block"])
            else:
                for pin in (pins[i], pins[j]):
                    if blocks[pin["block"]].get("source") == "cell":
                        geometry = block_geometry_for(pin["block"])
        if cross_block_geometry_for is not None:
            if pins[i]["block"] == pins[j]["block"]:
                cross_geometry = cross_block_geometry_for(pins[i]["block"])
            else:
                for pin in (pins[i], pins[j]):
                    if blocks[pin["block"]].get("source") == "cell":
                        cross_geometry = cross_block_geometry_for(pin["block"])
        return geometry, cross_geometry

    # Route-vs-route cross-layer retry (issue #1680): `leg_conflict` (#1057)
    # only ever runs *after* `route_two_pin()` has already returned
    # `routed: True` on the primary `route_layer` -- so, unlike checks 3/4's
    # own same-layer-short retry inside that function (#1168/#1393), a
    # rejection here never got a chance to try `cross_block_route_layer`
    # before failing the whole net. This closure re-runs the identical pin
    # pair with the primary/cross layer roles swapped -- `cross_geometry`
    # (this leg's own drawn-geometry cache for `cross_block_route_layer`,
    # the same one checks 3/4's own retry already uses) standing in for
    # `block_geometry`, `cross_block_geometry_for` standing in for
    # `block_geometry_for` -- so every one of `route_two_pin`'s own checks
    # (including its own via-drop resolution for a port whose reported
    # layer differs from this new "primary") runs exactly as they would for
    # a leg drawn on `cross_block_route_layer` from the start, not only the
    # narrower same-block short case #1393 already covers. No further
    # fallback layer is offered to this retried call (`cross_block_route_
    # layer=None`) -- one level of retry, mirroring the bound every other
    # detour search in this module already enforces (#1167/#1393).
    def _retry_leg_on_cross_layer(
        i: int,
        j: int,
        cross_geometry: Any,
        leg_waypoints_um: list[tuple[float, float]] | None,
    ) -> dict[str, Any] | None:
        if cross_block_route_layer is None or leg_conflict is None:
            return None
        retry_width_um = (
            cross_block_width_um if cross_block_width_um is not None else width_um
        )
        retry = route_two_pin(
            pins[i],
            pins[j],
            blocks,
            offsets_um,
            placed_bboxes_um,
            retry_width_um,
            cross_block_route_layer,
            extraction_deck,
            cross_geometry,
            waypoints_um=leg_waypoints_um,
            leg_conflict=leg_conflict,
            block_geometry_for=cross_block_geometry_for,
        )
        if not retry["routed"]:
            return None
        retry_reason = leg_conflict(
            retry["points_um"],
            retry.get("via_drops", []),
            retry.get("stub_widen", []),
            retry.get("route_layer"),
        )
        if retry_reason is not None:
            return None
        return retry

    legs: list[dict[str, Any]] = []
    total_length_um = 0.0

    # Explicit legs (#1529): caller-steered pin pairs, routed and seeded into
    # the spanning tree *before* the automatic nearest-first search below.
    # Each is resolved to its pin indices via `first_seen` -- from_pin/to_pin
    # were already validated (at request-parse time, see _parse_legs) to be
    # members of this net's own pins[], so every (block, port) key here is
    # guaranteed present when route_bundle() is reached through compose()'s
    # normal request path; the lookup is still defensive (a direct caller of
    # route_bundle() could pass a mismatched pins/explicit_legs pair). Routed
    # through the exact same route_two_pin()/leg_conflict path as an
    # auto-selected candidate, so every routability check still applies --
    # an explicit leg is steered, not exempted. Unlike an auto candidate,
    # an explicit leg is always attempted even when its two pins are already
    # in one component (a caller may want redundant strap metal), and it is
    # never skipped in favour of "the next candidate" -- there is only one
    # candidate for a named pair.
    for leg_spec in explicit_legs or ():
        from_pin, to_pin = leg_spec["from_pin"], leg_spec["to_pin"]
        i = first_seen.get((from_pin["block"], from_pin["port"]))
        j = first_seen.get((to_pin["block"], to_pin["port"]))
        if i is None or j is None:
            raise GenComposeError(
                "route_bundle(): explicit_legs entry references a "
                "from_pin/to_pin not present in this net's own pins -- "
                "validated at request-parse time, so this indicates a "
                "direct caller with a mismatched pins/explicit_legs pair"
            )
        geometry, cross_geometry = _leg_geometry(i, j)
        result = route_two_pin(
            pins[i],
            pins[j],
            blocks,
            offsets_um,
            placed_bboxes_um,
            width_um,
            route_layer,
            extraction_deck,
            geometry,
            waypoints_um=leg_spec.get("waypoints_um"),
            cross_block_route_layer=cross_block_route_layer,
            cross_block_geometry=cross_geometry,
            cross_block_width_um=cross_block_width_um,
            leg_conflict=leg_conflict,
            block_geometry_for=block_geometry_for,
            cross_block_geometry_for=cross_block_geometry_for,
        )
        reason = None if result["routed"] else result["reason"]
        if result["routed"] and leg_conflict is not None:
            reason = leg_conflict(
                result["points_um"],
                result.get("via_drops", []),
                result.get("stub_widen", []),
                result.get("route_layer"),
            )
            if (
                reason is not None
                and result.get("route_layer") != cross_block_route_layer
            ):
                retry = _retry_leg_on_cross_layer(
                    i, j, cross_geometry, leg_spec.get("waypoints_um")
                )
                if retry is not None:
                    result = retry
                    reason = None
        if reason is None and result["routed"]:
            if _union(i, j):
                components -= 1
            total_length_um += result["route_length_um"]
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "explicit": True,
                    "routed": True,
                    "route_length_um": result["route_length_um"],
                    "reason": None,
                    "points_um": result["points_um"],
                    "via_drops": result.get("via_drops", []),
                    "stub_widen": result.get("stub_widen", []),
                    "route_layer": result.get("route_layer"),
                    "width_um": result.get("width_um", width_um),
                }
            )
        else:
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "explicit": True,
                    "routed": False,
                    "route_length_um": None,
                    "reason": reason,
                    "points_um": None,
                    "via_drops": [],
                    "stub_widen": [],
                }
            )

    for i, j in candidates:
        if components == 1:
            break
        if _find(i) == _find(j):
            continue  # already connected through other legs -- no leg needed

        geometry, cross_geometry = _leg_geometry(i, j)
        result = route_two_pin(
            pins[i],
            pins[j],
            blocks,
            offsets_um,
            placed_bboxes_um,
            width_um,
            route_layer,
            extraction_deck,
            geometry,
            waypoints_um=waypoints_um,
            cross_block_route_layer=cross_block_route_layer,
            cross_block_geometry=cross_geometry,
            cross_block_width_um=cross_block_width_um,
            leg_conflict=leg_conflict,
            block_geometry_for=block_geometry_for,
            cross_block_geometry_for=cross_block_geometry_for,
        )
        reason = None if result["routed"] else result["reason"]
        if result["routed"] and leg_conflict is not None:
            reason = leg_conflict(
                result["points_um"],
                result.get("via_drops", []),
                result.get("stub_widen", []),
                result.get("route_layer"),
            )
            if (
                reason is not None
                and result.get("route_layer") != cross_block_route_layer
            ):
                retry = _retry_leg_on_cross_layer(i, j, cross_geometry, waypoints_um)
                if retry is not None:
                    result = retry
                    reason = None

        if reason is None and result["routed"]:
            _union(i, j)
            components -= 1
            total_length_um += result["route_length_um"]
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "routed": True,
                    "route_length_um": result["route_length_um"],
                    "reason": None,
                    "points_um": result["points_um"],
                    "via_drops": result.get("via_drops", []),
                    "stub_widen": result.get("stub_widen", []),
                    "route_layer": result.get("route_layer"),
                    "width_um": result.get("width_um", width_um),
                }
            )
        else:
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "pin_indices": (i, j),
                    "routed": False,
                    "route_length_um": None,
                    "reason": reason,
                    "points_um": None,
                    "via_drops": [],
                    "stub_widen": [],
                }
            )

    if components == 1:
        return {
            "routed": True,
            "route_length_um": total_length_um,
            # Only the accepted legs: a candidate the router rejected on the
            # way to a working spanning tree is not part of the result --
            # *except* a rejected caller-named `explicit_legs` leg (#1529),
            # which is kept. An auto-selected candidate is the router's own
            # guess and its rejection is an implementation detail; a named
            # leg is caller intent, and dropping it would let the automatic
            # search silently re-route around the caller's own steering and
            # still report `status: "routed"` with no trace that the
            # requested path was never drawn.
            "legs": [
                leg for leg in legs if leg["routed"] or leg.get("explicit", False)
            ],
            "reason": None,
            "status": "routed",
        }

    # --- Unroutable: report which pins were stranded and why ----------------
    groups: dict[int, list[int]] = {}
    for index in range(pin_count):
        groups.setdefault(_find(index), []).append(index)
    # The largest component is "the net"; every pin outside it is stranded.
    main_root = max(groups, key=lambda root: (len(groups[root]), -root))
    stranded = sorted(
        index
        for root, members in groups.items()
        if root != main_root
        for index in members
    )
    stranded_set = set(stranded)

    blocker = next(
        (
            leg
            for leg in legs
            if not leg["routed"] and set(leg["pin_indices"]) & stranded_set
        ),
        None,
    )
    blocker_reason = blocker["reason"] if blocker is not None else None

    if pin_count == 2:
        # A two-pin net has exactly one candidate leg, so the net's own reason
        # *is* that leg's reason -- byte-identical to the message compose()
        # reported before this function generalised the two-pin path.
        reason = blocker_reason or "no routable path between the net's two pins"
    else:
        refs = ", ".join(f"'{_pin_ref(pins[index])}'" for index in stranded)
        reason = (
            f"{len(stranded)} of {pin_count} pins could not be connected into "
            f"one net ({refs}) -- every candidate leg reaching them was "
            "rejected (per-leg detail in nets[].legs[])"
        )
        if blocker is not None:
            reason += (
                f"; nearest rejection ('{_pin_ref(blocker['pins'][0])}' -> "
                f"'{_pin_ref(blocker['pins'][1])}'): {blocker_reason}"
            )

    # Partial routing (issue #1169): a net that could not be fully connected
    # keeps every leg the spanning-tree search *did* accept -- their geometry
    # is real, DRC-checked (route_two_pin's checks 1-6, plus the route-vs-route
    # collision check #1057 via `leg_conflict`) metal, drawn exactly as it
    # would be for a fully-routed net, so `legs[].routed` still means "this
    # leg's metal is in the composed output" at the leg level. Only the pins
    # that ended up stranded -- and any leg the router tried and rejected on
    # the way to reaching them -- are left undrawn. `status` distinguishes the
    # two ways a net can be incomplete: "partial" (some legs drawn) vs.
    # "unrouted" (no leg was ever accepted) -- the caller must not have to
    # infer this by counting `legs[].routed`, since a zero-leg net and a
    # not-fully-spanned net both report `routed: false` at the net level.
    drawn_leg_count = sum(1 for leg in legs if leg["routed"])
    status = "partial" if drawn_leg_count > 0 else "unrouted"

    return {
        "routed": False,
        "route_length_um": total_length_um if drawn_leg_count > 0 else None,
        "legs": legs,
        "reason": reason,
        "status": status,
    }


#: Reason string every declare-only net/leg (#1188) reports -- distinct from
#: any geometry-based rejection reason :func:`route_bundle`/:func:`route_two_pin`
#: produce, so a caller can tell "not requested" apart from "tried and failed"
#: by string alone as well as by ``routing`` being empty in the echoed request.
_DECLARE_ONLY_REASON = "routing not requested"


def _declare_only_bundle_result(pins: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a :func:`route_bundle`-shaped result for a declare-only net (#1188).

    Used by :func:`compose` in place of :func:`route_bundle` when
    ``request.routing`` is absent/``{}``: the net's ``{block, port}`` pins
    were already validated by :func:`_parse_connectivity`, but no routing was
    requested, so nothing is drawn. Reports the same spanning-tree shape a
    routed net would (one leg per pair needed to span every pin, matching a
    repeated ``{block, port}`` pin needing no leg of its own -- see
    :func:`route_bundle`'s docstring), except every leg is ``routed: False``
    with :data:`_DECLARE_ONLY_REASON`, so ``nets[]``/``unrouted_nets[]``
    report a declare-only net exactly like an unroutable one, distinguished
    only by the reason string. Candidate pairs are picked in pin-index order
    (not the nearest-first geometric order :func:`route_bundle` uses) --
    positions play no role here since nothing is drawn, so there is no
    geometry to order by; this still yields the same deterministic,
    byte-reproducible leg list run to run.
    """
    pin_count = len(pins)
    parent = list(range(pin_count))

    def _find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(left: int, right: int) -> bool:
        root_left, root_right = _find(left), _find(right)
        if root_left == root_right:
            return False
        parent[max(root_left, root_right)] = min(root_left, root_right)
        return True

    components = pin_count
    first_seen: dict[tuple[str, str], int] = {}
    for index, pin in enumerate(pins):
        key = (pin["block"], pin["port"])
        if key in first_seen:
            if _union(first_seen[key], index):
                components -= 1
        else:
            first_seen[key] = index

    legs: list[dict[str, Any]] = []
    for i in range(pin_count):
        if components == 1:
            break
        for j in range(i + 1, pin_count):
            if components == 1:
                break
            if _find(i) == _find(j):
                continue
            _union(i, j)
            components -= 1
            legs.append(
                {
                    "pins": [pins[i], pins[j]],
                    "routed": False,
                    "route_length_um": None,
                    "reason": _DECLARE_ONLY_REASON,
                    "points_um": None,
                    "via_drops": [],
                    "stub_widen": [],
                }
            )

    return {
        "routed": False,
        "route_length_um": None,
        "legs": legs,
        "reason": _DECLARE_ONLY_REASON,
        "status": "unrouted",
    }
