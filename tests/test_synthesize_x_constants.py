"""Surviving x-constant disclosure (issue #5074): the written netlist is
scanned with the same regex `klt place-and-route`'s pre-flight uses, so a
netlist P&R will refuse is reported by `klt synthesize` first."""

import json

import pytest

from klayout_tools.synthesize import run_synthesize
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
