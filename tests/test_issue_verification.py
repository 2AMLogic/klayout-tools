import pytest
from unittest.mock import MagicMock
from klayout_tools.place_and_route_via_tip import _report, check_via_pad_tips
from klayout_tools.place_and_route_via_pad import _drcfill_regions


def test_drcfill_regions_none_def_reports_skipped():
    regions, scan_report = _drcfill_regions(MagicMock(), variant="asap7", def_path=None, dbu_um=0.001)
    assert regions is None
    assert scan_report["status"] == "skipped"
    assert scan_report["reason"] == "no DEF handed in"
    assert scan_report["unresolved"] == 0


def test_drcfill_regions_non_asap7_reports_skipped():
    regions, scan_report = _drcfill_regions(MagicMock(), variant="sky130", def_path="dummy.def", dbu_um=0.001)
    assert regions is None
    assert scan_report["status"] == "skipped"
    assert "not ASAP7" in scan_report["reason"]


def test_check_via_pad_tips_includes_router_fill_scan_in_report():
    kdb = MagicMock()
    layout = MagicMock()
    top_cell = MagicMock()
    
    # 1. Non-ASAP7
    res = check_via_pad_tips(kdb, layout=layout, top_cell=top_cell, variant="sky130", lef_to_gds={})
    assert res["status"] == "skipped"
    assert "router_fill_scan" in res
    assert res["router_fill_scan"]["status"] == "skipped"

    # 2. With drcfill_scan supplied
    custom_scan = {
        "status": "scanned",
        "reason": None,
        "unresolved": 0
    }
    # When variant is asap7 but no layers match
    res2 = check_via_pad_tips(
        kdb,
        layout=layout,
        top_cell=top_cell,
        variant="asap7",
        lef_to_gds={},
        drcfill_scan=custom_scan
    )
    assert "router_fill_scan" in res2
    assert res2["router_fill_scan"]["status"] == "scanned"
    assert res2["router_fill_scan"]["unresolved"] == 0
