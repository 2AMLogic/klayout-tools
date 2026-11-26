"""Above-the-primary-plane leg retries for ``klt gen-compose`` routing.

Hosts the two ``route_bundle()`` leg retries that move a leg off its net's
own routing plane, split out of ``gen_compose_routing.py`` (which sits at its
per-file size-ratchet ceiling) the same way ``gen_compose_plane_change.py``
was:

* ``PlaneClimbContext.retry_leg_on_cross_layer`` -- the request-configured
  ``routing.cross_block_layer_role`` retry (issue #1680), relocated verbatim
  from a ``route_bundle()`` closure;
* ``PlaneClimbContext.climb_leg`` -- the automatic metals-stack **plane
  climb** (issue #6920): a leg the fixed one-jog backbone cannot route on
  its own plane -- because the shape reaches a pin from behind (#1895),
  plows through a placed block's drawn metal, crosses a net routed
  earlier, crosses a self-net's or own-block's own pad metal (#433/#453/
  #1527), or would merge with a guard/collector ring's own loop (#199
  case 2 / #434, plane-conditional since #1960) -- is retried, plane by
  plane up the resolved PDK's own ``metals`` stack, before the net is
  reported unroutable.

The climb is the automated form of the escape the #1895 rejection text
already names by hand ("route to a layer_role with a metal2/via stack
instead, or configure routing.cross_block_layer_role"): on a row of
generator blocks the higher planes are empty (each block draws only its
local-interconnect pads, so the #1656/#1681 drawn-geometry exemptions hold
there), every routability check re-runs in full against the candidate
plane's own geometry, and each accepted attempt is gated by the caller's
``leg_conflict`` exactly as a same-plane leg is -- nothing is waived to make
a climb fit. A net whose own plane was *named* by the caller
(``connectivity[].layer_role``, #1655) is never climbed ("explicit wins"),
and a caller-supplied ``waypoints_um`` path is never re-routed (the caller
owns that path, #634).

``route_two_pin`` is imported *inside* the retry methods rather than at
module scope -- the same deferred-import discipline
``gen_compose_plane_change.py`` uses -- because ``gen_compose_routing``
imports this module at *its* module scope to give ``route_bundle()`` this
context, so a module-scope import back would be a load-time cycle.
"""

from __future__ import annotations

from functools import partial
from typing import Any, NamedTuple

from .decks import ExtractionDeck
from .gen_compose_layer_rules import (
    _is_gf180_9k_11k_top,
    _min_width_um_for_layer,
    _port_own_layer,
    _route_spacing_rule,
    routing_planes_above,
)
from .gen_compose_plane_change import RouteConflict, _leg_conflict_reason

#: Rejection-reason fragments a higher plane can lift. The #1895 far-side
#: ("from behind") family and the block-interior crossings come from
#: ``gen_compose_routing._obstacle_rejection_reason``; the three
#: already-routed-net fragments from ``gen_compose_pad_conflicts`` -- the
#: route-vs-route gate ``route_bundle()`` itself runs. The pad-contact marker
#: covers a landing pad touching another net's block on the current plane.
#:
#: The self-net classes (``#433``'s pad footprint, ``#453``'s degenerate
#: same-facing jog and drawn-metal overlap, ``#1527``'s own-block escape,
#: and the ``leg's drawn``-family deck-spacing conflict ``#1904`` reports)
#: each name the met2/via escape in their own remedy text and are
#: layer-guarded at the point of the check -- ``#433`` skips a pad on
#: another physical layer outright, and the drawn-metal reads read the
#: obstacle's geometry *on the route layer* (empty for a generator block on
#: a plane it draws nothing on) -- so the climbed retry re-evaluates every
#: one against the candidate plane's own geometry exactly as the climb's
#: full re-run contract requires (#6912).
#:
#: The ring classes (``#199`` case 2's closed ring, ``#434``'s
#: opening-crossing family) were deliberately left out of the #6920 climb
#: on the theory they were "a property of the pin pair, not of the plane".
#: They are plane-conditional since #1960: ``_leg_plane_clears_ring``
#: proves a backbone drawn on a plane at least one via above the ring's
#: own conductor -- with every via-drop landing pad clear of the ring's
#: trace -- shares no conductor with the loop, and the retry runs it with
#: the climbed plane as the route layer (#6912: an own-ringed ``mos_array``
#: group in a mixed row is otherwise unroutable, the exact "needs no
#: floorplan change at all" escape #1960 documented for ``gen-compose``).
#: Still left alone: a too-narrow channel, an unresolvable via drop, and a
#: gapped ring that reports too few side ports to check at all.
_CLIMBABLE_REASON_MARKERS = (
    "reaches the pin from behind",
    "through unrelated block",
    "through its own pin's block",
    "still crossed a placed block",
    "crosses already-routed net",
    "overlaps already-routed net",
    "shares a conductive edge with already-routed net",
    "shares a conductive corner with already-routed net",
    "a pad touches a block hosting no pin of its net",
    "touches foreign port",
    "self-net backbone crosses",
    "self-net between two same-facing ports",
    "self-net's drawn",
    "leg's drawn",
    "has a closed guard/collector ring",
    "'s ring on its",
)


def _climbable(reason: str | None) -> bool:
    if not reason:
        return False
    return any(marker in reason for marker in _CLIMBABLE_REASON_MARKERS) or (
        "comes within " in reason and "of already-routed net" in reason
    )


def _endpoint_geometry_for(
    pins: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    i: int,
    j: int,
    fetch: Any,
) -> Any:
    """Fetch endpoint obstacles for a bundle leg on the selected layer.

    A same-block leg uses its own geometry. Between blocks, only hand-declared
    cell ports identify the drawn geometry reliably enough for the own-block
    escape check (#1527). A generator can draw unreported geometry, such as a
    MOS array's dummy matching columns (#295, #462).

    The fetcher populates one shared cache. Visit both cell endpoints in pin
    order so the returned cache includes the geometry of both blocks.
    """
    if fetch is None:
        return None
    if pins[i]["block"] == pins[j]["block"]:
        return fetch(pins[i]["block"])
    geometry = None
    for pin in (pins[i], pins[j]):
        if blocks[pin["block"]].get("source") == "cell":
            geometry = fetch(pin["block"])
    return geometry


class PlaneClimbContext(NamedTuple):
    """The ``route_bundle()`` state a cross-plane leg retry runs against."""

    pins: list[dict[str, Any]]
    blocks: dict[str, dict[str, Any]]
    offsets_um: dict[str, dict[str, float]]
    placed_bboxes_um: dict[str, dict[str, float]]
    width_um: float
    route_layer: tuple[int, int] | None
    pdk_variant: str | None
    extraction_deck: ExtractionDeck | None
    cross_block_route_layer: tuple[int, int] | None
    cross_block_width_um: float | None
    cross_block_geometry_for: Any
    geometry_for_layer: Any
    leg_conflict: Any
    grid_um: float
    auto: bool
    width_aware_conflict: Any
    landing_pad_side: Any
    route_dbu: Any

    def retry_leg_on_cross_layer(
        self,
        i: int,
        j: int,
        cross_geometry: Any,
        leg_waypoints_um: list[tuple[float, float]] | None,
    ) -> dict[str, Any] | None:
        """Route-vs-route cross-layer retry (issue #1680), relocated verbatim
        from ``route_bundle()``: re-run the identical pin pair with the
        primary/cross layer roles swapped so every ``route_two_pin()`` check
        runs exactly as it would for a leg drawn on
        ``cross_block_route_layer`` from the start. The caller checks the
        returned candidate for an electrical conflict."""
        from .gen_compose_routing import route_two_pin

        if self.cross_block_route_layer is None or self.leg_conflict is None:
            return None
        retry_width_um = (
            self.cross_block_width_um
            if self.cross_block_width_um is not None
            else self.width_um
        )
        retry = route_two_pin(
            self.pins[i],
            self.pins[j],
            self.blocks,
            self.offsets_um,
            self.placed_bboxes_um,
            retry_width_um,
            self.cross_block_route_layer,
            self.extraction_deck,
            cross_geometry,
            waypoints_um=leg_waypoints_um,
            leg_conflict=self.leg_conflict,
            block_geometry_for=self.cross_block_geometry_for,
            grid_um=self.grid_um,
        )
        return retry if retry["routed"] else None

    def planes_above(self) -> tuple[tuple[int, int], ...]:
        """The deck's own metals-stack planes strictly above this net's
        ``route_layer``, in stack order; empty when the net's plane is not a
        metals-stack member (or no deck was given)."""
        return routing_planes_above(
            self.extraction_deck, self.route_layer, self.pdk_variant
        )

    def retry_rejected_leg(
        self,
        i: int,
        j: int,
        waypoints: Any,
        width_um: float,
        cross_geometry: Any,
        result: dict[str, Any],
        conflict: RouteConflict,
    ) -> tuple[dict[str, Any], RouteConflict | None]:
        """Try configured and automatic planes, preserving any fallback diagnostic."""
        tried_cross = self.cross_block_route_layer == result.get("route_layer")
        if result["routed"] and self.leg_conflict is not None and not tried_cross:
            retry = self.retry_leg_on_cross_layer(i, j, cross_geometry, waypoints)
            if retry is not None:
                fallback = _leg_conflict_reason(retry, self.leg_conflict)
                if fallback is None:
                    return retry, None
                conflict = RouteConflict(
                    f"{conflict.reason}; cross-block fallback {fallback.reason}",
                    conflict.blocking_net or fallback.blocking_net,
                )
            tried_cross = True
        climbed = self.climb_leg(
            i, j, waypoints, width_um, conflict.reason, tried_cross
        )
        return (climbed, None) if climbed is not None else (result, conflict)

    def climb_leg(
        self,
        i: int,
        j: int,
        leg_waypoints_um: list[tuple[float, float]] | None,
        leg_width_um: float,
        reason: str | None,
        tried_cross: bool,
    ) -> dict[str, Any] | None:
        """Automatic metals-stack plane climb (issue #6920): retry the
        rejected leg on each higher plane, first clean win is the leg's
        result. Skipped for a net whose plane the caller named (``auto`` is
        False), for a caller-supplied waypoint path, for a rejection class a
        higher plane cannot lift, and when no per-plane geometry fetcher is
        available (the #1656/#1681 exemptions the climb depends on read
        drawn geometry per plane). The configured ``cross_block_route_layer``
        is skipped only if the preceding route already tried it."""
        if (
            not self.auto
            or leg_waypoints_um is not None
            or not _climbable(reason)
            or self.geometry_for_layer is None
        ):
            return None
        for plane in self.planes_above():
            if tried_cross and plane == self.cross_block_route_layer:
                continue
            retry = self._attempt_on(i, j, plane, leg_width_um)
            if retry is not None:
                return retry
        return None

    def _can_land(self, index: int, plane: tuple[int, int]) -> bool:
        """A climb needs either a same-plane port or a physical via ladder."""
        from .gen_compose_routing import _resolve_via_drop_layer

        pin = self.pins[index]
        port = self.blocks[pin["block"]]["ports"][pin["port"]]
        source = _port_own_layer(port)
        if source is None or self.extraction_deck is None:
            return False
        if source == plane:
            return True
        ladder, _ = _resolve_via_drop_layer(self.extraction_deck, plane, source)
        return bool(ladder)

    def _pad_is_clear(
        self,
        box: Any,
        drop: dict[str, Any],
        layer: tuple[int, int],
        plane: tuple[int, int],
        spacing: float,
    ) -> bool:
        """On the port plane, only metal connected at the declared port may overlap."""
        import klayout.db as kdb

        search = box.enlarged(spacing)
        for block, bbox in self.placed_bboxes_um.items():
            if block != drop["block_id"] and not search.touches(
                kdb.DBox(bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"])
            ):
                continue
            geometry = self.geometry_for_layer(layer)(block)[block]
            if geometry is None:
                continue
            unit = geometry["dbu"]
            pad = kdb.Region(box.to_itype(unit))
            nearby = geometry["region"].interacting(kdb.Region(search.to_itype(unit)))
            # Only the merged source conductor containing the pin owns the pad.
            if (
                block == drop["block_id"]
                and layer != plane
                and layer in drop["terminal_landing_layers"]
            ):
                pin = kdb.Point(round(drop["x_um"] / unit), round(drop["y_um"] / unit))
                nearby = kdb.Region(
                    [p for p in nearby.each_merged() if not p.inside(pin)]
                )
            if not nearby.interacting(pad).is_empty():
                return False
            if (
                spacing
                and not nearby.separation_check(pad, round(spacing / unit)).is_empty()
            ):
                return False
        return True

    def _landings_clear(
        self, drops: list[dict[str, Any]], plane: tuple[int, int]
    ) -> bool:
        """Check the pads as drawn, on every hop including intermediate metals."""
        from .gen_compose_landing_pads import _make_landing_pad_side_um
        from .gen_compose_via_cuts import drawn_landing_box, via_drop_cut_side_um

        if not drops:
            return True
        if self.pdk_variant is None:
            return False
        side = (
            self.landing_pad_side
            or _make_landing_pad_side_um(self.pdk_variant).drawn_side
        )
        dbu = self.route_dbu() if self.route_dbu is not None else 0.001
        for drop in drops:
            via = drop["via_layer"]
            cut = via_drop_cut_side_um(self.pdk_variant, via)
            for layer in drop["landing_layers"]:
                # A top-metal port can also be the source of a downward via.
                if self.pdk_variant == "gf180mcuA" and layer == (42, 0):
                    return False
                box = drawn_landing_box(
                    drop, side(layer, via, drop), cut, dbu
                ).to_dtype(dbu)
                if (
                    self.pdk_variant == "gf180mcuB"
                    and layer == (46, 0)
                    and box.width() < 0.75 - 1e-9
                ):
                    # Native MT.1/MT.4 on the 11K top need 0.44 um width and
                    # 0.5625 um² area; accept a pad that meets both on its own.
                    return False
                rule = _route_spacing_rule(self.pdk_variant, layer)
                if not self._pad_is_clear(
                    box, drop, layer, plane, rule[0] if rule else 0.0
                ):
                    return False
        return True

    def _attempt_on(
        self, i: int, j: int, plane: tuple[int, int], leg_width_um: float
    ) -> dict[str, Any] | None:
        """One full ``route_two_pin()`` pass on ``plane`` plus the
        ``leg_conflict`` gate. The attempt draws at the plane's own deck
        minimum when the leg's width is below it -- the same flooring
        ``routing.cross_block_width_um`` applies to the configured cross
        plane (#1620), so a climb never draws sub-minimum metal on a plane
        whose floor is wider than the primary's."""
        from .gen_compose_routing import route_two_pin

        if not all(self._can_land(index, plane) for index in (i, j)):
            return None
        # The 3LM 30K top requires thick-metal enclosures and a via array.
        # The ordinary one-cut ladder cannot guarantee those rules.
        if self.pdk_variant == "gf180mcuA" and plane == (42, 0):
            return None
        width_um = leg_width_um
        if (
            plane == self.cross_block_route_layer
            and self.cross_block_width_um is not None
        ):
            width_um = self.cross_block_width_um
        if self.pdk_variant is not None:
            floor = _min_width_um_for_layer(self.pdk_variant, plane)
            if floor is not None and width_um < floor[0]:
                width_um = floor[0]
        if _is_gf180_9k_11k_top(self.pdk_variant, plane):
            # The native 9K and 11K options both require 0.44 um (MT.1).
            width_um = max(width_um, 0.44)
        conflict_check = (
            partial(self.width_aware_conflict, candidate_width_um=width_um)
            if self.width_aware_conflict is not None
            else self.leg_conflict
        )
        fetch = self.geometry_for_layer(plane)
        retry = route_two_pin(
            self.pins[i],
            self.pins[j],
            self.blocks,
            self.offsets_um,
            self.placed_bboxes_um,
            width_um,
            plane,
            self.extraction_deck,
            _endpoint_geometry_for(self.pins, self.blocks, i, j, fetch),
            waypoints_um=None,
            leg_conflict=conflict_check,
            block_geometry_for=fetch,
            grid_um=self.grid_um,
        )
        if not retry["routed"] or not self._landings_clear(retry["via_drops"], plane):
            return None
        if conflict_check is not None:
            conflict = conflict_check(
                retry["points_um"],
                retry.get("via_drops", []),
                retry.get("stub_widen", []),
                retry.get("route_layer"),
            )
            if conflict is not None:
                return None
        return retry
