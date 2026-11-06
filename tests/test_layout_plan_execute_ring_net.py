"""Tests for the group-level tap/ring ``net`` on ``guard_ring`` device groups
(#5095). Split out of `test_layout_plan_execute.py` to keep it under its
file-size ceiling.
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import execute_layout_plan_document
from test_layout_plan_execute import (
    _GATE_RAIL_NETLIST,
    _GATE_RAIL_NETLIST_WITH_UNMAPPED_BULK,
    _gate_rail_request,
    _make_install,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    return root


def _ring_request(tmp_path, pdk_root, ring_net, generator="guard_ring"):
    request = _gate_rail_request(
        tmp_path, pdk_root, _GATE_RAIL_NETLIST_WITH_UNMAPPED_BULK
    )
    ring = {"id": "ring", "devices": [], "generator": generator}
    if ring_net is not None:
        ring["net"] = ring_net
    request["device_groups"].append(ring)
    request["rows"] = [
        {"order": ["b1", "b2", "b3"], "spacing_um": 2.0, "align": "bottom"},
        {"order": ["ring"], "spacing_um": 2.0, "align": "bottom"},
    ]
    return request


def test_guard_ring_net_binds_ring_port_and_clears_unmapped(tmp_path, pdk_root):
    # VNW (M3's bulk) is unmapped without a tap; a ring tied to VNW gives it
    # one pin. A single-pin net is skipped silently, so it leaves
    # unmapped_netlist_nets[] without becoming a connectivity entry.
    request = _ring_request(tmp_path, pdk_root, "VNW")
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert "VNW" not in response["unmapped_netlist_nets"]


def test_guard_ring_net_joins_existing_net_pins(tmp_path, pdk_root):
    # D1 is reached by device terminals; the ring pin merges into the set.
    request = _ring_request(tmp_path, pdk_root, "D1")
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    d1 = next(n for n in response["nets"] if n["net"] == "D1")
    blocks = {p["block"] for p in d1["pins"]}
    assert "ring" in blocks and {"b1", "b2"} <= blocks


def test_guard_ring_unknown_net_is_application_error(tmp_path, pdk_root):
    from klayout_tools.layout_plan import LayoutPlanError

    request = _ring_request(tmp_path, pdk_root, "NOPE")
    with pytest.raises(LayoutPlanError, match="ring.*NOPE"):
        execute_layout_plan_document(request, request_dir=str(tmp_path))


def test_net_on_non_guard_ring_is_application_error(tmp_path, pdk_root):
    from klayout_tools.layout_plan import LayoutPlanError

    request = _gate_rail_request(tmp_path, pdk_root, _GATE_RAIL_NETLIST)
    request["device_groups"][0]["net"] = "D1"
    with pytest.raises(LayoutPlanError, match="only supported on a 'guard_ring'"):
        execute_layout_plan_document(request, request_dir=str(tmp_path))
