"""Tests for the netlist-port pin labels and unrouted reasons that
`klayout_tools.layout_plan_execute` reports (#4966). Split out of
`test_layout_plan_execute.py` to keep it under its file-size ceiling.
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import execute_layout_plan_document
from test_layout_plan_execute import (
    _GATE_RAIL_NETLIST,
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


def test_execute_promotes_subckt_ports_to_labelled_pins(tmp_path, pdk_root):
    # #4966: every subckt port net becomes a labelled top-level pin so `klt
    # lvs` has ports to compare; D1 is also router-wired, so it is promoted
    # without a duplicate label (compose's router_labelled handling).
    request = _gate_rail_request(tmp_path, pdk_root, _GATE_RAIL_NETLIST)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    pins_by_net = {pin["net"]: pin for pin in response["pins"]}
    assert set(pins_by_net) == {"VBIAS", "D1", "D2", "D3"}
    assert all(pin["labelled"] for pin in pins_by_net.values())
    assert response["unrouted_reasons"] == []


def test_execute_surfaces_unrouted_reasons(tmp_path, pdk_root):
    # #4966: a net in `unrouted_nets[]` carries its compose() reason.
    netlist = """
.subckt gate_rail VBIAS D1 D2 D3
M1 D1 VBIAS VSS VSS nfet L=0.28U W=0.42U
M2 D2 VBIAS VSS VSS nfet L=0.28U W=0.42U
M3 D3 VBIAS VSS VSS nfet L=0.28U W=0.42U
.ends
"""
    request = _gate_rail_request(tmp_path, pdk_root, netlist)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert "VSS" in response["unrouted_nets"]
    reasons = {r["net"]: r["reason"] for r in response["unrouted_reasons"]}
    assert set(reasons) == set(response["unrouted_nets"])
    assert reasons["VSS"]


def test_execute_warns_for_subckt_port_without_mapped_device_port(tmp_path, pdk_root):
    # Port VX touches only M3's bulk (deliberately unmapped): a warning, no
    # label, no crash.
    netlist = """
.subckt gate_rail VBIAS D1 D2 D3 VX
M1 D1 VBIAS S1 S1 nfet L=0.28U W=0.42U
M2 D2 VBIAS D1 D1 nfet L=0.28U W=0.42U
M3 D3 VBIAS S3 VX nfet L=0.28U W=0.42U
.ends
"""
    request = _gate_rail_request(tmp_path, pdk_root, netlist)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert any("netlist port 'VX'" in w for w in response["warnings"])
    assert "VX" not in {pin["net"] for pin in response["pins"]}
