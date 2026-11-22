"""Acceptance test for issue #6920: the six-device mixed sky130 block
(xhigh ``res_array`` + MIM ``cap_array`` + four 5 V ``hvi`` ``mos_array``
units + two bulk-tie ``guard_ring`` groups) routes under *default* routing --
no ``waypoints_um``, no ``legs``, no ``cross_block_layer_role``: the router's
automatic metals-stack plane climb (#6920) lifts the legs the fixed one-jog
li1 backbone cannot route (the #1895 "reaches the pin from behind" class and
cross-net conflicts) onto higher planes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import (
    execute_layout_plan_document,
    exit_code_for,
)

_NETLIST = Path(__file__).parent / "data" / "glitch_filter_mixed_sky130.spice"


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


def _request(tmp_path, pdk_root):
    groups = [
        {
            "id": "r1",
            "devices": [{"name": "1", "device_class": "RES_XHIGH_PO"}],
            "generator": "res_array",
            "params": {"num": 1, "dummy": 0, "flavor": "xhigh"},
        },
        {
            "id": "c1",
            "devices": [{"name": "1", "device_class": "SKY130_FD_PR__MODEL__CAP_MIM"}],
            "generator": "cap_array",
            "params": {"num": 1},
        },
    ]
    for gid, cls, num, w in (
        ("p1", "PFET", "1", 2.0),
        ("n2", "NFET", "2", 1.0),
        ("p3", "PFET", "3", 2.0),
        ("n4", "NFET", "4", 1.0),
    ):
        groups.append(
            {
                "id": gid,
                "devices": [{"name": num, "device_class": cls}],
                "generator": "mos_array",
                "topology": "array",
                "params": {
                    "rows": 1,
                    "cols": 1,
                    "dummy": 0,
                    "voltage_flavor": "hvi",
                    "w_um": w,
                    "l_um": 0.5,
                },
            }
        )
    groups.append(
        {"id": "vdd_ring", "devices": [], "generator": "guard_ring", "net": "VDD33"}
    )
    groups.append(
        {"id": "gnd_ring", "devices": [], "generator": "guard_ring", "net": "GND"}
    )
    return {
        "netlist": {
            "path": str(_NETLIST),
            "top": "glitch_filter",
            "form": "subckt-call",
            "deck": "sky130",
        },
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": groups,
        "rows": [
            {
                "order": ["r1", "c1", "p1", "n2", "p3", "n4"],
                "spacing_um": 3.0,
                "align": "bottom",
            },
            {"order": ["vdd_ring", "gnd_ring"], "spacing_um": 3.0, "align": "bottom"},
        ],
        "options": {
            "cell_name": "glitch_filter_0",
            "output": str(tmp_path / "glitch_filter_0.gds"),
        },
    }


def test_mixed_six_device_block_routes_under_default_routing(tmp_path, pdk_root):
    request = _request(tmp_path, pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []


def test_mixed_block_rows_reordered_still_routes(tmp_path, pdk_root):
    # Edge case from the issue's test plan: the same block with `rows`
    # reordered (the tie-ring row placed first) still routes.
    request = _request(tmp_path, pdk_root)
    request["rows"] = list(reversed(request["rows"]))
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []


def test_facing_pair_and_two_device_group_stay_on_the_primary_plane(tmp_path, pdk_root):
    # Edge cases from the issue's test plan: a block where the pins face
    # each other across the channel (unchanged behaviour -- every leg stays
    # on the primary plane, the climb never fires), including a 2-device
    # `mos_array` group (two units in series, the shared strap carrying the
    # same net on both sides).
    netlist_path = tmp_path / "pair.spice"
    netlist_path.write_text(
        ".subckt pair A B MID S0\n"
        "M1 MID B S0 S0 nfet L=0.5U W=1U\n"
        "M2 A B MID MID nfet L=0.5U W=1U\n"
        ".ends\n"
    )
    request = {
        "netlist": {"path": str(netlist_path), "top": "pair"},
        "pdk": {"variant": "sky130A", "root": str(pdk_root)},
        "device_groups": [
            {
                "id": "m1",
                "devices": ["1"],
                "generator": "mos_array",
                "topology": "array",
                "params": {"rows": 1, "cols": 1, "dummy": 0},
            },
            {
                "id": "m2",
                "devices": ["2"],
                "generator": "mos_array",
                "topology": "array",
                "params": {"rows": 1, "cols": 2, "dummy": 0},
            },
        ],
        "rows": [{"order": ["m1", "m2"], "spacing_um": 2.0, "align": "bottom"}],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {"cell_name": "pair_0", "output": str(tmp_path / "pair_0.gds")},
    }
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []
