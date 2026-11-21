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


def _same_facing_row_fixture():
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
    bboxes = {
        "a": {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 10.0},
        "b": {"x0": 24.0, "y0": 0.0, "x1": 34.0, "y1": 10.0},
        "m": {"x0": 13.0, "y0": 0.0, "x1": 21.0, "y1": 10.0},
    }
    offsets = {block_id: {"x": 0.0, "y": 0.0} for block_id in blocks}
    pins = [{"block": "a", "port": "Y"}, {"block": "b", "port": "A"}]
    return blocks, offsets, bboxes, pins


def _geometry_for_layer(bboxes):
    """``geometry_for_layer(layer)``-shaped factory: every block draws its own
    bbox on li1 (so the primary-plane obstacle/far-side checks fire against
    real drawn metal) and nothing on any higher plane (so the #1656/#1681
    exemptions hold there, exactly as the #6920 row's generators do)."""

    def factory(layer):
        cache: dict[str, dict | None] = {}

        def fetch(block_id, _cache=cache, _layer=layer):
            if block_id in _cache:
                return _cache
            if _layer == LI1:
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


def _climb_bundle(fixture, **overrides):
    blocks, offsets, bboxes, pins = fixture
    kwargs = dict(
        block_geometry_for=_geometry_for_layer(bboxes)(LI1),
        geometry_for_layer=_geometry_for_layer(bboxes),
        extraction_deck=get_extraction_deck("sky130"),
        pdk_variant="sky130A",
    )
    kwargs.update(overrides)
    return gen_compose.route_bundle(pins, blocks, offsets, bboxes, 0.17, LI1, **kwargs)


def test_rejected_leg_climbs_to_the_next_metals_plane():
    # On li1 the same-facing pair is unroutable (the backbone reaches b's pin
    # from behind, #1895, and/or crosses m's drawn metal). The climb retries
    # the leg on met1, where neither block draws anything, and the net routes.
    result = _climb_bundle(_same_facing_row_fixture())
    assert result["routed"] is True, result["reason"]
    drawn = [leg for leg in result["legs"] if leg["routed"]]
    assert len(drawn) == 1
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
