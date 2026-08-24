"""Tests for `klt equiv` and the `klayout_tools.equiv` library.

Four tiers, mirroring `tests/test_synthesize.py`'s own structure:

- **Pure unit tests** exercise request validation (`run_equiv`'s own
  error paths -- missing/malformed `gold`/`gate`, bad `timeout_s`/
  `port_map`, unresolvable source/liberty files, unsupported engine) and
  the stdout-parsing helpers (`_classify_sat_result`, `_parse_signal_table`,
  `_decode_bin`, `_build_counterexample`) directly against **canned** text,
  no subprocess involved -- these always run, in any CI environment.
- **Deterministic timeout tests** (mocked `subprocess.run`, no real `yosys`
  needed) drive the full `run_equiv()` pipeline -- not just
  `_classify_sat_result` in isolation -- through every "incomplete solve"
  flavor (internal SAT-solver timeout, external process-level kill,
  unrecognized solver output) and confirm each is always `"inconclusive"`,
  never `"equivalent"`, with proof metadata explaining why. See that
  section's own docstring for why a real, genuinely-hard miter was tried
  and rejected as flaky.
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
- **Sequential seeded-broken negative controls** (issue #1314, Phase 2 of
  Epic #707): two more deliberately-broken pairs for the `"yosys-sequential"`
  engine, per `docs/design/sequential-equivalence-survey.md` SS4.3, again each
  derived from an existing known-good fixture in this module -- a
  "buffer-insertion + duplicated register" mutant (derived from
  `_SEQ_GOLD_DFF_RTL`/`_SEQ_GATE_DFF_RTL_BUFFERED`, the already-proven
  register-*preserving* buffer-insertion pair, with an extra pipeline
  register stage added -- a state-element-set change register
  correspondence must catch) and a "reset-style mismatch" mutant (derived
  from `_SEQ_GOLD_SIMPLE_DFF_RTL`, async reset on `gold` vs. synchronous
  reset on `gate`). Both assert `status == "counterexample"` and an
  iverilog/vvp-confirmed multi-cycle counterexample, mirroring #832's own
  two-sided acceptance bar extended to the sequential engine.
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

from helpers.subprocess_fakes import fake_completed
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
# Deterministic timeout tests: mocked subprocess, no real yosys/wall-clock
# --------------------------------------------------------------------------- #
#
# Issue #707 Phase 1b's own scope: "guarantee timeout reports inconclusive,
# never equivalent", with a test that "forces the timeout/incomplete-solve
# path deterministically (not relying on wall-clock flakiness)". A real,
# genuinely-hard miter was tried while developing this test and rejected: a
# `yosys`'s own `sat -timeout N` only interrupts once the solver reaches an
# internal checkpoint, which a pathologically hard instance may not reach
# for minutes (observed live: an 8-24 bit multiplier-vs-shift-add-multiplier
# miter still had not hit a `-timeout 2` internal interrupt after 60s of
# real wall-clock solving) -- exactly the flakiness the acceptance criterion
# warns against, since "hard enough to not finish, but not so slow it stalls
# CI" is not a portable, machine-independent property. Injecting the exact
# stdout a real interrupted solve produces (`subprocess.run` mocked, no real
# `yosys` binary involved at all) is the deterministic substitute the
# issue's own "injected low timeout bound" wording calls for, and -- unlike
# the real-Yosys tests below -- these run in *any* CI environment, including
# one with no `yosys`/`iverilog` install.


def _mock_yosys_run(*, run_stdout: str | None = None, raise_timeout: bool = False):
    """Build a `subprocess.run` stand-in answering the two calls a real
    `yosys` binary would receive from `run_equiv` -- the main
    `yosys -s <script>` invocation and the `yosys -V` version probe -- from
    canned data.

    `raise_timeout=True` simulates the *external* process-level kill
    (`subprocess.TimeoutExpired`, what a real too-small `timeout_s`
    produces against a real subprocess); otherwise the main call
    "completes" with `run_stdout` and returncode 0 (mirrors a real `yosys`
    process that finishes running its script -- including an internal SAT
    solver that gave up on its own `-timeout` -- without ever being killed).
    """

    def _run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            if raise_timeout:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))
            return fake_completed(stdout=run_stdout or "", returncode=0)
        if cmd == ["yosys", "-V"]:
            return fake_completed(stdout="Yosys 0.67 (git sha1 deadbeef)\n")
        raise AssertionError(f"unexpected subprocess.run call in mock: {cmd!r}")

    return _run


def test_solver_internal_timeout_is_inconclusive_never_equivalent(
    tmp_path, monkeypatch
):
    """Forces the *internal* Yosys SAT-solver-timeout path (`sat -timeout N`
    interrupting gracefully, process exits 0) through the full
    `run_equiv()` pipeline -- not just the canned-text
    `test_classify_sat_result_timeout_never_equivalent` unit test above,
    which only exercises `_classify_sat_result` in isolation."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_TIMEOUT_TEXT)
    )

    report = run_equiv(request_path)

    assert report["status"] == "inconclusive"
    assert report["status"] != "equivalent"
    assert report["counterexample"] is None
    assert report["diagnostics"][0]["code"] == "solver_timeout"
    assert "timeout" in report["diagnostics"][0]["message"].lower()
    assert "inconclusive" in report["diagnostics"][0]["message"].lower()


def test_process_level_timeout_mocked_is_inconclusive_never_equivalent(
    tmp_path, monkeypatch
):
    """The *external* process-kill flavor of the same guarantee, forced via
    a mocked `subprocess.TimeoutExpired` rather than a real too-small
    `timeout_s` racing a real subprocess -- portable to any CI environment,
    complementing the real-`yosys`
    `test_process_timeout_is_inconclusive_never_equivalent` below."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(equiv.subprocess, "run", _mock_yosys_run(raise_timeout=True))

    report = run_equiv(request_path, timeout_s=5.0)

    assert report["status"] == "inconclusive"
    assert report["status"] != "equivalent"
    assert report["counterexample"] is None
    assert report["diagnostics"][0]["code"] == "process_timeout"
    assert "inconclusive" in report["diagnostics"][0]["message"].lower()


def test_unrecognized_solver_output_mocked_is_inconclusive_never_equivalent(
    tmp_path, monkeypatch
):
    """A third "incomplete solve" flavor: `yosys` exits 0 but its output
    matches none of the known SAT-verdict patterns (a resource bound other
    than the modelled `-timeout`, or a future Yosys version's own wording
    change) -- also never reported as `"equivalent"`."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_UNRECOGNIZED_TEXT)
    )

    report = run_equiv(request_path)

    assert report["status"] == "inconclusive"
    assert report["status"] != "equivalent"
    assert report["diagnostics"][0]["code"] == "unrecognized_solver_output"


def test_solver_success_mocked_is_equivalent_not_inconclusive(tmp_path, monkeypatch):
    """Regression guard for the mocked-subprocess harness itself: a genuine
    `SUCCESS!` verdict must still report `"equivalent"` -- the timeout
    coverage above must not come at the cost of misclassifying a real
    completed proof (acceptance criterion: existing equivalent/
    counterexample cases stay correct)."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_SUCCESS_TEXT)
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"
    assert report["diagnostics"] == []


# --------------------------------------------------------------------------- #
# Issue #1349 AC1: a solver-reported counterexample whose own iverilog/vvp
# replay does NOT reproduce a diverging output must be reported
# `"inconclusive"`, never `"counterexample"` -- the engine already computes
# `confirmed_by_simulation: False` and appends `counterexample_not_reproduced`
# to `diagnostics`, it just did not act on it (the defect this issue tracks,
# discovered live against a real post-route netlist pair -- see the issue
# body's own evidence). Forced deterministically here by faking
# `_confirm_counterexample`/`_confirm_sequential_counterexample` themselves
# (the smallest surface that exercises `run_equiv`'s own status-downgrade
# decision) rather than every downstream iverilog/vvp subprocess call --
# portable to any CI environment, no real `iverilog` install required.
# --------------------------------------------------------------------------- #


def test_unconfirmed_combinational_counterexample_downgrades_to_inconclusive(
    tmp_path, monkeypatch
):
    """Combinational (`"yosys"`) engine path -- `run_equiv`'s block that
    calls `_confirm_counterexample` right after `_classify_sat_result`
    reports `"counterexample"` (`src/klayout_tools/equiv.py`, near the
    `"engine": "icarus"`/confirmation block)."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_FAIL_TEXT)
    )

    def _fake_confirm_not_reproduced(
        *, counterexample, netlist_path, output_dir, diagnostics
    ):
        # Mirrors the real `_confirm_counterexample`'s own "not reproduced"
        # outcome shape (a simulation that ran, but found no divergence).
        counterexample["simulation"] = {
            "engine": "icarus",
            "engine_version": "12.0",
            "gold_outputs": {"y": "0"},
            "gate_outputs": {"y": "0"},
            "diverging_outputs": [],
        }
        counterexample["confirmed_by_simulation"] = False
        diagnostics.append(
            {
                "severity": "warning",
                "code": "counterexample_not_reproduced",
                "message": "re-running the solver's counterexample through "
                "the flattened netlists via iverilog/vvp did not reproduce "
                "a diverging output -- treat this counterexample with "
                "suspicion",
            }
        )

    monkeypatch.setattr(equiv, "_confirm_counterexample", _fake_confirm_not_reproduced)

    report = run_equiv(request_path)

    assert report["status"] == "inconclusive"
    assert report["status"] != "counterexample"
    # The counterexample object itself is preserved (issue #1349: it's still
    # useful evidence for a human to inspect why the run is inconclusive) --
    # only the top-level verdict changes.
    assert report["counterexample"] is not None
    assert report["counterexample"]["confirmed_by_simulation"] is False
    assert any(
        diag["code"] == "counterexample_not_reproduced"
        for diag in report["diagnostics"]
    )


def test_confirmed_by_simulation_none_keeps_combinational_counterexample_status(
    tmp_path, monkeypatch
):
    """Regression guard for the downgrade above: `confirmed_by_simulation is
    None` (simulation could not be attempted at all, e.g. no `iverilog` on
    `$PATH`) must NOT be downgraded -- there is no simulation evidence
    either way, so the solver's own `"counterexample"` verdict stands."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_FAIL_TEXT)
    )

    def _fake_confirm_unavailable(
        *, counterexample, netlist_path, output_dir, diagnostics
    ):
        counterexample["confirmed_by_simulation"] = None
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog not found on $PATH -- counterexample "
                "reported by the solver only, not independently confirmed "
                "by simulation",
            }
        )

    monkeypatch.setattr(equiv, "_confirm_counterexample", _fake_confirm_unavailable)

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    assert report["counterexample"]["confirmed_by_simulation"] is None


def test_cli_solver_internal_timeout_mocked_exits_four_never_zero(
    tmp_path, capsys, monkeypatch
):
    """CLI-level counterpart of
    `test_solver_internal_timeout_is_inconclusive_never_equivalent`: exit
    code 4 (never 0), matching the real-yosys
    `test_cli_inconclusive_exits_four_never_zero` below but for the
    internal-solver-timeout flavor specifically, and without needing a
    real `yosys` install."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GOLD_AND)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_yosys_run(run_stdout=_SAT_TIMEOUT_TEXT)
    )

    exit_code = main(["equiv", request_path, "--format", "json"])

    assert exit_code == 4
    assert exit_code != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "inconclusive"
    assert out["diagnostics"][0]["code"] == "solver_timeout"


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


# --------------------------------------------------------------------------- #
# Issue #1313 (Phase 2 of Epic #707): "yosys-sequential" engine --
# register-correspondence sequential equivalence via equiv_make/
# equiv_simple/equiv_induct/equiv_status, with a bounded-SAT stage-2
# fallback to extract a genuine, confirmable counterexample when stage 1
# leaves cells unproven. See `equiv.py`'s own module docstring ("Engine:
# yosys-sequential" section) for the full two-stage rationale, and
# `docs/design/sequential-equivalence-survey.md` SS4.2 for why this is the
# evidence-matched first sequential-equivalence technique to ship.
#
# `_SEQ_GOLD_DFF_RTL`/`_SEQ_GATE_DFF_RTL_BUFFERED` model the shape this
# repo's own P&R pipeline actually, measurably produces (buffer insertion
# on the combinational logic feeding a register -- same register set, same
# names, zero register-count change; see `docs/cli/place-and-route.md`'s
# "As-built netlist" section, quoted by the survey's own SS2.2): a
# double-inverter pair inserted between the adder and the register it
# feeds is functionally a no-op, structurally a real insertion, exactly
# the "40 of ~720 changed instances... unchanged instance names" shape
# the survey's own real, measured corpus run found.
# --------------------------------------------------------------------------- #

_SEQ_GOLD_DFF_RTL = """\
module top(input clk, input rst, input [3:0] a, input [3:0] b, output reg [3:0] q);
  wire [3:0] sum = a + b;
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'b0;
    else q <= sum;
endmodule
"""

# Register-preserving transformation: a buffer-insertion-shaped mutation
# (double-invert on the combinational cone feeding the register) -- same
# register set/names, only the combinational logic changed. Functionally
# identical to `_SEQ_GOLD_DFF_RTL`.
_SEQ_GATE_DFF_RTL_BUFFERED = """\
module top(input clk, input rst, input [3:0] a, input [3:0] b, output reg [3:0] q);
  wire [3:0] sum_raw = a + b;
  wire [3:0] sum = ~(~sum_raw);
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'b0;
    else q <= sum;
endmodule
"""

_SEQ_GOLD_SIMPLE_DFF_RTL = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= d;
endmodule
"""

# Structurally different (extra double-inverter buffer pair on the D
# input) but functionally identical to `_SEQ_GOLD_SIMPLE_DFF_RTL`.
_SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED = """\
module top(input clk, input rst, input d, output reg q);
  wire d_buf = ~(~d);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= d_buf;
endmodule
"""

# Seeded-broken: `d` is inverted before the register -- a real "polarity
# flip" bug class, not a synthetic/arbitrary corruption. Same register set
# as `_SEQ_GOLD_SIMPLE_DFF_RTL`, so this is exactly the shape register
# correspondence must catch (a genuinely different combinational cone
# feeding an otherwise-corresponding register).
_SEQ_GATE_SIMPLE_DFF_RTL_INVERTED = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 1'b0;
    else q <= ~d;
endmodule
"""

# Issue #1314 (Phase 2 of Epic #707): two more seeded-broken sequential
# negative controls, following #832's own precedent (deliberately-broken
# pairs derived from an existing known-good fixture already in this module)
# extended to the sequential engine per
# `docs/design/sequential-equivalence-survey.md` SS4.3.
#
# (a) A **buffer-insertion mutant that also duplicates a register** --
# derived from `_SEQ_GOLD_DFF_RTL`/`_SEQ_GATE_DFF_RTL_BUFFERED` above (the
# already-proven register-*preserving* buffer-insertion pair): the same
# double-inverter buffer insertion, but an extra pipeline register stage is
# also (incorrectly) inserted between the combinational cone and the
# original register -- a hypothetical P&R bug that *did* change the
# state-element set (register count 1 -> 2), the exact negative control
# SS4.3 names as the case register correspondence must catch, unlike the
# register-*preserving* buffer insertion already proven equivalent above.
_SEQ_GOLD_DUP_RTL = _SEQ_GOLD_DFF_RTL

_SEQ_GATE_DUP_REGISTER_RTL = """\
module top(input clk, input rst, input [3:0] a, input [3:0] b, output reg [3:0] q);
  wire [3:0] sum_raw = a + b;
  wire [3:0] sum = ~(~sum_raw);
  reg [3:0] q_pipe;
  always @(posedge clk or posedge rst)
    if (rst) q_pipe <= 4'b0;
    else q_pipe <= sum;
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'b0;
    else q <= q_pipe;
endmodule
"""

# (b) A **reset-polarity/style mismatch mutant** -- derived from
# `_SEQ_GOLD_SIMPLE_DFF_RTL` above, SS3.5's own flagged risk class: `gold`
# resets `q` *asynchronously* (`posedge clk or posedge rst` -- reset takes
# effect immediately, independent of `clk`); `gate` resets `q`
# *synchronously* (`posedge clk` only -- reset is only sampled on a clock
# edge), a real "async vs. sync reset style" bug class SS4.3 names
# explicitly, not a synthetic/arbitrary corruption. Same register set/names
# as `_SEQ_GOLD_SIMPLE_DFF_RTL`, so this exercises the same
# register-correspondence machinery the register-count mutant above does
# not.
_SEQ_GOLD_RESET_RTL = _SEQ_GOLD_SIMPLE_DFF_RTL

_SEQ_GATE_SYNC_RESET_RTL = """\
module top(input clk, input rst, input d, output reg q);
  always @(posedge clk)
    if (rst) q <= 1'b0;
    else q <= d;
endmodule
"""

# Note: `equiv_make`/`equiv_induct`/`clk2fflogic` are plain built-in Yosys
# passes (verified live against this repo's own pinned Yosys v0.67 while
# developing this engine) -- no separate `sby`/`eqy` install is needed to
# run the "yosys-sequential" engine itself, only `yosys` (already gated by
# `HAVE_YOSYS` below, the same guard the combinational engine's own tests
# use).


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_proves_register_preserving_buffer_insertion(tmp_path):
    """Acceptance criterion: the engine drives equiv_make/equiv_simple/
    equiv_induct/equiv_status end-to-end and reports `"equivalent"` on a
    register-preserving transformation shaped like this repo's own real,
    measured P&R output (SS2.2 of the survey)."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "yosys-sequential"
    assert report["status"] == "equivalent"
    assert report["counterexample"] is None
    assert report["diagnostics"] == []
    assert report["induction_depth"] == equiv.DEFAULT_INDUCTION_DEPTH
    assert os.path.isfile(report["artifacts"]["script_path"])
    assert os.path.isfile(report["artifacts"]["netlist_path"])
    # Stage 2 (the bounded counterexample search) never runs when stage 1
    # alone already proves equivalence.
    assert report["artifacts"]["stage2_script_path"] is None
    assert report["artifacts"]["stage2_log_path"] is None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_proves_buffer_pair_on_single_bit_register(tmp_path):
    """A second, minimal register-preserving pair (single-bit register,
    double-inverter buffer on D) -- the simplest possible instance of the
    same transformation shape."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"
    assert report["counterexample"] is None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_seeded_broken_pair_produces_counterexample(tmp_path):
    """Acceptance criterion: a seeded-broken (deliberate D-input polarity
    inversion) register-preserving pair must produce a `"counterexample"`,
    never `"equivalent"` -- the negative control this engine's whole
    purpose is to catch."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_INVERTED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert "q" in counterexample["diverging_outputs"]
    assert counterexample["first_diverging_cycle"] is not None
    assert len(counterexample["cycles"]) == equiv.DEFAULT_INDUCTION_DEPTH
    # Stage 2 (the bounded counterexample search) always runs when stage 1
    # cannot prove equivalence.
    assert report["artifacts"]["stage2_script_path"] is not None
    assert os.path.isfile(report["artifacts"]["stage2_script_path"])


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_sequential_engine_counterexample_confirmed_by_multicycle_simulation(
    tmp_path,
):
    """The epic's own "a counterexample is executable" discipline,
    extended for multi-cycle testbenches (this issue's own acceptance
    criterion): the concrete cycle-by-cycle trace is independently re-run
    through both netlists via iverilog/vvp, not just trusted from the
    solver."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_INVERTED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert "q" in simulation["diverging_outputs"]
    # The simulation independently reproduces the *same* diverging output
    # the solver reported -- not merely "some" divergence, somewhere.
    assert set(simulation["diverging_outputs"]) & set(
        counterexample["diverging_outputs"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_sequential_engine_buffer_insertion_with_duplicated_register_is_counterexample(
    tmp_path,
):
    """Issue #1314 seeded-broken negative control (a): a buffer-insertion
    mutant that also duplicates a register (state-element-set change, not
    the register-*preserving* transformation
    `test_sequential_engine_proves_register_preserving_buffer_insertion`
    above proves equivalent) must be reported `"counterexample"`, never a
    false `"equivalent"`, with an executable, iverilog/vvp-confirmed
    counterexample."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_DUP_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_DUP_REGISTER_RTL)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert "q" in counterexample["diverging_outputs"]
    assert counterexample["first_diverging_cycle"] is not None
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert set(simulation["diverging_outputs"]) & set(
        counterexample["diverging_outputs"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
def test_sequential_engine_reset_style_mismatch_is_counterexample(tmp_path):
    """Issue #1314 seeded-broken negative control (b): an async-vs-sync
    reset-style mismatch (SS3.5's own flagged risk class) must be reported
    `"counterexample"`, never a false `"equivalent"` or a spurious
    `"inconclusive"`-forever hang, with an executable,
    iverilog/vvp-confirmed counterexample."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_RESET_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SYNC_RESET_RTL)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert "q" in counterexample["diverging_outputs"]
    assert counterexample["first_diverging_cycle"] is not None
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    assert simulation["engine"] == "icarus"
    assert set(simulation["diverging_outputs"]) & set(
        counterexample["diverging_outputs"]
    )


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_timeout_is_inconclusive_never_equivalent(tmp_path):
    """This module's own scope requirement, restated for the sequential
    engine: a process-level timeout is always `"inconclusive"`, never
    `"equivalent"`."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path, timeout_s=1e-6)

    assert report["status"] == "inconclusive"
    assert report["status"] != "equivalent"
    assert report["counterexample"] is None
    assert report["diagnostics"][0]["code"] == "process_timeout"


# --------------------------------------------------------------------------- #
# Issue #1349 AC1, sequential-engine counterpart: `_run_sequential`'s stage-2
# path (`src/klayout_tools/equiv.py`, the block that calls
# `_confirm_sequential_counterexample` right after stage 2's own
# `_classify_sat_result` reports `"counterexample"`) must apply the identical
# `confirmed_by_simulation`-gated downgrade the combinational engine's tests
# above cover. This is exactly the false-verdict shape the issue's own real
# post-route evidence describes: a stage-2 bounded-SAT counterexample whose
# solver-reported trace does not reproduce via iverilog/vvp.
#
# Deterministic and toolchain-independent: `subprocess.run` is mocked for
# both yosys stages (stage 1 reports "not all proven" so stage 2 runs; stage
# 2 reports a SAT `FAIL!` verdict), `_parse_module_ports` is faked to avoid
# needing a real Yosys-written netlist file on disk, and
# `_confirm_sequential_counterexample` is faked exactly as the combinational
# test above fakes `_confirm_counterexample` -- no real `yosys`/`iverilog`
# install required.
# --------------------------------------------------------------------------- #


def _mock_sequential_yosys_run(stage1_stdout: str, stage2_stdout: str):
    """`subprocess.run` stand-in for `_run_sequential`'s two `yosys -s
    <script>` stages plus the `yosys -V` version probe -- distinguishes the
    two stages by their generated script filename (`equiv_seq_stage1.ys` /
    `equiv_seq_stage2.ys`, per `_run_sequential`'s own naming), mirroring
    `_mock_yosys_run`'s combinational-engine precedent above."""

    def _run(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            script_path = cmd[2]
            if script_path.endswith("stage1.ys"):
                return fake_completed(stdout=stage1_stdout, returncode=0)
            if script_path.endswith("stage2.ys"):
                return fake_completed(stdout=stage2_stdout, returncode=0)
            raise AssertionError(f"unexpected yosys script in mock: {script_path!r}")
        if cmd == ["yosys", "-V"]:
            return fake_completed(stdout="Yosys 0.67 (git sha1 deadbeef)\n")
        raise AssertionError(f"unexpected subprocess.run call in mock: {cmd!r}")

    return _run


# Stage 1: at least one unproven `$equiv` cell -- matches neither
# `_EQUIV_NONE_FOUND_RE` nor `_EQUIV_ALL_PROVEN_RE`, so `_run_sequential`
# falls through to stage 2 (real Yosys's own wording for this case, per this
# module's own docstring).
_SEQ_STAGE1_PARTIALLY_UNPROVEN_TEXT = """
Found 3 unproven $equiv cells (3 groups) in equiv:
Proved 1 previously unproven $equiv cells.
  Of those cells 1 are proven and 2 are unproven.
"""


def test_unconfirmed_sequential_counterexample_downgrades_to_inconclusive(
    tmp_path, monkeypatch
):
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_sequential_yosys_run(
            stage1_stdout=_SEQ_STAGE1_PARTIALLY_UNPROVEN_TEXT,
            stage2_stdout=_SAT_FAIL_TEXT,
        ),
    )
    monkeypatch.setattr(
        equiv,
        "_parse_module_ports",
        lambda netlist_path, module_name: {
            "clk": "input",
            "rst": "input",
            "d": "input",
            "q": "output",
        },
    )

    def _fake_confirm_not_reproduced(
        *, counterexample, ports, netlist_path, output_dir, diagnostics
    ):
        counterexample["simulation"] = {
            "engine": "icarus",
            "engine_version": "12.0",
            "cycles": [],
            "diverging_outputs": [],
        }
        counterexample["confirmed_by_simulation"] = False
        diagnostics.append(
            {
                "severity": "warning",
                "code": "counterexample_not_reproduced",
                "message": "re-running the solver's counterexample trace "
                "through the flattened netlists via iverilog/vvp did not "
                "reproduce a diverging output on any replayed cycle -- "
                "treat this counterexample with suspicion",
            }
        )

    monkeypatch.setattr(
        equiv, "_confirm_sequential_counterexample", _fake_confirm_not_reproduced
    )

    report = run_equiv(request_path)

    assert report["status"] == "inconclusive"
    assert report["status"] != "counterexample"
    assert report["counterexample"] is not None
    assert report["counterexample"]["confirmed_by_simulation"] is False
    assert any(
        diag["code"] == "counterexample_not_reproduced"
        for diag in report["diagnostics"]
    )


def test_confirmed_by_simulation_none_keeps_sequential_counterexample_status(
    tmp_path, monkeypatch
):
    """Regression guard, sequential counterpart of the combinational
    `confirmed_by_simulation is None` guard above: no simulation evidence
    either way must NOT be downgraded."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_sequential_yosys_run(
            stage1_stdout=_SEQ_STAGE1_PARTIALLY_UNPROVEN_TEXT,
            stage2_stdout=_SAT_FAIL_TEXT,
        ),
    )
    monkeypatch.setattr(
        equiv,
        "_parse_module_ports",
        lambda netlist_path, module_name: {
            "clk": "input",
            "rst": "input",
            "d": "input",
            "q": "output",
        },
    )

    def _fake_confirm_unavailable(
        *, counterexample, ports, netlist_path, output_dir, diagnostics
    ):
        counterexample["confirmed_by_simulation"] = None
        diagnostics.append(
            {
                "severity": "warning",
                "code": "simulation_unavailable",
                "message": "iverilog not found on $PATH -- counterexample "
                "reported by the solver only, not independently confirmed "
                "by simulation",
            }
        )

    monkeypatch.setattr(
        equiv, "_confirm_sequential_counterexample", _fake_confirm_unavailable
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample"
    assert report["counterexample"]["confirmed_by_simulation"] is None


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_accepts_combinational_design_too(tmp_path):
    """The sequential engine is a superset, not a narrower scope: a purely
    combinational pair (zero registers) degrades gracefully to a
    combinational proof via `equiv_simple` alone."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_AND_EQUIVALENT)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_custom_induction_depth(tmp_path):
    """`request.induction_depth` overrides `DEFAULT_INDUCTION_DEPTH` and is
    echoed back in the report."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
            "induction_depth": 2,
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent"
    assert report["induction_depth"] == 2


@pytest.mark.parametrize("bad_depth", [0, -1, "4", 1.5, True])
def test_sequential_engine_bad_induction_depth_is_error(tmp_path, bad_depth):
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
            "induction_depth": bad_depth,
        },
    )

    with pytest.raises(EquivError, match="induction_depth must be a positive integer"):
        run_equiv(request_path)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_cli_exit_codes(tmp_path, capsys):
    """CLI-level counterpart, mirroring the combinational engine's own
    `test_cli_equivalent_exits_zero`/`test_cli_counterexample_exits_three`:
    the sequential engine's `status` maps through the same shared 0/3/4
    exit-code table."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)
    equivalent_request = _write_request(
        tmp_path / "equivalent.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )
    exit_code = main(["equiv", equivalent_request, "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "equivalent"

    _write(tmp_path / "gate_broken.v", _SEQ_GATE_SIMPLE_DFF_RTL_INVERTED)
    broken_request = _write_request(
        tmp_path / "broken.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate_broken.v"]),
            "engine": "yosys-sequential",
        },
    )
    exit_code = main(["equiv", broken_request, "--format", "json"])
    assert exit_code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "counterexample"


# --------------------------------------------------------------------------- #
# Issue #1313 acceptance criterion: "Validated on at least one real
# register-preserving P&R transformation from this repo's own pipeline."
#
# Requires a real `openroad` binary and a real, host-resolved sky130 PDK
# install -- this repo's own CI does not install `openroad` (grep-confirmed
# against `.github/workflows/ci.yml`, mirroring `test_place_and_route.py`'s
# own `HAVE_OPENROAD`-gated integration tests, which are skipped in CI for
# the identical reason), so this test never runs there. It is a real, not
# fake, live-verification path for a machine that does have `openroad`
# installed -- the same posture `test_place_and_route.py`'s
# `test_integration_real_openroad_gcd_worked_example` already established.
# --------------------------------------------------------------------------- #


def _find_real_sky130_pnr_variant() -> tuple[str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    a real `sky130_fd_sc_hd` liberty + tech/cell LEF + GDS view -- the four
    assets a real P&R run actually needs. Returns `(root, variant)` or
    `None`. Mirrors `test_place_and_route.py`'s own
    `_find_real_pnr_variant()` (duplicated rather than imported cross-module
    -- see this file's own note on why, above); this repo's own liberty-only
    `_find_any_cell_library()` in *this* file is not sufficient here since
    it never checks for LEF/GDS at all."""
    try:
        result = pdk_module.list_pdks()
    except Exception:
        return None
    cell_library = "sky130_fd_sc_hd"
    for install in result["installs"]:
        for variant in install["variants"]:
            lib_dir = os.path.join(
                install["root"], variant["name"], "libs.ref", cell_library
            )
            if all(
                os.path.exists(os.path.join(lib_dir, sub))
                for sub in (
                    "lib",
                    os.path.join("techlef", f"{cell_library}__nom.tlef"),
                    os.path.join("lef", f"{cell_library}.lef"),
                    os.path.join("gds", f"{cell_library}.gds"),
                )
            ):
                return install["root"], variant["name"]
    return None


_REAL_SKY130_PNR_VARIANT = _find_real_sky130_pnr_variant()


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    shutil.which("openroad") is None, reason="openroad is not installed on this machine"
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
@pytest.mark.xfail(
    reason=(
        "issue #1323 ran this for real (openroad 26Q3-1499-g46ab99414e via "
        "Docker + a real volare sky130A install) and found the "
        "yosys-sequential engine reported 'counterexample', not "
        "'equivalent' -- OpenROAD's real fanout/hold-repair buffer "
        "insertion (e.g. sky130_fd_sc_hd__buf_4 'place<N>' instances) "
        "renames intermediate nets, defeating equiv_make's literal "
        "wire-name correspondence (159 'Unproven $equiv' cells). Issue "
        "#1349 fixed the *reporting* half of this: as of #1349, that "
        "unreproduced counterexample is correctly reported 'inconclusive' "
        "(confirmed_by_simulation: false), never the unsound "
        "'counterexample' verdict it used to be -- re-verified live against "
        "the same real toolchain, 2026-08-23. The underlying *proof-"
        "strength* gap (equiv_induct not converging on this netlist's "
        "159 name-mismatched internal $equiv cells) is unfixed and tracked "
        "by issue #1353; remove this xfail once that lands (pytest will "
        "then report XPASS as the signal)."
    ),
    strict=False,
)
def test_sequential_engine_real_pnr_register_preserving_transformation(
    tmp_path, monkeypatch
):
    """`klt synthesize`'s pre-P&R netlist vs. `klt place-and-route`'s real,
    post-route `verilog_path` (issue #996) on the GCD worked example.

    As of issue #1349 (2026-08-23), this is confirmed to run for real
    against a genuine `openroad`/sky130A toolchain and correctly reports
    `"inconclusive"` (not the false `"counterexample"` #1349 fixed), but
    does NOT yet reach `"equivalent"` -- see the `xfail` reason above and
    issue #1353 for the full root-cause evidence. The intent (once #1353
    lands) is still to prove `"equivalent"`, the direct real-P&R validation
    this issue's own acceptance criteria requires."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    cell_library, corner = "sky130_fd_sc_hd", "tt_025C_1v80"
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.place_and_route import run_place_and_route

    # The canonical GCD RTL is the committed worked example -- the same file
    # `tests/corpus/place_and_route/regenerate.sh` treats as the GCD source of
    # truth (its `[gcd]="$REPO_ROOT/examples/functional-verification/gcd.v"`
    # mapping), and the same one `test_functional_verification.py` reads via its
    # own `EXAMPLE_DIR`. An earlier revision of this test pointed at a
    # `tests/corpus/place_and_route/gcd/gcd.v` fixture that was never committed
    # (there is no such per-design subdirectory convention in this repo), so the
    # test unconditionally skipped before reaching openroad/PDK/yosys-sequential.
    # This path is tracked in git, so a missing file is a repo bug, not an
    # environment condition -- assert rather than skip, so the miss can never
    # again masquerade as a legitimately-skipped environment gate.
    gcd_rtl_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "functional-verification"
        / "gcd.v"
    )
    assert gcd_rtl_path.is_file(), f"tracked GCD RTL missing: {gcd_rtl_path}"

    synth_dir = tmp_path / "synth"
    synth_dir.mkdir()
    _write(synth_dir / "gcd.v", gcd_rtl_path.read_text(encoding="utf-8"))
    synth_request = _write_request(
        synth_dir / "synth.json",
        {
            "sources": ["gcd.v"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )
    synth_report = run_synthesize(synth_request)
    assert synth_report["status"] == "ok"

    pnr_dir = tmp_path / "pnr"
    pnr_dir.mkdir()
    # Request shape mirrors `test_place_and_route.py`'s own
    # `_base_request()`/`test_integration_real_openroad_gcd_worked_example`
    # -- duplicated here (rather than imported cross-module) since
    # `tests/` has no `__init__.py` package structure to import through.
    pnr_request = _write_request(
        pnr_dir / "pnr.json",
        {
            "engine": "openroad",
            "netlist": synth_report["netlist_path"],
            "hdl_toplevel": "gcd",
            "pdk": {"cell_library": cell_library, "corner": corner},
            "floorplan": {
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "unithd",
            },
            "io": {"layer_h": "met3", "layer_v": "met2"},
            "constraints": {"clock_port": "clk", "clock_period_ns": 1.1},
            "seed": 1,
            "target_stage": "route",
        },
    )
    pnr_report = run_place_and_route(pnr_request)
    assert pnr_report["status"] == "ok"
    assert pnr_report["verilog_path"] is not None

    liberty_path, _corner, _pdk_info = synthesize_module._resolve_liberty(
        cell_library, corner
    )

    equiv_dir = tmp_path / "equiv"
    equiv_dir.mkdir()
    equiv_request = _write_request(
        equiv_dir / "equiv.json",
        {
            "gold": _side(
                [synth_report["netlist_path"]], top="gcd", liberty=liberty_path
            ),
            "gate": _side(
                [pnr_report["verilog_path"]], top="gcd", liberty=liberty_path
            ),
            "engine": "yosys-sequential",
            "timeout_s": 300,
        },
    )
    equiv_report = run_equiv(equiv_request)

    # Evidence for issue #1323 / Epic #707 acceptance criterion 3: print the
    # full report (status/artifacts/diagnostics) so a `-s` run captures it
    # verbatim for the PR's evidence trail.
    print(
        "\n=== issue #1323 real pre/post-route equiv_report ===\n"
        + json.dumps(equiv_report, indent=2)
    )

    assert equiv_report["status"] == "equivalent", equiv_report["diagnostics"]


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
