"""Acceptance test for issues #6920/#6912: the six-device mixed sky130 block
(xhigh ``res_array`` + MIM ``cap_array`` + four 5 V ``hvi`` ``mos_array``
units + two bulk-tie ``guard_ring`` groups) routes under *default* routing --
no ``waypoints_um``, no ``legs``, no ``cross_block_layer_role``: the router's
automatic metals-stack plane climb (#6920, extended to the ring-conflict and
self-net classes by #6912) lifts the legs the fixed one-jog li1 backbone
cannot route (the #1895 "reaches the pin from behind" class, cross-net
conflicts, and the #199/#434 ring classes an own-ringed pfet group's inner
ports hit) onto higher planes.

#6912's remaining acceptance, carried here: the written GDS is LVS-clean
against the block's own netlist as a *four-terminal* compare (the resistor
extracts as the declared ``res_xhigh_po`` class with its well tie on GND,
the pfet bodies on VDD33 and the nfet bodies on GND -- compared, never
ignored or waived), and DRC-clean under the curated sky130 deck as drawn,
with no rule waived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import pdk
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from klayout_tools.layout_plan_execute import (
    execute_layout_plan_document,
    exit_code_for,
)
from klayout_tools.lvs import run_lvs

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
        params = {
            "rows": 1,
            "cols": 1,
            "dummy": 0,
            "voltage_flavor": "hvi",
            "w_um": w,
            "l_um": 0.5,
        }
        if cls == "PFET":
            # The pfet bulk tie (#6912): the array's own tap ring contacts
            # its (flavor-derived) well up to the routed VDD33 -- without
            # it, sky130's arrays draw no taps and the LVS bulk compare
            # leaves the body on the deck's synthesized substrate net.
            params["add_guard_ring"] = True
        groups.append(
            {
                "id": gid,
                "devices": [{"name": num, "device_class": cls}],
                "generator": "mos_array",
                "topology": "array",
                "params": params,
            }
        )
    groups.append(
        {"id": "vdd_ring", "devices": [], "generator": "guard_ring", "net": "VDD33"}
    )
    groups.append(
        {
            "id": "gnd_ring",
            "devices": [],
            "generator": "guard_ring",
            "net": "GND",
            # A substrate tie (#6912): without `add_well: false` the ring
            # draws its nwell enclosure, the deck classifies every tap as a
            # well tie, and the NFET bodies (and the resistor's W terminal)
            # stay on the synthesized `vsubs` global instead of GND.
            "params": {"add_well": False},
        }
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


def test_mixed_block_derives_mos_flavor_from_the_declared_class(tmp_path, pdk_root):
    # #6912: a mos group whose params do not name a flavor reads it from the
    # declared device class (PFET -> pfet, NFET -> nfet), mirroring #6919's
    # res_array derivation -- pre-fix, a `device_class: "PFET"` group drew
    # the nfet default silently (no well, wrong polarity).
    request = _request(tmp_path, pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    warnings = [w for w in response["warnings"] if "params.flavor" in w]
    assert warnings == [
        f"device_groups (id '{gid}'): derived params.flavor='{flavor}' "
        "from the declared MOS class"
        for gid, flavor in (
            ("p1", "pfet"),
            ("n2", "nfet"),
            ("p3", "pfet"),
            ("n4", "nfet"),
        )
    ], response["warnings"]


def test_mixed_six_device_block_lvs_matches_four_terminal(tmp_path, pdk_root):
    """#6912's LVS acceptance: `klt lvs` of the written GDS against the
    block's own netlist reports `match` as a four-terminal compare.

    The layout-side evidence first: the resistor extracts as the *declared*
    xhigh class with its well tie on the real GND net, the pfet bodies on
    VDD33 and the nfet bodies on GND -- none on the deck's synthesized
    `vsubs` global, nothing left floating. Then the compare itself: every
    net (bulk nets included) matched by name, no error-severity mismatch,
    and the body check `verified` (all four MOS bodies inspected against
    real schematic nets). The request sets no `ignore_mos_bulk`-style
    option and no waiver -- the bulk nets are compared, not waved through.
    """
    request = _request(tmp_path, pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0
    gds = request["options"]["output"]

    extracted = run_extract(gds, "sky130")
    nets = sorted(n["name"] for n in extracted["nets"])
    assert nets == ["FLAG_F", "FLAG_IN", "GND", "RC", "RCB", "VDD33"]
    by_class: dict[str, list[dict]] = {}
    for device in extracted["devices"]:
        by_class.setdefault(device["class"], []).append(device["nets"])
    assert set(by_class) == {
        "nfet",
        "pfet",
        "res_xhigh_po",
        "sky130_fd_pr__model__cap_mim",
    }
    assert len(by_class["nfet"]) == 2 and len(by_class["pfet"]) == 2
    for nets_map in by_class["pfet"]:
        assert nets_map["b"] == "VDD33", nets_map
    for nets_map in by_class["nfet"]:
        assert nets_map["b"] == "GND", nets_map
    assert by_class["res_xhigh_po"] == [{"a": "RC", "b": "FLAG_IN", "w": "GND"}]
    assert by_class["sky130_fd_pr__model__cap_mim"] == [{"a": "RC", "b": "GND"}]

    report = run_lvs(
        json.dumps(
            {
                "layout": {"file": gds, "deck": "sky130"},
                "reference": {
                    "netlist": str(_NETLIST),
                    "top": "glitch_filter",
                    "form": "subckt-call",
                    "deck": "sky130",
                },
            }
        )
    )
    assert report["status"] == "match", report["mismatches"]
    assert not [m for m in report["mismatches"] if m.get("severity") == "error"]
    counts = report["counts"]["devices"]
    assert (counts["layout"], counts["reference"], counts["matched"]) == (6, 6, 6)
    correspondence = {
        (n["layout"], n["reference"]): n.get("pin")
        for n in report["net_correspondence"]
    }
    assert correspondence == {
        ("FLAG_F", "FLAG_F"): True,
        ("FLAG_IN", "FLAG_IN"): True,
        ("GND", "GND"): True,
        ("RC", "RC"): True,
        ("RCB", "RCB"): True,
        ("VDD33", "VDD33"): True,
    }
    body = report["body_verification"]
    assert body["status"] == "verified"
    assert body["inspected_count"] == 4
    assert body["finding_count"] == 0


def test_mixed_six_device_block_drc_clean_curated_sky130(tmp_path, pdk_root):
    """#6912's DRC acceptance: `klt drc --deck sky130` on the written GDS is
    reported. The block is clean as drawn -- no rule waived, no waivers
    document passed (the call has no such argument), and #5704's 2.16um
    xhigh end heads landed, so the historic end-head finding no longer
    fires. The layers_checked assertion pins the clean verdict as real
    coverage of the drawn stack, not a vacuous empty-deck pass.
    """
    request = _request(tmp_path, pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    report = run_drc(request["options"]["output"], "sky130")
    assert report["status"] == "clean", report["violations"]
    assert report["violation_count"] == 0
    checked = set(report["coverage"]["layers_checked"])
    assert {
        "64/20",  # nwell (the pfet wells and the VDD33 ring)
        "65/20",  # active
        "65/44",  # tap (both rings' ties)
        "66/44",  # licon
        "67/20",  # li1
        "68/20",  # met1 (the primary routing plane)
    } <= checked, sorted(checked)
