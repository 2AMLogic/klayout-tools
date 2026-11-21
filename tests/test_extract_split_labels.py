"""`klt extract` split_net_labels[] -- one drawn label on 2+ nets (issue #3920)."""

from __future__ import annotations

import klayout.db as kdb

from klayout_tools.extract import run_extract
from test_extract import _make_inverter_layout, _write_gds


def _with_island(layout: kdb.Layout, text: str, x: int) -> kdb.Layout:
    """Add a disjoint li1 island labelled ``text`` -- a second, unstrapped net."""
    top = layout.top_cell()
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(x, 5000, x + 400, 5400))
    top.shapes(layout.layer(67, 5)).insert(kdb.Text(text, kdb.Trans(x + 200, 5200)))
    return layout


def _strapped(layout: kdb.Layout) -> kdb.Layout:
    """Strap the fixture's two ``Y`` drain pads into one net (one li1 bar)."""
    top = layout.top_cell()
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(1800, 200, 2000, 2800))
    return layout


def _extract(layout, tmp_path):
    path = _write_gds(layout, tmp_path / "split.gds")
    return run_extract(path, "sky130", output=str(tmp_path / "split.spice"))


def test_split_label_reported_with_warning(tmp_path):
    report = _extract(
        _with_island(_strapped(_make_inverter_layout()), "VPWR", 6000), tmp_path
    )

    assert len(report["split_net_labels"]) == 1
    entry = report["split_net_labels"][0]
    assert entry["label"] == "VPWR"
    assert entry["count"] == 2
    assert len(set(entry["nets"])) == 2
    assert {n["net_id"] for n in report["nets"]} >= set(entry["nets"])
    assert any("'VPWR'" in w and "2 separate nets" in w for w in report["warnings"])
    assert report["merged_net_labels"] == []


def test_connected_labels_not_reported(tmp_path):
    report = _extract(_strapped(_make_inverter_layout()), tmp_path)

    assert report["split_net_labels"] == []
    assert not any("separate nets" in w for w in report["warnings"])


def test_merged_labels_not_reported_as_split(tmp_path):
    report = _extract(_strapped(_make_inverter_layout(extra_y_label="Y2")), tmp_path)

    assert report["merged_net_labels"] == [{"net": "Y|Y2", "labels": ["Y", "Y2"]}]
    assert report["split_net_labels"] == []
