"""Issue #6075, measured: the zero-delay drop against the real simulators.

The classification-level tests live in
``test_functional_verification_sdf_internal_bit_select.py``; this module
pins the raw-simulator verdicts the fix rests on (raw ``iverilog``/``vvp``
13.0, spike §3.7's dated addendum methodology: 0-of-N entries applied on a
bit-selected internal net at any fanout, the byte-identical scalar-net
control annotating cleanly) and the end-to-end behavior through
``klt``'s own pipeline (real cocotb + Icarus; the 0-of-4 fixture completes
with the class counted, a mixed net still fails loudly on its non-zero
member). Skipped, not failed, on machines without the tools -- the
sibling integration fixtures' posture.

The fixtures (``_DUT_V``, ``_sdf_text``, the writers) are shared with the
classification module and imported from it, so the measured verdicts and
the classification tests can never drift onto different nets.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import run_functional_verification
from klayout_tools.functional_verification_sdf_internal_bus import (
    SDF_INTERNAL_BUS_NET_DROPPED_CLASS,
)
from test_functional_verification_sdf_internal_bit_select import (
    _DUT_V,
    _sdf_text,
    _write,
    _write_request,
)

HAVE_ICARUS = shutil.which("iverilog") is not None


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()


# --------------------------------------------------------------------------- #
# Measured verdicts against the real simulator (spike §3.7's dated
# addendum: raw `iverilog`/`vvp` 13.0, the sibling fixtures' methodology)
# --------------------------------------------------------------------------- #


def _raw_tb_v(sdf_path: str) -> str:
    return f"""\
`timescale 1ns/1ns
module tb;
  reg in = 0;
  wire o1, o2, o3, o4, o5, o6, o7, o8;
  initial begin
    $sdf_annotate("{sdf_path}", dut);
  end
  dut dut(.in(in), .out1(o1), .out2(o2), .out3(o3), .out4(o4),
          .out5(o5), .out6(o6), .out7(o7), .out8(o8));
  initial begin
    #10 in = 1;
    #5 $display("DONE");
    $finish;
  end
endmodule
"""


def _compile_and_run(workdir: Path) -> str:
    vvp_out = workdir / "sim.vvp"
    build = subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-gspecify",
            "-ginterconnect",
            "-o",
            str(vvp_out),
            str(workdir / "dut.v"),
            str(workdir / "tb.v"),
        ],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"iverilog lacks -ginterconnect: {build.stderr.strip()[:120]}")
    proc = subprocess.run(["vvp", str(vvp_out)], capture_output=True, text=True)
    return proc.stdout + proc.stderr


@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_real_icarus_internal_bit_select_net_applies_zero_of_n(tmp_path):
    """The measured verdict the issue's field report carries, pinned here:
    ALL FOUR zero-delay entries on the bit-selected internal net fail --
    0-of-4, not #5705's last-declared-survivor N-1-of-N."""
    _write(tmp_path / "dut.v", _DUT_V)
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    _write(tmp_path / "tb.v", _raw_tb_v(sdf_path))

    transcript = _compile_and_run(tmp_path)

    assert transcript.count("VPI error: Could not insert intermodpath!") == 4
    assert transcript.count("Could not find intermodpath!") == 4
    assert "DONE" in transcript


@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_real_icarus_scalar_internal_net_control_annotates_cleanly(tmp_path):
    """The control that isolates the mechanism: the byte-identical entries
    on a scalar internal net annotate cleanly -- same SDF text, same
    fanout, no diagnostics."""
    _write(tmp_path / "dut.v", _DUT_V)
    sdf_text = _sdf_text()
    for load in ("u_l1", "u_l2", "u_l3", "u_l4"):
        line = (
            f"      (INTERCONNECT u_src.Y {load}.A"
            " (0.000:0.000:0.000) (0.000:0.000:0.000))\n"
        )
        assert line in sdf_text
        sdf_text = sdf_text.replace(line, "")
    sdf_path = _write(tmp_path / "route.sdf", sdf_text)
    _write(tmp_path / "tb.v", _raw_tb_v(sdf_path))

    transcript = _compile_and_run(tmp_path)

    assert "Could not find intermodpath!" not in transcript
    assert "Could not insert intermodpath!" not in transcript
    assert "DONE" in transcript


# --------------------------------------------------------------------------- #
# End-to-end through the verb (real cocotb + Icarus; skipped, not failed,
# on machines without them -- the sibling integration fixtures' posture)
# --------------------------------------------------------------------------- #


_TESTBENCH_PY = """\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_outputs_settle(dut):
    dut_in = getattr(dut, "in")
    dut_in.value = 0
    await Timer(10, unit="ns")
    dut_in.value = 1
    await Timer(5, unit="ns")
"""


def _run_pipeline(tmp_path: Path) -> dict:
    _write(tmp_path / "dut.v", _DUT_V)
    _write(tmp_path / "test_dut.py", _TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _sdf_text())
    request = {
        "engine": "icarus",
        "sources": ["dut.v"],
        "hdl_toplevel": "dut",
        "testbench": {"module": "test_dut"},
        "options": {"sdf": {"file": "route.sdf", "corner": "typ"}},
    }
    return run_functional_verification(
        _write_request(tmp_path / "request.json", request)
    )


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_internal_bus_net_zero_delay_is_dropped(tmp_path):
    """The 0-of-4 fixture completes once the class drops the entries; the
    response reports the new class, and the normalized copy carries no
    INTERCONNECT entry at all."""
    report = _run_pipeline(tmp_path)

    sdf = report["environment"]["sdf"]
    assert sdf["partial"] is True
    assert sdf["dropped"][SDF_INTERNAL_BUS_NET_DROPPED_CLASS]["count"] == 4
    normalized = (
        tmp_path
        / ".klt"
        / "functional-verification"
        / ("klt_sdf_internal_bus_net_dropped.sdf")
    )
    assert normalized.exists()
    normalized_text = normalized.read_text(encoding="utf-8")
    assert "u_src.Y" not in normalized_text
    # The byte-identical scalar-net entries stay: only the bit-selected
    # internal net's entries were removed.
    assert normalized_text.count("(INTERCONNECT") == 4


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_internal_bus_net_mixed_keeps_failing(tmp_path):
    """The #5769 boundary, pinned live: the non-zero entry on the same net
    survives the drop pass and still fails the run loudly."""
    _write(tmp_path / "dut.v", _DUT_V)
    _write(tmp_path / "test_dut.py", _TESTBENCH_PY)
    _write(
        tmp_path / "route.sdf",
        _sdf_text().replace(
            "(INTERCONNECT u_src.Y u_l4.A (0.000:0.000:0.000) (0.000:0.000:0.000))",
            "(INTERCONNECT u_src.Y u_l4.A (5.000:5.000:5.000) (5.000:5.000:5.000))",
        ),
    )
    request = {
        "engine": "icarus",
        "sources": ["dut.v"],
        "hdl_toplevel": "dut",
        "testbench": {"module": "test_dut"},
        "options": {"sdf": {"file": "route.sdf", "corner": "typ"}},
    }
    with pytest.raises(fv.FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(_write_request(tmp_path / "request.json", request))
