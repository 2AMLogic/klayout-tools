"""Tests for `klayout_tools.layout_plan_execute` (issue #1155, Phase C of
`docs/design/netlist-driven-layout-spike.md`).

PDK resolution is exercised against a fabricated open_pdks-layout install
under `tmp_path` (mirrors `test_gen.py`/`test_gen_compose.py`) -- CI never
downloads a real PDK.

Covers: per-group netlist-derived parameter resolution + override warning,
row-of-rows offset stacking (multi-row, all three `align` values), abutment
constraint solving (a never-row-placed group resolved purely via
`abutment[]`, and abutment overriding an already row-placed pair, with the
`warnings[]` entry it produces), derived connectivity feeding both the
two-pin and bundle (`route_bundle`) routing paths, `unmapped_netlist_nets[]`
reporting (a bulk-only net no device_groups[] port reaches), the exit-code
trichotomy (0/1/3), and the documented placement-coverage / empty-plan
error cases.
"""

from __future__ import annotations

import os

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import (
    LayoutPlanExecuteError,
    execute_layout_plan_document,
    exit_code_for,
    partial_success,
)


def _make_install(root, variant):
    variant_dir = root / variant
    (variant_dir / "libs.tech").mkdir(parents=True)
    return variant_dir


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


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _pdk_spec(pdk_root) -> dict:
    return {"variant": "sky130A", "root": str(pdk_root)}


# A gate-rail netlist (mirrors `test_gen_compose.py`'s own
# `_gate_rail_blocks`/`_gate_rail_request` fixture shape, which is proven to
# route cleanly): three unit NFETs sharing one gate net (a 3-pin bundle),
# M1's drain wired to M2's source (a 2-pin net between adjacent blocks'
# facing ports), and every other terminal on its own unique net.
_GATE_RAIL_NETLIST = """
.subckt gate_rail VBIAS D1 D2 D3
M1 D1 VBIAS S1 S1 nfet L=0.28U W=0.42U
M2 D2 VBIAS D1 D1 nfet L=0.28U W=0.42U
M3 D3 VBIAS S3 S3 nfet L=0.28U W=0.42U
.ends
"""

# The same netlist, but M3's bulk ties to a net ("VNW") nothing else
# touches -- since a MOS `B`/bulk terminal is deliberately excluded from
# this module's per-device port map (scope decision 5), `VNW` has zero
# resolved pins and lands in `unmapped_netlist_nets[]`.
_GATE_RAIL_NETLIST_WITH_UNMAPPED_BULK = """
.subckt gate_rail VBIAS D1 D2 D3
M1 D1 VBIAS S1 S1 nfet L=0.28U W=0.42U
M2 D2 VBIAS D1 D1 nfet L=0.28U W=0.42U
M3 D3 VBIAS S3 VNW nfet L=0.28U W=0.42U
.ends
"""


def _gate_rail_groups() -> list[dict]:
    return [
        {
            "id": f"b{i}",
            "devices": [str(i)],
            "generator": "mos_array",
            "topology": "array",
            "params": {"rows": 1, "cols": 1, "dummy": 0, "gate_contact": True},
        }
        for i in (1, 2, 3)
    ]


def _gate_rail_request(tmp_path, pdk_root, netlist_text: str) -> dict:
    netlist_path = _write(tmp_path, "gate_rail.spice", netlist_text)
    return {
        "schema": "klt.layout_plan.request/1",
        "netlist": {"path": netlist_path, "top": "gate_rail"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": _gate_rail_groups(),
        "rows": [{"order": ["b1", "b2", "b3"], "spacing_um": 2.0, "align": "bottom"}],
        "routing": {"layer_role": "metal", "width_um": 0.42},
        "options": {
            "cell_name": "gate_rail_0",
            "output": str(tmp_path / "gate_rail_0.gds"),
        },
    }


# ---------------------------------------------------------------------------
# Full pipeline: connectivity (two-pin + bundle), exit-code trichotomy.
# ---------------------------------------------------------------------------


def test_execute_routes_two_pin_and_bundle_nets_full_success(tmp_path, pdk_root):
    request = _gate_rail_request(tmp_path, pdk_root, _GATE_RAIL_NETLIST)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["schema_version"] == 1
    assert os.path.isfile(response["gds_path"])
    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []
    assert partial_success(response) is False
    assert exit_code_for(response) == 0

    nets_by_name = {net["net"]: net for net in response["nets"]}
    assert set(nets_by_name) == {"D1", "VBIAS"}

    # D1: M1's drain (b1.U0_D) <-> M2's source (b2.U0_S) -- a 2-pin net.
    two_pin = nets_by_name["D1"]
    assert two_pin["routed"] is True
    assert {(p["block"], p["port"]) for p in two_pin["pins"]} == {
        ("b1", "U0_D"),
        ("b2", "U0_S"),
    }

    # VBIAS: all three gates -- a 3-pin bundle net, routed via
    # gen_compose.route_bundle() as a 2-leg spanning tree (issue #1073),
    # never a second, separate call from this module.
    bundle = nets_by_name["VBIAS"]
    assert bundle["routed"] is True
    assert len(bundle["legs"]) == 2
    assert all(leg["routed"] for leg in bundle["legs"])
    assert {(p["block"], p["port"]) for p in bundle["pins"]} == {
        ("b1", "U0_G"),
        ("b2", "U0_G"),
        ("b3", "U0_G"),
    }

    # device_groups[] response extends gen_compose's blocks[] shape with
    # resolved_params/devices, per the spike's response field table.
    by_id = {g["id"]: g for g in response["device_groups"]}
    assert by_id["b1"]["devices"] == [{"name": "1", "device_class": "NFET"}]
    assert by_id["b1"]["resolved_params"]["l_um"] == pytest.approx(0.28)
    assert by_id["b1"]["resolved_params"]["w_um"] == pytest.approx(0.42)
    # mos_array is one of the two generators that actually declares a
    # `params.topology` field (_TOPOLOGY_PARAM_GENERATORS) -- the plan's
    # declared `topology: "array"` must still reach `klt gen`'s params
    # unchanged (issue #1160's second acceptance criterion).
    assert by_id["b1"]["resolved_params"]["topology"] == "array"
    assert "offset_um" in by_id["b1"] and "bbox_um" in by_id["b1"]


def test_execute_reports_unmapped_netlist_net_as_partial_success(tmp_path, pdk_root):
    request = _gate_rail_request(
        tmp_path, pdk_root, _GATE_RAIL_NETLIST_WITH_UNMAPPED_BULK
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    # Every net that *does* resolve still routes cleanly -- only the
    # bulk-only net is affected.
    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == ["VNW"]
    assert partial_success(response) is True
    assert exit_code_for(response) == 3


def test_execute_reports_same_facing_pins_as_unrouted_partial_success(
    tmp_path, pdk_root
):
    # All three sources tied to one common net: mos_array's `S` port faces
    # the same direction on every unit in a row, so a same-facing multi-pin
    # bundle is a genuine, deterministic routing failure (not a connectivity
    # derivation bug) -- `unrouted_nets[]` reports it, not an exception.
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
    assert partial_success(response) is True
    assert exit_code_for(response) == 3


# ---------------------------------------------------------------------------
# Per-group parameter resolution: netlist-derived sizing + override warning.
# ---------------------------------------------------------------------------


def test_resolve_group_params_netlist_derived_and_override_warning(tmp_path, pdk_root):
    netlist = """
.subckt one_fet D G
M1 D G VSS VSS nfet L=0.5U W=2U
.ends
"""
    netlist_path = _write(tmp_path, "one_fet.spice", netlist)
    request = {
        "netlist": {"path": netlist_path, "top": "one_fet"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": [
            {
                "id": "m1",
                "devices": ["1"],
                "generator": "mos_array",
                "params": {"rows": 1, "cols": 1, "dummy": 0, "w_um": 3.0},
            }
        ],
        "rows": [{"order": ["m1"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / "one_fet_0.gds")},
    }
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    # l_um is netlist-derived (no override); w_um is the caller's override,
    # which diverges from the netlist's own W=2U -- flagged, not silent.
    assert group["resolved_params"]["l_um"] == pytest.approx(0.5)
    assert group["resolved_params"]["w_um"] == pytest.approx(3.0)
    assert any(
        "params.w_um override" in warning and "diverges" in warning
        for warning in response["warnings"]
    )


def test_diff_pair_group_with_declared_topology_executes_successfully(
    tmp_path, pdk_root
):
    """`diff_pair` declares no `params.topology` of its own -- Phase B's
    `_GENERATOR_TOPOLOGY_SUPPORT` nonetheless accepts `topology:
    "common_centroid"` on a `diff_pair` group as a plan-level assertion
    that already matches what the generator always draws. Before issue
    #1160, `_resolve_group_params` injected that declared topology into
    every group's `params` unconditionally, so this exact plan reached
    `klt gen` with an unknown `params.topology` and died with
    `LayoutPlanExecuteError` in Phase C despite passing Phase B validation.
    """
    netlist = """
.subckt one_fet D G
M1 D G VSS VSS nfet L=0.28U W=0.42U
.ends
"""
    netlist_path = _write(tmp_path, "one_fet.spice", netlist)
    request = {
        "netlist": {"path": netlist_path, "top": "one_fet"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": [
            {
                "id": "dp",
                "devices": ["1"],
                "generator": "diff_pair",
                "topology": "common_centroid",
            }
        ],
        "rows": [{"order": ["dp"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / "diff_pair_0.gds")},
    }

    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["schema_version"] == 1
    assert os.path.isfile(response["gds_path"])

    group = response["device_groups"][0]
    # The declared topology is accepted (no error, no warning -- it already
    # matches diff_pair's always-common-centroid layout) but never injected
    # into `params`, since `diff_pair` has no `params.topology` field for
    # `klt gen` to accept.
    assert "topology" not in group["resolved_params"]
    assert not any("topology" in warning for warning in response["warnings"])


# ---------------------------------------------------------------------------
# Row-of-rows placement + abutment (pure geometry -- resistor_strip, which
# has no per-device netlist mapping, keeps these tests decoupled from
# connectivity concerns).
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_netlist(tmp_path):
    return _write(tmp_path, "empty.spice", ".subckt empty\n.ends\n")


def _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows, abutment=None):
    return {
        "netlist": {"path": empty_netlist, "top": "empty"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": groups,
        "rows": rows,
        "abutment": abutment or [],
        "options": {"output": str(tmp_path / "geom_0.gds")},
    }


def _strip_group(group_id: str, length_um: float, width_um: float) -> dict:
    return {
        "id": group_id,
        "devices": [],
        "generator": "resistor_strip",
        "params": {"length_um": length_um, "width_um": width_um, "num": 1},
    }


def test_row_of_rows_offset_stacking_bottom_align(tmp_path, pdk_root, empty_netlist):
    groups = [
        _strip_group("a", 2.0, 1.0),
        _strip_group("b", 2.0, 3.0),
        _strip_group("c", 2.0, 1.0),
    ]
    rows = [
        {"order": ["a", "b"], "spacing_um": 1.0, "align": "bottom"},
        {"order": ["c"], "spacing_um": 0.0, "align": "bottom"},
    ]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    # Row 1 ("a","b"): compute_row_offsets places "a" at x=0, "b" one
    # spacing_um past "a"'s right edge; "bottom" align keeps both at y=0.
    assert offsets["a"] == pytest.approx({"x": 0.0, "y": 0.0})
    assert offsets["b"] == pytest.approx({"x": 3.0, "y": 0.0})
    # Row 2 ("c"): stacked above row 1 by row 1's tallest group ("b",
    # height 3.0um) -- no additional inter-row margin (the spike's own
    # row-of-rows description names none).
    assert offsets["c"] == pytest.approx({"x": 0.0, "y": 3.0})


def test_row_align_top_and_center(tmp_path, pdk_root, empty_netlist):
    groups = [_strip_group("short", 1.0, 1.0), _strip_group("tall", 1.0, 4.0)]

    top_request = _geometry_request(
        tmp_path,
        pdk_root,
        empty_netlist,
        groups,
        [{"order": ["short", "tall"], "spacing_um": 0.5, "align": "top"}],
    )
    top_response = execute_layout_plan_document(top_request, request_dir=str(tmp_path))
    offsets = {g["id"]: g["offset_um"] for g in top_response["device_groups"]}
    # "top" align: both groups' top edges land at the row height (4.0um).
    assert offsets["short"]["y"] == pytest.approx(3.0)  # 4.0 - 1.0 (own height)
    assert offsets["tall"]["y"] == pytest.approx(0.0)  # 4.0 - 4.0

    center_request = _geometry_request(
        tmp_path,
        pdk_root,
        empty_netlist,
        groups,
        [{"order": ["short", "tall"], "spacing_um": 0.5, "align": "center"}],
    )
    center_response = execute_layout_plan_document(
        center_request, request_dir=str(tmp_path)
    )
    offsets = {g["id"]: g["offset_um"] for g in center_response["device_groups"]}
    # "center" align: each group's own vertical center lands at half the
    # row height (2.0um).
    assert offsets["short"]["y"] == pytest.approx(1.5)  # 2.0 - 0.5 (half own height)
    assert offsets["tall"]["y"] == pytest.approx(0.0)  # 2.0 - 2.0


def test_abutment_places_a_group_never_listed_in_rows(
    tmp_path, pdk_root, empty_netlist
):
    groups = [_strip_group("a", 2.0, 1.0), _strip_group("ring", 1.0, 1.0)]
    rows = [{"order": ["a"], "spacing_um": 0.0, "align": "bottom"}]
    abutment = [{"a": "a", "b": "ring", "edge": "right", "gap_um": 0.5}]
    request = _geometry_request(
        tmp_path, pdk_root, empty_netlist, groups, rows, abutment
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    # "ring" never appears in rows[] -- it is placed purely via the
    # abutment[] chain anchored to "a" (already row-placed): "a"'s bbox is
    # (0,0,2,1); "ring"'s right-abuts with a 0.5um gap -> x = 2.0 + 0.5.
    assert offsets["ring"]["x"] == pytest.approx(2.5)
    # No offset hint existed for "ring" on the perpendicular (y) axis, so it
    # centers on "a"'s own vertical center (both groups are 1.0um tall
    # here, so this is 0.0 either way -- see the "diverging heights"
    # variant below for a test that actually distinguishes centering from
    # a coincidental match).
    assert offsets["ring"]["y"] == pytest.approx(0.0)


def test_abutment_centers_a_freshly_placed_group_on_the_anchor(
    tmp_path, pdk_root, empty_netlist
):
    groups = [_strip_group("a", 2.0, 4.0), _strip_group("tab", 1.0, 1.0)]
    rows = [{"order": ["a"], "spacing_um": 0.0, "align": "bottom"}]
    abutment = [{"a": "a", "b": "tab", "edge": "right", "gap_um": 0.0}]
    request = _geometry_request(
        tmp_path, pdk_root, empty_netlist, groups, rows, abutment
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    # "a"'s bbox is (0,0,2,4), vertical center 2.0; "tab" (height 1.0,
    # raw center 0.5) has never been placed before, so its y offset centers
    # its own vertical center on "a"'s: 2.0 - 0.5 = 1.5.
    assert offsets["tab"]["y"] == pytest.approx(1.5)


def test_abutment_overrides_row_derived_placement_with_warning(
    tmp_path, pdk_root, empty_netlist
):
    groups = [
        _strip_group("a", 2.0, 1.0),
        _strip_group("b", 2.0, 3.0),
    ]
    rows = [{"order": ["a", "b"], "spacing_um": 1.0, "align": "bottom"}]
    # "b" is already row-placed (x=3.0 from compute_row_offsets); the
    # abutment constraint below re-anchors it much farther away and must
    # win, per this module's documented precedence (scope decision 4).
    abutment = [{"a": "a", "b": "b", "edge": "right", "gap_um": 9.0}]
    request = _geometry_request(
        tmp_path, pdk_root, empty_netlist, groups, rows, abutment
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    assert offsets["b"]["x"] == pytest.approx(11.0)  # 2.0 (a.x1) + 9.0
    # The perpendicular (y) axis is untouched by the override -- still the
    # row-derived value.
    assert offsets["b"]["y"] == pytest.approx(0.0)
    assert any(
        "overrides device_groups 'b'" in warning for warning in response["warnings"]
    )


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_execute_rejects_empty_device_groups(tmp_path, pdk_root, empty_netlist):
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, [], [])
    with pytest.raises(LayoutPlanExecuteError, match="nothing to generate"):
        execute_layout_plan_document(request, request_dir=str(tmp_path))


def test_execute_rejects_a_group_with_no_placement_path(
    tmp_path, pdk_root, empty_netlist
):
    groups = [_strip_group("a", 2.0, 1.0), _strip_group("b", 2.0, 1.0)]
    rows = [{"order": ["a"], "spacing_um": 0.0}]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    with pytest.raises(LayoutPlanExecuteError, match="'b'"):
        execute_layout_plan_document(request, request_dir=str(tmp_path))


def test_execute_wraps_a_generation_failure_as_exit_1(
    tmp_path, pdk_root, empty_netlist
):
    groups = [
        {
            "id": "a",
            "devices": [],
            "generator": "resistor_strip",
            "params": {"length_um": -1.0},
        }
    ]
    rows = [{"order": ["a"], "spacing_um": 0.0}]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    with pytest.raises(LayoutPlanExecuteError, match="generation failed"):
        execute_layout_plan_document(request, request_dir=str(tmp_path))


def test_execute_propagates_phase_b_validation_errors_unchanged(
    tmp_path, pdk_root, empty_netlist
):
    # An unresolvable device_groups[].devices reference is Phase B's own
    # LayoutPlanError -- this module must never swallow or re-wrap it.
    from klayout_tools.layout_plan import LayoutPlanError

    groups = [
        {
            "id": "a",
            "devices": ["does-not-exist"],
            "generator": "resistor_strip",
        }
    ]
    rows = [{"order": ["a"], "spacing_um": 0.0}]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    with pytest.raises(LayoutPlanError):
        execute_layout_plan_document(request, request_dir=str(tmp_path))
