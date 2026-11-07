"""Tests for `options.ignore_mos_bulk` (issue #5125)."""

from __future__ import annotations

import json

import pytest

from klayout_tools import lvs
from klayout_tools.lvs import LvsError, run_lvs

# Layout whose bulks sit on anonymous nets (as a tap-less extraction leaves them).
_LAYOUT = """
.subckt inv A Y VPWR VGND
M1 Y A VGND pwell_anon nfet L=0.15U W=0.65U
M2 Y A VPWR nwell_anon pfet L=0.15U W=1.0U
.ends
"""
_REFERENCE = """
.subckt inv A Y VPWR VGND
XM1 Y A VGND VGND sky130_fd_pr__nfet_01v8 L=0.15u W=0.65u
XM2 Y A VPWR VPWR sky130_fd_pr__pfet_01v8 L=0.15u W=1.0u
.ends
"""
# G/D/S topology error: the NMOS gate is tied to Y instead of A.
_LAYOUT_BAD = _LAYOUT.replace("M1 Y A VGND", "M1 Y Y VGND")


def _request(tmp_path, layout_text, options):
    layout = tmp_path / "layout.spice"
    layout.write_text(layout_text)
    reference = tmp_path / "ref.spice"
    reference.write_text(_REFERENCE)
    return json.dumps(
        {
            "layout": {"netlist": str(layout), "top": "inv"},
            "reference": {
                "netlist": str(reference),
                "top": "inv",
                "form": "subckt-call",
            },
            "options": options,
        }
    )


def test_default_does_not_ignore_bulk(tmp_path):
    report = run_lvs(_request(tmp_path, _LAYOUT, {}))
    assert report["status"] == "mismatch"
    assert report["options"]["ignore_mos_bulk"] is False
    assert not [
        m for m in report["mismatches"] if m["category"] == "device.bulk_ignored"
    ]


def test_ignore_mos_bulk_matches_and_discloses(tmp_path):
    report = run_lvs(_request(tmp_path, _LAYOUT, {"ignore_mos_bulk": True}))
    assert report["status"] == "match"
    assert report["options"]["ignore_mos_bulk"] is True
    entries = [
        m for m in report["mismatches"] if m["category"] == "device.bulk_ignored"
    ]
    # One warning per MOS class and side (2 classes x 2 sides).
    assert len(entries) == 4
    assert {e["severity"] for e in entries} == {"warning"}
    assert {e["side"] for e in entries} == {"layout", "reference"}


def test_ignore_mos_bulk_still_catches_topology_error(tmp_path):
    report = run_lvs(_request(tmp_path, _LAYOUT_BAD, {"ignore_mos_bulk": True}))
    assert report["status"] == "mismatch"


def test_non_boolean_is_request_error():
    with pytest.raises(LvsError, match="must be a boolean"):
        lvs._parse_ignore_mos_bulk({"ignore_mos_bulk": "yes"}, "klayout")


def test_netgen_engine_is_request_error():
    with pytest.raises(LvsError, match="engine 'klayout'"):
        lvs._parse_ignore_mos_bulk({"ignore_mos_bulk": True}, "netgen")
    assert lvs._parse_ignore_mos_bulk({"ignore_mos_bulk": False}, "netgen") is False


def test_rerun_reconstructs_ignore_mos_bulk():
    committed = {
        "options": {"ignore_mos_bulk": True},
        "layout": "layout.spice",
        "reference": "ref.spice",
        "top": "inv",
    }
    request = lvs._reconstruct_lvs_request(committed)
    assert request["options"]["ignore_mos_bulk"] is True
    committed["options"]["ignore_mos_bulk"] = False
    request = lvs._reconstruct_lvs_request(committed)
    assert "ignore_mos_bulk" not in request.get("options", {})
