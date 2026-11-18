"""Issue #6503: gf180mcu's medium-voltage `mos_array` ring geometry.

The auto tap ring's Nwell and `Dualgate` marker, sized off the ring's
*final* (grid-snapped) Comp edge, meet the native `gf180mcu.drc` runset's
`DF.4d_MV` (0.16um Nwell overlap of NCOMP) and `DV.6` (0.24um Dualgate
enclose COMP) at every `ring_padding_um` -- including the 1.05..1.28um
rule-swapping window where the old core-anchored marker traded `DV.6` for
`DV.3`, and off-grid paddings whose outward ring snap used to leave the
nominal well short *and* off-grid. Split out of `test_gen.py` (the
`test_gen_sg13g2_ptap.py` precedent) to respect the file-size ratchet.
"""

from __future__ import annotations

import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen import generate
from test_gen import _gf180mcu_device_active_region, _make_install


@pytest.fixture()
def both_pdk_root(tmp_path):
    root = tmp_path / "pdk_install"
    _make_install(root, "sky130A")
    _make_install(root, "gf180mcuD")
    return root


#: The issue #6503 reproduction cell's fixed params: a 1x1 gf180mcu `pfet`
#: `medium_voltage` array with a contacted gate and an east-opened auto tap
#: ring -- everything except `ring_padding_um`, which the tests below vary.
_ISSUE_6503_PARAMS = {
    "w_um": 0.42,
    "l_um": 0.55,
    "rows": 1,
    "cols": 1,
    "dummy": 0,
    "flavor": "pfet",
    "voltage_flavor": "medium_voltage",
    "gate_contact": True,
    "add_guard_ring": True,
    "ring_gap_side": "E",
    "ring_gap_um": 1.0,
    "ring_gap_offset_um": -0.7,
}

#: DF.4d_MV ("Min. (Nwell overlap of NCOMP) outside DNWELL") is 0.16um, and
#: DV.6 ("Min. Dualgate enclose COMP (except substrate tap)") is 0.24um --
#: both measured by the native runset with a strict less-than, so exactly
#: the threshold passes (the same no-headroom convention `_insert_implant_ring`
#: documents for NP.5b/PP.5b). gf180mcu's manufacturing grid is 0.005um.
_NWELL_ENCLOSURE_UM = 0.16
_DUALGATE_ENCLOSURE_UM = 0.24
_GRID_UM = 0.005


def _issue_6503_generate(tmp_path, root, ring_padding_um, name):
    """Generate the issue #6503 cell at one ``ring_padding_um`` (gf180mcuD
    synthetic install -- same family as the issue's gf180mcuA) and return
    its flattened layout, top cell and output path."""
    import klayout.db as kdb

    output = tmp_path / f"{name}.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "gf180mcuD", "root": str(root)},
            "params": dict(_ISSUE_6503_PARAMS, ring_padding_um=ring_padding_um),
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cells()[0]
    top.flatten(-1, True)
    return layout, top, str(output)


def _region_covering(top, layer_index, probe_box):
    """Whether ``top``'s merged shapes on ``layer_index`` overlap ``probe_box``."""
    import klayout.db as kdb

    region = kdb.Region(top.begin_shapes_rec(layer_index)).merged()
    return not region.overlapping(kdb.Region(probe_box)).is_empty()


def _assert_issue_6503_enclosures(layout, top):
    """The fix's geometry contract, on the flattened cell: the ring's Nwell
    and the `Dualgate` marker each sit exactly their rule's distance past
    the ring's *final* (grid-snapped) Comp edge on all four sides, the well
    edges stay on the manufacturing grid, and the east opening is preserved
    -- Comp absent through it, well and marker covering it."""
    import klayout.db as kdb

    dbu = layout.dbu
    n = int(round(_NWELL_ENCLOSURE_UM / dbu))
    d = int(round(_DUALGATE_ENCLOSURE_UM / dbu))
    grid = int(round(_GRID_UM / dbu))
    li_comp = layout.layer(22, 0)  # Comp: the tap ring's own band
    comp = top.bbox_per_layer(li_comp)
    nwell = top.bbox_per_layer(layout.layer(21, 0))
    dualgate = top.bbox_per_layer(layout.layer(55, 0))
    assert (comp.left - nwell.left, comp.bottom - nwell.bottom) == (n, n)
    assert (nwell.right - comp.right, nwell.top - comp.top) == (n, n)
    assert (comp.left - dualgate.left, comp.bottom - dualgate.bottom) == (d, d)
    assert (dualgate.right - comp.right, dualgate.top - comp.top) == (d, d)
    for edge in (nwell.left, nwell.bottom, nwell.right, nwell.top):
        assert edge % grid == 0, edge
    # The requested east opening (1.0um at offset -0.7) survives: no Comp
    # through the opening, Comp present in the band just beyond its ends.
    mid = (comp.bottom + comp.top) // 2 + int(round(-0.7 / dbu))
    u = lambda um: int(round(um / dbu))  # noqa: E731
    opening = kdb.Box(
        comp.right - u(0.40), mid - u(0.48), comp.right - u(0.02), mid + u(0.48)
    )
    assert not _region_covering(top, li_comp, opening)
    beyond = kdb.Box(
        comp.right - u(0.40), mid + u(0.52), comp.right - u(0.02), mid + u(0.80)
    )
    assert _region_covering(top, li_comp, beyond)
    # ...and both the well and the marker cover the opening, so the C-shaped
    # ring is still one enclosed conductor all the way around.
    assert _region_covering(top, layout.layer(21, 0), opening)
    assert _region_covering(top, layout.layer(55, 0), opening)


def test_mos_array_gf180_mv_ring_well_and_dualgate_enclose_final_ring(
    tmp_path, both_pdk_root
):
    """Issue #6503's own reproduction at the default `ring_padding_um`: the
    ring's Nwell is sized off the ring's *final* Comp edge (the nominal box
    missed DF.4d_MV's 0.16um by 10nm, and `_insert_ring`'s outward snap can
    eat another grid step), and the `Dualgate` marker grows past that same
    edge by DV.6's 0.24um instead of staying core-anchored inside the ring
    band."""
    layout, top, output = _issue_6503_generate(
        tmp_path, both_pdk_root, 0.5, "issue_6503_default"
    )
    _assert_issue_6503_enclosures(layout, top)

    drc_report = run_drc(output, "gf180mcu")
    assert drc_report["status"] == "clean", drc_report["violations"]


@pytest.mark.parametrize("ring_padding_um", [1.0, 1.05, 1.2, 1.3, 2.0, 0.5015, 1.0475])
def test_mos_array_gf180_mv_ring_enclosure_holds_across_padding_sweep(
    tmp_path, both_pdk_root, ring_padding_um
):
    """The same enclosure contract holds at every `ring_padding_um` the
    issue swept -- including inside the 1.05..1.28 rule-swapping window,
    where the core-anchored marker traded DV.6 for DV.3, and at off-grid
    paddings (0.5015, 1.0475) whose outward ring snap used to leave the
    nominal well both short of DF.4d_MV and off-grid (`nwell_OFFGRID`)."""
    layout, top, _ = _issue_6503_generate(
        tmp_path, both_pdk_root, ring_padding_um, f"issue_6503_p{ring_padding_um}"
    )
    _assert_issue_6503_enclosures(layout, top)


@pytest.mark.parametrize("medium_voltage", [False, True])
def test_mos_array_gf180_ringless_tap_well_margin_and_core_anchored_marker(
    tmp_path, both_pdk_root, medium_voltage
):
    """Ringless MV tap pads also need DF.4d_MV's 0.16um Nwell enclosure;
    the LV pads keep 0.15um. The MV marker keeps its pre-#6503 core box.
    Device Comp spans the footprint on three sides (the gate landing pad
    extends the top), so those sides pin the marker box exactly."""
    import klayout.db as kdb

    output = tmp_path / "issue_6503_ringless.gds"
    params = {
        k: v
        for k, v in _ISSUE_6503_PARAMS.items()
        if k
        not in ("add_guard_ring", "ring_gap_side", "ring_gap_um", "ring_gap_offset_um")
    }
    if not medium_voltage:
        params.pop("voltage_flavor")
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": params,
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cells()[0]
    top.flatten(-1, True)
    comp = kdb.Region(top.begin_shapes_rec(layout.layer(22, 0))).merged()
    nplus = kdb.Region(top.begin_shapes_rec(layout.layer(32, 0))).merged()
    pads = comp & nplus
    assert pads.count() == 4  # NCOMP well ties, excluding the PCOMP device
    nwell = kdb.Region(top.begin_shapes_rec(layout.layer(21, 0))).merged()
    margin = int(round((0.16 if medium_voltage else 0.15) / layout.dbu))
    # The pad well retains the ring-sized box: the north/south pads meet
    # its nominal edge; east/west pad edges sit half a tap width inside.
    expected_well = kdb.Box(
        *(int(round(edge / layout.dbu)) for edge in (-1.22, -1.22, 2.77, 3.06))
    )
    if medium_voltage:
        expected_well = expected_well.enlarged(int(round(0.01 / layout.dbu)))
    assert nwell.bbox() == expected_well
    assert pads.bbox().bottom - nwell.bbox().bottom == margin
    assert nwell.bbox().top - pads.bbox().top == margin
    assert (pads.sized(margin) - nwell).is_empty()
    dualgate = top.bbox_per_layer(layout.layer(55, 0))
    if not medium_voltage:
        assert dualgate.empty()
        return
    assert (pads - kdb.Region(dualgate)).is_empty()  # DF.4d_MV applies to these pads
    core = _gf180mcu_device_active_region(layout).bbox()
    expected = int(round(1.2 / layout.dbu))
    assert core.left - dualgate.left == expected
    assert core.bottom - dualgate.bottom == expected
    assert dualgate.right - core.right == expected
    assert dualgate.top - core.top >= expected


def test_mos_array_gf180_pfet_ring_without_mv_keeps_default_well_margin(
    tmp_path, both_pdk_root
):
    """A 3.3V pfet ring (no `voltage_flavor`) keeps the 0.15um default ring
    well margin byte-for-byte -- DF.4d_MV's 0.16um is scoped to the Comp the
    `Dualgate` marker covers, so the MV-gated bump leaves this geometry
    alone (issue #6503's non-medium-voltage-preserved criterion)."""
    import klayout.db as kdb

    output = tmp_path / "issue_6503_no_mv.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": {
                k: v for k, v in _ISSUE_6503_PARAMS.items() if k != "voltage_flavor"
            },
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cells()[0]
    top.flatten(-1, True)
    comp = top.bbox_per_layer(layout.layer(22, 0))
    nwell = top.bbox_per_layer(layout.layer(21, 0))
    default = int(round(0.15 / layout.dbu))
    assert (comp.left - nwell.left, comp.bottom - nwell.bottom) == (default, default)
    assert (nwell.right - comp.right, nwell.top - comp.top) == (default, default)
    assert 55 not in {layout.get_info(i).layer for i in layout.layer_indexes()}


def test_mos_array_gf180_nfet_ring_mv_draws_no_well_but_encloses_ring(
    tmp_path, both_pdk_root
):
    """The marker-past-ring growth is flavour-independent (the marker always
    is), while the ring's own well tie stays pfet-only: an nfet array with
    the marker and ring draws no Nwell at all yet still encloses the ring's
    Comp by 0.24um. This is a disclosed exception to issue #6503's non-pfet
    preservation criterion: only the marker box changes."""
    import klayout.db as kdb

    output = tmp_path / "issue_6503_nfet.gds"
    generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "gf180mcuD", "root": str(both_pdk_root)},
            "params": dict(_ISSUE_6503_PARAMS, flavor="nfet"),
            "options": {"output": str(output)},
        }
    )
    layout = kdb.Layout()
    layout.read(str(output))
    top = layout.top_cells()[0]
    top.flatten(-1, True)
    comp = top.bbox_per_layer(layout.layer(22, 0))
    dualgate = top.bbox_per_layer(layout.layer(55, 0))
    d = int(round(_DUALGATE_ENCLOSURE_UM / layout.dbu))
    assert (comp.left - dualgate.left, comp.bottom - dualgate.bottom) == (d, d)
    assert (dualgate.right - comp.right, dualgate.top - comp.top) == (d, d)
    assert top.bbox_per_layer(layout.layer(21, 0)).empty()


def test_voltage_flavor_ring_margins_resolve_gf180mcu_only():
    """The issue #6503 margins resolve on gf180mcu and nobody else, exactly
    like `_PDK_RING_WELL_MARGIN_UM`'s own per-family gating -- every other
    family's marker/well geometry stays byte-for-byte."""
    from klayout_tools.gen_layer_mv_ring import (
        _mv_ring_well_margin_um,
        _voltage_flavor_ring_margin_um,
    )
    from klayout_tools.gen_layer_params import _ring_well_margin_um

    assert _voltage_flavor_ring_margin_um("gf180mcu") == 0.24  # DV.6
    assert _mv_ring_well_margin_um("gf180mcu") == 0.16  # DF.4d_MV
    assert _mv_ring_well_margin_um("gf180mcu") > _ring_well_margin_um("gf180mcu")
    for family in ("sky130", "sg13g2", "sg13cmos5l"):
        assert _voltage_flavor_ring_margin_um(family) == 0.0
        assert _mv_ring_well_margin_um(family) == 0.0
