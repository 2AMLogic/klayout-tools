"""Issue #4568: gf180mcu LDMOS_XTOR (226/0) is reported as an unsupported device."""

from __future__ import annotations

from klayout import db as kdb

from klayout_tools.extract import run_extract


def _layout(*, ldmos: bool, mos_cap: bool = False) -> kdb.Layout:
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    if ldmos:
        top.shapes(layout.layer(226, 0)).insert(kdb.Box(0, 0, 6000, 8000))
    if mos_cap:
        top.shapes(layout.layer(166, 5)).insert(kdb.Box(10000, 0, 14000, 4000))
    return layout


def _run(layout: kdb.Layout, tmp_path):
    gds = tmp_path / "t.gds"
    layout.write(str(gds))
    return run_extract(gds, "gf180mcu", output=str(tmp_path / "t.spice"))


def test_ldmos_marker_reported_as_unsupported(tmp_path):
    report = _run(_layout(ldmos=True), tmp_path)
    assert report["status"] == "extracted"
    (entry,) = report["unsupported_devices"]
    assert entry["kind"] == "ldmos"
    assert entry["marker"] == "226/0"
    assert entry["count"] == 1
    assert any("ldmos" in w and "226/0" in w for w in report["warnings"])


def test_no_ldmos_marker_reports_nothing(tmp_path):
    report = _run(_layout(ldmos=False), tmp_path)
    assert report["unsupported_devices"] == []
    assert not any("unsupported device" in w for w in report["warnings"])


def test_ldmos_and_mos_cap_both_reported(tmp_path):
    report = _run(_layout(ldmos=True, mos_cap=True), tmp_path)
    assert {e["kind"] for e in report["unsupported_devices"]} == {
        "ldmos",
        "mos_capacitor",
    }
