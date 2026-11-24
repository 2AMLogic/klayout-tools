"""Coverage for SG13G2 implant-to-active rules on the #7640 layout."""

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.drc import run_drc


@pytest.mark.parametrize("psd_margin_nm", [None, 0, 179, 180])
def test_psd_coverage_tracks_drawn_implant(tmp_path: Path, psd_margin_nm: int | None):
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    active = kdb.Box(0, 0, 1000, 1000)
    top.shapes(layout.layer(1, 0)).insert(active)
    top.shapes(layout.layer(31, 0)).insert(active.enlarged(500))
    if psd_margin_nm is not None:
        top.shapes(layout.layer(14, 0)).insert(active.enlarged(psd_margin_nm))
    path = tmp_path / "implant.gds"
    layout.write(str(path))

    report = run_drc(str(path), "sg13g2")
    assert report["status"] == "clean"
    unchecked = {
        item["rule"]
        for item in report["coverage"]["unchecked_rules"]
        if item["layer"] == "14/0"
    }
    expected = {"pSD.c", "pSD.c1", "pSD.d", "pSD.d1"}
    assert unchecked == (expected if psd_margin_nm is not None else set())
