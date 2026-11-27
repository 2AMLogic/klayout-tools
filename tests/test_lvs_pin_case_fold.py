"""Gate-level Verilog reference: case-insensitive library pin binding (issue
#4815). Split out of test_lvs.py for the file-size ratchet."""

from __future__ import annotations

import json

import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.verilog_netlist import (
    VerilogNetlistError,
    convert_gate_level_verilog,
    convert_gate_level_verilog_expanded,
)
from test_lvs import (
    _GATE_LEVEL_LAYOUT_SPICE,
    _GATE_LEVEL_LIBRARY_SPICE,
    _GATE_LEVEL_REFERENCE_VERILOG,
    _make_fake_pdk_library,
    _write,
)

_CASE_FOLD_VERILOG = (
    "module m(a, y);\n  input a;\n  output y;\n"
    "  cellx u0 (.RESETN(a), .Y(y));\n"
    "  cellx u1 (.RESETN(a), .Y(y));\nendmodule\n"
)


def test_convert_gate_level_verilog_folds_pin_case_when_unambiguous():
    """Issue #4815: Liberty-spelled `RESETN` binds to CDL-spelled `RESETn`."""
    folds: list[dict[str, str]] = []
    out = convert_gate_level_verilog(
        _CASE_FOLD_VERILOG,
        pin_order_lookup=lambda _c: ["RESETn", "Y"],
        port_case_folds=folds,
    )
    assert ".SUBCKT cellx RESETn Y" in out
    assert [f["to"] for f in folds] == ["RESETn", "RESETn"]
    assert folds[0] == {
        "cell": "cellx",
        "instance": "u0",
        "from": "RESETN",
        "to": "RESETn",
    }


def test_convert_gate_level_verilog_exact_match_wins_and_records_no_fold():
    text = (
        "module m(a, y);\n  input a;\n  output y;\n"
        "  cellx u0 (.A(a), .Y(y));\nendmodule\n"
    )
    folds: list[dict[str, str]] = []
    out = convert_gate_level_verilog(
        text, pin_order_lookup=lambda _c: ["A", "a", "Y"], port_case_folds=folds
    )
    assert folds == []
    assert out == convert_gate_level_verilog(
        text, pin_order_lookup=lambda _c: ["A", "a", "Y"]
    )


def test_convert_gate_level_verilog_case_twin_pins_stay_refused():
    with pytest.raises(VerilogNetlistError, match="not in its resolved PDK pin list"):
        convert_gate_level_verilog(
            _CASE_FOLD_VERILOG,
            pin_order_lookup=lambda _c: ["RESETn", "RESETn".swapcase(), "Y"],
        )


def test_convert_gate_level_verilog_fold_colliding_with_bound_pin_errors():
    text = (
        "module m(a, y);\n  input a;\n  output y;\n"
        "  cellx u0 (.RESETn(a), .RESETN(a), .Y(y));\nendmodule\n"
    )
    with pytest.raises(VerilogNetlistError, match="connects pin 'RESETn' twice"):
        convert_gate_level_verilog(text, pin_order_lookup=lambda _c: ["RESETn", "Y"])


def test_convert_gate_level_verilog_expanded_folds_pin_case():
    folds: list[dict[str, str]] = []
    out = convert_gate_level_verilog_expanded(
        _CASE_FOLD_VERILOG,
        pin_order_lookup=lambda _c: ["RESETn", "Y"],
        cell_body_lookup=lambda _c: ".SUBCKT cellx RESETn Y\n.ENDS cellx\n",
        port_case_folds=folds,
    )
    assert len(folds) == 2
    assert "Xu0 RESETn" not in out  # nets, not pin names, appear on the X line
    assert ".SUBCKT cellx RESETn Y" in out


def test_run_lvs_gate_level_verilog_pin_case_fold_is_disclosed_warning(tmp_path):
    """Issue #4815: the fold surfaces as a warning entry, status unchanged."""
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    reference_path = _write(
        tmp_path / "ref.v",
        _GATE_LEVEL_REFERENCE_VERILOG.replace(".A(in)", ".a(in)"),
    )
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == "reference.gate_level_pin_case_folded"
    ]
    assert entry["severity"] == "warning"
    assert entry["details"] == {
        "cell": "mylib__inv_1",
        "from": "a",
        "to": "A",
        "instance_count": 1,
    }
