"""Tests for `request.routing.nets` per-net routing hints (issue #5094);
see `klayout_tools.layout_plan_routing`. Split from
`test_layout_plan_execute.py` to keep that file under its size ratchet."""

from __future__ import annotations

import pytest

import test_layout_plan_execute as base
from klayout_tools.layout_plan_execute import (
    LayoutPlanExecuteError,
    execute_layout_plan_document,
)

# Re-bind the base module's fixtures/helper so pytest collects them here.
_isolate = base._isolate
pdk_root = base.pdk_root
_bjt_bus_request = base._bjt_bus_request


@pytest.mark.parametrize(
    "nets, match",
    [
        (["EBUS"], "must be a JSON object"),
        ({"EBUS": "metal2"}, "must be a JSON object"),
        ({"EBUS": {"bogus": 1}}, "unknown field"),
        ({"EBUS": {}}, "at least one"),
        ({"EBUS": {"layer_role": ""}}, "layer_role must be a non-empty string"),
        ({"EBUS": {"width_um": 0}}, "width_um must be a positive number"),
        ({"EBUS": {"width_um": True}}, "width_um must be a positive number"),
    ],
)
def test_resolve_routing_spec_rejects_bad_nets(tmp_path, pdk_root, nets, match):
    request = _bjt_bus_request(tmp_path, pdk_root, {"nets": nets})
    with pytest.raises(LayoutPlanExecuteError, match=match):
        execute_layout_plan_document(request, request_dir=str(tmp_path))


def test_apply_net_routing_copies_overrides_and_warns_on_unmatched():
    from klayout_tools.layout_plan_routing import apply_net_routing

    connectivity = [
        {"net": "A", "pins": ["x", "y"]},
        {"net": "B", "pins": ["x", "y", "z"]},
    ]
    warnings = apply_net_routing(
        connectivity,
        {
            "A": {"layer_role": "metal2"},
            "B": {"width_um": 0.5},
            "NOPE": {"width_um": 1.0},
        },
    )
    assert connectivity[0] == {"net": "A", "pins": ["x", "y"], "layer_role": "metal2"}
    assert connectivity[1] == {"net": "B", "pins": ["x", "y", "z"], "width_um": 0.5}
    assert len(warnings) == 1 and "NOPE" in warnings[0]


def test_execute_routing_nets_forwarded_and_not_in_compose_routing(
    tmp_path,
    pdk_root,
    monkeypatch,
):
    from klayout_tools import layout_plan_execute as mod

    seen = {}
    real_compose = mod.compose

    def spy(compose_request, **kwargs):
        seen["request"] = compose_request
        return real_compose(compose_request, **kwargs)

    monkeypatch.setattr(mod, "compose", spy)
    request = _bjt_bus_request(
        tmp_path,
        pdk_root,
        {
            "layer_role": "metal",
            "width_um": 0.17,
            "cross_block_layer_role": "metal2",
            "nets": {"EBUS": {"width_um": 0.2}, "MISSING": {"layer_role": "metal2"}},
        },
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert "nets" not in seen["request"]["routing"]
    by_net = {c["net"]: c for c in seen["request"]["connectivity"]}
    assert by_net["EBUS"]["width_um"] == 0.2
    assert any("MISSING" in w for w in response["warnings"])
    for net, entry in by_net.items():
        if net != "EBUS":
            assert "width_um" not in entry and "layer_role" not in entry


def test_resolve_net_routing_accepts_waypoints_incl_plane_bearing():
    from klayout_tools.layout_plan_routing import resolve_net_routing

    wps = [[1, 2], [3.5, 4, "metal3"]]
    out = resolve_net_routing({"A": {"waypoints_um": wps}}, ValueError)
    assert out == {"A": {"waypoints_um": wps}}


@pytest.mark.parametrize(
    "bad",
    [[], "x", [[1]], [[1, "a"]], [[1, 2, ""]], None],
)
def test_resolve_net_routing_rejects_bad_waypoints(bad):
    from klayout_tools.layout_plan_routing import resolve_net_routing

    with pytest.raises(
        ValueError, match=r"request\.routing\.nets\['A'\]\.waypoints_um"
    ):
        resolve_net_routing({"A": {"waypoints_um": bad}}, ValueError)


def test_empty_net_entry_message_lists_waypoints():
    from klayout_tools.layout_plan_routing import resolve_net_routing

    with pytest.raises(ValueError, match="waypoints_um"):
        resolve_net_routing({"A": {}}, ValueError)


def test_apply_net_routing_forwards_waypoints():
    from klayout_tools.layout_plan_routing import apply_net_routing

    connectivity = [{"net": "A", "pins": ["x", "y"]}]
    wps = [[1, 2]]
    apply_net_routing(connectivity, {"A": {"waypoints_um": wps}})
    assert connectivity[0]["waypoints_um"] == wps


_PINS = [
    {"block": "b1", "port": "U0_G"},
    {"block": "b2", "port": "U0_G"},
    {"block": "b3", "port": "U0_G"},
]


def _leg(a=0, b=2, **extra):
    return {"from_pin": _PINS[a], "to_pin": _PINS[b], **extra}


def test_resolve_net_routing_accepts_legs_and_rejects_bad_shape():
    from klayout_tools.layout_plan_routing import resolve_net_routing

    legs = [_leg()]
    assert resolve_net_routing({"A": {"legs": legs}}, ValueError) == {
        "A": {"legs": legs}
    }
    for bad in ([], "x", None):
        with pytest.raises(ValueError, match=r"nets\['A'\]\.legs must be a non-empty"):
            resolve_net_routing({"A": {"legs": bad}}, ValueError)


def test_legs_and_waypoints_um_together_is_an_error():
    from klayout_tools.layout_plan_routing import resolve_net_routing

    with pytest.raises(ValueError, match="cannot set both"):
        resolve_net_routing(
            {"A": {"legs": [_leg()], "waypoints_um": [[1, 2]]}}, ValueError
        )


@pytest.mark.parametrize(
    "legs, match",
    [
        ([_leg(a=0, b=0)], "different pins"),
        (
            [{"from_pin": _PINS[0], "to_pin": {"block": "b9", "port": "U0_G"}}],
            "own pins",
        ),
        ([_leg(bogus=1)], "unrecognized key"),
    ],
)
def test_apply_net_routing_validates_legs_against_entry_pins(legs, match):
    from klayout_tools.layout_plan_routing import apply_net_routing

    connectivity = [{"net": "A", "pins": list(_PINS)}]
    with pytest.raises(ValueError, match=match) as excinfo:
        apply_net_routing(connectivity, {"A": {"legs": legs}})
    assert "request.routing.nets['A'].legs[0]" in str(excinfo.value)
    assert "legs" not in connectivity[0]


def test_apply_net_routing_forwards_legs_on_two_pin_net():
    from klayout_tools.layout_plan_routing import apply_net_routing

    connectivity = [{"net": "A", "pins": _PINS[:2]}]
    legs = [_leg(a=0, b=1, waypoints_um=[[1, 2]])]
    apply_net_routing(connectivity, {"A": {"legs": legs}})
    assert connectivity[0]["legs"] == legs


def test_execute_routes_three_pin_net_with_explicit_leg(
    tmp_path, pdk_root, monkeypatch
):
    from klayout_tools import layout_plan_execute as mod

    seen = {}
    real_compose = mod.compose

    def spy(compose_request, **kwargs):
        seen["request"] = compose_request
        return real_compose(compose_request, **kwargs)

    monkeypatch.setattr(mod, "compose", spy)
    request = base._gate_rail_request(tmp_path, pdk_root, base._GATE_RAIL_NETLIST)
    legs = [_leg(a=0, b=2)]
    request["routing"]["nets"] = {"VBIAS": {"legs": legs}}
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    by_net = {c["net"]: c for c in seen["request"]["connectivity"]}
    assert by_net["VBIAS"]["legs"] == legs
    # The named b1<->b3 leg is seeded into the spanning tree, so the routed
    # net contains exactly that pin pair (the unconstrained run joins
    # adjacent b1-b2 / b2-b3 instead).
    vbias = next(n for n in response["nets"] if n["net"] == "VBIAS")
    pairs = {frozenset(p["block"] for p in leg["pins"]) for leg in vbias["legs"]}
    assert frozenset({"b1", "b3"}) in pairs


def test_execute_rejects_legs_naming_pin_outside_net(tmp_path, pdk_root):
    request = base._gate_rail_request(tmp_path, pdk_root, base._GATE_RAIL_NETLIST)
    request["routing"]["nets"] = {
        "VBIAS": {
            "legs": [_leg(a=0, b=1, **{"to_pin": {"block": "b1", "port": "U0_S"}})]
        }
    }
    with pytest.raises(
        LayoutPlanExecuteError, match=r"request\.routing\.nets\['VBIAS'\]\.legs"
    ):
        execute_layout_plan_document(request, request_dir=str(tmp_path))
