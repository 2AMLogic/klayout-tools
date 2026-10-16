"""Issue #2958: `options.sdf` per-bit IOPATH entries onto a macro's bus output.

OpenSTA writes a hard macro's bus output one bit at a time
(`IOPATH CLK DOUT[0] ...`) while the vendor model declares one whole-vector
module path (`(CLK *> DOUT)`). Icarus 13.0 cannot parse the unescaped entry
(`Invalid/malformed IOPATH`) nor match the escaped one (`Unable to match
ModPath`), so `_drop_sdf_bit_selected_bus_iopaths` removes exactly those
entries -- decided from the request's own cell models -- and the response
counts them as `iopath_bus_bit_modpath`, naming every affected arc.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_sdf as fvs
from klayout_tools.functional_verification import run_functional_verification


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()
HAVE_ICARUS = shutil.which("iverilog") is not None

_RESULTS_XML = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_one" classname="test_bus_top" time="0.01" \
sim_time_ns="10.0" />
  </testsuite>
</testsuites>
"""

#: `klt_mac`: a bus output reached only by a whole-vector path. `klt_mix`: a
#: bus output that also has a bit-indexed path. `klt_buf`: scalar ports.
_CELL_MODELS_V = """\
module klt_mac (CLK, DOUT, FLAG);
  input CLK;
  output [3:0] DOUT;
  output FLAG;
  assign DOUT = 4'b0;
  assign FLAG = CLK;
  specify
    (CLK *> DOUT) = (1.0,1.0);
    (CLK => FLAG) = (1.0,1.0);
  endspecify
endmodule

module klt_mix (CLK, DOUT);
  input CLK;
  output [1:0] DOUT;
  assign DOUT = 2'b0;
  specify
    (CLK => DOUT[0]) = (1.0,1.0);
    (CLK *> DOUT) = (1.0,1.0);
  endspecify
endmodule

module klt_buf (A, Z);
  input A;
  output Z;
  assign Z = A;
  specify
    (A => Z) = (0.0, 0.0);
  endspecify
endmodule
"""

_NETLIST_V = """\
module bus_top (clk, a, d, f, w);
  input clk;
  input a;
  output [3:0] d;
  output f;
  output w;
  klt_mac u_mac (.CLK(clk), .DOUT(d), .FLAG(f));
  klt_buf u_buf (.A(a), .Z(w));
endmodule
"""

_TESTBENCH_PY = '''\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_unate_buffer_settles_within_one_ns(dut):
    """Passes at zero delay; fails once the buffer's real 2 ns IOPATH delay
    is annotated -- proving the SDF still applied after the bus drop."""
    dut.a.value = 0
    dut.clk.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(1, unit="ns")
    assert dut.w.value == 1, f"w={dut.w.value} 1 ns after a rose (expected 1)"
'''


def _bit_entries(dst: str, extra: str = "") -> str:
    return (
        "\n".join(
            f"      (IOPATH CLK {dst}[{bit}] (3.0:3.0:3.0) (3.0:3.0:3.0))"
            for bit in range(4)
        )
        + extra
    )


def _sdf_text(*, escaped: bool = False, mac_extra: str = "") -> str:
    body = _bit_entries("DOUT")
    if escaped:
        body = body.replace("[", "\\[").replace("]", "\\]")
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "bus_top")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "klt_mac")
    (INSTANCE u_mac)
    (DELAY (ABSOLUTE
{body}
      (IOPATH CLK FLAG (2.0:2.0:2.0) (2.0:2.0:2.0)){mac_extra}
    ))
  )
  (CELL
    (CELLTYPE "klt_buf")
    (INSTANCE u_buf)
    (DELAY (ABSOLUTE
      (IOPATH A Z (2.000:2.000:2.000) (2.000:2.000:2.000))
    ))
  )
)
"""


_EXPECTED_ARCS = [{"celltype": "klt_mac", "from": "CLK", "to": "DOUT"}]


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# Model parsing
# --------------------------------------------------------------------------- #


def test_whole_vector_arcs_are_the_bus_only_arcs():
    arcs = fvs._verilog_cell_whole_vector_arcs("klt_mac", [_CELL_MODELS_V])
    assert ("CLK", "DOUT") in arcs
    # A scalar output is never a bus arc.
    assert ("CLK", "FLAG") not in arcs


def test_whole_vector_arcs_exclude_a_bus_with_a_bit_indexed_path():
    assert fvs._verilog_cell_whole_vector_arcs("klt_mix", [_CELL_MODELS_V]) == set()


def test_whole_vector_arcs_empty_without_the_model():
    assert fvs._verilog_cell_whole_vector_arcs("nope", [_CELL_MODELS_V]) == set()


def test_whole_vector_arcs_empty_on_an_unparseable_path():
    model = (
        "module klt_mac (CLK, DOUT);\n  input CLK;\n  output [3:0] DOUT;\n"
        "  specify\n    (CLK *> DOUT) = (1.0,1.0);\n    (CLK *> ) = (1.0,1.0);\n"
        "  endspecify\nendmodule\n"
    )
    assert fvs._verilog_cell_whole_vector_arcs("klt_mac", [model]) == set()


def test_whole_vector_arcs_handle_an_ansi_header():
    model = (
        "module klt_mac (input CLK, output reg [3:0] DOUT, output FLAG);\n"
        "  specify\n    (CLK *> DOUT) = (1.0,1.0);\n  endspecify\nendmodule\n"
    )
    assert fvs._verilog_cell_whole_vector_arcs("klt_mac", [model]) == {("CLK", "DOUT")}


# --------------------------------------------------------------------------- #
# The drop
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("escaped", [False, True])
def test_drop_removes_bit_selected_bus_entries(tmp_path, escaped):
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text(escaped=escaped))
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models])

    assert result is not None
    text, dropped, arcs = result
    assert dropped == 4
    assert arcs == _EXPECTED_ARCS
    assert "DOUT" not in text
    # Scalar-port entry on the same cell and the buffer are untouched.
    assert "(IOPATH CLK FLAG (2.0:2.0:2.0) (2.0:2.0:2.0))" in text
    assert "(IOPATH A Z (2.000:2.000:2.000) (2.000:2.000:2.000))" in text


def test_drop_keeps_a_bus_with_a_bit_indexed_model_path(tmp_path):
    sdf = _sdf_text().replace("klt_mac", "klt_mix")
    sdf_path = _write(tmp_path / "route.sdf", sdf)
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    assert fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models]) is None


def test_drop_keeps_a_bit_select_on_a_scalar_port(tmp_path):
    sdf = _sdf_text(
        mac_extra="\n      (IOPATH CLK FLAG[0] (1.0:1.0:1.0) (1.0:1.0:1.0))"
    )
    sdf_path = _write(tmp_path / "route.sdf", sdf)
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models])

    assert result is not None
    assert result[1] == 4
    assert "(IOPATH CLK FLAG[0]" in result[0]


def test_drop_keeps_an_entry_whose_input_is_not_a_model_path(tmp_path):
    sdf = _sdf_text(
        mac_extra="\n      (IOPATH RST DOUT[0] (1.0:1.0:1.0) (1.0:1.0:1.0))"
    )
    sdf_path = _write(tmp_path / "route.sdf", sdf)
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models])

    assert result is not None
    assert "(IOPATH RST DOUT[0]" in result[0]


def test_drop_keeps_a_cond_wrapped_iopath(tmp_path):
    sdf = _sdf_text(
        mac_extra="\n      (COND en (IOPATH CLK DOUT[0] (1.0:1.0:1.0) (1.0:1.0:1.0)))"
    )
    sdf_path = _write(tmp_path / "route.sdf", sdf)
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models])

    assert result is not None
    assert result[1] == 4
    assert "(COND en (IOPATH CLK DOUT[0]" in result[0]


def test_drop_ignores_a_celltype_without_a_model(tmp_path):
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    models = _write(
        tmp_path / "cells.v", _CELL_MODELS_V.replace("klt_mac", "klt_other")
    )

    assert fvs._drop_sdf_bit_selected_bus_iopaths(sdf_path, [models]) is None


def test_drop_is_a_noop_without_a_bus_output_model(tmp_path):
    """No source declares a bus output next to a specify block: the SDF is
    never even read."""
    models = _write(
        tmp_path / "cells.v",
        "module klt_buf (A, Z); input A; output Z;\n"
        "specify (A => Z) = (1,1); endspecify endmodule\n",
    )

    assert (
        fvs._drop_sdf_bit_selected_bus_iopaths(
            str(tmp_path / "does-not-exist.sdf"), [models]
        )
        is None
    )


# --------------------------------------------------------------------------- #
# JSON contract (stubbed runner)
# --------------------------------------------------------------------------- #


def _stub_runner(monkeypatch, runner: _FakeRunner) -> None:
    monkeypatch.setattr(
        fv,
        "_import_runner",
        lambda: types.SimpleNamespace(get_runner=lambda name: runner),
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")


def _stage(tmp_path: Path, *, testbench: str = "# stub -- never imported\n") -> None:
    _write(tmp_path / "cells.v", _CELL_MODELS_V)
    _write(tmp_path / "bus_top.v", _NETLIST_V)
    _write(tmp_path / "test_bus_top.py", testbench)
    _write(tmp_path / "route.sdf", _sdf_text())


def _request(tmp_path: Path, name: str, sdf: dict | None) -> str:
    request: dict = {
        "engine": "icarus",
        "sources": ["bus_top.v", "cells.v"],
        "hdl_toplevel": "bus_top",
        "testbench": {"module": "test_bus_top", "testcase": None},
    }
    if sdf is not None:
        request["options"] = {"sdf": sdf}
    path = tmp_path / name
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_stubbed_sdf_reports_the_dropped_bus_arcs(tmp_path, monkeypatch):
    _stage(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(tmp_path, "request.json", {"file": "route.sdf"})
    )

    sdf = report["environment"]["sdf"]
    assert sdf["file"] == str(tmp_path / "route.sdf")
    assert sdf["partial"] is True
    assert sdf["dropped"] == {
        "iopath_bus_bit_modpath": {
            "count": 4,
            "reason": fv.SDF_BENIGN_DIAGNOSTIC_REASONS[
                fv.SDF_BUS_BIT_MODPATH_DROPPED_CLASS
            ],
            "arcs": _EXPECTED_ARCS,
        }
    }
    normalized = (
        tmp_path
        / ".klt"
        / "functional-verification"
        / "klt_sdf_bus_bit_modpath_dropped.sdf"
    )
    assert normalized.is_file()
    assert "DOUT" not in normalized.read_text(encoding="utf-8")


def test_stubbed_sdf_interconnect_mode_never_reaches_the_bus_pass(
    tmp_path, monkeypatch
):
    _stage(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(
            tmp_path,
            "request.json",
            {"file": "route.sdf", "entries": "interconnect"},
        )
    )

    assert report["environment"]["sdf"]["dropped"].keys() == {
        "iopath_interconnect_only"
    }


# --------------------------------------------------------------------------- #
# Integration: real cocotb + real Icarus (skipped when either is absent)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
@pytest.mark.parametrize("escaped", [False, True])
def test_integration_real_icarus_sdf_drops_bus_bit_iopaths(tmp_path, escaped):
    """Before the fix both spellings raised `FunctionalVerificationError`.
    The run must now complete, report the dropped arc, and still annotate the
    scalar buffer's IOPATH (that test flips from pass to fail)."""
    _stage(tmp_path, testbench=_TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _sdf_text(escaped=escaped))
    annotated = run_functional_verification(
        _request(tmp_path, "request-sdf.json", {"file": "route.sdf", "corner": "typ"})
    )

    assert {t["name"]: t["status"] for t in annotated["tests"]} == {
        "test_unate_buffer_settles_within_one_ns": "failed"
    }
    sdf = annotated["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is True
    assert sdf["dropped"]["iopath_bus_bit_modpath"]["count"] == 4
    assert sdf["dropped"]["iopath_bus_bit_modpath"]["arcs"] == _EXPECTED_ARCS
