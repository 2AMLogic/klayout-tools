"""Tests for inferred-memory handling in `klt equiv`'s sequential engine
(issue #5369).

`equiv_make` refuses a module that still holds a `$mem`, so the sequential
engine's per-side prep runs `memory_map` right after `proc`; the
combinational engine's script does not. Split out of `tests/test_equiv.py`
to keep that file under its per-file size ceiling (issue #2766).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import equiv, equiv_sequential
from klayout_tools.equiv import run_equiv

HAVE_YOSYS = shutil.which("yosys") is not None

_GOLD_DFF_RTL = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= d;
endmodule
"""

_GATE_DFF_RTL_BUFFERED = """\
module top(input clk, input rst, input d, output reg q);
  wire d_buf = ~(~d);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= d_buf;
endmodule
"""

_SEQ_MEM_RTL = """\
module top(input clk, input we, input [1:0] wa, input [1:0] ra,
           input [3:0] d, output [3:0] q);
  reg [3:0] arr [0:3];
  always @(posedge clk) if (we) arr[wa] <= d;
  assign q = arr[ra];
endmodule
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _side(sources: list[str], top: str = "top", **extra) -> dict:
    return {"sources": sources, "top": top, **extra}


def test_sequential_engine_scripts_lower_memories_after_proc(tmp_path):
    """Issue #5369: both sequential script stages run `memory_map` right
    after `proc` on each side (so `equiv_make` never sees a `$mem`); the
    combinational engine's script does not."""
    gold = _side([_write(tmp_path / "gold.v", _GOLD_DFF_RTL)])
    gate = _side([_write(tmp_path / "gate.v", _GATE_DFF_RTL_BUFFERED)])

    stage1_path = str(tmp_path / "stage1.ys")
    equiv_sequential._write_sequential_stage1_script(
        script_path=stage1_path,
        gold=gold,
        gate=gate,
        port_map={},
        netlist_path=str(tmp_path / "netlist.v"),
        induction_depth=4,
    )
    stage2_path = str(tmp_path / "stage2.ys")
    equiv_sequential._write_sequential_stage2_script(
        script_path=stage2_path,
        gold=gold,
        gate=gate,
        port_map={},
        ports={"clk": "input", "d": "input", "q": "output"},
        bmc_depth=4,
        sat_timeout_s=60,
    )
    for path in (stage1_path, stage2_path):
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        assert lines.count("memory_map") == 2  # once per gold/gate side
        proc_idxs = [i for i, line in enumerate(lines) if line == "proc"]
        assert len(proc_idxs) == 2
        for i in proc_idxs:
            assert lines[i + 1] == "memory_map"

    comb_path = str(tmp_path / "comb.ys")
    equiv._write_script(
        script_path=comb_path,
        gold=gold,
        gate=gate,
        port_map={},
        netlist_path=str(tmp_path / "comb_netlist.v"),
        sat_timeout_s=60,
    )
    comb_lines = Path(comb_path).read_text(encoding="utf-8").splitlines()
    assert "memory_map" not in comb_lines


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_handles_inferred_memory(tmp_path):
    """Issue #5369: a runtime-indexed unpacked array (an inferred `$mem`)
    reaches `equiv_make` and gets a real verdict, not an EquivError."""
    _write(tmp_path / "gold.v", _SEQ_MEM_RTL)
    _write(tmp_path / "gate.v", _SEQ_MEM_RTL)
    request_path = tmp_path / "r.json"
    request_path.write_text(
        json.dumps(
            {
                "gold": _side(["gold.v"]),
                "gate": _side(["gate.v"]),
                "engine": "yosys-sequential",
            }
        ),
        encoding="utf-8",
    )

    report = run_equiv(str(request_path))

    assert report["status"] == "equivalent"
