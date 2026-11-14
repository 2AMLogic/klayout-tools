"""Replicated-driver fan-out control through ``klt synthesize`` (issue #4538).

A design that duplicates a high-fan-out driver to cap fan-out needs the copies
to survive the flow. Bare ``(* keep *)`` does not stop Yosys' ``opt_merge``
folding identical flops; ``(* keep_hierarchy *)`` on a small slice module does.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from klayout_tools import synthesize
from test_synthesize import _REAL_SKY130_VARIANT
from test_synthesize_arithmetic import _synth_script_path, requires_engine

requires_fanout_engine = pytest.mark.skipif(
    _REAL_SKY130_VARIANT is None,
    reason="needs yosys and an installed sky130_fd_sc_hd liberty",
)

#: Two registers with the same `d`, each driving its own half of the loads.
_BARE_KEEP_RTL = """
module top (input clk, input d, input [7:0] a, output [7:0] y);
  (* keep *) reg q0;
  (* keep *) reg q1;
  always @(posedge clk) begin q0 <= d; q1 <= d; end
  assign y = {a[7:4] & {4{q1}}, a[3:0] & {4{q0}}};
endmodule
"""

#: The same replication, but each copy lives in a `keep_hierarchy` slice.
_SLICE_RTL = """
(* keep_hierarchy *)
module slice (input clk, input d, input [3:0] a, output [3:0] y);
  reg q;
  always @(posedge clk) q <= d;
  assign y = a & {4{q}};
endmodule

module top (input clk, input d, input [7:0] a, output [7:0] y);
  slice s0 (.clk(clk), .d(d), .a(a[3:0]), .y(y[3:0]));
  slice s1 (.clk(clk), .d(d), .a(a[7:4]), .y(y[7:4]));
endmodule
"""


def _synthesize(tmp_path, monkeypatch, rtl):
    root, variant = _REAL_SKY130_VARIANT
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)
    (tmp_path / "top.v").write_text(rtl)
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "sources": ["top.v"],
                "hdl_toplevel": "top",
                "pdk": {"cell_library": "sky130_fd_sc_hd"},
            }
        )
    )
    report = synthesize.run_synthesize(str(request_path))
    script = _synth_script_path(tmp_path, report, "top")
    with open(os.path.join(os.path.dirname(script), "top_synth.v")) as handle:
        return report, handle.read()


def _flop_count(netlist: str) -> int:
    """Flop instances across the whole netlist, hierarchy included."""
    return len(re.findall(r"^\s*sky130_fd_sc_hd__df\w+\s", netlist, re.MULTILINE))


@requires_engine
@requires_fanout_engine
def test_bare_keep_does_not_stop_replicated_flops_merging(tmp_path, monkeypatch):
    """The documented counterexample: `(* keep *)` leaves one flop."""
    _, netlist = _synthesize(tmp_path, monkeypatch, _BARE_KEEP_RTL)
    assert _flop_count(netlist) == 1


@requires_engine
@requires_fanout_engine
def test_keep_hierarchy_slice_keeps_replicated_flops_distinct(tmp_path, monkeypatch):
    """The supported idiom: one flop per `keep_hierarchy` slice instance."""
    _, netlist = _synthesize(tmp_path, monkeypatch, _SLICE_RTL)
    assert len(re.findall(r"^\s*slice\s+\S+\s*\(", netlist, re.MULTILINE)) == 2
    assert _flop_count(netlist) == 1  # one definition, instantiated twice
