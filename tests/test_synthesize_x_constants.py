"""Surviving x-constant disclosure (issue #5074): the written netlist is
scanned with the same regex `klt place-and-route`'s pre-flight uses, so a
netlist P&R will refuse is reported by `klt synthesize` first."""

import json

import pytest

from klayout_tools import synthesize
from klayout_tools.synthesize import run_synthesize
from klayout_tools.synthesize_x_scan import netlist_x_constant_warning
from test_synthesize import _setup_success_env, _stub_yosys_success

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_surviving_x_constant_assign_adds_warning(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(
        monkeypatch,
        netlist_body=(
            "module gcd();\n"
            "  assign \\some.net  = 24'hxxxxxx;\n"
            "  assign ok = 1'h0;\n"
            "  // assign commented = 1'hx;\n"
            "  assign x = 1'hx;\n"
            "endmodule\n"
        ),
    )

    report = run_synthesize(request_path)

    text = json.dumps(report["warnings"])
    assert "netlist_x_constants" in text
    assert "2 bare x-valued constant" in text
    assert "line(s) 2, 5" in text
    assert "24'hxxxxxx" in text


def test_clean_netlist_has_no_x_constant_warning(tmp_path, monkeypatch):
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(
        monkeypatch, netlist_body="module gcd();\n  assign a = 8'hbe;\nendmodule\n"
    )

    report = run_synthesize(request_path)

    assert "netlist_x_constants" not in json.dumps(report["warnings"])


def test_x_constants_under_unsupported_library_blame_the_library(tmp_path, monkeypatch):
    """Issue #5074: when the `cell_library` had no tie-cell mapping, the x
    bits survived because `setundef -zero`/`hilomap` never ran -- so the
    advice must name that cause, not send the user after their RTL."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    monkeypatch.delitem(synthesize._TIE_CELLS, "sky130_fd_sc_hd")
    _stub_yosys_success(
        monkeypatch, netlist_body="module gcd();\n  assign x = 1'hx;\nendmodule\n"
    )

    report = run_synthesize(request_path)

    text = report["warnings"]["by_category"]
    assert text["netlist_x_constants"] == 1
    advice = next(
        rep["text"]
        for rep in report["warnings"]["representatives"]
        if rep["category"] == "netlist_x_constants"
    )
    assert "1 bare x-valued constant" in advice
    assert "sky130_fd_sc_hd" in advice
    assert "no verified tie-cell mapping" in advice
    assert "unsupported_cell_library" in advice
    # The RTL advice is actively wrong for this cause and must not appear.
    assert "in the RTL or drive it explicitly" not in advice


def test_x_constants_under_supported_library_keep_rtl_advice(tmp_path, monkeypatch):
    """The supported-library path is untouched by #5074's re-scope: with a
    verified tie-cell mapping the pass did run, so the RTL is at fault."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    assert "sky130_fd_sc_hd" in synthesize._TIE_CELLS
    _stub_yosys_success(
        monkeypatch, netlist_body="module gcd();\n  assign x = 1'hx;\nendmodule\n"
    )

    report = run_synthesize(request_path)

    advice = next(
        rep["text"]
        for rep in report["warnings"]["representatives"]
        if rep["category"] == "netlist_x_constants"
    )
    assert "Remove the undriven/unused source signal in the RTL" in advice
    assert "tie-cell mapping" not in advice


def test_netlist_x_constant_warning_defaults_to_rtl_advice():
    """The `has_tie_cells` keyword defaults to the supported-library
    behaviour, so a caller that does not thread it sees today's text."""
    warning = netlist_x_constant_warning("assign x = 1'hx;\n")

    assert "in the RTL or drive it explicitly" in warning["netlist_x_constants"]


def test_netlist_x_constant_warning_omits_unnamed_library():
    """`cell_library=None` with no tie cells still blames the library, just
    without a name to quote."""
    warning = netlist_x_constant_warning("assign x = 1'hx;\n", has_tie_cells=False)

    text = warning["netlist_x_constants"]
    assert "cell_library has no verified tie-cell mapping" in text
    assert "None" not in text
