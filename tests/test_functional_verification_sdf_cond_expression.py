"""Issue #5296: `COND`-wrapped `IOPATH` entries with operator conditions.

Icarus 13.0 rejects `COND ~D`, `COND D&SETN`, `COND ~CLK&~D&SETN` as
`Invalid/malformed COND` (aborting the whole annotation) but accepts a bare
`COND D` and `COND D==1'b1`. `_drop_sdf_cond_expression_iopaths` drops only
the rejected shapes and keeps everything else.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from klayout_tools import functional_verification_sdf_steps as fvs

HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None

_VERILOG = """\
module cellx(input CLK, input D, input SETN, output reg Q);
  specify
    if (D) (posedge CLK => (Q:D)) = (1,1);
    (posedge CLK => (Q:D)) = (1,1);
  endspecify
  always @(posedge CLK) Q <= D;
endmodule
module top(input CLK, input D, input SETN, output Q);
  cellx u(.CLK(CLK), .D(D), .SETN(SETN), .Q(Q));
  initial $sdf_annotate("t.sdf", top);
endmodule
"""

_D = "(0.1:0.1:0.1) (0.1:0.1:0.1)"


def _sdf(*entries: str) -> str:
    body = "\n".join(f"   {e}" for e in entries)
    return (
        '(DELAYFILE (SDFVERSION "3.0") (DESIGN "top") (TIMESCALE 1ns)\n'
        ' (CELL (CELLTYPE "cellx") (INSTANCE u)\n'
        f"  (DELAY (ABSOLUTE\n{body}\n  ))))\n"
    )


def _drop(tmp_path, text):
    path = tmp_path / "t.sdf"
    path.write_text(text)
    return fvs._drop_sdf_cond_expression_iopaths(str(path))


@pytest.mark.parametrize("cond", ["~D", "!D", "D&SETN", "(~D & SETN)", "~CLK&~D&SETN"])
def test_expression_cond_dropped_unconditional_kept(tmp_path, cond):
    result = _drop(
        tmp_path,
        _sdf(
            f"(COND {cond} (IOPATH (posedge CLK) Q {_D}))",
            f"(IOPATH (posedge CLK) Q {_D})",
        ),
    )
    assert result is not None
    text, count, arcs = result
    assert count == 1
    assert arcs == [{"celltype": "cellx", "from": "CLK", "to": "Q"}]
    assert "COND" not in text
    assert "(IOPATH (posedge CLK) Q" in text


@pytest.mark.parametrize("cond", ["D", "D==1'b1"])
def test_simple_cond_left_alone(tmp_path, cond):
    assert _drop(tmp_path, _sdf(f"(COND {cond} (IOPATH (posedge CLK) Q {_D}))")) is None


def test_no_cond_is_noop(tmp_path):
    assert _drop(tmp_path, _sdf(f"(IOPATH (posedge CLK) Q {_D})")) is None


def test_only_entry_dropped_leaves_valid_cell(tmp_path):
    result = _drop(tmp_path, _sdf(f"(COND ~D (IOPATH (posedge CLK) Q {_D}))"))
    assert result is not None
    text, count, _ = result
    assert count == 1
    assert "ABSOLUTE" not in text
    assert '(CELLTYPE "cellx")' in text


@pytest.mark.skipif(not HAVE_ICARUS, reason="Icarus Verilog not installed")
def test_icarus_annotates_after_drop(tmp_path):
    (tmp_path / "t.v").write_text(_VERILOG)
    sdf = _sdf(
        f"(COND ~CLK&~D&SETN (IOPATH (posedge CLK) Q {_D}))",
        f"(IOPATH (posedge CLK) Q {_D})",
    )
    (tmp_path / "t.sdf").write_text(sdf)

    def run() -> str:
        subprocess.run(
            ["iverilog", "-gspecify", "-o", "t.vvp", "t.v"], cwd=tmp_path, check=True
        )
        out = subprocess.run(
            ["vvp", "t.vvp"], cwd=tmp_path, capture_output=True, text=True
        )
        return out.stdout + out.stderr

    assert "SDF ERROR" in run()
    text, _, _ = fvs._drop_sdf_cond_expression_iopaths(str(tmp_path / "t.sdf"))
    (tmp_path / "t.sdf").write_text(text)
    assert "SDF ERROR" not in run()
