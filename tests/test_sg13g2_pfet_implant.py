"""Drawn PMOS source/drain masks and independent curated-deck coverage."""

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc
from klayout_tools.gen import generate


@pytest.mark.parametrize(
    ("params", "well_um"),
    [
        ({"dummy": 0}, 0.31),
        ({"cols": 3, "rows": 2}, 0.31),
        ({"voltage_flavor": "hv", "add_guard_ring": True}, 0.62),
    ],
)
def test_pfet_device_and_dummy_active_have_psd_and_well(tmp_path, params, well_um):
    root = tmp_path / "pdk"
    (root / "ihp-sg13g2" / "libs.tech").mkdir(parents=True)
    path = tmp_path / "pfet.gds"
    report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "ihp-sg13g2", "root": str(root)},
            "params": {"w_um": 1.6, "l_um": 0.4, "flavor": "pfet", **params},
            "options": {"output": str(path)},
        }
    )
    assert report["drc_hints"]["sd_implant_present"]
    layout = kdb.Layout()
    layout.read(str(path))
    top = layout.top_cell()

    def region(layer):
        return kdb.Region(top.begin_shapes_rec(layout.layer(layer, 0))).merged()

    device = region(1).overlapping(region(5))
    assert not device.is_empty()
    assert (device.sized(round(0.18 / layout.dbu)) - region(14)).is_empty()
    assert (device.sized(round(well_um / layout.dbu)) - region(31)).is_empty()
    drc = run_drc(str(path), "sg13g2")
    assert drc["violation_count"] == 0
    assert "implant_warnings" not in drc["coverage"]


@pytest.mark.parametrize(
    ("mask", "gate", "warns"),
    [
        (None, True, True),
        ("partial", True, True),
        ("psd", True, False),
        ("nsd", True, False),
        (None, False, False),
    ],
)
def test_missing_implant_on_independent_hierarchical_stream(
    tmp_path, mask, gate, warns
):
    layout = kdb.Layout()
    layout.dbu = 0.001
    child = layout.create_cell("DEVICE")
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(child.cell_index(), kdb.Trans(500, 700)))
    active = kdb.Box(0, 0, 1200, 800)
    child.shapes(layout.layer(1, 0)).insert(active)
    child.shapes(layout.layer(31, 0)).insert(active.enlarged(500))
    if gate:
        child.shapes(layout.layer(5, 0)).insert(kdb.Box(500, -200, 700, 1000))
    if mask:
        implant = kdb.Box(0, 0, 500, 800) if mask == "partial" else active.enlarged(180)
        child.shapes(layout.layer(7 if mask == "nsd" else 14, 0)).insert(implant)
    path = tmp_path / "control.gds"
    layout.write(str(path))
    for tile in (None, (3, 3)):
        coverage = run_drc(str(path), "sg13g2", top="TOP", tile=tile)["coverage"]
        warnings = coverage.get("implant_warnings", [])
        assert [w["cell"] for w in warnings] == (["TOP"] if warns else [])
        if warns:
            assert "pSD (14/0)" in warnings[0]["description"]
