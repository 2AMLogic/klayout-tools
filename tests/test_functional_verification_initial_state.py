"""Tests for `options.initial_state` of `klt functional-verification`
(issue #2998): option validation, generated shim, the stubbed multi-seed run,
and (when `iverilog` is installed) the real force-and-clock drive.

Hermetic: the cell models are a tiny sky130-shaped dfrtp with a real UDP.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers.cocotb_fakes import FakeCocotbRunner  # noqa: E402
from klayout_tools import functional_verification as fv  # noqa: E402
from klayout_tools.functional_verification import (  # noqa: E402
    FunctionalVerificationError,
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from klayout_tools.functional_verification_initial_state import (  # noqa: E402
    build_initial_state_verilog,
)
from klayout_tools.reset_coverage import sequential_elements  # noqa: E402

MODELS = """\
primitive sky130_fd_sc_hd__udp_dff$PR_pp$PG$N (Q, D, CLK, RESET, NOTIFIER, VPWR, VGND);
  output Q; reg Q; input D, CLK, RESET, NOTIFIER, VPWR, VGND;
  table
    ? ? 1 ? ? ? : ? : 0;
    0 r 0 ? ? ? : ? : 0;
    1 r 0 ? ? ? : ? : 1;
    x r 0 ? ? ? : ? : x;
    ? n 0 ? ? ? : ? : -;
    * ? 0 ? ? ? : ? : -;
  endtable
endprimitive
module sky130_fd_sc_hd__dfrtp_1 (Q, CLK, D, RESET_B, VPWR, VGND, VPB, VNB);
  output Q; input CLK, D, RESET_B, VPWR, VGND, VPB, VNB;
  wire buf_Q, RESET;
  not not0 (RESET, RESET_B);
  sky130_fd_sc_hd__udp_dff$PR_pp$PG$N dff0 (buf_Q, D, CLK, RESET, , VPWR, VGND);
  buf buf0 (Q, buf_Q);
endmodule
"""

NETLIST = """\
module top (clk, rst_n, d0, d1, d2, d3, q0, q1, q2, q3);
  input clk, rst_n, d0, d1, d2, d3; output q0, q1, q2, q3;
  sky130_fd_sc_hd__dfrtp_1 f0 (.CLK(clk), .D(d0), .RESET_B(rst_n), .Q(q0));
  sky130_fd_sc_hd__dfrtp_1 f1 (.CLK(clk), .D(d1), .RESET_B(rst_n), .Q(q1));
  sky130_fd_sc_hd__dfrtp_1 \\u/f2[0]  (.CLK(clk), .D(d2), .RESET_B(rst_n), .Q(q2));
  sky130_fd_sc_hd__dfrtp_1 f3 (.CLK(clk), .D(d3), .RESET_B(rst_n), .Q(q3));
endmodule
"""

TB = """\
module tb;
  reg clk, rst_n, d0, d1, d2, d3;
  wire q0, q1, q2, q3;
  top dut (clk, rst_n, d0, d1, d2, d3, q0, q1, q2, q3);
  // cocotb-like: undriven until time 1, clock starts high, reset off.
  initial begin
    #1 clk = 1; rst_n = 1; d0 = 0; d1 = 0; d2 = 0; d3 = 0;
    forever #5 clk = ~clk;
  end
  // Sampled between the capturing cycle's end (t=16) and the next edge.
  initial begin #17; $display("Q=%b%b%b%b", q0, q1, q2, q3); $finish; end
endmodule
"""

RESULTS_XML = """\
<testsuites name="results"><testsuite name="all" package="all">
<property name="random_seed" value="7" />
<testcase name="test_a" classname="test_tb" time="0.1" sim_time_ns="10.0" />
</testsuite></testsuites>
"""


def _stage(tmp_path: Path) -> None:
    (tmp_path / "models.v").write_text(MODELS, encoding="utf-8")
    (tmp_path / "net.v").write_text(NETLIST, encoding="utf-8")
    (tmp_path / "test_tb.py").write_text("# stub\n", encoding="utf-8")


def _request(tmp_path: Path, initial_state, **options) -> str:
    request = {
        "engine": "icarus",
        "sources": ["models.v", "net.v"],
        "hdl_toplevel": "top",
        "testbench": {"module": "test_tb"},
        "options": {"initial_state": initial_state, **options},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _state(**kw):
    return {"mode": "random", "seeds": [1], "netlist": "net.v", **kw}


@pytest.mark.parametrize(
    "initial_state, match",
    [
        ("x", "must be a JSON object"),
        ({"mode": "zero", "netlist": "net.v"}, "mode must be one of"),
        ({"mode": "random", "netlist": "net.v"}, "seeds must be a non-empty"),
        ({"mode": "random", "seeds": [], "netlist": "net.v"}, "seeds must be"),
        ({"mode": "random", "seeds": [True], "netlist": "net.v"}, "seeds must be"),
        ({"mode": "random", "seeds": [1.5], "netlist": "net.v"}, "seeds must be"),
        ({"mode": "random", "seeds": [2, 2], "netlist": "net.v"}, "must not repeat"),
        ({"mode": "x", "seeds": [1], "netlist": "net.v"}, "only valid with mode"),
        ({"mode": "x"}, "netlist must be a non-empty path"),
        ({"mode": "x", "netlist": "net.v", "bogus": 1}, "unknown key"),
        ({"mode": "x", "netlist": "missing.v"}, "not found"),
    ],
)
def test_bad_shapes_are_request_errors_naming_the_key(tmp_path, initial_state, match):
    _stage(tmp_path)
    with pytest.raises(FunctionalVerificationError, match=match) as info:
        run_functional_verification(_request(tmp_path, initial_state))
    assert "initial_state" in str(info.value)


def test_verilator_is_a_request_error(tmp_path):
    _stage(tmp_path)
    request_path = _request(tmp_path, _state())
    doc = json.loads(Path(request_path).read_text())
    doc["engine"] = "verilator"
    Path(request_path).write_text(json.dumps(doc))
    with pytest.raises(FunctionalVerificationError, match="requires engine 'icarus'"):
        run_functional_verification(request_path)


def test_mutations_combination_is_rejected(tmp_path):
    _stage(tmp_path)
    proposals = tmp_path / "p.json"
    proposals.write_text("{}")
    with pytest.raises(FunctionalVerificationError):
        run_functional_verification_with_mutations(
            _request(tmp_path, _state()), str(proposals)
        )


def test_sequential_elements_derive_pins_from_the_models(tmp_path):
    _stage(tmp_path)
    drivable, unsupported = sequential_elements(
        str(tmp_path / "net.v"), [str(tmp_path / "models.v")], "top"
    )
    assert unsupported == []
    assert [e["instance"] for e in drivable] == ["f0", "f1", "u/f2[0]", "f3"]
    first = drivable[0]
    assert (first["d_pin"], first["d_net"]) == ("D", "d0")
    assert (first["capture_pin"], first["capture_net"]) == ("CLK", "clk")


def test_generated_shim_x_and_random_and_escaped_names(tmp_path):
    _stage(tmp_path)
    elements, _ = sequential_elements(
        str(tmp_path / "net.v"), [str(tmp_path / "models.v")], "top"
    )
    kwargs = {"elements": elements, "scope": "top", "clock": "clk"}
    x_text = build_initial_state_verilog(mode="x", **kwargs)
    assert "force top.f0.D = 1'bx;" in x_text
    assert "klt_init_seed" not in x_text
    random_text = build_initial_state_verilog(mode="random", **kwargs)
    assert '$value$plusargs("klt_init_seed=%d"' in random_text
    assert "force top.f0.D = klt_bit_0;" in random_text
    assert "force top.\\u/f2[0] .D = klt_bit_2;" in random_text
    # Only D is forced; the clock and reset are never touched, and the window
    # is the first full real clock cycle.
    assert ".CLK" not in random_text and "RESET_B" not in random_text
    assert random_text.index("@(negedge top.clk);") < random_text.index(
        "@(posedge top.clk);"
    )
    assert random_text.index("@(posedge top.clk);") < random_text.index(
        "release top.f0.D;"
    )


def test_shared_d_net_shares_one_draw():
    elements = [
        {
            "instance": f"f{i}",
            "d_pin": "D",
            "d_net": "d" if i < 2 else f"d{i}",
            "capture_pin": "CLK",
            "capture_net": "clk",
        }
        for i in range(3)
    ]
    text = build_initial_state_verilog(
        mode="random", elements=elements, scope="top", clock="clk"
    )
    assert text.count("$random(klt_seed)") == 2
    assert "force top.f1.D = klt_bit_0;" in text


def test_clock_is_inferred_when_unique_and_required_otherwise(tmp_path):
    base = {
        "capture_pin": "CLK",
        "d_pin": "D",
        "d_net": "d",
        "instance": "f",
        "cell": "c",
    }
    one = [{**base, "capture_net": "clk"}, {**base, "capture_net": "clk"}]
    assert fv._choose_initial_state_clock(one, None) == "clk"
    assert fv._choose_initial_state_clock(one, "core_clk") == "core_clk"
    two = [{**base, "capture_net": "clk_a"}, {**base, "capture_net": "clk_b"}]
    with pytest.raises(FunctionalVerificationError, match="initial_state.clock"):
        fv._choose_initial_state_clock(two, None)
    assert fv._choose_initial_state_clock(two, "clk_a") == "clk_a"


def test_bad_clock_key_is_a_request_error(tmp_path):
    _stage(tmp_path)
    with pytest.raises(FunctionalVerificationError, match="clock must be a non-empty"):
        run_functional_verification(_request(tmp_path, _state(clock=3)))


class _SeedRecordingRunner(FakeCocotbRunner):
    def __init__(self):
        super().__init__(RESULTS_XML)
        self.test_calls: list[dict] = []

    def test(self, **kwargs):
        self.test_calls.append(kwargs)
        super().test(**kwargs)


def _stub(monkeypatch, runner):
    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=lambda n: runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")


def test_stubbed_random_run_is_one_regression_per_seed(tmp_path, monkeypatch):
    _stage(tmp_path)
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)

    report = run_functional_verification(_request(tmp_path, _state(seeds=[11, -5])))

    assert [c["plusargs"] for c in runner.test_calls] == [
        ["+klt_init_seed=11"],
        ["+klt_init_seed=-5"],
    ]
    assert len({c["results_xml"] for c in runner.test_calls}) == 2
    assert report["test_count"] == 2
    assert [t["initial_state_seed"] for t in report["tests"]] == [11, -5]
    state = report["environment"]["initial_state"]
    assert state["mode"] == "random" and state["seeds"] == [11, -5]
    assert state["state_elements"] == 4 and state["d_nets"] == 4
    assert state["clock"] == "clk"
    assert [r["seed"] for r in state["runs"]] == [11, -5]
    build = runner.build_kwargs
    assert ["-s", "klt_initial_state"] == build["build_args"][:2]
    assert build["sources"][-1].endswith("klt_initial_state.v")
    assert Path(build["sources"][-1]).is_file()


def test_stubbed_x_run_is_single_and_echoes_no_seeds(tmp_path, monkeypatch):
    _stage(tmp_path)
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)

    report = run_functional_verification(
        _request(tmp_path, {"mode": "x", "netlist": "net.v"})
    )

    assert len(runner.test_calls) == 1
    assert runner.test_calls[0]["plusargs"] == []
    assert report["environment"]["initial_state"]["seeds"] == []
    assert "initial_state_seed" not in report["tests"][0]


def test_run_without_initial_state_reports_null(tmp_path, monkeypatch):
    _stage(tmp_path)
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)
    request_path = _request(tmp_path, {"mode": "x", "netlist": "net.v"})
    doc = json.loads(Path(request_path).read_text())
    del doc["options"]["initial_state"]
    Path(request_path).write_text(json.dumps(doc))

    report = run_functional_verification(request_path)

    assert report["environment"]["initial_state"] is None


def test_hard_macro_is_a_request_error(tmp_path, monkeypatch):
    _stage(tmp_path)
    (tmp_path / "net.v").write_text(
        NETLIST.replace("endmodule", "  sram_macro m0 (.clk0(clk));\nendmodule"),
        encoding="utf-8",
    )
    _stub(monkeypatch, _SeedRecordingRunner())
    with pytest.raises(
        FunctionalVerificationError, match="m0 \\(sram_macro: hard_macro"
    ):
        run_functional_verification(_request(tmp_path, _state()))


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")
class TestRealIcarusDrive:
    """The generated shim against a real Icarus: seeds reproduce, differ, and
    mode x leaves every flop unknown; the captured value survives the
    release of D."""

    def _run(self, tmp_path: Path, mode: str, seed: int | None) -> str:
        _stage(tmp_path)
        elements, _ = sequential_elements(
            str(tmp_path / "net.v"), [str(tmp_path / "models.v")], "top"
        )
        (tmp_path / "tb.v").write_text(TB, encoding="utf-8")
        shim = tmp_path / f"shim_{mode}.v"
        shim.write_text(
            build_initial_state_verilog(
                mode=mode, elements=elements, scope="tb.dut", clock="clk"
            ),
            encoding="utf-8",
        )
        vvp = tmp_path / f"a_{mode}.vvp"
        sources = [tmp_path / n for n in ("models.v", "net.v", "tb.v")]
        subprocess.run(
            [
                "iverilog",
                "-g2012",
                "-o",
                str(vvp),
                "-s",
                "tb",
                "-s",
                "klt_initial_state",
            ]
            + [str(p) for p in (*sources, shim)],
            check=True,
            capture_output=True,
        )
        plusarg = [f"+klt_init_seed={seed}"] if seed is not None else []
        out = subprocess.run(
            ["vvp", str(vvp), *plusarg], check=True, capture_output=True, text=True
        ).stdout
        return next(line for line in out.splitlines() if line.startswith("Q="))

    def test_same_seed_same_state_different_seeds_differ(self, tmp_path):
        states = {seed: self._run(tmp_path, "random", seed) for seed in (1, 2, 3, 4)}
        assert self._run(tmp_path, "random", 2) == states[2]
        assert len(set(states.values())) > 1
        assert all("x" not in s for s in states.values())

    def test_mode_x_starts_every_flop_unknown(self, tmp_path):
        assert self._run(tmp_path, "x", None) == "Q=xxxx"


EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "functional-verification"
    / "initial-state"
)

PROBE_TESTBENCH = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer


@cocotb.test()
async def probe(dut):
    # Reset deasserted: whatever the flops hold right after the first full clock
    # cycle (negedge 5ns, posedge 10ns, negedge 15ns) is the power-up state.
    dut.rst_n.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await Timer(17, unit="ns")
    with open("power_up.txt", "w") as handle:
        handle.write(f"{dut.q_rst.value}{dut.q_free.value}")
"""


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


needs_real_toolchain = pytest.mark.skipif(
    not _have_cocotb() or shutil.which("iverilog") is None,
    reason="cocotb and iverilog are both required",
)


def _stage_example(tmp_path: Path) -> None:
    for path in EXAMPLE_DIR.iterdir():
        shutil.copy(path, tmp_path / path.name)


def _probe(tmp_path: Path, initial_state: dict) -> str:
    request = json.loads((EXAMPLE_DIR / "request-x.json").read_text())
    request["options"]["initial_state"] = initial_state
    request["testbench"] = {"module": "probe_power_up"}
    request_path = tmp_path / "probe-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    report = run_functional_verification(str(request_path))
    assert report["status"] == "pass", report
    return (
        (tmp_path / ".klt" / "functional-verification" / "power_up.txt")
        .read_text()
        .lower()
    )


@needs_real_toolchain
def test_integration_worked_example_passes_in_both_modes(tmp_path):
    _stage_example(tmp_path)
    for name, seeds in (("request-x.json", []), ("request-random.json", [1, 2, 3])):
        report = run_functional_verification(str(tmp_path / name))
        assert report["status"] == "pass", report["tests"]
        assert report["environment"]["initial_state"]["seeds"] == seeds
        assert report["test_count"] == 2 * max(1, len(seeds))


@needs_real_toolchain
def test_integration_flops_really_start_in_the_requested_state(tmp_path):
    """X mode: every flop unknown. Random mode: resolved bits, reproducible
    for a seed and varying across seeds -- read back from the live flops."""
    _stage_example(tmp_path)
    (tmp_path / "probe_power_up.py").write_text(PROBE_TESTBENCH, encoding="utf-8")
    assert _probe(tmp_path, {"mode": "x", "netlist": "netlist.v"}) == "xx"
    seen = {}
    for seed in (1, 2, 3, 4, 5, 6):
        state = _probe(
            tmp_path, {"mode": "random", "seeds": [seed], "netlist": "netlist.v"}
        )
        assert set(state) <= {"0", "1"}
        seen[seed] = state
    again = _probe(tmp_path, {"mode": "random", "seeds": [3], "netlist": "netlist.v"})
    assert again == seen[3]
    assert len(set(seen.values())) > 1
