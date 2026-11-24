"""Tests for `layout-plan execute`'s `strap_splits` derivation (issue #5557).

Split out of `test_layout_plan_execute.py` (file-size ratchet). For a
`diff_pair` group resolving `splits > 1` whose width can fit the strap,
`strap_splits: true` is derived (with a warning), so the plan's connectivity
binds the generator's device-level `Q1_S`-style ports and every sub-instance
is connected; an explicit `params.strap_splits` override still wins and
divergence is reported.
"""

import pytest

from klayout_tools.layout_plan_execute import execute_layout_plan_document


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _pdk_spec(pdk_root) -> dict:
    return {"variant": "sky130A", "root": str(pdk_root)}


@pytest.fixture()
def pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    (root / "sky130A" / "libs.tech").mkdir(parents=True)
    return root


def _two_fet_request(tmp_path, pdk_root, params: dict, name="dp_plan") -> dict:
    """A two-device matched-pair plan (the layout a `diff_pair` group
    exists for): both legs' W is large enough to fit the strap tracks."""
    netlist = """
.subckt matched_pair D1 G1 S1 D2 G2 S2
M1 D1 G1 S1 VSS nfet L=0.28U W=20U
M2 D2 G2 S2 VSS nfet L=0.28U W=20U
.ends
"""
    netlist_path = _write(tmp_path, "matched_pair.spice", netlist)
    return {
        "netlist": {"path": netlist_path, "top": "matched_pair"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": [
            {
                "id": "dp",
                "devices": ["1", "2"],
                "generator": "diff_pair",
                "params": {"splits": 2, **params},
            }
        ],
        "rows": [{"order": ["dp"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / f"{name}.gds")},
    }


def _plan_device_ports(response):
    return sorted(
        {
            pin["port"]
            for net in response["nets"]
            for pin in net.get("pins", [])
            if not pin["port"].startswith("TAP_")
        }
        | {pin["port"] for pin in response["pins"]}
    )


@pytest.mark.parametrize("splits", [2, 4])
def test_strap_splits_derived_for_splits_over_one(tmp_path, pdk_root, splits):
    """A two-device `diff_pair` plan at `splits=2` and at `splits=4`
    resolves `strap_splits: true` (with a warning) and its pins name the
    generator's device-level `Q1_S`-style ports only -- no change to the
    k-th-device <-> k-th-candidate binding rule itself (issue #5557)."""
    request = _two_fet_request(tmp_path, pdk_root, {"splits": splits})
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["strap_splits"] is True
    assert any(
        "dp" in warning and "strap_splits" in warning
        for warning in response["warnings"]
    )
    ports = _plan_device_ports(response)
    assert ports, "expected the plan to bind device ports"
    assert set(ports) == {"Q1_S", "Q1_D", "Q1_G", "Q2_S", "Q2_D", "Q2_G"}


def test_explicit_strap_splits_false_override_wins_and_warns(tmp_path, pdk_root):
    """An explicit `params.strap_splits: false` override beats the derived
    value and the divergence is reported in `warnings[]` like any other
    derived-param override; the plan falls back to the unstrapped
    sub-instance-level ports and the existing binding rule."""
    request = _two_fet_request(
        tmp_path, pdk_root, {"strap_splits": False}, "dp_unstrapped"
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["strap_splits"] is False
    assert any(
        "strap_splits" in warning and "diverges" in warning
        for warning in response["warnings"]
    )
    ports = _plan_device_ports(response)
    assert "Q1_1_S" in ports, f"expected unstrapped sub-instance ports, got {ports}"


def _narrow_pair_netlist(tmp_path) -> dict:
    """A matched pair whose whole-device W (2U) is too narrow for the strap
    at any `splits >= 2`."""
    path = _write(
        tmp_path,
        "narrow_pair_shared.spice",
        ".subckt narrow_pair D1 G1 S1 D2 G2 S2\n"
        "M1 D1 G1 S1 VSS nfet L=0.28U W=2U\n"
        "M2 D2 G2 S2 VSS nfet L=0.28U W=2U\n"
        ".ends\n",
    )
    return {"path": path, "top": "narrow_pair"}


def _strap_declined_warnings(response):
    return [w for w in response["warnings"] if "strap_splits was NOT derived" in w]


def test_narrow_width_plan_does_not_derive_strap_but_warns(tmp_path, pdk_root):
    """A plan whose resolved unit width cannot fit the strap tracks derives
    nothing -- it keeps today's unstrapped behaviour rather than failing a
    request the caller never made (the generator's own validation would
    reject an explicit `strap_splits` at this width) -- but it must say so:
    the unstrapped binding leaves `splits - 1` sub-instances per device
    floating, which is #5557's own failure mode, so a silent decline would
    recreate it with a success status."""
    netlist = """
.subckt narrow_pair D1 G1 S1 D2 G2 S2
M1 D1 G1 S1 VSS nfet L=0.28U W=2U
M2 D2 G2 S2 VSS nfet L=0.28U W=2U
.ends
"""
    netlist_path = _write(tmp_path, "narrow_pair.spice", netlist)
    request = {
        "netlist": {"path": netlist_path, "top": "narrow_pair"},
        "pdk": _pdk_spec(pdk_root),
        "device_groups": [
            {
                "id": "dp",
                "devices": ["1", "2"],
                "generator": "diff_pair",
                "params": {"splits": 2},
            }
        ],
        "rows": [{"order": ["dp"], "spacing_um": 0.0}],
        "options": {"output": str(tmp_path / "dp_narrow.gds")},
    }
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"].get("strap_splits") is None
    # Netlist W=2U over splits=2 resolves a 1.0 um unit, under the 2.35 floor.
    (warning,) = _strap_declined_warnings(response)
    assert "'dp'" in warning
    assert "resolved unit w_um (1.0)" in warning
    assert "STRAP_MIN_W_UM (2.35)" in warning
    assert "1 of the 2 sub-instances per device" in warning


def test_narrow_width_warning_counts_unbound_sub_instances(tmp_path, pdk_root):
    """The warning's consequence tracks the resolved `splits`: at
    `splits=4` and W=2U (unit 0.5 um) three of the four sub-instances per
    device stay unbound -- the measured case from the review of this PR,
    where `execute` returned success with six of eight sub-instances
    floating and no warning."""
    request = _two_fet_request(tmp_path, pdk_root, {"splits": 4}, "dp_narrow_splits4")
    request["netlist"] = _narrow_pair_netlist(tmp_path)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["splits"] == 4
    assert group["resolved_params"].get("strap_splits") is None
    (warning,) = _strap_declined_warnings(response)
    assert "resolved unit w_um (0.5)" in warning
    assert "3 of the 4 sub-instances per device" in warning


def test_explicit_strap_splits_false_at_narrow_width_is_silent(tmp_path, pdk_root):
    """An explicit `params.strap_splits: false` is the caller's deliberate
    opt-out, so a narrow plan that also declines the strap on width draws no
    extra "not derived" warning -- only a *silent* decline is the defect."""
    request = _two_fet_request(
        tmp_path, pdk_root, {"strap_splits": False}, "dp_narrow_off"
    )
    request["netlist"] = _narrow_pair_netlist(tmp_path)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["device_groups"][0]["resolved_params"]["strap_splits"] is False
    assert _strap_declined_warnings(response) == []


def test_unresolved_width_declines_with_a_warning():
    """No netlist-derived `w_um` and no override: the PCell default (0.42 um)
    applies, which is under the floor, so the decline is warned about too
    (the width in the message says it is unset, not a number)."""
    from klayout_tools.layout_plan_diff_pair import apply_strap_splits

    resolved: dict = {}
    derived: dict = {}
    group = {"id": "dp", "generator": "diff_pair"}
    warnings = apply_strap_splits(resolved, derived, group, {"splits": 3})

    assert "strap_splits" not in resolved and "strap_splits" not in derived
    (warning,) = warnings
    assert "strap_splits was NOT derived" in warning
    assert "unset" in warning
    assert "2 of the 3 sub-instances per device" in warning


@pytest.mark.parametrize("bad_w_um", [0, -1.0, "wide", None])
def test_invalid_explicit_w_um_is_left_to_the_generator(bad_w_um):
    """A non-numeric/non-positive explicit `w_um` is the generator
    validator's to reject, not a width-floor decline: no derivation and no
    strap warning from this helper."""
    from klayout_tools.layout_plan_diff_pair import apply_strap_splits

    resolved: dict = {}
    derived: dict = {}
    group = {"id": "dp", "generator": "diff_pair"}
    warnings = apply_strap_splits(
        resolved, derived, group, {"splits": 2, "w_um": bad_w_um}
    )

    assert warnings == []
    assert "strap_splits" not in resolved


def test_explicit_strap_splits_true_passes_through(tmp_path, pdk_root):
    """An explicit `params.strap_splits: true` (matching the derived value)
    is accepted with no divergence warning."""
    request = _two_fet_request(
        tmp_path, pdk_root, {"strap_splits": True}, "dp_explicit"
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["strap_splits"] is True
    assert not any(
        "strap_splits" in warning and "diverges" in warning
        for warning in response["warnings"]
    )


def test_explicit_unit_w_um_override_fits_strap_check(tmp_path, pdk_root):
    """An explicit `params.w_um` override is the *per-unit* PCell param
    (`apply_diff_pair_unit_w_um` deliberately leaves it alone), so the strap
    width check uses it as-is instead of dividing it by `splits` again -- a
    per-unit width that fits (3.0 um >= 2.35 um at splits=2) derives the
    strap. Dividing the override too (3.0/2 < 2.35) silently skipped it,
    leaving every sub-instance but one per device floating."""
    request = _two_fet_request(tmp_path, pdk_root, {"w_um": 3.0}, "dp_explicit_w_um")
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"]["w_um"] == 3.0
    assert group["resolved_params"]["strap_splits"] is True
    ports = _plan_device_ports(response)
    assert set(ports) == {"Q1_S", "Q1_D", "Q1_G", "Q2_S", "Q2_D", "Q2_G"}


def test_explicit_unit_w_um_override_below_floor_does_not_derive(tmp_path, pdk_root):
    """Symmetric negative: an explicit per-unit `w_um` below the strap
    floor derives nothing -- the override is *not* divided by `splits`, so
    the floor comparison is apples-to-apples on both sides -- and the
    decline is warned about, naming the override's own value (2.0, not
    2.0 / splits)."""
    request = _two_fet_request(
        tmp_path, pdk_root, {"w_um": 2.0}, "dp_explicit_w_um_narrow"
    )
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    group = response["device_groups"][0]
    assert group["resolved_params"].get("strap_splits") is None
    (warning,) = _strap_declined_warnings(response)
    assert "resolved unit w_um (2.0)" in warning
