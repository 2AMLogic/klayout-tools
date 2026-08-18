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


# device_groups[].orientation (#1166) must actually reach compose() through
# this module's Phase C execution path, not just be validated/echoed by Phase
# B (`layout_plan.validate_layout_plan`) -- the same same-facing-drain
# scenario `test_gen_compose.py`'s
# `test_compose_same_facing_drain_net_is_unrouted_without_orientation` /
# `test_compose_mirrored_block_routes_same_facing_drain_net_and_is_drc_clean`
# pair exercises directly against `compose()`, driven here end to end from a
# netlist-derived `klt.layout_plan.request/1` document instead.
_SAME_FACING_DRAIN_NETLIST = """
.subckt inverter VOUT
M1 VOUT G1 S1 S1 nfet L=0.28U W=0.42U
M2 VOUT G2 S2 S2 nfet L=0.28U W=0.42U
.ends
"""


def _same_facing_drain_groups(p_orientation=None) -> list[dict]:
    p_group = {
        "id": "p",
        "devices": ["2"],
        "generator": "mos_array",
        "params": {"rows": 1, "cols": 1, "dummy": 0},
    }
    if p_orientation is not None:
        p_group["orientation"] = p_orientation
    return [
        {
            "id": "n",
            "devices": ["1"],
            "generator": "mos_array",
            "params": {"rows": 1, "cols": 1, "dummy": 0},
        },
        p_group,
    ]


def _same_facing_drain_request(tmp_path, pdk_root, p_orientation=None) -> dict:
    netlist_path = _write(tmp_path, "inverter.spice", _SAME_FACING_DRAIN_NETLIST)
    return {
        "netlist": {"path": netlist_path, "top": "inverter"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": _same_facing_drain_groups(p_orientation),
        "rows": [{"order": ["n", "p"], "spacing_um": 1.0}],
        "routing": {"layer_role": "metal", "width_um": 0.17},
        "options": {
            "cell_name": "inverter_0",
            "output": str(tmp_path / "inverter_0.gds"),
        },
    }


def test_execute_same_facing_drain_net_is_unrouted_without_orientation(
    tmp_path, pdk_root
):
    request = _same_facing_drain_request(tmp_path, pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert "VOUT" in response["unrouted_nets"]
    assert partial_success(response) is True


def test_execute_group_orientation_reaches_compose_and_routes_same_facing_drain_net(
    tmp_path, pdk_root
):
    request = _same_facing_drain_request(tmp_path, pdk_root, p_orientation="mirror_x")
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["unrouted_nets"] == []
    nets_by_name = {net["net"]: net for net in response["nets"]}
    assert nets_by_name["VOUT"]["routed"] is True


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

    `W=1U` (rather than the PDK's minimum-size `0.42U`) is deliberate: with
    the default `splits=2`, the netlist-derived `w_um` is auto-divided
    (issue #1165) to `0.5`, which must still clear `diff_pair`'s own
    `UNIT_MIN_W_UM=0.42` structural floor.
    """
    netlist = """
.subckt one_fet D G
M1 D G VSS VSS nfet L=0.28U W=1U
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


def _one_fet_diff_pair_request(tmp_path, pdk_root, w_spice: str, params: dict) -> dict:
    netlist = f"""
.subckt one_fet D G
M1 D G VSS VSS nfet L=0.28U {w_spice}
.ends
"""
    netlist_path = _write(tmp_path, "one_fet.spice", netlist)
    return {
        "netlist": {"path": netlist_path, "top": "one_fet"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": [
            {
                "id": "dp",
                "devices": ["1"],
                "generator": "diff_pair",
                "params": params,
            }
        ],
        "rows": [{"order": ["dp"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / "diff_pair_0.gds")},
    }


def test_diff_pair_w_um_auto_divided_by_default_splits(tmp_path, pdk_root):
    """`diff_pair`'s `w_um` is a *unit* sub-instance width -- the generator
    draws each matched device as `splits` (default 2) unit instances wide.
    A netlist device with `W=20U` must resolve `w_um=10.0`, not `20.0`, so
    the drawn device (both unit instances together) reproduces the netlist
    `W` -- and the auto-division must always be reported in `warnings[]`.
    """
    request = _one_fet_diff_pair_request(tmp_path, pdk_root, "W=20U", {})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["w_um"] == pytest.approx(10.0)
    assert group["resolved_params"]["l_um"] == pytest.approx(0.28)
    assert any(
        "dp" in warning and "splits" in warning and "10.0" in warning
        for warning in response["warnings"]
    )


def test_diff_pair_w_um_auto_divided_by_overridden_splits(tmp_path, pdk_root):
    """An explicit `params.splits` override changes the divisor used to
    derive `w_um` from the netlist `W`."""
    request = _one_fet_diff_pair_request(tmp_path, pdk_root, "W=20U", {"splits": 4})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["w_um"] == pytest.approx(5.0)
    assert any(
        "dp" in warning and "splits" in warning and "5.0" in warning
        for warning in response["warnings"]
    )


def test_diff_pair_splits_of_one_is_a_silent_no_op(tmp_path, pdk_root):
    """`params.splits: 1` makes the division a no-op (`w_um` unchanged from
    the netlist `W`) -- and since nothing was actually auto-divided, no
    `warnings[]` entry is expected."""
    request = _one_fet_diff_pair_request(tmp_path, pdk_root, "W=20U", {"splits": 1})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["w_um"] == pytest.approx(20.0)
    assert not any("splits" in warning for warning in response["warnings"])


def test_diff_pair_zero_netlist_w_falls_back_to_generator_default(tmp_path, pdk_root):
    """A netlist `W == 0.0` (KLayout's structural default for an unset
    device-class param) is "the netlist did not specify a geometric size,"
    not a real value to divide -- it must fall through to `diff_pair`'s own
    default `w_um`, untouched by the split-division logic."""
    request = _one_fet_diff_pair_request(tmp_path, pdk_root, "W=0", {})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert "w_um" not in group["resolved_params"]
    assert not any("splits" in warning for warning in response["warnings"])


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


def _strip_group(
    group_id: str, length_um: float, width_um: float, *, spacing_um: float = 0.0
) -> dict:
    # ``spacing_um`` also feeds ``resistor_strip``'s own declared
    # ``drc_hints.min_spacing_um`` (gen.py's ``_resistor_strip_describe``) --
    # explicitly zeroed by default so the row-offset/align/abutment tests
    # below stay decoupled from the inter-row-margin behavior covered by the
    # dedicated tests in "Inter-row margin" below (issue #1170). With
    # ``num=1`` a nonzero ``spacing_um`` has no effect on this group's own
    # drawn geometry (there is only one unit resistor to space).
    return {
        "id": group_id,
        "devices": [],
        "generator": "resistor_strip",
        "params": {
            "length_um": length_um,
            "width_um": width_um,
            "num": 1,
            "spacing_um": spacing_um,
        },
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
    # height 3.0um) -- no inter-row margin here since every group's own
    # declared drc_hints.min_spacing_um is 0.0 (see `_strip_group`'s default
    # `spacing_um=0.0`); the dedicated "Inter-row margin" tests below cover
    # the nonzero-default and explicit-override cases (issue #1170).
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
# Inter-row margin (issue #1170): `_resolve_placement()` no longer stacks
# `rows[]` at exactly the running height total with 0.00um separation by
# default -- it now adds a margin before every row after the first.
# ---------------------------------------------------------------------------


def test_multi_row_default_margin_uses_larger_declared_min_spacing_um(
    tmp_path, pdk_root, empty_netlist
):
    """With no explicit `rows[].margin_um`, the default vertical gap between
    two rows is the larger of the two rows' groups' own declared
    `drc_hints.min_spacing_um` -- not the previous, always-0.00um default.
    """
    groups = [
        _strip_group("a", 2.0, 1.0, spacing_um=0.3),
        _strip_group("b", 2.0, 1.0, spacing_um=0.6),
    ]
    rows = [
        {"order": ["a"], "spacing_um": 0.0, "align": "bottom"},
        {"order": ["b"], "spacing_um": 0.0, "align": "bottom"},
    ]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    # Row 1 ("a") sits at y=0, height 1.0um. Row 2 ("b") stacks on top with
    # the larger of the two rows' declared min_spacing_um (0.6, from "b")
    # as the default inter-row margin.
    assert offsets["a"]["y"] == pytest.approx(0.0)
    assert offsets["b"]["y"] == pytest.approx(1.0 + 0.6)

    # The advisory clearance warning that fired by default before this fix
    # (gen_compose's "closer than ... declared drc_hints.min_spacing_um")
    # must not fire now that the gap satisfies both groups' declared minimum.
    assert not any("closer than" in warning for warning in response["warnings"])


def test_multi_row_explicit_margin_um_overrides_default(
    tmp_path, pdk_root, empty_netlist
):
    groups = [
        _strip_group("a", 2.0, 1.0, spacing_um=0.3),
        _strip_group("b", 2.0, 1.0, spacing_um=0.6),
    ]
    rows = [
        {"order": ["a"], "spacing_um": 0.0, "align": "bottom"},
        {
            "order": ["b"],
            "spacing_um": 0.0,
            "align": "bottom",
            "margin_um": 2.0,
        },
    ]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    # An explicit rows[].margin_um wins outright over the declared-spacing
    # default (2.0um here, larger than the 0.6um default would have been).
    assert offsets["b"]["y"] == pytest.approx(1.0 + 2.0)


def test_multi_row_explicit_margin_um_zero_forces_flush_stack(
    tmp_path, pdk_root, empty_netlist
):
    """An explicit `margin_um: 0.0` is a deliberate caller choice, distinct
    from leaving it unset, and is honored even when the declared-spacing
    default would otherwise add a gap -- the clearance check stays advisory
    only (#692), never a hard error."""
    groups = [
        _strip_group("a", 2.0, 1.0, spacing_um=0.3),
        _strip_group("b", 2.0, 1.0, spacing_um=0.6),
    ]
    rows = [
        {"order": ["a"], "spacing_um": 0.0, "align": "bottom"},
        {"order": ["b"], "spacing_um": 0.0, "align": "bottom", "margin_um": 0.0},
    ]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert exit_code_for(response) == 0

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    assert offsets["b"]["y"] == pytest.approx(1.0)  # flush -- no margin added


def test_single_row_plan_is_unaffected_by_margin_default(
    tmp_path, pdk_root, empty_netlist
):
    """A single-row plan has no "previous row" to add a margin after, so a
    nonzero declared min_spacing_um must not shift anything -- regression
    guard for existing single-row plans (issue #1170's acceptance
    criteria)."""
    groups = [_strip_group("a", 2.0, 1.0, spacing_um=0.6)]
    rows = [{"order": ["a"], "spacing_um": 0.0, "align": "bottom"}]
    request = _geometry_request(tmp_path, pdk_root, empty_netlist, groups, rows)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    offsets = {g["id"]: g["offset_um"] for g in response["device_groups"]}
    assert offsets["a"] == pytest.approx({"x": 0.0, "y": 0.0})


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
