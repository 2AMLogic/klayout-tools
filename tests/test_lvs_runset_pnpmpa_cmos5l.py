"""``pnpMPA`` on ``sg13cmos5l``: only the PDK runset recognises it (issue #4927).

The curated ``sg13cmos5l`` deck declares no bipolar class, so the PDK's own
runset (``bjt_extraction.lvs:78-79``, ``CustomBJTExtractor.new('pnpMPA',
true)``) is the supported path. Skipped when klayout or the PDK is absent.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from klayout_tools.lvs import run_lvs


def _pnpmpa_inputs():
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13cmos5l")["assets"]["klayout"]
    except Exception:  # noqa: BLE001 - any resolution failure means "absent"
        pytest.skip("no ihp-sg13cmos5l PDK install")
    lvs_dir = os.path.join(root, "tech", "lvs")
    case = os.path.join(lvs_dir, "testing", "testcases", "unit", "bjt_devices")
    paths = (
        os.path.join(lvs_dir, "sg13cmos5l.lvs"),
        os.path.join(case, "layout", "pnpMPA.gds"),
        os.path.join(case, "netlist", "pnpMPA.cdl"),
    )
    if not all(os.path.isfile(p) for p in paths):
        pytest.skip("ihp-sg13cmos5l install lacks the pnpMPA LVS testcase")
    return paths


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_runset_recognises_pnpmpa_on_sg13cmos5l():
    deck, gds, cdl = _pnpmpa_inputs()
    report = run_lvs(
        json.dumps(
            {
                "engine": "klayout-runset",
                "layout": {"file": gds, "top": "pnpMPA_lvs"},
                "reference": {"netlist": cdl, "top": "pnpMPA_lvs"},
                "deck_file": deck,
            }
        )
    )
    assert report["engine"] == "klayout-runset"
    assert report["status"] == "match"
    # pnpMPA (QQ1) plus the PDK testcase's own ptap1 tap resistor (RR2).
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert "pnpMPA" in report["coverage"]["checked"]


def test_curated_sg13cmos5l_deck_extracts_no_pnpmpa():
    """Records the contrast: the curated deck sees no bipolar device here."""
    from klayout_tools.extract import extract_netlist_from_layout

    _, gds, _ = _pnpmpa_inputs()
    netlist = extract_netlist_from_layout(gds, "sg13cmos5l", top="pnpMPA_lvs")[0]
    assert len(list(netlist.top_circuit().each_device())) == 0
