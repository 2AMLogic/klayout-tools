"""`nets[].legs[].blocking_net` — the per-leg blocking-net report (#6961).

Split into its own module (the established `test_gen_compose_*.py` satellite
pattern) rather than extending `test_gen_compose.py`: that file sits exactly
at its file-size-ratchet ceiling, so these regressions live here instead.
Fixtures come from `test_gen_compose.py` the same way
`test_extract_dead_metal_omit.py` reuses `test_extract.py`'s.

What #6961 added: a rejected leg whose rejection names an already-routed net
carries that net's own label as structured data — the exact
`connectivity[].net` string carried alongside the reason from the
route-vs-route conflict check itself, never extracted back out of the
wording. Omitted for successful legs (including a cross-layer retry's
accepted leg, which discards its earlier collision) and for geometry-based
rejections that name no net.
"""

import pytest

from klayout_tools.gen_compose import GenComposeError, compose
from test_gen_compose import _crossing_routes_fixture, _gen_block, _make_install


@pytest.fixture()
def pdk_root(tmp_path):
    # Declared here rather than imported from test_gen_compose: pytest
    # collects fixtures from this module's own namespace, and an imported
    # one reads as an unused name to ruff (the same reason
    # test_extract_dead_metal_omit.py redeclares its own).
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _three_net_crossing_request(tmp_path, pdk_root, output):
    """Six resistor blocks with two vertical nets sharing a waypoint lane."""
    origins = {
        "bw": {"x": 0.0, "y": 15.0},
        "be": {"x": 30.0, "y": 15.0},
        "bn1": {"x": 10.0, "y": 25.0},
        "bs1": {"x": 18.0, "y": 5.0},
        "bn2": {"x": 10.0, "y": 45.0},
        "bs2": {"x": 18.0, "y": -15.0},
    }
    names = list(origins)
    reports = {
        name: _gen_block(tmp_path, pdk_root, "resistor_strip", name, num=1)
        for name in names
    }
    return {
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "blocks": [{"id": name, "generator_report": reports[name]} for name in names],
        "placement": {
            "strategy": "explicit",
            "order": names,
            "origins_um": origins,
        },
        "connectivity": [
            {
                "net": "NET_H",
                "pins": [
                    {"block": "bw", "port": "P2"},
                    {"block": "be", "port": "P1"},
                ],
            },
            {
                "net": "NET_V1",
                "pins": [
                    {"block": "bn1", "port": "P2"},
                    {"block": "bs1", "port": "P1"},
                ],
                "waypoints_um": [[16.0, 25.21], [16.0, 5.21]],
            },
            {
                "net": "NET_V2",
                "pins": [
                    {"block": "bn2", "port": "P2"},
                    {"block": "bs2", "port": "P1"},
                ],
                "waypoints_um": [[16.0, 45.21], [16.0, -15.21]],
            },
        ],
        "routing": {
            "layer_role": "metal",
            "width_um": 0.17,
            "cross_block_layer_role": "metal2",
        },
        "options": {"cell_name": "cross_both_layers", "output": str(output)},
    }, reports


def test_blocking_net_on_the_both_layers_cross_layer_failure(tmp_path, pdk_root):
    # The #1680 both-layers regression (`test_compose_route_vs_route_
    # collision_that_conflicts_on_both_layers_still_fails` in
    # test_gen_compose.py) extended with #6961's structured field: NET_H
    # routes on the primary plane; NET_V1 is rejected there (crosses NET_H)
    # but its cross-layer retry succeeds; NET_V2 is rejected on the primary
    # plane for the same reason and its own cross-layer retry collides with
    # NET_V1's accepted cross-layer backbone -- rejected on both layers, the
    # net stays unrouted with NET_H as the reported blocker (the cross-plane
    # collision only decides the *retry*; it never replaces the reported
    # reason), and NET_V1's successfully retried leg carries no blocker
    # field at all.
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "cross_both_layers.gds"
    )
    report = compose(request)

    nets_by_name = {net["net"]: net for net in report["nets"]}
    assert nets_by_name["NET_H"]["routed"] is True
    assert nets_by_name["NET_V1"]["routed"] is True
    assert nets_by_name["NET_V2"]["routed"] is False
    assert report["unrouted_nets"] == ["NET_V2"]
    blocker_leg = next(
        leg for leg in nets_by_name["NET_V2"]["legs"] if leg["reason"] is not None
    )
    assert "crosses already-routed net" in blocker_leg["reason"]
    assert blocker_leg["blocking_net"] == "NET_H"
    # No routed leg anywhere in the report carries a blocker field --
    # NET_V1's cross-layer-retried leg discarded its earlier collision.
    assert all(
        "blocking_net" not in leg
        for net in report["nets"]
        for leg in net["legs"]
        if leg["routed"]
    )


@pytest.mark.parametrize("pin_plane", [False, True])
def test_blocking_net_follows_the_accepted_net_exactly(tmp_path, pdk_root, pin_plane):
    # #6961: reversing connectivity[] order flips which net "wins" (see
    # test_compose_route_vs_route_collision_is_order_dependent in
    # test_gen_compose.py) -- the rejected leg's blocking_net must follow the
    # *accepted* net, and it must carry the exact net label even when that
    # label contains punctuation an extraction of it from the reason prose
    # would mangle (a quote would truncate a `'([^']+)'` regex match; '/',
    # '[', ']' and '#' would survive it -- together they prove the field is
    # the label itself, never parsed back out of the message).
    request = _crossing_routes_fixture(tmp_path, pdk_root)
    if pin_plane:
        request["connectivity"][0]["layer_role"] = "metal"
    request["connectivity"][0]["net"] = "h.rail#2"  # NET_H
    request["connectivity"][1]["net"] = "v/bus[1]'"  # NET_V
    request["connectivity"] = list(reversed(request["connectivity"]))
    request["options"] = {
        "cell_name": "cross_blocker_exact",
        "output": str(tmp_path / "cross_blocker_exact.gds"),
    }
    report = compose(request)

    nets_by_name = {net["net"]: net for net in report["nets"]}
    assert nets_by_name["v/bus[1]'"]["routed"] is True
    if not pin_plane:
        assert report["unrouted_nets"] == []
        assert nets_by_name["h.rail#2"]["routed"] is True
        assert all(
            "blocking_net" not in leg for leg in nets_by_name["h.rail#2"]["legs"]
        )
        return
    assert nets_by_name["h.rail#2"]["routed"] is False
    assert report["unrouted_nets"] == ["h.rail#2"]
    blocker_leg = next(
        leg for leg in nets_by_name["h.rail#2"]["legs"] if leg["reason"] is not None
    )
    assert blocker_leg["blocking_net"] == "v/bus[1]'"


def test_blocking_net_per_leg_on_a_partially_routed_net(tmp_path, pdk_root):
    # #6961: a partially-routed multi-pin net reports blocking_net on each
    # rejected leg independently. NET_H routes first across the row; a 3-pin
    # NET_V has one leg wholly above NET_H's channel (routed) and two legs
    # that must drop through it to reach the south block (both rejected
    # against NET_H's accepted backbone) -- the net comes back `partial`
    # with the accepted leg carrying no blocking_net and each rejected leg
    # naming NET_H.
    bw = _gen_block(tmp_path, pdk_root, "resistor_strip", "bw", num=1)
    be = _gen_block(tmp_path, pdk_root, "resistor_strip", "be", num=1)
    bn = _gen_block(tmp_path, pdk_root, "resistor_strip", "bn", num=1)
    bn2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "bn2", num=1)
    bs = _gen_block(tmp_path, pdk_root, "resistor_strip", "bs", num=1)
    output = tmp_path / "cross_partial.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "bw", "generator_report": bw},
                {"id": "be", "generator_report": be},
                {"id": "bn", "generator_report": bn},
                {"id": "bn2", "generator_report": bn2},
                {"id": "bs", "generator_report": bs},
            ],
            "placement": {
                "strategy": "explicit",
                "order": ["bw", "be", "bn", "bn2", "bs"],
                "origins_um": {
                    "bw": {"x": 0.0, "y": 15.0},
                    "be": {"x": 30.0, "y": 15.0},
                    "bn": {"x": 10.0, "y": 25.0},
                    "bn2": {"x": 10.0, "y": 45.0},
                    "bs": {"x": 18.0, "y": 5.0},
                },
            },
            "connectivity": [
                {
                    "net": "NET_H",
                    "pins": [
                        {"block": "bw", "port": "P2"},
                        {"block": "be", "port": "P1"},
                    ],
                },
                {
                    "net": "NET_V",
                    "layer_role": "metal",  # Keep the tested single-plane rejection.
                    "pins": [
                        {"block": "bn2", "port": "P2"},
                        {"block": "bn", "port": "P2"},
                        {"block": "bs", "port": "P1"},
                    ],
                },
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "cross_partial", "output": str(output)},
        }
    )

    nets_by_name = {net["net"]: net for net in report["nets"]}
    assert nets_by_name["NET_H"]["routed"] is True
    net_v = nets_by_name["NET_V"]
    assert net_v["routed"] is False
    assert net_v["status"] == "partial"
    assert report["unrouted_nets"] == ["NET_V"]
    rejected = [leg for leg in net_v["legs"] if not leg["routed"]]
    accepted = [leg for leg in net_v["legs"] if leg["routed"]]
    assert rejected and accepted  # some metal drawn, some legs not
    # Each rejected leg names NET_H on its own -- the field is per leg, not
    # per net.
    assert {leg["blocking_net"] for leg in rejected} == {"NET_H"}
    assert all(
        "crosses already-routed net 'NET_H'" in leg["reason"] for leg in rejected
    )
    # The accepted leg (wholly north of NET_H's channel) conflicts with
    # nothing: no blocker field, like every successful leg.
    assert all("blocking_net" not in leg for leg in accepted)
    assert all("blocking_net" not in leg for leg in nets_by_name["NET_H"]["legs"])


def test_blocking_net_omitted_on_geometry_only_rejections(tmp_path, pdk_root):
    # #6961: blocking_net is reserved for a rejection that names an
    # already-routed *net*. A leg rejected by block geometry alone -- the
    # same shape test_cli_gen_compose_partial_success_exit_3 uses
    # (b1.P1 reachable neither across b1's own body nor around it) -- must
    # omit the field entirely, on every leg, while the exit-3 report shape
    # (unrouted_nets, status) stays exactly as before.
    b1 = _gen_block(tmp_path, pdk_root, "resistor_strip", "b1")
    b2 = _gen_block(tmp_path, pdk_root, "resistor_strip", "b2")
    output = tmp_path / "geometry_only.gds"
    report = compose(
        {
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "blocks": [
                {"id": "b1", "generator_report": b1},
                {"id": "b2", "generator_report": b2},
            ],
            "placement": {
                "strategy": "row",
                "order": ["b1", "b2"],
                "spacing_um": 1.0,
            },
            "connectivity": [
                {
                    "net": "BUS",
                    "layer_role": "metal",  # Keep the tested single-plane rejection.
                    "pins": [
                        {"block": "b1", "port": "P1"},
                        {"block": "b1", "port": "P2"},
                        {"block": "b2", "port": "P1"},
                    ],
                }
            ],
            "routing": {"layer_role": "metal", "width_um": 0.17},
            "options": {"cell_name": "geometry_only", "output": str(output)},
        }
    )
    assert report["unrouted_nets"] == ["BUS"]
    bus = next(net for net in report["nets"] if net["net"] == "BUS")
    assert bus["routed"] is False
    assert bus["status"] == "partial"  # b1.P2 -> b2.P1 draws; the P1 legs don't
    rejected = [leg for leg in bus["legs"] if not leg["routed"]]
    assert rejected  # the rejected legs fail against b1's own geometry alone
    assert all(leg["reason"] is not None for leg in rejected)
    assert all("blocking_net" not in leg for leg in rejected)


def test_suggestion_rejects_ambiguous_replay_names(tmp_path, pdk_root):
    output = tmp_path / "repeated.gds"
    request, _ = _three_net_crossing_request(tmp_path, pdk_root, output)
    h, _, v2 = request["connectivity"]
    h["net"] = v2["net"]
    request["routing"]["suggest_layers"] = True
    with pytest.raises(GenComposeError, match="unique.*repeated net: 'NET_V2'"):
        compose(request)
    assert not output.exists()


def test_disabled_suggestions_preserve_declare_only(tmp_path, pdk_root):
    request, _ = _three_net_crossing_request(
        tmp_path, pdk_root, tmp_path / "declare.gds"
    )
    del request["routing"]
    original = compose(request)
    request["routing"] = {"suggest_layers": False}
    assert compose(request) == original
