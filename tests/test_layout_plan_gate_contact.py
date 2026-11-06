"""``gate_contact`` derivation from gate-net fan-out in ``layout-plan
execute`` (issue #5093)."""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import execute_layout_plan_document


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


_GATE_PORT_NETLIST = """
.subckt gc D G
M1 D G VSS VSS nfet L=0.5U W=2U
.ends
"""

_GATE_INTERNAL_SINGLE_NETLIST = """
.subckt gc D S
M1 D g1 S S nfet L=0.5U W=2U
.ends
"""

_GATE_RAIL_NETLIST = """
.subckt gc VBIAS D1 D2 D3
M1 D1 VBIAS S1 S1 nfet L=0.28U W=0.42U
M2 D2 VBIAS D1 D1 nfet L=0.28U W=0.42U
M3 D3 VBIAS S3 S3 nfet L=0.28U W=0.42U
.ends
"""


def _request(tmp_path, pdk_root, netlist_text, ids, params):
    netlist_path = tmp_path / "gc.spice"
    netlist_path.write_text(netlist_text)
    return {
        "netlist": {"path": str(netlist_path), "top": "gc"},
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": [
            {
                "id": f"m{i}",
                "devices": [str(i)],
                "generator": "mos_array",
                "topology": "array",
                "params": {"rows": 1, "cols": 1, "dummy": 0, **params},
            }
            for i in ids
        ],
        "rows": [
            {"order": [f"m{i}" for i in ids], "spacing_um": 2.0, "align": "bottom"}
        ],
        "routing": {"layer_role": "metal", "width_um": 0.42},
        "options": {"cell_name": "gc_0", "output": str(tmp_path / "gc_0.gds")},
    }


def _run(tmp_path, pdk_root, netlist_text, ids=(1,), params=None):
    request = _request(tmp_path, pdk_root, netlist_text, ids, params or {})
    return execute_layout_plan_document(request, request_dir=str(tmp_path))


def _resolved(response):
    return response["device_groups"][0]["resolved_params"]


def test_derived_for_port_gate_net(tmp_path, pdk_root):
    response = _run(tmp_path, pdk_root, _GATE_PORT_NETLIST)
    assert _resolved(response)["gate_contact"] is True
    assert any("derived params.gate_contact=true" in w for w in response["warnings"])


def test_not_derived_for_single_pin_internal_gate(tmp_path, pdk_root):
    response = _run(tmp_path, pdk_root, _GATE_INTERNAL_SINGLE_NETLIST)
    assert "gate_contact" not in _resolved(response)
    assert not any("gate_contact" in w for w in response["warnings"])


def test_explicit_false_override_wins(tmp_path, pdk_root):
    response = _run(
        tmp_path, pdk_root, _GATE_PORT_NETLIST, params={"gate_contact": False}
    )
    assert _resolved(response)["gate_contact"] is False
    assert not any("gate_contact" in w for w in response["warnings"])


def test_shared_gate_rail_routes_only_with_derivation(tmp_path, pdk_root):
    derived = _run(tmp_path, pdk_root, _GATE_RAIL_NETLIST, ids=(1, 2, 3))
    assert derived["unrouted_reasons"] == []

    forced_bare = _run(
        tmp_path,
        pdk_root,
        _GATE_RAIL_NETLIST,
        ids=(1, 2, 3),
        params={"gate_contact": False},
    )
    assert any(
        r["net"] == "VBIAS" and "bare-poly gate" in r["reason"]
        for r in forced_bare["unrouted_reasons"]
    )
