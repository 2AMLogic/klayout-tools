"""Tests for the leaf-cell Y-band partition under ``klt economy --attribute``
(issue #5189).

Split out of ``test_economy.py`` for the file-size ratchet.
"""

import klayout.db as kdb
import pytest

from klayout_tools.economy import economy_report
from klayout_tools.economy_bands import leaf_bands


def _make_leaf_rows_layout():
    layout = kdb.Layout()
    m1 = layout.layer(1, 0)
    m2 = layout.layer(2, 0)
    top = layout.create_cell("LEAF")
    top.shapes(m1).insert(kdb.Box(0, 0, 4000, 1000))
    top.shapes(m2).insert(kdb.Box(0, 200, 4000, 800))  # same band as m1 row
    top.shapes(m1).insert(kdb.Box(0, 3000, 4000, 4000))
    return layout


def test_attribution_leaf_bands(tmp_path):
    path = tmp_path / "a.gds"
    _make_leaf_rows_layout().write(str(path))
    attr = economy_report(str(path), attribute=True)["attribution"]
    bands = attr["bands"]
    assert [b["layers"] for b in bands] == [
        ["1/0"],
        ["1/0", "2/0"],
        ["1/0"],
        [],
        ["1/0"],
    ]
    assert sum(b["area_um2"] for b in bands) == pytest.approx(4.0 * 4.0)
    channel = bands[3]
    assert channel["drawn_area_um2"] == 0
    assert channel["area_um2"] == pytest.approx(4.0 * 2.0)
    assert bands[1]["drawn_area_um2"] == pytest.approx(4.0 * 0.6)


def test_attribution_leaf_single_layer_one_band(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 4000, 2000))
    path = tmp_path / "a.gds"
    layout.write(str(path))
    attr = economy_report(str(path), attribute=True)["attribution"]
    assert len(attr["bands"]) == 1
    assert attr["bands"][0]["layers"] == ["1/0"]
    assert attr["bands"][0]["area_um2"] == pytest.approx(8.0)


def test_attribution_bands_absent_with_children(tmp_path):
    layout = kdb.Layout()
    li = layout.layer(1, 0)
    sub = layout.create_cell("SUB")
    sub.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Vector(0, 0))))
    path = tmp_path / "a.gds"
    layout.write(str(path))
    assert "bands" not in economy_report(str(path), attribute=True)["attribution"]


def test_leaf_bands_empty_bbox_returns_no_bands():
    layout = kdb.Layout()
    layout.layer(1, 0)
    top = layout.create_cell("EMPTY")
    assert leaf_bands(kdb, layout, top, top.bbox(), layout.dbu, set()) == []
