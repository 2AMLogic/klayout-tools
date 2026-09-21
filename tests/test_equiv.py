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
import re
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

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

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


def _synth_netlist_path(synth_request_path: str, hdl_toplevel: str, run_id: str) -> str:
    """The real absolute path `run_synthesize` wrote its mapped netlist to.

    Issue #1844 normalized the response's own `netlist_path` field to the
    `{path, scope}` shape (and `tmp_path` is not a git repo, so it reports
    `scope: "external"`, `path: None` -- correctly omitting the absolute
    path). These integration tests still need the real filesystem path to
    wire into a downstream request, so this reconstructs it directly from
    `run_synthesize`'s own documented convention (`synthesize.py`'s module
    docstring): `.klt/synthesize/<run_id>/<hdl_toplevel>_synth.v`, next to the
    request file.
    """
    request_dir = os.path.dirname(os.path.abspath(synth_request_path))
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id
    ):
        raise ValueError("invalid synthesis run_id")
    return os.path.join(
        request_dir, ".klt", "synthesize", run_id, f"{hdl_toplevel}_synth.v"
    )


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


def test_yosys_error_message_wasi_sandbox_script_not_found_hint(tmp_path):
    """When yosys reports it could not read a script file that verifiably
    exists on the host filesystem, the raised error names the WASI-sandbox
    (e.g. yowasp-yosys) hypothesis -- see issue #1368/#1755."""
    existing_script = tmp_path / "equiv_seq_stage1.ys"
    existing_script.write_text("read_verilog gold.v\n", encoding="utf-8")

    stderr = (
        f"ERROR: Can't open script file `{existing_script}' for "
        "reading: No such file or directory\n"
    )

    message = equiv._yosys_error_message("", stderr, 1)

    assert "yosys equivalence check failed:" in message
    assert "WASI-sandboxed build" in message
    assert "yowasp-yosys" in message
    assert "$PATH" in message


def test_yosys_error_message_missing_script_no_hint(tmp_path):
    """The same 'script file ... for reading' message is left unchanged --
    no WASI-sandbox hint appended -- when the referenced path genuinely does
    not exist on the host filesystem (a different failure)."""
    missing_script = tmp_path / "does-not-exist" / "equiv_seq_stage1.ys"

    stderr = (
        f"ERROR: Can't open script file `{missing_script}' for "
        "reading: No such file or directory\n"
    )

    message = equiv._yosys_error_message("", stderr, 1)

    assert message == (
        "yosys equivalence check failed: ERROR: Can't open script file "
        f"`{missing_script}' for reading: No such file or directory"
    )
    assert "WASI" not in message


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
        *, counterexample, netlist_path, output_dir, diagnostics, sim_backend
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
        *, counterexample, netlist_path, output_dir, diagnostics, sim_backend
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

# Issue #1353: a *miniature* of the real post-route failure mode -- two
# genuinely equivalent designs that share an internal wire *name* (`n1`)
# carrying opposite polarity on the two sides, exactly as OpenROAD's
# resizing/repair/cloning passes leave same-named internal wires meaning
# different things. `equiv_make` pairs `n1` by name, that pairing can never
# be proven, and (because a `$equiv` cell is also a cut point) the false
# pairing poisons the `y`/`z` output obligations downstream of it too. The
# `(* keep *)` attributes stop Yosys's own `opt_clean` from folding the
# wires away before `equiv_make` runs, which is what makes this small RTL
# stand in for a real gate-level netlist's named cell-pin wires.
_SEQ_GOLD_RENAMED_INTERNAL_RTL = """\
module top(input clk, input rst, input a, input b, input c,
           output reg y, output reg z);
  (* keep *) wire n1;
  assign n1 = a & b;
  always @(posedge clk) begin
    if (rst) begin y <= 1'b0; z <= 1'b0; end
    else begin y <= n1 | c; z <= n1 & c; end
  end
endmodule
"""

_SEQ_GATE_RENAMED_INTERNAL_RTL = """\
module top(input clk, input rst, input a, input b, input c,
           output reg y, output reg z);
  (* keep *) wire n1;
  assign n1 = ~(a & b);
  always @(posedge clk) begin
    if (rst) begin y <= 1'b0; z <= 1'b0; end
    else begin y <= (~n1) | c; z <= (~n1) & c; end
  end
endmodule
"""

# Issue #1353 negative control: same same-named-internal-wire shape as the
# pair above, but `z` is genuinely broken (`|` where the gold has `&`). Cut-
# point refinement must NOT launder this into `"equivalent"` -- the `z`
# obligation is a top-level port, so it is never blacklisted.
_SEQ_GATE_RENAMED_INTERNAL_BROKEN_RTL = """\
module top(input clk, input rst, input a, input b, input c,
           output reg y, output reg z);
  (* keep *) wire n1;
  assign n1 = ~(a & b);
  always @(posedge clk) begin
    if (rst) begin y <= 1'b0; z <= 1'b0; end
    else begin y <= (~n1) | c; z <= (~n1) | c; end
  end
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
        *, counterexample, ports, netlist_path, output_dir, diagnostics, sim_backend
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
        *, counterexample, ports, netlist_path, output_dir, diagnostics, sim_backend
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


def test_sequential_engine_scripts_include_extra_opt_pass(tmp_path):
    """Issue #1353: both `"yosys-sequential"` script stages run a real
    `opt -noff` pass (beyond the plain dead-code-only `opt_clean` both
    engines already ran) after `flatten` and before `equiv_make`, so
    OpenROAD-inserted identity buffer/repair chains collapse before
    `equiv_make`'s name-based wire matching runs (see
    `_side_prep_lines`'s own `extra_opt` docstring for the full
    rationale). No subprocess/real `yosys` needed -- this only inspects
    the generated `.ys` script text, mirroring this module's other
    pure-generation unit tests."""
    gold = _side([_write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)])
    gate = _side([_write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)])

    stage1_path = str(tmp_path / "stage1.ys")
    equiv._write_sequential_stage1_script(
        script_path=stage1_path,
        gold=gold,
        gate=gate,
        port_map={},
        netlist_path=str(tmp_path / "netlist.v"),
        induction_depth=4,
    )
    stage1_lines = Path(stage1_path).read_text(encoding="utf-8").splitlines()
    assert stage1_lines.count("opt -noff") == 2  # once per gold/gate side
    # Must run after `flatten` (the normalization this pass strengthens)
    # and before `equiv_make` (the pass it exists to help converge).
    flatten_idx = stage1_lines.index("flatten")
    opt_idx = stage1_lines.index("opt -noff")
    equiv_make_idx = next(
        i for i, line in enumerate(stage1_lines) if line.startswith("equiv_make ")
    )
    assert flatten_idx < opt_idx < equiv_make_idx

    stage2_path = str(tmp_path / "stage2.ys")
    equiv._write_sequential_stage2_script(
        script_path=stage2_path,
        gold=gold,
        gate=gate,
        port_map={},
        ports={"clk": "input", "d": "input", "q": "output"},
        bmc_depth=4,
        sat_timeout_s=60,
    )
    stage2_lines = Path(stage2_path).read_text(encoding="utf-8").splitlines()
    assert stage2_lines.count("opt -noff") == 2

    # The combinational engine's own script is deliberately unchanged --
    # `extra_opt` defaults to `False` and only the sequential engine's
    # writers pass `extra_opt=True` (see `_write_script` -- unaffected by
    # this issue's fix, zero regression risk to its own already-shipped
    # miter/SAT recipe).
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
    assert "opt -noff" not in comb_lines


def test_sequential_stage1_script_emits_blacklist_when_given(tmp_path):
    """Issue #1353: stage 1's cut-point refinement loop re-runs the same
    script with an `equiv_make -blacklist <file>` flag; without a blacklist
    the emitted line stays byte-identical to what shipped before. No
    subprocess needed -- this inspects the generated `.ys` text only."""
    gold = _side([_write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)])
    gate = _side([_write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_BUFFERED)])

    plain_path = str(tmp_path / "plain.ys")
    equiv._write_sequential_stage1_script(
        script_path=plain_path,
        gold=gold,
        gate=gate,
        port_map={},
        netlist_path=str(tmp_path / "netlist.v"),
        induction_depth=4,
    )
    plain_lines = Path(plain_path).read_text(encoding="utf-8").splitlines()
    assert "equiv_make gold gate equiv" in plain_lines

    blacklist_path = str(tmp_path / "bl.txt")
    refined_path = str(tmp_path / "refined.ys")
    equiv._write_sequential_stage1_script(
        script_path=refined_path,
        gold=gold,
        gate=gate,
        port_map={},
        netlist_path=str(tmp_path / "netlist.v"),
        induction_depth=4,
        blacklist_path=blacklist_path,
    )
    refined_lines = Path(refined_path).read_text(encoding="utf-8").splitlines()
    assert f"equiv_make -blacklist {blacklist_path} gold gate equiv" in refined_lines
    assert "equiv_make gold gate equiv" not in refined_lines


def test_parse_unproven_equiv_signals_strips_gold_gate_suffixes():
    """Issue #1353: the refinement loop's input parser, against real
    `equiv_status` output shapes (copied verbatim from a live GCD
    pre/post-route run's own `equiv_seq_stage1.log`)."""
    stdout = "\n".join(
        [
            "24. Executing EQUIV_STATUS pass.",
            "Found 1521 $equiv cells in equiv:",
            "  Of those cells 1428 are proven and 93 are unproven.",
            "  Unproven $equiv $auto$equiv_make.cc:295:find_same_wires$10212: "
            "\\_560_.B_gold \\_560_.B_gate",
            "  Unproven $equiv $auto$equiv_make.cc:258:find_same_wires$29: "
            "\\result_gold [3] \\result_gate [3]",
            "Found a total of 93 unproven $equiv cells.",
        ]
    )

    names = equiv._parse_unproven_equiv_signals(stdout)

    # Bit-select tokens (`[3]`) carry no `_gold`/`_gate` suffix and are
    # dropped; the multi-bit port itself is still reported by its base name
    # so the caller can recognise (and refuse to blacklist) it.
    assert names == {"_560_.B", "result"}
    assert equiv._parse_unproven_equiv_signals("nothing to see here") == set()


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_refines_false_internal_cutpoints(tmp_path):
    """Issue #1353 (the bug this issue is about, in miniature): two
    genuinely equivalent designs that share an internal wire *name*
    carrying opposite polarity -- the shape OpenROAD's resizing/repair/
    cloning passes produce on a real post-route netlist.

    Before stage 1's cut-point refinement loop, `equiv_make`'s name-based
    pairing made `n1` an unprovable obligation *and* a false cut point,
    leaving even the `y`/`z` output obligations unproven (verified directly
    against Yosys: 3 unproven `$equiv` cells, 0 proven) so the engine could
    not report `"equivalent"`. With refinement, `n1` is dropped from the
    pairing and both output obligations are proven from the two designs'
    own real logic."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_RENAMED_INTERNAL_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_RENAMED_INTERNAL_RTL)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "equivalent", report["diagnostics"]
    assert report["counterexample"] is None

    # The weakened obligation set is reported, not silently applied.
    codes = [d["code"] for d in report["diagnostics"]]
    assert codes == ["equiv_cutpoint_refinement"]
    assert all(d["severity"] == "info" for d in report["diagnostics"])

    blacklist_path = report["artifacts"]["stage1_blacklist_path"]
    assert blacklist_path is not None and os.path.isfile(blacklist_path)
    blacklisted = Path(blacklist_path).read_text(encoding="utf-8").split()
    assert "n1" in blacklisted
    # Top-level ports are never dropped -- they are the obligations that
    # define equivalence.
    assert not {"clk", "rst", "a", "b", "c", "y", "z"} & set(blacklisted)


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
def test_sequential_engine_refinement_cannot_launder_broken_gate(tmp_path):
    """Issue #1353's soundness guard: the same same-named-internal-wire
    shape as the test above, but with a genuinely broken `z` output. Cut-
    point refinement drops only internal wires, never a top-level port, so
    the broken output obligation survives and the run must not report
    `"equivalent"`."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_RENAMED_INTERNAL_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_RENAMED_INTERNAL_BROKEN_RTL)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] != "equivalent"
    blacklist_path = report["artifacts"]["stage1_blacklist_path"]
    if blacklist_path is not None:
        blacklisted = Path(blacklist_path).read_text(encoding="utf-8").split()
        assert "z" not in blacklisted


# --------------------------------------------------------------------------- #
# Issue #1999: Verilog *escaped* identifiers (a leading `\`, terminated by
# whitespace rather than by the next non-identifier character) are how a real
# P&R/synthesis flow spells a port whose name contains `.`, `[`, `]` or `/`
# -- e.g. a flattened hierarchical output `\q.x` or a bit-blasted bus bit
# `\q[0]`. `write_verilog -noattr` therefore emits their declarations with a
# space before the semicolon (`  output \q.x ;`), which the stage-1 refinement
# loop's port parser must tolerate: a top-level port it fails to recognise is
# misclassified as an internal wire and *blacklisted* as a cut point, deleting
# the one proof obligation that would have caught a real difference on it.
# --------------------------------------------------------------------------- #


def _seq_escaped_port_rtl(escaped: str, *, broken: bool) -> str:
    """Issue #1999 fixture: the `_SEQ_*_RENAMED_INTERNAL_RTL` shape above
    (a same-named internal wire `n1` at opposite polarity, so cut-point
    refinement genuinely has to run) with a *second*, escaped-identifier
    output port alongside the plain `y`.

    `broken=False` is the gold side; `broken=True` is a gate whose only
    functional difference is on the escaped port (`|` where gold has `&`),
    so the escaped port carries the sole proof obligation that distinguishes
    the two designs.
    """
    n1 = "~(a & b)" if broken else "a & b"
    read_n1 = "(~n1)" if broken else "n1"
    escaped_expr = f"{read_n1} | c" if broken else "n1 & c"
    return (
        "module top(input clk, input rst, input a, input b, input c,\n"
        f"           output reg y, output reg \\{escaped} );\n"
        "  (* keep *) wire n1;\n"
        f"  assign n1 = {n1};\n"
        "  always @(posedge clk) begin\n"
        f"    if (rst) begin y <= 1'b0; \\{escaped}  <= 1'b0; end\n"
        f"    else begin y <= {read_n1} | c; \\{escaped}  <= {escaped_expr}; end\n"
        "  end\n"
        "endmodule\n"
    )


def test_parse_module_ports_matches_escaped_identifier_ports(tmp_path):
    """Issue #1999 root cause, isolated: `write_verilog -noattr` terminates
    an escaped identifier with whitespace, so an escaped port declaration
    reads `output \\q.x ;` -- a space before the `;`. `_parse_module_ports`
    must still record it (under the same `\\`-stripped spelling
    `_parse_unproven_equiv_signals` produces), or `_run_sequential` will
    mistake a top-level port for an internal wire and blacklist it."""
    netlist = tmp_path / "netlist.v"
    netlist.write_text(
        "\n".join(
            [
                "module gate(a, \\q.x );",
                "  input a;",
                "  output \\q.x ;",
                "endmodule",
                "module gold(clk, a, sum, \\q.x , \\q[0] , \\top/u1/z );",
                "  input clk;",
                "  wire clk;",
                "  input [3:0] a;",
                "  output [3:0] sum;",
                "  output \\q.x ;",
                "  reg \\q.x ;",
                "  output \\q[0] ;",
                "  wire \\q[0] ;",
                "  inout \\top/u1/z ;",
                "  wire n1;",
                "endmodule",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ports = equiv._parse_module_ports(str(netlist), "gold")

    assert ports == {
        "clk": "input",
        "a": "input",
        "sum": "output",
        "q.x": "output",
        "q[0]": "output",
        "top/u1/z": "inout",
    }
    # Plain (non-escaped) declarations are unaffected, and a non-port `wire`
    # declaration is still not a port.
    assert "n1" not in ports


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.parametrize("escaped", ["q.x", "q[0]"])
def test_sequential_refinement_never_blacklists_an_escaped_top_level_port(
    tmp_path, escaped
):
    """Issue #1999: two gate-level netlists whose only functional difference
    is on an *escaped* top-level output port must never be reported
    `"equivalent"`.

    Before the fix, `_PORT_DECL_RE` could not match `output \\q.x ;` (the
    whitespace an escaped identifier is terminated by sits between the name
    and the `;`), so the port never reached `_parse_module_ports`' result,
    stage 1's refinement loop classified it as an internal wire, and
    `equiv_make -blacklist` dropped the only obligation that distinguishes
    the two designs -- leaving the untouched `y` obligation to be proven and
    the run to report a false `"equivalent"` (verified live on Yosys 0.69)."""
    _write(tmp_path / "gold.v", _seq_escaped_port_rtl(escaped, broken=False))
    _write(tmp_path / "gate.v", _seq_escaped_port_rtl(escaped, broken=True))
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] != "equivalent", report["diagnostics"]

    blacklist_path = report["artifacts"]["stage1_blacklist_path"]
    if blacklist_path is not None:
        blacklisted = Path(blacklist_path).read_text(encoding="utf-8").split()
        # The escaped port is a top-level obligation, never a cut point --
        # but the genuinely-internal `n1` still is, so refinement did run.
        assert escaped not in blacklisted
        assert "y" not in blacklisted
        assert "n1" in blacklisted


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.parametrize("escaped", ["q.x", "q[0]"])
def test_sequential_escaped_port_counterexample_is_simulation_confirmed(
    tmp_path, escaped
):
    """Issue #1999, downstream half: once an escaped port survives port
    parsing it also flows into stage 2's `-show <name>_gold/-show
    <name>_gate` dump and into the iverilog/vvp confirmation testbench, so
    the generated testbench has to spell it as a legal Verilog escaped
    identifier. Otherwise the confirmation compile fails and a genuine
    counterexample is silently downgraded to `"inconclusive"`."""
    _write(tmp_path / "gold.v", _seq_escaped_port_rtl(escaped, broken=False))
    _write(tmp_path / "gate.v", _seq_escaped_port_rtl(escaped, broken=True))
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
        },
    )

    report = run_equiv(request_path)

    assert report["status"] == "counterexample", report["diagnostics"]
    counterexample = report["counterexample"]
    assert escaped in counterexample["diverging_outputs"]
    assert counterexample["confirmed_by_simulation"] is True
    assert set(report["counterexample"]["simulation"]["diverging_outputs"]) & {escaped}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("y", "y"),
        ("_560_", "_560_"),
        ("sum$next", "sum$next"),
        ("q.x", "\\q.x "),
        ("q[0]", "\\q[0] "),
        ("top/u1/z", "\\top/u1/z "),
        ("2fast", "\\2fast "),
    ],
)
def test_verilog_ident_escapes_only_when_required(name, expected):
    """Issue #1999: names that are already legal simple identifiers are
    emitted verbatim (so every existing generated testbench is byte-for-byte
    unchanged); anything else becomes a whitespace-terminated Verilog
    escaped identifier."""
    assert equiv._verilog_ident(name) == expected


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
    shutil.which("openroad") is None
    or os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1",
    reason="openroad is not installed on this machine",
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_sequential_engine_real_pnr_register_preserving_transformation(
    tmp_path, monkeypatch
):
    """`klt synthesize`'s pre-P&R netlist vs. `klt place-and-route`'s real,
    post-route `verilog_path` (issue #996) on the GCD worked example.

    Carried an `xfail` marker from issue #1323 until issue #1353: the
    50-flop post-route netlist left 93 `$equiv` cells unproven (98 before
    this engine's `opt -noff` normalization pass), all of them same-named
    *internal* wires OpenROAD's resizing/repair/cloning legitimately
    repurposed, so the engine reported `"inconclusive"`. #1353's stage-1
    cut-point refinement loop drops exactly those wrongly-paired internal
    wires (never a top-level port) and the run converges to `"equivalent"`
    in a single refinement pass -- verified live 2026-08-24 against
    `openroad` 26Q3-1510-g6cb3f2b704 (`openroad/orfs:latest`), a real
    pinned volare sky130A install, and this repo's pinned Yosys
    `0.67+post`, so the marker is gone and this is now a genuine pass."""
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
            "netlist": _synth_netlist_path(
                synth_request, "gcd", synth_report["run_id"]
            ),
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
                [_synth_netlist_path(synth_request, "gcd", synth_report["run_id"])],
                top="gcd",
                liberty=liberty_path,
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
@pytest.mark.skipif(
    shutil.which("openroad") is None
    or os.environ.get("KLT_SKIP_OPENROAD_TESTS") == "1",
    reason="openroad is not installed on this machine",
)
@pytest.mark.skipif(
    _REAL_SKY130_PNR_VARIANT is None,
    reason="no real sky130_fd_sc_hd LEF/liberty/GDS set resolves via list_pdks()",
)
def test_sequential_engine_real_pnr_mult8_register_preserving_transformation(
    tmp_path, monkeypatch
):
    """Issue #1353's acceptance criterion 3: run the engine against a
    *second* `tests/corpus/place_and_route` design, not just GCD --
    `mult8` (`tests/corpus/statime/mult8.v`), the same source
    `tests/corpus/place_and_route/regenerate.sh` uses for its own `mult8`
    fixture.

    **What this test does and does not establish, stated plainly.** `mult8`
    is purely combinational (no clock, no registers -- "No handshake, no
    clock: `p` is a pure combinational function of `a`/`b`" per `mult8.v`'s
    own header comment; `regenerate.sh` names an *output* port `p[15]` as
    `DESIGN_CLOCK_PORT` purely to satisfy `klt place-and-route`'s
    `constraints.clock_port` requirement, not because the design has real
    sequential state). Measured live on 2026-08-24, OpenROAD's route-stage
    output for this design is *instance-for-instance identical* to the
    pre-route netlist (same 260 cells, same instance names) -- so this pair
    already converged before #1353 and does not itself exercise the
    cut-point refinement loop. It is kept as a genuine second-design
    regression guard: it proves the whole `klt synthesize` ->
    `klt place-and-route` -> `klt equiv --engine yosys-sequential` path
    still reports `"equivalent"` on a second real, independently-P&R'd
    corpus design, and would catch a refinement change that broke the
    already-converging case. The design that actually exercises the fix is
    the GCD canary above."""
    root, variant = _REAL_SKY130_PNR_VARIANT
    cell_library, corner = "sky130_fd_sc_hd", "tt_025C_1v80"
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.setenv("PDK", variant)

    from klayout_tools.place_and_route import run_place_and_route

    mult8_rtl_path = (
        Path(__file__).resolve().parents[1] / "tests" / "corpus" / "statime" / "mult8.v"
    )
    assert mult8_rtl_path.is_file(), f"tracked mult8 RTL missing: {mult8_rtl_path}"

    synth_dir = tmp_path / "synth"
    synth_dir.mkdir()
    _write(synth_dir / "mult8.v", mult8_rtl_path.read_text(encoding="utf-8"))
    synth_request = _write_request(
        synth_dir / "synth.json",
        {
            "sources": ["mult8.v"],
            "hdl_toplevel": "mult8",
            "pdk": {"cell_library": cell_library, "corner": corner},
        },
    )
    synth_report = run_synthesize(synth_request)
    assert synth_report["status"] == "ok"

    pnr_dir = tmp_path / "pnr"
    pnr_dir.mkdir()
    # Mirrors `tests/corpus/place_and_route/regenerate.sh`'s own `mult8`
    # request shape (`DESIGN_CLOCK_PORT[mult8]="p[15]"`,
    # `DESIGN_CLOCK_PERIOD_NS[mult8]="6.0"`).
    pnr_request = _write_request(
        pnr_dir / "pnr.json",
        {
            "engine": "openroad",
            "netlist": _synth_netlist_path(
                synth_request, "mult8", synth_report["run_id"]
            ),
            "hdl_toplevel": "mult8",
            "pdk": {"cell_library": cell_library, "corner": corner},
            "floorplan": {
                "method": "utilization",
                "utilization_pct": 38,
                "aspect_ratio": 1.0,
                "core_margin_um": 2.0,
                "site": "unithd",
            },
            "io": {"layer_h": "met3", "layer_v": "met2"},
            "constraints": {"clock_port": "p[15]", "clock_period_ns": 6.0},
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
                [_synth_netlist_path(synth_request, "mult8", synth_report["run_id"])],
                top="mult8",
                liberty=liberty_path,
            ),
            "gate": _side(
                [pnr_report["verilog_path"]], top="mult8", liberty=liberty_path
            ),
            "engine": "yosys-sequential",
            "timeout_s": 300,
        },
    )
    equiv_report = run_equiv(equiv_request)

    # Evidence for issue #1353's own acceptance criterion 3: print the full
    # report (status/artifacts/diagnostics) so a `-s` run captures it
    # verbatim for the PR's evidence trail.
    print(
        "\n=== issue #1353 real pre/post-route mult8 equiv_report ===\n"
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
                [
                    _synth_netlist_path(
                        good_synth_request, "adder4", good_synth_report["run_id"]
                    )
                ],
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
                [
                    _synth_netlist_path(
                        bad_synth_request, "adder4", bad_synth_report["run_id"]
                    )
                ],
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


# --------------------------------------------------------------------------- #
# Issue #2223: optional Verilator fast-path replay backend
# --------------------------------------------------------------------------- #
#
# `request.sim_backend` / `--sim-backend` selects which simulator *replays*
# a counterexample (an axis orthogonal to `request.engine`, which selects
# the proof engine). Three tiers here, mirroring this module's own
# structure:
#
# - Pure unit tests for the backend-pairing and agreement helpers (no
#   subprocess at all).
# - Deterministic full-pipeline tests with `subprocess.run` mocked, so the
#   agreement policy -- including the *negative control*, an injected
#   backend disagreement -- runs in any CI environment, with or without a
#   real `verilator` install.
# - Real-toolchain integration tests (skipif) proving the two backends
#   actually agree, byte for byte, on a real counterexample replay.

HAVE_VERILATOR = shutil.which("verilator") is not None

#: The replay stdout a diverging (`gold != gate`) single-vector run emits --
#: exactly the shape `_SAT_FAIL_TEXT`'s own counterexample testbench
#: produces.
_REPLAY_DIVERGES = "EQUIV_SIM gold y 0\nEQUIV_SIM gate y 1\n"

#: The same replay, but *not* diverging -- the "counterexample did not
#: reproduce" outcome.
_REPLAY_AGREES = "EQUIV_SIM gold y 0\nEQUIV_SIM gate y 0\n"

#: A diverging replay whose `gold` value is undefined on one bit -- what a
#: 4-state engine (Icarus) reports where a 2-state engine (Verilator)
#: reports a defined `0`.
_REPLAY_DIVERGES_4STATE = "EQUIV_SIM gold y x\nEQUIV_SIM gate y 1\n"
_REPLAY_DIVERGES_2STATE = "EQUIV_SIM gold y 0\nEQUIV_SIM gate y 1\n"


def _mock_replay_run(
    *,
    sat_stdout: str = _SAT_FAIL_TEXT,
    iverilog_stdout: str = _REPLAY_DIVERGES,
    verilator_stdout: str = _REPLAY_DIVERGES,
    verilator_missing: bool = False,
):
    """A `subprocess.run` stand-in answering *every* call `run_equiv` makes
    on the counterexample path -- the yosys proof and version probe, plus
    each replay backend's compile/run/version-probe calls -- from canned
    data, so the backend-agreement policy is exercised deterministically
    with no real `yosys`/`iverilog`/`verilator` install.

    `verilator_missing=True` reproduces a machine with no `verilator` on
    `$PATH` (a `FileNotFoundError` from every `verilator` invocation), which
    is what the "behavior is exactly today's when Verilator is absent"
    regression tests below assert against.
    """

    # `(argv[0], argv[1])` -> canned result, so one lookup answers every
    # call instead of a branch per tool (keeps this helper under the
    # repository's own max-complexity gate).
    replies = {
        ("yosys", "-s"): fake_completed(stdout=sat_stdout, returncode=0),
        ("yosys", "-V"): fake_completed(stdout="Yosys 0.67 (git sha1 deadbeef)\n"),
        ("iverilog", "-V"): fake_completed(
            stdout="Icarus Verilog version 12.0 (stable)\n"
        ),
        ("iverilog", "-g2012"): fake_completed(returncode=0),
        ("verilator", "--version"): fake_completed(
            stdout="Verilator 5.050 2025-01-01 rev v5.050\n"
        ),
        ("verilator", "--binary"): fake_completed(returncode=0),
    }

    def _run(cmd, **kwargs):
        if cmd[0] == "verilator" and verilator_missing:
            raise FileNotFoundError("verilator: not found")
        if cmd[0] == "vvp":
            return fake_completed(stdout=iverilog_stdout)
        if cmd[0].endswith("_verilator_bin"):
            return fake_completed(stdout=verilator_stdout)
        reply = replies.get(tuple(cmd[:2]))
        if reply is None:
            raise AssertionError(f"unexpected subprocess.run call in mock: {cmd!r}")
        return reply

    return _run


def _broken_pair_request(tmp_path) -> str:
    """A minimal seeded-broken request (AND vs. OR) whose proof the mocked
    `subprocess.run` above answers with `_SAT_FAIL_TEXT`."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    return _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )


# --- pure unit tests (no subprocess) --------------------------------------- #


def test_supported_sim_backends_and_default():
    """`iverilog` stays the default *and* the canonical backend for
    evidence -- Verilator is additive, never a replacement."""
    assert equiv.SUPPORTED_SIM_BACKENDS == ("iverilog", "verilator", "both")
    assert equiv.DEFAULT_SIM_BACKEND == "iverilog"
    assert equiv.CANONICAL_SIM_BACKEND == "iverilog"


@pytest.mark.parametrize(
    ("sim_backend", "expected"),
    [
        ("iverilog", ("iverilog", None)),
        ("verilator", ("verilator", None)),
        ("both", ("iverilog", "verilator")),
    ],
)
def test_replay_backends_pairing(sim_backend, expected):
    assert equiv._replay_backends(sim_backend) == expected


def test_unsupported_sim_backend_is_error(tmp_path):
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "sim_backend": "modelsim",
        },
    )
    with pytest.raises(EquivError, match="unsupported sim_backend 'modelsim'"):
        run_equiv(request_path)


@pytest.mark.parametrize(
    ("canonical", "cross", "explained"),
    [
        # Differs only where the 4-state run is undefined -> explained by
        # the declared 2-state/4-state modelling gap.
        ("x", "0", True),
        ("x", "1", True),
        ("1x0", "110", True),
        ("z", "0", True),
        # A differing *defined* bit is a real disagreement, never explained.
        ("0", "1", False),
        ("110", "100", False),
        # Differing widths / a signal one backend never reported.
        ("10", "0", False),
        (None, "0", False),
        ("0", None, False),
    ],
)
def test_explains_two_state(canonical, cross, explained):
    assert equiv._explains_two_state(canonical, cross) is explained


def test_compare_replay_outputs_classifies_each_difference():
    mismatches = equiv._compare_replay_outputs(
        canonical={"gold": {"q": "x", "y": "1"}, "gate": {"q": "0", "y": "1"}},
        cross={"gold": {"q": "0", "y": "0"}, "gate": {"q": "0", "y": "1"}},
        cross_four_state=False,
    )
    assert mismatches == [
        {
            "side": "gold",
            "name": "q",
            "canonical": "x",
            "cross_check": "0",
            "explained_by": "two_state_backend",
        },
        {
            "side": "gold",
            "name": "y",
            "canonical": "1",
            "cross_check": "0",
            "explained_by": None,
        },
    ]


def test_compare_replay_outputs_never_explains_for_a_four_state_cross_check():
    """The `explained_by: "two_state_backend"` escape hatch is keyed on the
    cross-check backend's *declared* value modelling -- it is never applied
    to a 4-state backend, which has no such excuse."""
    mismatches = equiv._compare_replay_outputs(
        canonical={"gold": {"q": "x"}, "gate": {}},
        cross={"gold": {"q": "0"}, "gate": {}},
        cross_four_state=True,
    )
    assert [entry["explained_by"] for entry in mismatches] == [None]


# --- deterministic full-pipeline tests (mocked subprocess) ------------------ #


def test_default_sim_backend_is_echoed_and_runs_iverilog_only(tmp_path, monkeypatch):
    """Omitting `sim_backend` reproduces the pre-#2223 behavior exactly: the
    canonical `iverilog` replay, no cross-check, no Verilator invocation of
    any kind (the mock raises `AssertionError` on an unexpected call)."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_replay_run(verilator_missing=True)
    )

    report = run_equiv(request_path)

    assert report["sim_backend"] == "iverilog"
    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    assert counterexample["simulation"]["engine"] == "icarus"
    assert counterexample["simulation"]["four_state"] is True
    assert counterexample["simulation_cross_check"] is None
    assert report["diagnostics"] == []


def test_both_backends_agreeing_confirms_the_counterexample(tmp_path, monkeypatch):
    """Acceptance criterion 1: with both backends reaching the same verdict
    on the same replayed bytes, the verdict stands and the agreement is
    reported explicitly."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(equiv.subprocess, "run", _mock_replay_run())

    report = run_equiv(request_path, sim_backend="both")

    assert report["sim_backend"] == "both"
    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    cross_check = counterexample["simulation_cross_check"]
    assert cross_check["engine"] == "verilator"
    assert cross_check["engine_version"] == "5.050"
    assert cross_check["four_state"] is False
    assert cross_check["agreement"] == "agree"
    assert cross_check["confirmed_by_simulation"] is True
    assert cross_check["output_mismatches"] == []
    # Byte-for-byte identical replayed values, not merely the same verdict.
    assert cross_check["gold_outputs"] == counterexample["simulation"]["gold_outputs"]
    assert cross_check["gate_outputs"] == counterexample["simulation"]["gate_outputs"]
    assert report["diagnostics"] == []


def test_backend_disagreement_is_an_explicit_error_never_a_pass(tmp_path, monkeypatch):
    """Acceptance criterion 2 (negative control): an injected backend
    disagreement -- `iverilog` reproduces the divergence, `verilator` does
    not -- is surfaced as an error-severity diagnostic and an
    `"inconclusive"` verdict. Neither backend is silently preferred, so
    `confirmed_by_simulation` is reset to `None` rather than adopting the
    canonical run's own `True`."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_replay_run(
            iverilog_stdout=_REPLAY_DIVERGES,
            verilator_stdout=_REPLAY_AGREES,
        ),
    )

    report = run_equiv(request_path, sim_backend="both")

    assert report["status"] == "inconclusive"
    assert report["status"] != "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is None
    cross_check = counterexample["simulation_cross_check"]
    assert cross_check["agreement"] == "disagree"
    assert cross_check["confirmed_by_simulation"] is False
    # Neither backend's raw evidence is discarded -- both runs are still
    # reported in full, so the disagreement is auditable.
    assert counterexample["simulation"]["gate_outputs"] == {"y": "1"}
    assert cross_check["gate_outputs"] == {"y": "0"}

    disagreements = [
        diag
        for diag in report["diagnostics"]
        if diag["code"] == "sim_backend_disagreement"
    ]
    assert len(disagreements) == 1
    assert disagreements[0]["severity"] == "error"
    assert "iverilog" in disagreements[0]["message"]
    assert "verilator" in disagreements[0]["message"]


def test_backend_disagreement_exits_four_never_zero(tmp_path, monkeypatch, capsys):
    """The same negative control through the CLI: a backend disagreement
    never exits `0` (and never exits `3`, the "proven non-equivalent"
    code) -- it takes the `4`/"ran, but the result isn't trustworthy" exit
    every other untrustworthy outcome takes."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_replay_run(verilator_stdout=_REPLAY_AGREES),
    )

    exit_code = main(
        ["equiv", request_path, "--sim-backend", "both", "--format", "json"]
    )

    assert exit_code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "inconclusive"
    assert out["sim_backend"] == "both"


def test_two_state_only_difference_is_explained_not_a_disagreement(
    tmp_path, monkeypatch
):
    """A bit that differs *only* where the 4-state canonical run reported
    `x` is the one declared backend-specific difference between the two
    engines -- explained by `explained_by: "two_state_backend"`, reported as
    an `info` diagnostic, and never escalated into a disagreement."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess,
        "run",
        _mock_replay_run(
            iverilog_stdout=_REPLAY_DIVERGES_4STATE,
            verilator_stdout=_REPLAY_DIVERGES_2STATE,
        ),
    )

    report = run_equiv(request_path, sim_backend="both")

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    cross_check = counterexample["simulation_cross_check"]
    assert cross_check["agreement"] == "agree"
    assert [entry["explained_by"] for entry in cross_check["output_mismatches"]] == [
        "two_state_backend"
    ]
    codes = [diag["code"] for diag in report["diagnostics"]]
    assert codes == ["sim_backend_output_difference"]
    assert "sim_backend_disagreement" not in codes


def test_verilator_absent_leaves_canonical_evidence_byte_identical(
    tmp_path, monkeypatch
):
    """Acceptance criterion 3: with no `verilator` on `$PATH`, asking for
    `"both"` produces exactly the canonical `iverilog` run's own evidence --
    same verdict, same replayed bytes, same `status` -- plus an explicit
    "the cross-check could not run" record. Absence is reported, never
    fabricated and never silently swapped for the canonical backend's own
    numbers."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_replay_run(verilator_missing=True)
    )
    canonical_report = run_equiv(request_path, sim_backend="iverilog")

    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_replay_run(verilator_missing=True)
    )
    both_report = run_equiv(request_path, sim_backend="both")

    assert both_report["status"] == canonical_report["status"] == "counterexample"
    canonical_ce = canonical_report["counterexample"]
    both_ce = both_report["counterexample"]
    assert both_ce["confirmed_by_simulation"] is True
    assert both_ce["simulation"] == canonical_ce["simulation"]
    # Everything except the two additive cross-check surfaces is unchanged.
    assert {
        key: value for key, value in both_ce.items() if key != "simulation_cross_check"
    } == {
        key: value
        for key, value in canonical_ce.items()
        if key != "simulation_cross_check"
    }

    cross_check = both_ce["simulation_cross_check"]
    assert cross_check["agreement"] == "unavailable"
    assert cross_check["confirmed_by_simulation"] is None
    assert cross_check["gold_outputs"] is None
    assert cross_check["gate_outputs"] is None
    codes = [diag["code"] for diag in both_report["diagnostics"]]
    assert codes == ["sim_backend_cross_check_unavailable"]
    assert canonical_report["diagnostics"] == []


def test_verilator_backend_absent_never_falls_back_to_iverilog(tmp_path, monkeypatch):
    """`sim_backend: "verilator"` with no `verilator` installed degrades to
    the same "could not confirm" outcome a missing `iverilog` already
    produces -- it must never quietly run `iverilog` instead and report the
    result as Verilator's."""
    request_path = _broken_pair_request(tmp_path)
    monkeypatch.setattr(
        equiv.subprocess, "run", _mock_replay_run(verilator_missing=True)
    )

    report = run_equiv(request_path, sim_backend="verilator")

    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is None
    assert counterexample["simulation"] is None
    assert counterexample["simulation_cross_check"] is None
    diagnostic = report["diagnostics"][0]
    assert diagnostic["code"] == "simulation_unavailable"
    assert diagnostic["message"].startswith("verilator not found on $PATH")
    # The solver's own verdict stands -- no simulation evidence either way.
    assert report["status"] == "counterexample"


def test_cli_sim_backend_flag_overrides_the_request_field(tmp_path, monkeypatch):
    """`--sim-backend` overrides `request.sim_backend`, the same
    override-the-request-field precedence `--timeout-s` already has."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "sim_backend": "verilator",
        },
    )
    monkeypatch.setattr(equiv.subprocess, "run", _mock_replay_run())

    from_request = run_equiv(request_path)
    assert from_request["sim_backend"] == "verilator"
    assert from_request["counterexample"]["simulation"]["engine"] == "verilator"

    monkeypatch.setattr(equiv.subprocess, "run", _mock_replay_run())
    overridden = run_equiv(request_path, sim_backend="iverilog")
    assert overridden["sim_backend"] == "iverilog"
    assert overridden["counterexample"]["simulation"]["engine"] == "icarus"


def test_sequential_replay_backend_disagreement_is_flagged(tmp_path, monkeypatch):
    """The multi-cycle replay path applies the identical agreement policy:
    a cross-check backend that does not reproduce the reported divergence
    anywhere in the trace is a disagreement, so the caller downgrades the
    verdict."""
    counterexample = {
        "cycles": [],
        "diverging_outputs": ["q"],
        "first_diverging_cycle": 2,
        "confirmed_by_simulation": None,
        "simulation": None,
        "simulation_cross_check": None,
    }
    replies = {
        "iverilog": "EQUIV_SIM_CYCLE 2 gold q 1\nEQUIV_SIM_CYCLE 2 gate q 0\n",
        "verilator": "EQUIV_SIM_CYCLE 2 gold q 0\nEQUIV_SIM_CYCLE 2 gate q 0\n",
    }

    def _fake_replay(*, backend, netlist_path, tb_path, output_dir, stem):
        return (replies[backend], None)

    monkeypatch.setattr(equiv, "_run_replay_backend", _fake_replay)
    monkeypatch.setattr(equiv, "_sim_backend_version", lambda backend: "0.0")

    diagnostics: list[dict] = []
    equiv._confirm_sequential_counterexample(
        counterexample=counterexample,
        ports={"q": "output"},
        netlist_path=str(tmp_path / "netlist.v"),
        output_dir=str(tmp_path),
        diagnostics=diagnostics,
        sim_backend="both",
    )

    assert counterexample["confirmed_by_simulation"] is None
    assert equiv._replay_backends_disagreed(counterexample) is True
    assert counterexample["simulation_cross_check"]["agreement"] == "disagree"
    assert [diag["code"] for diag in diagnostics] == ["sim_backend_disagreement"]
    assert diagnostics[0]["severity"] == "error"


def test_sequential_replay_two_state_start_state_is_explained(tmp_path, monkeypatch):
    """The sequential replay's one *expected* cross-backend difference: a
    real 4-state simulation starts its registers at `x`, a 2-state one at
    `0`. Same verdict, explained bits, no disagreement."""
    counterexample = {
        "cycles": [],
        "diverging_outputs": ["q"],
        "first_diverging_cycle": 2,
        "confirmed_by_simulation": None,
        "simulation": None,
        "simulation_cross_check": None,
    }
    replies = {
        "iverilog": (
            "EQUIV_SIM_CYCLE 1 gold q x\nEQUIV_SIM_CYCLE 1 gate q x\n"
            "EQUIV_SIM_CYCLE 2 gold q 1\nEQUIV_SIM_CYCLE 2 gate q 0\n"
        ),
        "verilator": (
            "EQUIV_SIM_CYCLE 1 gold q 0\nEQUIV_SIM_CYCLE 1 gate q 0\n"
            "EQUIV_SIM_CYCLE 2 gold q 1\nEQUIV_SIM_CYCLE 2 gate q 0\n"
        ),
    }

    def _fake_replay(*, backend, netlist_path, tb_path, output_dir, stem):
        return (replies[backend], None)

    monkeypatch.setattr(equiv, "_run_replay_backend", _fake_replay)
    monkeypatch.setattr(equiv, "_sim_backend_version", lambda backend: "0.0")

    diagnostics: list[dict] = []
    equiv._confirm_sequential_counterexample(
        counterexample=counterexample,
        ports={"q": "output"},
        netlist_path=str(tmp_path / "netlist.v"),
        output_dir=str(tmp_path),
        diagnostics=diagnostics,
        sim_backend="both",
    )

    assert counterexample["confirmed_by_simulation"] is True
    cross_check = counterexample["simulation_cross_check"]
    assert cross_check["agreement"] == "agree"
    assert equiv._replay_backends_disagreed(counterexample) is False
    assert {entry["cycle"] for entry in cross_check["output_mismatches"]} == {1}
    assert all(
        entry["explained_by"] == "two_state_backend"
        for entry in cross_check["output_mismatches"]
    )


# --- real-toolchain integration (skipif) ----------------------------------- #


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.skipif(
    not HAVE_VERILATOR, reason="verilator is not installed on this machine"
)
def test_real_backends_agree_on_a_real_counterexample_replay(tmp_path):
    """Acceptance criterion 1, against the real toolchain: the same
    counterexample replayed through a real `iverilog`/`vvp` and a real
    `verilator --binary` reaches the same verdict on byte-identical
    replayed values."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    report = run_equiv(request_path, sim_backend="both")

    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    simulation = counterexample["simulation"]
    cross_check = counterexample["simulation_cross_check"]
    assert simulation["engine"] == "icarus"
    assert cross_check["engine"] == "verilator"
    assert cross_check["engine_version"] is not None
    assert cross_check["agreement"] == "agree"
    assert cross_check["output_mismatches"] == []
    assert cross_check["gold_outputs"] == simulation["gold_outputs"]
    assert cross_check["gate_outputs"] == simulation["gate_outputs"]
    assert cross_check["diverging_outputs"] == simulation["diverging_outputs"] == ["y"]
    assert not [
        diag
        for diag in report["diagnostics"]
        if diag["code"] == "sim_backend_disagreement"
    ]


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.skipif(
    not HAVE_VERILATOR, reason="verilator is not installed on this machine"
)
def test_real_verilator_only_backend_matches_the_iverilog_backend(tmp_path):
    """Running the replay under `verilator` alone reaches the same verdict
    and the same replayed bytes as running it under `iverilog` alone -- the
    property that makes the fast path substitutable at all."""
    _write(tmp_path / "gold.v", _GOLD_AND)
    _write(tmp_path / "gate.v", _GATE_OR_BROKEN)
    request_path = _write_request(
        tmp_path / "r.json",
        {"gold": _side(["gold.v"]), "gate": _side(["gate.v"])},
    )

    icarus = run_equiv(request_path, sim_backend="iverilog")["counterexample"]
    verilator = run_equiv(request_path, sim_backend="verilator")["counterexample"]

    assert icarus["confirmed_by_simulation"] == verilator["confirmed_by_simulation"]
    assert verilator["simulation"]["engine"] == "verilator"
    assert verilator["simulation"]["four_state"] is False
    for field in ("gold_outputs", "gate_outputs", "diverging_outputs"):
        assert verilator["simulation"][field] == icarus["simulation"][field]


@pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
)
@pytest.mark.skipif(
    not HAVE_VERILATOR, reason="verilator is not installed on this machine"
)
def test_real_sequential_backends_agree_with_explained_start_state_difference(
    tmp_path,
):
    """The real multi-cycle replay under both backends: same verdict, and
    every replayed-bit difference confined to the declared 2-state/4-state
    start-state gap (`explained_by: "two_state_backend"`) -- never an
    unexplained disagreement."""
    _write(tmp_path / "gold.v", _SEQ_GOLD_SIMPLE_DFF_RTL)
    _write(tmp_path / "gate.v", _SEQ_GATE_SIMPLE_DFF_RTL_INVERTED)
    request_path = _write_request(
        tmp_path / "r.json",
        {
            "gold": _side(["gold.v"]),
            "gate": _side(["gate.v"]),
            "engine": "yosys-sequential",
            "sim_backend": "both",
        },
    )

    report = run_equiv(request_path)

    assert report["sim_backend"] == "both"
    assert report["status"] == "counterexample"
    counterexample = report["counterexample"]
    assert counterexample["confirmed_by_simulation"] is True
    cross_check = counterexample["simulation_cross_check"]
    assert cross_check["engine"] == "verilator"
    assert cross_check["agreement"] == "agree"
    assert all(
        entry["explained_by"] == "two_state_backend"
        for entry in cross_check["output_mismatches"]
    )
    assert not [
        diag
        for diag in report["diagnostics"]
        if diag["code"] == "sim_backend_disagreement"
    ]
