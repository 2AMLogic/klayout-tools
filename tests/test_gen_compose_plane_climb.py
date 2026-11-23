"""Automatic metals-stack plane climb for rejected routing legs (#6920).

A leg the fixed one-jog backbone cannot route on its own plane -- most
visibly a same-facing port pair in a row placement, where the one-jog shape
reaches a pin from behind (#1895) -- is retried, plane by plane up the
resolved PDK's own ``metals`` stack, by ``route_bundle()`` before the net is
reported unroutable. These tests drive ``route_bundle()`` directly over
hand-built blocks whose per-plane drawn geometry is faked through
``geometry_for_layer`` (the same lazy-fetcher contract ``compose()`` wires
in): the blocks draw their own bbox on li1 and nothing on any higher metal,
which is exactly the sky130 mixed-device row of #6920's reproduction.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools import gen_compose
from klayout_tools.decks import get_extraction_deck

LI1 = (67, 20)
MET1 = (68, 20)
DBU = 0.001


def _same_facing_row_fixture(port_layer=LI1):
    """Three blocks in a row; the two port blocks carry east-facing ports on
    their *east* edges -- a same-facing pair, the one shape
    ``manhattan_backbone`` documents it can never route (the connecting run
    reaches b's pin from behind, #1895, and the #1167 lane detour is scoped
    out for a leg's own-pin crossings), plus a middle obstacle block.
    """
    blocks = {
        "a": {
            "id": "a",
            "port_names": {"Y"},
            "ports": {"Y": {"x_um": 10.0, "y_um": 5.0, "direction_deg": 0}},
        },
        "b": {
            "id": "b",
            "port_names": {"A"},
            "ports": {"A": {"x_um": 34.0, "y_um": 5.0, "direction_deg": 0}},
        },
        "m": {"id": "m", "port_names": set(), "ports": {}},
    }
    for block_id, port_name in (("a", "Y"), ("b", "A")):
        blocks[block_id]["ports"][port_name]["layer"] = {
            "layer": port_layer[0],
            "datatype": port_layer[1],
        }
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "b": {"x0": 24.0, "y0": 0.0, "x1": 34.0, "y1": 10.0},
        "m": {"x0": 13.0, "y0": 0.0, "x1": 21.0, "y1": 10.0},
    }
    offsets = {block_id: {"x": 0.0, "y": 0.0} for block_id in blocks}
    pins = [{"block": "a", "port": "Y"}, {"block": "b", "port": "A"}]
    return blocks, offsets, bboxes, pins


def _geometry_for_layer(bboxes, occupied=(LI1,)):
    """``geometry_for_layer(layer)``-shaped factory: every block draws its own
    bbox on li1 (so the primary-plane obstacle/far-side checks fire against
    real drawn metal) and nothing on any higher plane (so the #1656/#1681
    exemptions hold there, exactly as the #6920 row's generators do)."""

    def factory(layer):
        cache: dict[str, dict | None] = {}

        def fetch(block_id, _cache=cache, _layer=layer):
            if block_id in _cache:
                return _cache
            if _layer in occupied:
                bb = bboxes[block_id]
                _cache[block_id] = {
                    "region": kdb.Region(
                        kdb.Box(
                            int(bb["x0"] / DBU),
                            int(bb["y0"] / DBU),
                            int(bb["x1"] / DBU),
                            int(bb["y1"] / DBU),
                        )
                    ),
                    "dbu": DBU,
                }
            else:
                _cache[block_id] = None
            return _cache

        return fetch

    return factory


def _climb_bundle(fixture, route_layer=LI1, **overrides):
    blocks, offsets, bboxes, pins = fixture
    kwargs = dict(
        block_geometry_for=_geometry_for_layer(bboxes)(LI1),
        geometry_for_layer=_geometry_for_layer(bboxes),
        extraction_deck=get_extraction_deck("sky130"),
        pdk_variant="sky130A",
    )
    kwargs.update(overrides)
    return gen_compose.route_bundle(
        pins, blocks, offsets, bboxes, 0.17, route_layer, **kwargs
    )


def test_rejected_leg_climbs_to_the_next_metals_plane():
    # On li1 the same-facing pair is unroutable (the backbone reaches b's pin
    # from behind, #1895, and/or crosses m's drawn metal). The climb retries
    # the leg on met1, where neither block draws anything, and the net routes.
    result = _climb_bundle(_same_facing_row_fixture())
    assert result["routed"] is True, result["reason"]
    drawn = [leg for leg in result["legs"] if leg["routed"]]
    assert len(drawn) == 1
    assert {drop["block_id"] for drop in drawn[0]["via_drops"]} == {"a", "b"}
    assert all(tuple(drop["via_layer"]) == (67, 44) for drop in drawn[0]["via_drops"])
    assert drawn[0]["route_layer"] == MET1
    assert drawn[0]["width_um"] == pytest.approx(0.17)  # >= met1's own floor


def test_climb_is_disabled_when_the_net_named_its_own_plane():
    # The #1655 "explicit wins" rule: a net (or caller) that pinned its own
    # plane gets no automatic climb -- auto_plane_climb=False keeps the
    # pre-#6920 single-plane verdict.
    result = _climb_bundle(_same_facing_row_fixture(), auto_plane_climb=False)
    assert result["routed"] is False
    rejected = [leg for leg in result["legs"] if not leg["routed"]]
    assert rejected
    assert "reaches the pin from behind" in rejected[0]["reason"]


def test_climbable_reason_classes():
    # #6912: the self-net pad-crossing classes (#433/#453/#1527 -- each
    # names the met2/via escape in its own remedy text and is layer-guarded
    # at the point of the check) and the guard/collector-ring classes
    # (#199 case 2 / #434 -- plane-conditional since #1960, so the climbed
    # retry re-proves no shared conductor via `_leg_plane_clears_ring`) are
    # climbable; the classes that stay a property of the pin pair (a
    # too-narrow channel, an unresolvable via drop, a gapped ring that
    # reports too few side ports to check at all) are not.
    from klayout_tools.gen_compose_plane_climb import _climbable

    climbable = (
        # #433 self-net pad footprint
        "self-net backbone crosses 1.545um through block 'p1''s own port "
        "'U0_D' on the same drawing layer -- bussing this net across the "
        "block would draw a silent short to that pad",
        # #453 degenerate same-facing jog
        "self-net between two same-facing ports on the same row jogs "
        "directly over block 'a''s own port 'U0_S'",
        # #453/#469 drawn-metal overlap
        "self-net's drawn 0.17um metal overlaps 0.3um^2 of block 'a''s own "
        "drawn pad metal on the route layer",
        # #1527 own-block escape
        "leg's drawn 0.17um metal overlaps 0.2um^2 of block 'c''s own drawn "
        "geometry on the route layer",
        # #199 case 2 closed ring
        "block 'p1' has a closed guard/collector ring (reports a TAP_*/"
        "COLL_* port and no GAP_* opening) -- a route to its non-tap port "
        "'U0_D' would cross the ring's own metal loop",
        # #434 opening-crossing family (no opening / outside / too close /
        # runs-along all share the "block 'X''s ring on its <side>" stem)
        "backbone crosses block 'p3''s ring on its E side, which declares "
        "no opening -- the route would merge this net with the ring's tap "
        "net",
        "backbone runs along block 'p3''s ring on its N side -- a route "
        "laid on the ring's own metal merges this net with the ring's tap "
        "net",
        "backbone crosses block 'p3''s ring on its E side at 1.87um, "
        "outside the [1.2, 2.2]um opening it declares",
    )
    for reason in climbable:
        assert _climbable(reason), reason

    not_climbable = (
        "channel between blocks 'a' and 'b' is 0.12um wide, less than the "
        "route's own 0.34um width",
        "block 'p1' declares a ring opening but reports no port locating "
        "its ring's N/S side(s) -- the route cannot be shown to pass "
        "through the opening",
        "no via declared between li1 and met1 for the via drop",
        None,
        "",
    )
    for reason in not_climbable:
        assert not _climbable(reason), reason


def test_climb_width_floors_up_to_the_planes_own_deck_minimum():
    # met3's curated deck floor (0.3um on sky130) is wider than the request's
    # 0.17um: a leg that has to climb that high draws at the plane's own
    # minimum, never below it. Force the depth by rejecting li1, met1 and
    # met2 through leg_conflict (a simulated collision on those planes only).
    _, _, bboxes, _ = _same_facing_row_fixture()

    def conflict(points_um, via_drops=None, stub_widen=None, layer=None):
        if layer in (LI1, MET1, (69, 20)):
            return "simulated collision with an already-routed net"
        return None

    result = _climb_bundle(_same_facing_row_fixture(), leg_conflict=conflict)
    assert result["routed"] is True, result["reason"]
    drawn = [leg for leg in result["legs"] if leg["routed"]][0]
    assert drawn["route_layer"] == (70, 20)  # met3: first plane past all three
    assert drawn["width_um"] >= 0.3


@pytest.mark.parametrize("missing", ["a", "b", "both"])
def test_automatic_climb_requires_a_layer_at_each_endpoint(missing):
    fixture = _same_facing_row_fixture()
    for block_id, port in (("a", "Y"), ("b", "A")):
        if missing in (block_id, "both"):
            del fixture[0][block_id]["ports"][port]["layer"]
    assert not _climb_bundle(fixture)["routed"]


@pytest.mark.parametrize("variant,depth", [("gf180mcuA", 3), ("gf180mcuB", 4)])
def test_gf180_climb_stops_at_the_selected_variant_upper_metal(variant, depth):
    deck = get_extraction_deck("gf180mcu")
    first = deck.metals[0]
    fixture = _same_facing_row_fixture(first)
    # Occupy each intermediate plane but leave the port landing clear.
    # A solid upper-metal polygon under the via is an unrelated conductor.
    inset = {name: {**box, "x1": box["x1"] - 1.5} for name, box in fixture[2].items()}
    geometry = _geometry_for_layer(inset, deck.metals[:depth])

    def route(selected):
        return _climb_bundle(
            fixture,
            first,
            pdk_variant=selected,
            extraction_deck=deck,
            block_geometry_for=geometry(first),
            geometry_for_layer=geometry,
        )

    assert not route(variant)["routed"]
    full_stack = route("gf180mcuD")
    assert full_stack["routed"]
    assert full_stack["legs"][0]["route_layer"] == deck.metals[depth]


def test_automatic_climb_tries_configured_plane_after_primary_fails():
    result = _climb_bundle(_same_facing_row_fixture(), cross_block_route_layer=MET1)
    assert result["routed"]
    assert result["legs"][0]["route_layer"] == MET1


def test_spacing_rejection_climbs_and_uses_actual_width_for_parallel_net():
    from klayout_tools.gen_compose_pad_conflicts import _leg_conflict_message
    from klayout_tools.gen_compose_routing import _drawn_leg_footprint_region

    met3 = (70, 20)
    fixture = _same_facing_row_fixture()
    fixture[0]["b"]["ports"]["A"]["direction_deg"] = 180
    fixture[0]["b"]["ports"]["A"]["x_um"] = 24.0
    # A 0.30 um-wide metal3 route centred 0.55 um above the candidate.
    neighbour = kdb.Region(kdb.Box(14000, 5400, 19000, 5700))
    rejected_widths = []

    def spacing(points, drops, stub, layer, *, candidate_width_um=0.17):
        if layer not in (met3, (71, 20)):
            return "comes within 0.17um of already-routed net 'PRE'"
        if layer != met3:
            return None
        region = _drawn_leg_footprint_region(
            points, candidate_width_um, drops, stub, DBU, layer
        )
        reason = _leg_conflict_message(
            "sky130A",
            [],
            layer,
            "PRE",
            region,
            neighbour,
            DBU,
            None,
            lambda _: (0.30, "met3.space.1"),
            True,
        )
        if reason is not None:
            rejected_widths.append(candidate_width_um)
        return reason

    empty = _geometry_for_layer(fixture[2], ())
    result = _climb_bundle(
        fixture,
        block_geometry_for=empty(LI1),
        geometry_for_layer=empty,
        leg_conflict=spacing,
        width_aware_conflict=spacing,
    )
    assert result["routed"], result["reason"]
    assert rejected_widths and set(rejected_widths) == {0.30}
    assert result["legs"][0]["route_layer"] == (71, 20)
