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
