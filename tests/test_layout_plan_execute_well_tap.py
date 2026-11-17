"""Tests for the ``WELL_TAP`` group body tie (#5948).

`mos_array`'s ring-less sg13cmos5l pfet draws one shared `WELL_TAP` port
(#1473) -- a third group-level body-tie shape alongside the gf180mcu
`TAP_PAD<k>` pads and a group's own guard-ring `TAP_<side>` port, both covered
in `test_layout_plan_execute_body_net.py`. Split out of that file to keep it
under its file-size ceiling. Like its siblings, PDK resolution runs against a
fabricated open_pdks-layout install under `tmp_path` -- CI never downloads a
real PDK. The extraction round-trip at the bottom needs no install at all:
`klt extract` reads the curated in-tree sg13cmos5l deck.
"""

from __future__ import annotations

import pytest

from klayout_tools import pdk
from klayout_tools.layout_plan_execute import (
    execute_layout_plan_document,
    exit_code_for,
    partial_success,
)
from test_layout_plan_execute import _make_install
from test_layout_plan_execute_body_net import (
    _amp_request,
    _extracted_bodies,
    _mos_group,
    _pins_of,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


@pytest.fixture()
def sg13cmos5l_pdk_root(tmp_path):
    # The standalone IHP cmos5l clone's own directory name -- the variant
    # `klt pdk` resolves (#1231's alias table maps it back to the sg13cmos5l
    # family whose pfet arrays draw `WELL_TAP`, #1473).
    root = tmp_path / "ihp-sg13cmos5l_pdk_install"
    _make_install(root, "ihp-sg13cmos5l")
    return root


# The sg13cmos5l stand-in for the gf180mcu amp netlist -- IHP's own 4-terminal
# LV/HV model names (`pdk_models._MOS_MODEL_TABLE` binds them for the
# sg13cmos5l deck), so the digest reports a separate `B` terminal per device.
_SG13_AMP_NETLIST = """
.subckt amp VDD VSS INP OUTP
MP1 OUTP INP VDD VDD sg13_hv_pmos L=0.4U W=1.0U
MN1 OUTN INP VSS VSS sg13_hv_nmos L=0.4U W=1.0U
.ends
"""


def _sg13_amp_request(tmp_path, sg13cmos5l_pdk_root):
    """The sg13cmos5l amp, routed on metal2 throughout: the pfet's `WELL_TAP`
    port sits past the unit's own drain on the same metal1 line, so a
    same-layer route from the source pad to the in-array tap would cross that
    drain pad -- the behind-the-pin guard (#1895) rightly refuses, and the
    unrouted reason itself names the metal2/via-stack alternative."""
    groups = [
        _mos_group("mp", [{"name": "P1", "device_class": "SG13_HV_PMOS"}], "pfet"),
        _mos_group("mn", [{"name": "N1", "device_class": "SG13_HV_NMOS"}], "nfet"),
    ]
    return _amp_request(
        tmp_path,
        sg13cmos5l_pdk_root,
        "ihp-sg13cmos5l",
        _SG13_AMP_NETLIST,
        groups,
        {"layer_role": "metal2", "width_um": 0.44},
    )


def test_well_tap_is_bound_to_the_declared_bulk_net(tmp_path, sg13cmos5l_pdk_root):
    """A ring-less sg13cmos5l pfet `mos_array` draws one shared `WELL_TAP` port
    (#1473) -- a third group-level tie shape the binder used to miss (#5948),
    warning that the generator reported no tap/ring port at all. It now binds
    like a tap pad: one pin on the declared bulk net, no untied-body warning.

    The nfet group of the same plan genuinely has no tie port on this family
    (only the pfet well is tapped), so its warning stays -- the remaining case
    the warning's cause clause describes."""
    request = _sg13_amp_request(tmp_path, sg13cmos5l_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert ("mp", "WELL_TAP") in _pins_of(response, "VDD")
    # One tie per group, never more -- the array draws a single well tap.
    taps = [
        port
        for owner, port in _pins_of(response, "VDD")
        if owner == "mp" and (port.startswith("TAP_") or port == "WELL_TAP")
    ]
    assert taps == ["WELL_TAP"]
    body_warnings = [w for w in response["warnings"] if "left untied" in w]
    assert len(body_warnings) == 1
    assert "'mn'" in body_warnings[0] and "'VSS'" in body_warnings[0]


def test_well_tap_binding_routes_and_exits_zero(tmp_path, sg13cmos5l_pdk_root):
    request = _sg13_amp_request(tmp_path, sg13cmos5l_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))

    assert response["unrouted_nets"] == []
    assert response["unmapped_netlist_nets"] == []
    assert partial_success(response) is False
    assert exit_code_for(response) == 0


def test_well_tap_binding_extracts_bodies_onto_a_named_net(
    tmp_path, sg13cmos5l_pdk_root
):
    """The evidence behind the sg13cmos5l criterion (#5948): with `WELL_TAP`
    bound, the pfet body extracts onto a *named* net carrying the declared
    bulk net -- not onto an anonymous `$` well -- and
    `unbiased_pmos_body_nets[]` stays empty (the deck's own tap-derived tie
    check, #1414). KLayout names the merged conductor `VDD|WELL_TAP` because
    the generator draws its own fixed `WELL_TAP` label on the tap metal
    (#1473) and both texts survive the merge; only the presence of a real
    label matters, and the `$`-free assertion is what rules the anonymous
    well out. The nfet body stays on the deck-synthesized `vsubs` global --
    sg13cmos5l draws no substrate tie for it, and this issue does not claim
    one."""
    request = _sg13_amp_request(tmp_path, sg13cmos5l_pdk_root)
    response = execute_layout_plan_document(request, request_dir=str(tmp_path))
    assert response["unrouted_nets"] == []

    bodies, report = _extracted_bodies(
        response["gds_path"], tmp_path, "welltap", deck="sg13cmos5l"
    )
    assert "VDD" in bodies["pfet"]
    assert "$" not in bodies["pfet"]
    assert report["unbiased_pmos_body_nets"] == []
