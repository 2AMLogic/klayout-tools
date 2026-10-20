"""Issue #2885: `options.sdf` IOPATH entries onto `ifnone` edge-sensitive arcs.

The gf180mcu standard-cell models give every non-unate arc (XOR/XNOR inputs,
MUX select) only `ifnone` + edge-sensitive module paths:

    ifnone
     (posedge A1 => (Z:A1)) = (1.0,1.0);
    ifnone
     (negedge A1 => (Z:A1)) = (1.0,1.0);

Icarus 13.0 discards every such path at elaboration (`sorry: ifnone with an
edge-sensitive path is not supported.`), so OpenSTA's unqualified
`(IOPATH A1 Z ...)` entries -- two per input pin, one per unateness sense --
fail with `SDF ERROR: ...: Unable to match ModPath A1 -> Z`, and the
diagnostic gate correctly rejected the whole run. Rewriting the entry to an
edge-qualified `(IOPATH (posedge A1) Z ...)` fails identically (verified
live): there is no modpath of either edge left to match.

`_drop_sdf_unsupported_edge_modpath_iopaths` drops exactly those entries --
decided from the request's own cell models, never from the diagnostic text --
and the response counts them as their own `environment.sdf.dropped` class,
naming every affected arc. Every other `IOPATH` (a unate cell, a MUX data
input that also has a supported conditional path, a genuinely mismatched
pin) is untouched.
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
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)


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
    <testcase name="test_one" classname="test_edge_top" time="0.01" \
sim_time_ns="10.0" />
  </testsuite>
</testsuites>
"""

#: Cell models in the gf180mcu shape (`ifndef FUNCTIONAL` specify branch,
#: comments between `ifnone` and the path, a `USE_POWER_PINS` dual header):
#: an XOR whose two arcs are *only* `ifnone` edge-sensitive, a MUX whose data
#: input also carries supported conditional/plain paths but whose select is
#: only `ifnone` edge-sensitive, and a unate buffer with an ordinary path.
_CELL_MODELS_V = """\
`ifdef USE_POWER_PINS
module klt_xor2 (A2, A1, Z, VDD, VSS);
inout VDD, VSS;
`else
module klt_xor2 (A2, A1, Z);
`endif
  input A1, A2;
  output Z;
  assign Z = A1 ^ A2;
`ifndef FUNCTIONAL
  specify
    // specify_block_begin
    ifnone
    // comb arc posedge A1 --> (Z:A1)
     (posedge A1 => (Z:A1)) = (1.0,1.0);
    ifnone
    // comb arc negedge A1 --> (Z:A1)
     (negedge A1 => (Z:A1)) = (1.0,1.0);
    ifnone
     (posedge A2 => (Z:A2)) = (1.0,1.0);
    ifnone
     (negedge A2 => (Z:A2)) = (1.0,1.0);
  endspecify
`endif
endmodule

module klt_mux2 (I0, I1, S, Z);
  input I0, I1, S;
  output Z;
  assign Z = S ? I1 : I0;
  specify
    if(I1===1'b0)
     (I0 => Z) = (1.0,1.0);
    ifnone
     (I0 => Z) = (1.0,1.0);
    ifnone
     (posedge S => (Z:S)) = (1.0,1.0);
    ifnone
     (negedge S => (Z:S)) = (1.0,1.0);
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
module edge_top (a, b, s, y, m, w);
  input a;
  input b;
  input s;
  output y;
  output m;
  output w;
  klt_xor2 u_xor (.A1(a), .A2(b), .Z(y));
  klt_mux2 u_mux (.I0(a), .I1(b), .S(s), .Z(m));
  klt_buf u_buf (.A(a), .Z(w));
endmodule
"""

_TESTBENCH_PY = '''\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_xor_arc_runs_without_module_path_delay(dut):
    """The dropped XOR arc has no modpath in the elaborated design, so `y`
    follows `a` immediately with or without the SDF -- which is exactly what
    the response's `arcs` list tells a caller."""
    dut.a.value = 0
    dut.b.value = 0
    dut.s.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(1, unit="ns")
    assert dut.y.value == 1, f"y={dut.y.value} 1 ns after a rose (expected 1)"


@cocotb.test()
async def test_unate_buffer_settles_within_one_ns(dut):
    """At zero delay `w` tracks `a` immediately, so this passes; once the
    buffer's real 2 ns IOPATH delay is annotated it fails -- the same "does
    the testbench's own verdict change" coverage metric the other SDF
    integration regressions use, proving the unate IOPATH still applies."""
    dut.a.value = 0
    dut.b.value = 0
    dut.s.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(1, unit="ns")
    assert dut.w.value == 1, f"w={dut.w.value} 1 ns after a rose (expected 1)"
'''


def _sdf_text(*, xor_a1_source: str = "A1", extra_xor_entry: str = "") -> str:
    """What OpenSTA's `write_sdf` emits for `_NETLIST_V`: two unqualified
    IOPATH entries per non-unate input pin (one per unateness sense), an
    ordinary IOPATH for the MUX data input and the unate buffer."""
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "edge_top")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "klt_xor2")
    (INSTANCE u_xor)
    (DELAY (ABSOLUTE
      (IOPATH {xor_a1_source} Z (8.428:8.428:8.428) (5.243:5.243:5.243))
      (IOPATH {xor_a1_source} Z (6.892:6.892:6.892) (2.878:2.878:2.878))
      (IOPATH A2 Z (7.000:7.000:7.000) (4.000:4.000:4.000))
      (IOPATH A2 Z (3.000:3.000:3.000) (2.000:2.000:2.000)){extra_xor_entry}
    ))
  )
  (CELL
    (CELLTYPE "klt_mux2")
    (INSTANCE u_mux)
    (DELAY (ABSOLUTE
      (IOPATH I0 Z (1.500:1.500:1.500) (1.500:1.500:1.500))
      (IOPATH S Z (2.500:2.500:2.500) (2.500:2.500:2.500))
      (IOPATH S Z (2.200:2.200:2.200) (2.200:2.200:2.200))
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


_EXPECTED_ARCS = [
    {"celltype": "klt_mux2", "from": "S", "to": "Z"},
    {"celltype": "klt_xor2", "from": "A1", "to": "Z"},
    {"celltype": "klt_xor2", "from": "A2", "to": "Z"},
]


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# Model parsing
# --------------------------------------------------------------------------- #


def test_discarded_arcs_are_the_ifnone_edge_only_arcs():
    """The XOR's both arcs and the MUX select are discarded by Icarus; the
    MUX data input keeps its conditional/plain paths, so it is not listed --
    and neither is the unate buffer."""
    texts = [_CELL_MODELS_V]

    assert fvs._verilog_cell_discarded_arcs("klt_xor2", texts) == {
        ("A1", "Z"),
        ("A2", "Z"),
    }
    assert fvs._verilog_cell_discarded_arcs("klt_mux2", texts) == {("S", "Z")}
    assert fvs._verilog_cell_discarded_arcs("klt_buf", texts) == set()


def test_discarded_arcs_with_only_one_qualified_arc():
    """A model with a single `ifnone` edge-sensitive path still loses that
    arc -- the drop does not need the posedge/negedge pair."""
    model = (
        "module klt_one (A, Z);\n  input A; output Z;\n  specify\n"
        "    ifnone (posedge A => (Z:A)) = (1.0,1.0);\n  endspecify\nendmodule\n"
    )

    assert fvs._verilog_cell_discarded_arcs("klt_one", [model]) == {("A", "Z")}


def test_discarded_arcs_ignore_a_non_ifnone_edge_path():
    """An edge-sensitive path *without* `ifnone` (a flop's clock arc) is one
    Icarus keeps, so an IOPATH onto it annotates normally and is never
    dropped -- even when an `ifnone` edge path for the same arc also exists."""
    model = (
        "module klt_dff (CLK, D, Q);\n  input CLK, D; output Q;\n  specify\n"
        "    (posedge CLK => (Q+:D)) = (1.0,1.0);\n"
        "    ifnone (posedge CLK => (Q+:D)) = (1.0,1.0);\n"
        "  endspecify\nendmodule\n"
    )

    assert fvs._verilog_cell_discarded_arcs("klt_dff", [model]) == set()


def test_discarded_arcs_is_empty_without_the_model():
    assert fvs._verilog_cell_discarded_arcs(
        "klt_xor2", ["module other; endmodule"]
    ) == (set())


def test_discarded_arcs_is_empty_on_an_unparseable_path_statement():
    """A path statement this module cannot parse might be the supported path
    that would have matched, so the whole cell is left alone."""
    model = _CELL_MODELS_V.replace(
        "     (posedge A2 => (Z:A2)) = (1.0,1.0);",
        "     (posedge => ) = (1.0,1.0);",
    )

    assert fvs._verilog_cell_discarded_arcs("klt_xor2", [model]) == set()


def test_discarded_arcs_ignore_timing_checks_and_specparams():
    model = (
        "module klt_dffx (CLK, D, Q);\n  input CLK, D; output Q;\n  specify\n"
        "    specparam tdelay = 1.0;\n"
        "    $setuphold(posedge CLK &&& (D==1'b0), D, 1.0, 1.0);\n"
        "    ifnone (posedge CLK => (Q:D)) = (1.0,1.0);\n"
        "  endspecify\nendmodule\n"
    )

    assert fvs._verilog_cell_discarded_arcs("klt_dffx", [model]) == {("CLK", "Q")}


# --------------------------------------------------------------------------- #
# SDF transform
# --------------------------------------------------------------------------- #


def test_drop_removes_only_the_ifnone_edge_arc_entries(tmp_path):
    """The `A1 -> Z` shape from the issue: both unqualified XOR entries per
    pin and both MUX-select entries are removed; the MUX data input and the
    unate buffer are byte-identical to their originals."""
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models])

    assert result is not None
    text, dropped, arcs = result
    assert dropped == 6
    assert arcs == _EXPECTED_ARCS
    assert "(IOPATH A1 Z" not in text
    assert "(IOPATH A2 Z" not in text
    assert "(IOPATH S Z" not in text
    assert "(IOPATH I0 Z (1.500:1.500:1.500) (1.500:1.500:1.500))" in text
    assert "(IOPATH A Z (2.000:2.000:2.000) (2.000:2.000:2.000))" in text
    # The emptied XOR `(DELAY ...)` is removed with its entries (an empty
    # `(DELAY (ABSOLUTE))` is an Icarus parse error); the cell itself stays.
    assert '(CELLTYPE "klt_xor2")' in text
    assert text.count("(DELAY (ABSOLUTE") == 2


def test_drop_handles_an_edge_qualified_iopath(tmp_path):
    """An already edge-qualified `(IOPATH (posedge A1) Z ...)` fails the
    same way inside Icarus (no modpath of either edge survives elaboration),
    so it is dropped identically."""
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text(xor_a1_source="(posedge A1)"))
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models])

    assert result is not None
    assert result[1] == 6
    assert "posedge A1" not in result[0]


def test_drop_keeps_a_genuinely_mismatched_iopath(tmp_path):
    """An IOPATH onto a pin the model declares no path for at all is a real
    mismatch: it is left in place to fail the diagnostic gate loudly."""
    sdf_path = _write(
        tmp_path / "route.sdf",
        _sdf_text(extra_xor_entry="\n      (IOPATH B Z (1.0:1.0:1.0) (1.0:1.0:1.0))"),
    )
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models])

    assert result is not None
    assert "(IOPATH B Z (1.0:1.0:1.0) (1.0:1.0:1.0))" in result[0]
    assert result[1] == 6


def test_drop_keeps_a_cond_wrapped_iopath(tmp_path):
    sdf_path = _write(
        tmp_path / "route.sdf",
        _sdf_text().replace(
            "(IOPATH A2 Z (7.000:7.000:7.000) (4.000:4.000:4.000))",
            "(COND A1 (IOPATH A2 Z (7.000:7.000:7.000) (4.000:4.000:4.000)))",
        ),
    )
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    result = fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models])

    assert result is not None
    assert result[1] == 5
    assert "(COND A1 (IOPATH A2 Z" in result[0]


def test_drop_is_a_noop_without_ifnone_models(tmp_path):
    """No source mentions `ifnone` (every sky130 design, every RTL source):
    the SDF is never even read."""
    models = _write(
        tmp_path / "cells.v",
        "module klt_buf (A, Z); input A; output Z; endmodule\n",
    )

    assert (
        fvs._drop_sdf_unsupported_edge_modpath_iopaths(
            str(tmp_path / "does-not-exist.sdf"), [models]
        )
        is None
    )


def test_drop_is_a_noop_when_no_entry_matches(tmp_path):
    """An SDF carrying no entry onto a discarded arc changes nothing."""
    sdf_path = _write(
        tmp_path / "route.sdf",
        '(DELAYFILE (SDFVERSION "3.0") (DESIGN "edge_top") (TIMESCALE 1ns)\n'
        '  (CELL (CELLTYPE "klt_buf") (INSTANCE u_buf)\n'
        "    (DELAY (ABSOLUTE (IOPATH A Z (2.0:2.0:2.0) (2.0:2.0:2.0)))))\n)\n",
    )
    models = _write(tmp_path / "cells.v", _CELL_MODELS_V)

    assert fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models]) is None


def test_drop_ignores_a_celltype_without_a_model(tmp_path):
    """A `CELLTYPE` whose model is not among the request's sources cannot be
    shown to have lost its modpaths, so its entries stay."""
    sdf_path = _write(tmp_path / "route.sdf", _sdf_text())
    models = _write(
        tmp_path / "cells.v", _CELL_MODELS_V.replace("klt_xor2", "klt_other")
    )

    result = fvs._drop_sdf_unsupported_edge_modpath_iopaths(sdf_path, [models])

    assert result is not None
    assert result[1] == 2
    assert "(IOPATH A1 Z" in result[0]


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
    _write(tmp_path / "edge_top.v", _NETLIST_V)
    _write(tmp_path / "test_edge_top.py", testbench)
    _write(tmp_path / "route.sdf", _sdf_text())


def _request(tmp_path: Path, name: str, sdf: dict | None) -> str:
    request: dict = {
        "engine": "icarus",
        "sources": ["edge_top.v", "cells.v"],
        "hdl_toplevel": "edge_top",
        "testbench": {"module": "test_edge_top", "testcase": None},
    }
    if sdf is not None:
        request["options"] = {"sdf": sdf}
    path = tmp_path / name
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def test_stubbed_sdf_reports_the_dropped_ifnone_edge_arcs(tmp_path, monkeypatch):
    """The JSON contract for issue #2885: `partial: true`, a counted
    `iopath_ifnone_edge_modpath` class with its reason, and an additive
    `arcs` list naming every dropped cell arc -- the drop is never silent.
    `environment.sdf.file` still names the caller's own SDF."""
    _stage(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(tmp_path, "request.json", {"file": "route.sdf"})
    )

    sdf = report["environment"]["sdf"]
    assert sdf["file"] == str(tmp_path / "route.sdf")
    assert sdf["annotated"] is True
    assert sdf["partial"] is True
    assert sdf["dropped"] == {
        "iopath_ifnone_edge_modpath": {
            "count": 6,
            "reason": fv.SDF_BENIGN_DIAGNOSTIC_REASONS[
                fv.SDF_IFNONE_EDGE_MODPATH_DROPPED_CLASS
            ],
            "arcs": _EXPECTED_ARCS,
        }
    }
    normalized = (
        tmp_path
        / ".klt"
        / "functional-verification"
        / "klt_sdf_ifnone_edge_modpath_dropped.sdf"
    )
    assert normalized.is_file()
    assert "(IOPATH S Z" not in normalized.read_text(encoding="utf-8")


def test_stubbed_sdf_interconnect_mode_never_reaches_the_ifnone_pass(
    tmp_path, monkeypatch
):
    """`options.sdf.entries: "interconnect"` drops every IOPATH first, so the
    #2885 class never appears alongside #2364's -- the two counts can never
    double-report one entry."""
    _stage(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML))

    report = run_functional_verification(
        _request(
            tmp_path,
            "request.json",
            {"file": "route.sdf", "entries": "interconnect"},
        )
    )

    dropped = report["environment"]["sdf"]["dropped"]
    assert dropped.keys() == {"iopath_interconnect_only"}
    assert "arcs" not in dropped["iopath_interconnect_only"]


# --------------------------------------------------------------------------- #
# Integration: real cocotb + real Icarus (skipped when either is absent)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_sdf_drops_ifnone_edge_modpath_iopaths(tmp_path):
    """The load-bearing regression for issue #2885, end to end against the
    real toolchain: before the fix this raised `FunctionalVerificationError`
    (`did not fully apply ... Unable to match ModPath A1 -> Z`). It must now
    run, report every dropped arc, and *still* annotate the unate buffer's
    IOPATH -- that test's verdict flips from pass to fail -- while the XOR arc
    (no modpath in the elaborated design) responds at zero delay either way."""
    _stage(tmp_path, testbench=_TESTBENCH_PY)
    plain = run_functional_verification(_request(tmp_path, "request-plain.json", None))
    annotated = run_functional_verification(
        _request(tmp_path, "request-sdf.json", {"file": "route.sdf", "corner": "typ"})
    )

    def statuses(report: dict) -> dict[str, str]:
        return {test["name"]: test["status"] for test in report["tests"]}

    assert statuses(plain) == {
        "test_xor_arc_runs_without_module_path_delay": "passed",
        "test_unate_buffer_settles_within_one_ns": "passed",
    }
    assert statuses(annotated) == {
        "test_xor_arc_runs_without_module_path_delay": "passed",
        "test_unate_buffer_settles_within_one_ns": "failed",
    }
    sdf = annotated["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is True
    assert sdf["dropped"]["iopath_ifnone_edge_modpath"]["count"] == 6
    assert sdf["dropped"]["iopath_ifnone_edge_modpath"]["arcs"] == _EXPECTED_ARCS


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
@pytest.mark.parametrize("corner", ["min", "typ", "max"])
def test_integration_real_icarus_sdf_ifnone_edge_drop_applies_at_every_corner(
    tmp_path, corner
):
    """The issue reports the failure at every SDF corner member; the drop is
    corner-independent, so every corner annotates cleanly."""
    _stage(tmp_path, testbench=_TESTBENCH_PY)

    report = run_functional_verification(
        _request(tmp_path, "request-sdf.json", {"file": "route.sdf", "corner": corner})
    )

    assert report["environment"]["sdf"]["corner"] == corner
    assert (
        report["environment"]["sdf"]["dropped"]["iopath_ifnone_edge_modpath"]["count"]
        == 6
    )


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog is not installed on this machine")
def test_integration_real_icarus_sdf_mismatched_iopath_still_fails_loud(tmp_path):
    """The exemption is bounded by the model, not by the diagnostic text: an
    IOPATH onto a pin the XOR model declares no path for at all still reaches
    Icarus, still fails `Unable to match ModPath`, and still raises."""
    _stage(tmp_path, testbench=_TESTBENCH_PY)
    _write(
        tmp_path / "route.sdf",
        _sdf_text(extra_xor_entry="\n      (IOPATH B Z (1.0:1.0:1.0) (1.0:1.0:1.0))"),
    )

    with pytest.raises(FunctionalVerificationError, match="did not fully apply") as exc:
        run_functional_verification(
            _request(tmp_path, "request-sdf.json", {"file": "route.sdf"})
        )
    assert "Unable to match ModPath B -> Z" in str(exc.value)
