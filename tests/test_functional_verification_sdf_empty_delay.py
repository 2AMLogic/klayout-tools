"""Valueless OpenSTA IOPATHs and their functional-verification receipts (#7481)."""

import json

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS
from klayout_tools import functional_verification_sdf_steps as steps
from klayout_tools.cli import main

_VALUED = "(IOPATH a y (7:7:7) (7:7:7))"


def _cell(instance, entries):
    return (
        f'(CELL (CELLTYPE "probe") (INSTANCE {instance}) (DELAY (ABSOLUTE {entries})))'
    )


def _sdf(*cells):
    return (
        '(DELAYFILE (SDFVERSION "3.0") (DESIGN "top") (TIMESCALE 1ps)\n'
        + "\n".join(cells)
        + ")\n"
    )


def test_removes_complete_entries_and_counts_instances(tmp_path):
    kept = (
        _VALUED,
        "(IOPATH CLK Q (0:0:0) (0:0:0))",
        "(IOPATH a y () ())",
        "(INTERCONNECT u/Q v/D (3:3:3))",
        "(IOPATH only_one_port)",
        "(IOPATH a y junk)",
        "(COND D (IOPATH a y) trailing)",
    )
    empty = "(IOPATH\n (posedge CLK)\n Q )"
    original = _sdf(
        _cell("u", "\n".join((*kept, empty, "(COND D (IOPATH b[1] y))"))),
        _cell("v", empty + "\n(CONDELSE (IOPATH a y))"),
    )
    path = tmp_path / "in.sdf"
    path.write_text(original)
    normalized, count, arcs = steps._drop_sdf_empty_delay_iopaths(str(path))
    assert count == 4
    assert arcs == [
        {"celltype": "probe", "from": src, "to": dst}
        for src, dst in [("CLK", "Q"), ("a", "y"), ("b[1]", "y")]
    ]
    assert normalized == _sdf(
        _cell("u", "\n".join(kept) + "\n\n"),
        '(CELL (CELLTYPE "probe") (INSTANCE v) )',
    )
    assert path.read_text() == original


@SKIP_NO_ICARUS
@pytest.mark.parametrize(
    ("with_empty", "malformed"), [(False, False), (True, False), (True, True)]
)
def test_cli_preserves_seven_ps_path_and_reports_parser_errors(
    tmp_path, capsys, with_empty, malformed
):
    (tmp_path / "bench.v").write_text(
        """`timescale 1ps/1ps
module probe(input a, output y);
  specify (a => y) = 0; endspecify
  buf(y, a);
endmodule
module top(input a, output y); probe u(a,y); endmodule
module tb;
  reg a; wire y; top dut(a,y);
  initial begin a=0; #20; a=1; #20; $finish; end
  always @(posedge y) begin
    if ($time == 27) $display("PASS"); else $display("FAIL");
  end
endmodule
"""
    )
    entries = _VALUED
    if with_empty:
        entries = "(IOPATH a y)\n" + entries + "\n(COND ~a (IOPATH a y))"
    if malformed:
        entries += "\n(IOPATH a)"
    original = _sdf(_cell("u", entries))
    path = tmp_path / "route.sdf"
    path.write_text(original)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": "klt.functional_verification.request/1",
                "engine": "icarus",
                "sources": ["bench.v"],
                "hdl_toplevel": "top",
                "testbench": {
                    "verilog_top": "tb",
                    "dut_instance": "tb.dut",
                    "pass_marker": "PASS",
                    "fail_marker": "FAIL",
                },
                "options": {"sdf": {"file": path.name, "corner": "typ"}},
            }
        )
    )
    code = main(["functional-verification", str(request), "--format", "json"])
    captured = capsys.readouterr()
    assert path.read_text() == original
    if malformed:
        assert code == 1
        error = json.loads(captured.err)["error"]
        assert "Invalid/malformed IOPATH" in error["message"]
        assert error["sdf"]["dropped"]["iopath_empty_delay"]["count"] == 1
        return
    assert code == 0
    report = json.loads(captured.out)
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is with_empty
    if with_empty:
        assert set(sdf["dropped"]) == {"iopath_empty_delay", "iopath_cond_expression"}
        for drop in sdf["dropped"].values():
            assert drop["count"] == 1
            assert drop["arcs"] == [{"celltype": "probe", "from": "a", "to": "y"}]
            assert drop["reason"]
    else:
        assert sdf["dropped"] == {}
