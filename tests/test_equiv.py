"""Tests for `klt equiv` and the `klayout_tools.equiv` library.

Three tiers, mirroring `tests/test_synthesize.py`'s own structure:

- **Pure unit tests** exercise request validation (`run_equiv`'s own
  error paths -- missing/malformed `gold`/`gate`, bad `timeout_s`/
  `port_map`, unresolvable source/liberty files, unsupported engine) and
  the stdout-parsing helpers (`_classify_sat_result`, `_parse_signal_table`,
  `_decode_bin`, `_build_counterexample`) directly against **canned** text,
  no subprocess involved -- these always run, in any CI environment.
- **Real-Yosys integration tests** (`@pytest.mark.skipif` when `yosys` is
  not on `$PATH`) run the actual orchestration end to end: a genuinely
  equivalent pair, a **seeded-broken** (deliberately non-equivalent) pair
  that must produce a counterexample, a sequential-design scope rejection,
  a `port_map` I/O remapping, and a real process-level timeout (a
  deliberately tiny `timeout_s` against a real `yosys` subprocess --
  genuinely exercises the `subprocess.TimeoutExpired` path, not mocked).
  Counterexample-confirmation tests additionally skip when `iverilog` is
  not on `$PATH`.
- **Seeded-broken negative controls** (issue #832, Phase 1a of Epic #707):
  two additional deliberately-broken pairs, each derived from an existing
  known-good/equivalent fixture already in this module rather than built
  from scratch -- a "seeded inversion" (an inverted `cout`, derived from
  `_ADDER4_RTL`) and a "dropped register" (a register-readback mux missing
  one register's own case arm, derived from `_REGSEL_RTL`, itself first
  proven equivalent to a structurally different rewrite). `klt equiv`'s
  Phase 1 scope is combinational-only (see `equiv.py`'s module docstring),
  so a *literal* dropped flip-flop cannot exercise the counterexample path
  at all -- it hits the scope-rejection error `test_sequential_design_is_
  rejected` below already covers, never a counterexample -- making a
  combinational register-readback mux the nearest faithful in-scope
  analogue of that bug class. Both pairs assert `status == "counterexample"`
  (never a false `"equivalent"`) and that the solver's counterexample
  vector is independently confirmed by re-running it through both
  netlists via iverilog/vvp.
- **Corpus integration test** (skipif when `yosys`/`iverilog` are missing,
  or no real PDK standard-cell library resolves) runs `klt synthesize`
  (this repo's own Yosys-backed synthesis flow) on a small, real
  combinational RTL design (a 4-bit ripple-carry adder) against whatever
  real, host-resolved standard-cell library is available, then `klt equiv`
  proves the synthesized gate netlist equivalent to its own RTL -- the
  acceptance criterion's "matches Yosys ... on >=1 corpus pair". A second
  synthesis of a seeded-broken mutant (dropped carry-in) is proven
  non-equivalent -- the "seeded-broken negative control" criterion. Issue
  #520 (the Tiny Tapeout corpus epic) has not yet landed checked-in RTL
  fixtures in this repo (only GDS standard-cell layouts exist under
  `tests/corpus/` today) -- this test substitutes the repo's own
  `klt synthesize` pipeline against a real, host-resolved open-PDK
  standard-cell library as the nearest faithful "corpus design" available
  until #520 ships RTL material.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import equiv
from klayout_tools import pdk as pdk_module
from klayout_tools import synthesize as synthesize_module
from klayout_tools.cli import main
from klayout_tools.equiv import EquivError, run_equiv
from klayout_tools.synthesize import run_synthesize

# --------------------------------------------------------------------------- #
# Fixtures (RTL sources)
# --------------------------------------------------------------------------- #

_GOLD_AND = """\
module top(input a, input b, output y);
  assign y = a & b;
endmodule
"""

# Structurally different from `_GOLD_AND` (operand order + always-block
# instead of a continuous assign) but functionally identical -- the
# "structurally identical" edge case from the issue's own test plan is
# actually "structurally *different*, functionally identical", since two
# byte-identical files would be a trivial/uninteresting proof.
_GATE_AND_EQUIVALENT = """\
module top(input a, input b, output reg y);
  always @* begin
    y = b & a;
  end
endmodule
"""

# Seeded-broken: OR instead of AND. Deliberately non-equivalent.
_GATE_OR_BROKEN = """\
module top(input a, input b, output y);
  assign y = a | b;
endmodule
"""

_GOLD_SEQ = """\
module top(input clk, input d, output reg q);
  always @(posedge clk) q <= d;
endmodule
"""

_GATE_RENAMED_PORTS = """\
module top(input in0, input in1, output out0);
  assign out0 = in0 & in1;
endmodule
"""

_ADDER4_RTL = """\
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b + cin;
endmodule
"""

# Seeded-broken: drops `cin` from the sum -- a real "forgot the carry-in"
# bug class, not a synthetic/arbitrary corruption.
_ADDER4_RTL_BROKEN = """\
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  assign {cout, sum} = a + b;
endmodule
"""

# Issue #832: "seeded inversion" negative control, derived from the same
# `_ADDER4_RTL` known-good fixture used above (and in
# `tests/test_synthesize_equiv_gate.py`) rather than built from scratch.
# `cout` is inverted -- a real "carry-out polarity flip" bug class (e.g. a
# stray `~`, or the classic active-high/active-low mixup), structurally
# distinct from `_ADDER4_RTL_BROKEN`'s "dropped operand" bug above.
_ADDER4_RTL_INVERTED_COUT = """\
module adder4 (
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire       cin,
    output wire [3:0] sum,
    output wire       cout
);
  wire [4:0] result = a + b + cin;
  assign sum = result[3:0];
  assign cout = ~result[4];
endmodule
"""

# Issue #832: "dropped register" negative control. `klt equiv`'s Phase 1
# scope is combinational-only (see `equiv.py`'s module docstring and
# `test_sequential_design_is_rejected` below) -- a design that actually
# contains a flip-flop is rejected outright with a scope `EquivError`, never
# reported as a counterexample, so a *literal* dropped-flip-flop pair cannot
# exercise the counterexample path this issue is proving. The nearest
# faithful, in-scope analogue is a combinational register **readback mux**
# (the block that selects among already-latched register outputs by
# address -- itself always pure combinational logic, even inside a real
# sequential register file) where one register's own case arm was never
# wired in. `_REGSEL_RTL` is the known-good mux; `sel == 2'd2` selects `r2`.
_REGSEL_RTL = """\
module regsel (
    input  wire [1:0] sel,
    input  wire [7:0] r0,
    input  wire [7:0] r1,
    input  wire [7:0] r2,
    output reg  [7:0] dout
);
  always @* begin
    case (sel)
      2'd0: dout = r0;
      2'd1: dout = r1;
      2'd2: dout = r2;
      default: dout = 8'h00;
    endcase
  end
endmodule
"""

# Seeded-broken: register `r2` was never wired into the mux -- a real
# "forgot to hook the new register into the read path" bug class -- so
# `sel == 2'd2` silently reads back `r1` instead of `r2`.
_REGSEL_RTL_DROPPED_REGISTER = """\
module regsel (
    input  wire [1:0] sel,
    input  wire [7:0] r0,
    input  wire [7:0] r1,
    input  wire [7:0] r2,
    output reg  [7:0] dout
);
  always @* begin
    case (sel)
      2'd0: dout = r0;
      2'd1: dout = r1;
      2'd2: dout = r1;
      default: dout = 8'h00;
    endcase
  end
endmodule
"""


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _side(sources: list[str], top: str = "top", **extra) -> dict:
    return {"sources": sources, "top": top, **extra}


HAVE_YOSYS = shutil.which("yosys") is not None
HAVE_IVERILOG = shutil.which("iverilog") is not None


# --------------------------------------------------------------------------- #
# Pure unit tests: request validation (no subprocess)
# --------------------------------------------------------------------------- #


def test_missing_gold_field_is_error(tmp_path):
    request_path = _write_request(tmp_path / "r.json", {"gate": _side(["gate.v"])})
    with pytest.raises(EquivError, match="missing required field: gold"):
        run_equiv(request_path)


def test_missing_gate_field_is_error(tmp_path):
    request_path = _write_request(tmp_path / "r.json", {"gold": _side(["gold.v"])})
    with pytest.raises(EquivError, match="missing required field: gate"):
        run_equiv(request_path)


def test_gold_not_object_is_error(tmp_path):
    request_path = _write_request(
        tmp_path / "r.json", {"gold": "nope", "gate": _side(["gate.v"])}
    )
    with pytest.raises(EquivError, match="request.gold must be a JSON object"):
        run_equiv(request_path)


def test_missing_sources_is_error(tmp_path):
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": {"top": "top"}, "gate": _side(["gate.v"])},
    )
    with pytest.raises(EquivError, match="gold.sources must be a non-empty array"):
        run_equiv(request_path)


def test_missing_top_is_error(tmp_path):
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": {"sources": ["gold.v"]}, "gate": _side(["gate.v"])},
    )
    with pytest.raises(EquivError, match="gold.top is required"):
        run_equiv(request_path)


def test_source_not_found_is_error(tmp_path):
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["missing.v"]), "gate": _side(["gate.v"])},
    )
    with pytest.raises(EquivError, match="gold source not found: missing.v"):
        run_equiv(request_path)


def test_liberty_not_found_is_error(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"], liberty="missing.lib"),
        },
    )
    with pytest.raises(EquivError, match="gate liberty file not found"):
        run_equiv(request_path)


def test_unsupported_engine_is_error(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "verific",
        },
    )
    with pytest.raises(EquivError, match="unsupported engine 'verific'"):
        run_equiv(request_path)


@pytest.mark.parametrize("bad_timeout", [0, -1, "60", True])
def test_bad_timeout_s_is_error(tmp_path, bad_timeout):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "timeout_s": bad_timeout,
        },
    )
    with pytest.raises(EquivError, match="timeout_s must be a positive number"):
        run_equiv(request_path)


def test_bad_port_map_is_error(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "port_map": {"a": 1},
        },
    )
    with pytest.raises(EquivError, match="request.port_map must be a JSON object"):
        run_equiv(request_path)


def test_load_request_arg_inline_json():
    request, request_dir = equiv.load_request_arg(
        json.dumps({"gold": _side(["gold.v"]), "gate": _side(["gate.v"])})
    )
    assert request["gold"]["top"] == "top"
    assert request_dir == os.getcwd()


# --------------------------------------------------------------------------- #
# Pure unit tests: stdout-parsing helpers (no subprocess)
# --------------------------------------------------------------------------- #

_SAT_SUCCESS_TEXT = """
Solving problem with 24 variables and 56 clauses..
SAT proof finished - no model found: SUCCESS!
"""

_SAT_FAIL_TEXT = """
Solving problem with 25 variables and 59 clauses..
SAT proof finished - model found: FAIL!

  Signal Name             Dec       Hex           Bin
  --------------- ----------- --------- -------------
  \\gate_y                   1         1             1
  \\gold_y                   0         0             0
  \\in_a                     1         1             1
  \\in_b                     0         0             0
  \\trigger                  1         1             1

End of script.
"""

_SAT_TIMEOUT_TEXT = """
Solving problem with 12727 variables and 37789 clauses..
Interrupted SAT solver: TIMEOUT!
"""

_SAT_UNRECOGNIZED_TEXT = "some unexpected yosys output\n"


def test_classify_sat_result_success():
    status, diagnostics = equiv._classify_sat_result(_SAT_SUCCESS_TEXT)
    assert status == "equivalent"
    assert diagnostics == []


def test_classify_sat_result_fail():
    status, diagnostics = equiv._classify_sat_result(_SAT_FAIL_TEXT)
    assert status == "counterexample"
    assert diagnostics == []


def test_classify_sat_result_timeout_never_equivalent():
    status, diagnostics = equiv._classify_sat_result(_SAT_TIMEOUT_TEXT)
    assert status == "inconclusive"
    assert diagnostics[0]["code"] == "solver_timeout"


def test_classify_sat_result_unrecognized_is_inconclusive_not_equivalent():
    status, diagnostics = equiv._classify_sat_result(_SAT_UNRECOGNIZED_TEXT)
    assert status == "inconclusive"
    assert diagnostics[0]["code"] == "unrecognized_solver_output"


def test_parse_signal_table():
    signals = equiv._parse_signal_table(_SAT_FAIL_TEXT)
    assert signals == {
        "gate_y": "1",
        "gold_y": "0",
        "in_a": "1",
        "in_b": "0",
        "trigger": "1",
    }


def test_parse_signal_table_no_table_returns_empty():
    assert equiv._parse_signal_table(_SAT_SUCCESS_TEXT) == {}


@pytest.mark.parametrize(
    ("bin_str", "expected"),
    [("0", 0), ("1", 1), ("1010", 10), ("0000", 0)],
)
def test_decode_bin_defined(bin_str, expected):
    assert equiv._decode_bin(bin_str) == expected


@pytest.mark.parametrize("bin_str", ["x", "1x0", "1-0", "z"])
def test_decode_bin_undefined_is_none(bin_str):
    assert equiv._decode_bin(bin_str) is None


def test_build_counterexample_shape():
    signals = equiv._parse_signal_table(_SAT_FAIL_TEXT)
    counterexample = equiv._build_counterexample(signals)
    assert counterexample["inputs"] == {
        "a": {"bin": "1", "width": 1, "value": 1},
        "b": {"bin": "0", "width": 1, "value": 0},
    }
    assert counterexample["gold_outputs"] == {"y": {"bin": "0", "width": 1, "value": 0}}
    assert counterexample["gate_outputs"] == {"y": {"bin": "1", "width": 1, "value": 1}}
    assert counterexample["diverging_outputs"] == ["y"]
    assert counterexample["confirmed_by_simulation"] is None
    assert counterexample["simulation"] is None


def test_confirm_counterexample_degrades_when_iverilog_missing(tmp_path, monkeypatch):
    """`_confirm_counterexample` never raises when the confirmation
    toolchain is unavailable -- it degrades to a diagnostic, leaving the
    solver-reported counterexample itself intact (per this module's own
    "gracefully around what you find" posture)."""

    def _raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("iverilog: not found")

    monkeypatch.setattr(equiv.subprocess, "run", _raise_file_not_found)

    counterexample = equiv._build_counterexample(
        equiv._parse_signal_table(_SAT_FAIL_TEXT)
    )
    diagnostics: list[dict] = []
    equiv._confirm_counterexample(
        counterexample=counterexample,
        netlist_path=str(tmp_path / "netlist.v"),
        output_dir=str(tmp_path),
        diagnostics=diagnostics,
    )

    assert counterexample["confirmed_by_simulation"] is None
    assert counterexample["simulation"] is None
    assert diagnostics[0]["code"] == "simulation_unavailable"


# --------------------------------------------------------------------------- #
# Real-Yosys integration tests
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_equivalent_structurally_different_same_function(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_AND_EQUIVALENT)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "yosys"
    assert report["status"] == "equivalent"
    assert report["counterexample"] is None
    assert report["diagnostics"] == []
    assert os.path.isfile(report["artifacts"]["script_path"])
    assert os.path.isfile(report["artifacts"]["netlist_path"])


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_seeded_broken_pair_produces_counterexample(tmp_path):
    """Acceptance criterion: a seeded-broken (deliberate inversion) pair
    must produce a counterexample, never `"equivalent"`."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["diverging_outputs"] == ["y"]
    # a=1, b=0 (or a=0, b=1) is the only vector where AND and OR diverge
    # with exactly one input high.
    inputs = counterexample["inputs"]
    assert sorted(int(v["value"]) for v in inputs.values()) == [0, 1]


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_counterexample_is_confirmed_by_independent_simulation(tmp_path):
    """The epic's own "a counterexample is executable" discipline: the
    concrete vector is independently re-run through both netlists via
    iverilog/vvp, not just trusted from the solver."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path)

    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert simulation["diverging_outputs"] == ["y"]
    # The independent simulation's own reported values must match the
    # solver's own reported values bit-for-bit -- not just "some" output
    # diverged.
    assert simulation["gold_outputs"]["y"] == counterexample["gold_outputs"]["y"]["bin"]
    assert simulation["gate_outputs"]["y"] == counterexample["gate_outputs"]["y"]["bin"]


# --------------------------------------------------------------------------- #
# Issue #832: seeded-broken negative controls (Phase 1a of Epic #707)
#
# Both pairs below are derived from an *existing* known-good/equivalent
# fixture already used elsewhere in this module (`_ADDER4_RTL` for the
# inversion, `_REGSEL_RTL` established as equivalent to itself here for the
# register-mux case), never built from scratch. Each seeded-broken pair must
# (a) report `"counterexample"`, never a false `"equivalent"`, and (b) have
# its solver-reported counterexample vector independently confirmed by
# re-running it through both flattened netlists via iverilog/vvp -- the
# epic's own "a counterexample is executable" discipline -- not just
# accepted on the SAT solver's say-so.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_regsel_known_good_pair_is_equivalent(tmp_path):
    """Establishes `_REGSEL_RTL` as a genuine known-good/equivalent pair
    (compared against itself under a different top-level instantiation is
    trivial, so this compares it against a structurally different but
    functionally identical rewrite) before the "dropped register" mutant
    below is derived from it."""
    _write(tmp_path / "gold.v", _REGSEL_RTL)
    # Structurally different (nested ternary instead of a case statement)
    # but functionally identical -- same "structurally different,
    # functionally identical" shape as `_GATE_AND_EQUIVALENT` above.
    gate_source = """\
module regsel (
    input  wire [1:0] sel,
    input  wire [7:0] r0,
    input  wire [7:0] r1,
    input  wire [7:0] r2,
    output wire [7:0] dout
);
  assign dout = (sel == 2'd0) ? r0 :
                (sel == 2'd1) ? r1 :
                (sel == 2'd2) ? r2 : 8'h00;
endmodule
"""
    _write(tmp_path / "gate.v", gate_source)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"], top="regsel"),
            "gate": _side(["gate.v"], top="regsel"),
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"
    assert report["counterexample"] is None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_seeded_inversion_pair_produces_counterexample(tmp_path):
    """Acceptance criterion: the "seeded inversion" negative control
    (inverted `cout`, derived from the existing `_ADDER4_RTL` known-good
    fixture) must produce a counterexample, never `"equivalent"`."""
    _write(tmp_path / "gold.v", _ADDER4_RTL)
    _write(tmp_path / "gate.v", _ADDER4_RTL_INVERTED_COUT)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"], top="adder4"),
            "gate": _side(["gate.v"], top="adder4"),
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    # `cout` is inverted for every input combination, so it always diverges;
    # `sum` is untouched and must never be reported as diverging.
    assert counterexample["diverging_outputs"] == ["cout"]


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_seeded_inversion_counterexample_confirmed_by_simulation(tmp_path):
    """The seeded-inversion counterexample vector is independently re-run
    through both netlists via iverilog/vvp and confirmed to actually
    diverge -- not just accepted on the solver's say-so."""
    _write(tmp_path / "gold.v", _ADDER4_RTL)
    _write(tmp_path / "gate.v", _ADDER4_RTL_INVERTED_COUT)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"], top="adder4"),
            "gate": _side(["gate.v"], top="adder4"),
        },
    )

    report = run_equiv(request_path)

    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert simulation["diverging_outputs"] == ["cout"]
    assert (
        simulation["gold_outputs"]["cout"]
        == counterexample["gold_outputs"]["cout"]["bin"]
    )
    assert (
        simulation["gate_outputs"]["cout"]
        == counterexample["gate_outputs"]["cout"]["bin"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_dropped_register_pair_produces_counterexample(tmp_path):
    """Acceptance criterion: the "dropped register" negative control
    (register `r2` never wired into the readback mux, derived from the
    `_REGSEL_RTL` known-good fixture established above) must produce a
    counterexample, never `"equivalent"`. The bug only manifests when
    `sel == 2'd2` and `r1 != r2` -- the SAT solver must pick exactly such a
    vector."""
    _write(tmp_path / "gold.v", _REGSEL_RTL)
    _write(tmp_path / "gate.v", _REGSEL_RTL_DROPPED_REGISTER)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"], top="regsel"),
            "gate": _side(["gate.v"], top="regsel"),
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["diverging_outputs"] == ["dout"]
    assert counterexample["inputs"]["sel"]["value"] == 2
    assert (
        counterexample["inputs"]["r1"]["value"]
        != counterexample["inputs"]["r2"]["value"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_dropped_register_counterexample_confirmed_by_simulation(tmp_path):
    """The dropped-register counterexample vector is independently re-run
    through both netlists via iverilog/vvp and confirmed to actually
    diverge -- not just accepted on the solver's say-so."""
    _write(tmp_path / "gold.v", _REGSEL_RTL)
    _write(tmp_path / "gate.v", _REGSEL_RTL_DROPPED_REGISTER)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"], top="regsel"),
            "gate": _side(["gate.v"], top="regsel"),
        },
    )

    report = run_equiv(request_path)

    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert simulation["diverging_outputs"] == ["dout"]
    assert (
        simulation["gold_outputs"]["dout"]
        == counterexample["gold_outputs"]["dout"]["bin"]
    )
    assert (
        simulation["gate_outputs"]["dout"]
        == counterexample["gate_outputs"]["dout"]["bin"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_design_is_rejected(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_SEQ)
    _write(tmp_path / "gate.v", _GOLD_SEQ)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    with pytest.raises(EquivError, match="combinational-only MVP"):
        run_equiv(request_path)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_port_map_remapping(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_RENAMED_PORTS)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "port_map": {"in0": "a", "in1": "b", "out0": "y"},
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_process_timeout_is_inconclusive_never_equivalent(tmp_path):
    """A real process-level timeout: `timeout_s` is set so small that the
    real `yosys` subprocess cannot possibly finish spawning + parsing in
    time, genuinely exercising `subprocess.TimeoutExpired` -- not mocked."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path, timeout_s=1e-6)

    assert report["status"] == "inconclusive"
    assert report["status"] != "equivalent"
    assert report["diagnostics"][0]["code"] == "process_timeout"
    assert report["counterexample"] is None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_timeout_s_flag_overrides_request(tmp_path):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"]), "timeout_s": 60},
    )

    report = run_equiv(request_path, timeout_s=1e-6)
    assert report["status"] == "inconclusive"


# --------------------------------------------------------------------------- #
# CLI exit codes
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_equivalent_exits_zero(tmp_path, capsys):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_AND_EQUIVALENT)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    exit_code = main(["equiv", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "equivalent"


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_counterexample_exits_three(tmp_path, capsys):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    exit_code = main(["equiv", request_path, "--format", "json"])

    assert exit_code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "counterexample"


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_cli_inconclusive_exits_four_never_zero(tmp_path, capsys):
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    exit_code = main(
        ["equiv", request_path, "--timeout-s", "0.000001", "--format", "json"]
    )

    assert exit_code == 4
    assert exit_code != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "inconclusive"


def test_cli_error_exits_one_with_json_error(tmp_path, capsys):
    request_path = _write_request(
        tmp_path / "r.json", {"gold": _side(["missing.v"]), "gate": _side(["gate.v"])}
    )

    exit_code = main(["equiv", request_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "equiv"
    assert "source not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, capsys):
    request_path = _write_request(
        tmp_path / "r.json", {"gold": _side(["missing.v"]), "gate": _side(["gate.v"])}
    )

    exit_code = main(["equiv", request_path])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt equiv:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["equiv"])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# Corpus integration: real `klt synthesize` -> real `klt equiv`
# --------------------------------------------------------------------------- #


def _find_any_cell_library() -> tuple[str, str, str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    at least one standard-cell **digital** library (`list_cell_libraries()`'s
    own `_fd_sc_`-name-convention filter -- excludes I/O-pad/macro/primitive-
    device libraries, which have no nominal corner to resolve a liberty
    against) -- returns ``(root, variant, cell_library, corner)`` or
    ``None``. Deliberately not pinned to sky130 specifically (mirrors
    `test_synthesize.py`'s own `_find_real_sky130_variant`, generalised to
    whatever family is actually installed on the machine running the tests
    -- this sandbox and CI may each resolve a different one).

    ``corner`` is `list_cell_libraries()`'s own `nominal_corner` field,
    which is always the bare corner suffix (e.g. ``"tt_025C_1v80"``, never
    ``"<cell_library>__tt_025C_1v80"``) regardless of what a library's `.lib`
    file's `default_operating_conditions` attribute reports (issue #820)."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            try:
                libraries = pdk_module.list_cell_libraries(
                    variant=variant["name"], root=install["root"]
                )
            except Exception:
                continue
            for library in libraries["libraries"]:
                return (
                    install["root"],
                    variant["name"],
                    library["name"],
                    library["nominal_corner"],
                )
    return None


_REAL_CELL_LIBRARY = _find_any_cell_library()


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_CELL_LIBRARY is None,
    reason="no real standard-cell liberty resolves via list_pdks() on this machine",
)
def test_corpus_rtl_vs_real_synthesized_gates(tmp_path, monkeypatch):
    """`klt synthesize` (this repo's own Yosys flow) against a real, host-
    resolved open-PDK cell library, then `klt equiv` proves the resulting
    gate netlist equivalent to its own RTL -- the acceptance criterion's
    "matches Yosys ... on >=1 corpus pair" -- and non-equivalent to a
    seeded-broken (dropped carry-in) mutant's own synthesis -- the "seeded-
    broken negative control" criterion. See this module's own docstring for
    why this substitutes for issue #520's not-yet-landed RTL corpus."""
    root, variant, cell_library, corner = _REAL_CELL_LIBRARY
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    liberty_path, _corner, _pdk_info = synthesize_module._resolve_liberty(
        cell_library, corner
    )

    good_dir = tmp_path / "good"
    good_dir.mkdir()
    _write(good_dir / "adder4.v", _ADDER4_RTL)
    good_synth_request = _write_request(
        good_dir / "synth.json",
        {
            "sources": ["adder4.v"],
            "hdl_toplevel": "adder4",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )
    good_synth_report = run_synthesize(good_synth_request)
    assert good_synth_report["status"] == "ok"

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    _write(bad_dir / "adder4.v", _ADDER4_RTL_BROKEN)
    bad_synth_request = _write_request(
        bad_dir / "synth.json",
        {
            "sources": ["adder4.v"],
            "hdl_toplevel": "adder4",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )
    bad_synth_report = run_synthesize(bad_synth_request)
    assert bad_synth_report["status"] == "ok"

    gold_dir = tmp_path / "equiv_gold"
    gold_dir.mkdir()
    gold_rtl = _write(gold_dir / "adder4.v", _ADDER4_RTL)

    # Positive: RTL vs. its own real synthesized gates -> equivalent.
    positive_request = _write_request(
        gold_dir / "positive.json",
        {
            "gold": _side([gold_rtl], top="adder4"),
            "gate": _side(
                [good_synth_report["netlist_path"]],
                top="adder4",
                liberty=liberty_path,
            ),
        },
    )
    positive_report = run_equiv(positive_request)
    assert positive_report["status"] == "equivalent", positive_report["diagnostics"]

    # Negative (seeded-broken corpus pair): RTL vs. the *mutant's* real
    # synthesized gates -> counterexample, independently confirmed.
    negative_request = _write_request(
        gold_dir / "negative.json",
        {
            "gold": _side([gold_rtl], top="adder4"),
            "gate": _side(
                [bad_synth_report["netlist_path"]],
                top="adder4",
                liberty=liberty_path,
            ),
        },
    )
    negative_report = run_equiv(negative_request)
    assert negative_report["status"] == "counterexample"
    assert negative_report["counterexample"]["confirmed_by_simulation"] is True
    assert "sum" in negative_report["counterexample"]["diverging_outputs"]


# Sanity: `subprocess` really is the module `_confirm_counterexample`'s own
# stub-friendliness test above patches (guards against a future refactor
# silently making that stub a no-op).
def test_equiv_uses_stdlib_subprocess():
    assert equiv.subprocess is subprocess
