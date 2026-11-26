"""Power-up state and SDF annotation on the same Icarus build."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from test_functional_verification_check import _check, _forbid_simulation
from test_functional_verification_initial_state import (
    EXAMPLE_DIR,
    _request,
    _SeedRecordingRunner,
    _state,
    _stub,
    needs_real_toolchain,
)

DUT = "klt_sdf_dut_wrapper.klt_sdf_dut"
ESCAPED_FLOP = r"\g[0].sub.ff"
ESCAPED_SDF_NAME = r"g\[0\]\.sub\.ff"
ALIAS = "g_0__sub_ff_klt1"

SDF = """\
(DELAYFILE (SDFVERSION "3.0") (DESIGN "top") (DIVIDER .) (TIMESCALE 1ns)
  (CELL (CELLTYPE "top") (INSTANCE) (DELAY (ABSOLUTE
    (INTERCONNECT d_free {instance}.D ({triple}) ({triple}))))))
""".strip()


def _stage(tmp_path, initial_state, *, escaped=False, delay=3):
    shutil.copyfile(EXAMPLE_DIR / "cell_models.v", tmp_path / "models.v")
    netlist = (EXAMPLE_DIR / "netlist.v").read_text()
    if escaped:
        netlist = netlist.replace(" f_free ", f" {ESCAPED_FLOP} ")
        netlist = netlist.replace("  input clk;", "  wire g_0__sub_ff;\n  input clk;")
    (tmp_path / "net.v").write_text(netlist)
    (tmp_path / "test_tb.py").write_text("# stub\n")
    sdf = SDF.format(
        instance=ESCAPED_SDF_NAME if escaped else "f_free",
        triple=f"{delay}:{delay}:{delay}",
    )
    (tmp_path / "route.sdf").write_text(sdf)
    return _request(tmp_path, initial_state, sdf={"file": "route.sdf"})


def _shim(tmp_path):
    return (tmp_path / ".klt/functional-verification/klt_initial_state.v").read_text()


class _PerSeedLogRunner(_SeedRecordingRunner):
    def __init__(self, transcripts):
        super().__init__()
        self.transcripts = iter(transcripts)

    def test(self, **kwargs):
        self.extra_test_log = next(self.transcripts)
        super().test(**kwargs)


def test_build_once_and_drive_both_seeds(tmp_path, monkeypatch):
    request = _stage(tmp_path, _state(seeds=[11, -5]))
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)
    report = run_functional_verification(request)
    build = runner.build_kwargs
    assert build["build_args"] == [
        "-gspecify",
        "-ginterconnect",
        "-s",
        "klt_sdf_annotate",
        "-T",
        "typ",
        "-s",
        "klt_initial_state",
    ]
    assert build["hdl_toplevel"] == "klt_sdf_dut_wrapper"
    assert [call["plusargs"] for call in runner.test_calls] == [
        ["+klt_init_seed=11"],
        ["+klt_init_seed=-5"],
    ]
    text = _shim(tmp_path)
    assert f"force {DUT}.f_rst.D = klt_bit_0;" in text
    assert f"release {DUT}.f_rst.D;" in text
    assert f"@(negedge {DUT}.clk);" in text
    first_log = Path(runner.test_calls[0]["log_file"])
    assert not first_log.with_name("test_icarus.log").exists()
    assert report.local_paths["test_log"] == str(first_log)
    assert (
        report["environment"]["test_log"]["sha256"]
        == "sha256:" + hashlib.sha256(first_log.read_bytes()).hexdigest()
    )
    assert report["environment"]["sdf"]["annotated"]
    assert [r["seed"] for r in report["environment"]["initial_state"]["runs"]] == [
        11,
        -5,
    ]
    assert [t["initial_state_seed"] for t in report["tests"]] == [11, -5]


def test_x_mode_uses_one_run(tmp_path, monkeypatch):
    request = _stage(tmp_path, {"mode": "x", "netlist": "net.v"})
    runner = _SeedRecordingRunner()
    _stub(monkeypatch, runner)
    report = run_functional_verification(request)
    assert [call["plusargs"] for call in runner.test_calls] == [[]]
    assert f"force {DUT}.f_rst.D = 1'bx;" in _shim(tmp_path)
    assert report["environment"]["initial_state"]["seeds"] == []
    assert report["environment"]["sdf"]["annotated"]


def test_without_sdf_uses_original_top(tmp_path, monkeypatch):
    _stage(tmp_path, _state())
    _stub(monkeypatch, _SeedRecordingRunner())
    run_functional_verification(_request(tmp_path, _state()))
    text = _shim(tmp_path)
    assert "force top.f_free.D = klt_bit_1;" in text
    assert "release top.f_free.D;" in text
    assert "@(posedge top.clk);" in text


def test_later_seed_annotation_error_fails_run(tmp_path, monkeypatch):
    request = _stage(tmp_path, _state(seeds=[11, -5]))
    runner = _PerSeedLogRunner(
        ["", "SDF ERROR: route.sdf:9: Unable to match ModPath D -> Q\n"]
    )
    _stub(monkeypatch, runner)
    with pytest.raises(FunctionalVerificationError, match="Unable to match ModPath"):
        run_functional_verification(request)


def test_benign_drops_count_build_and_first_run(tmp_path, monkeypatch):
    request = _stage(tmp_path, _state(seeds=[11, -5]))
    warning = "SDF WARNING: route.sdf:7: TIMINGCHECK not supported.\n"
    runner = _PerSeedLogRunner([warning, warning])
    runner.extra_build_log = warning
    _stub(monkeypatch, runner)
    report = run_functional_verification(request)
    dropped = report["environment"]["sdf"]["dropped"]
    assert dropped.keys() == {"timingcheck"}
    assert dropped["timingcheck"]["count"] == 2


def test_verilog_bench_rejects_initial_state(tmp_path):
    request = Path(_stage(tmp_path, _state()))
    doc = json.loads(request.read_text())
    doc["testbench"] = {"verilog_top": "tb", "dut_instance": "tb.dut"}
    request.write_text(json.dumps(doc))
    with pytest.raises(
        FunctionalVerificationError,
        match="initial_state cannot be combined with testbench.verilog_top",
    ):
        run_functional_verification(str(request))


def test_offline_check_detects_only_sdf_drift(tmp_path, monkeypatch, capsys):
    request = Path(_stage(tmp_path, _state()))
    _stub(monkeypatch, _SeedRecordingRunner())
    report = tmp_path / "report.json"
    report.write_text(json.dumps(run_functional_verification(str(request))))
    _forbid_simulation(monkeypatch)
    assert _check(request, report, capsys, 0) == {}
    sdf = tmp_path / "route.sdf"
    sdf.write_text(sdf.read_text().replace("3:3:3", "4:4:4"))
    assert _check(request, report, capsys, 3).keys() == {"provenance.sdf_sha256"}


PROBE = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer

@cocotb.test()
async def measure(dut):
    pin = dut.klt_sdf_dut.FLOP.D
    dut.rst_n.value = 1
    dut.d_free.value = 0
    dut.d_rst.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await Timer(17, unit="ns")
    state = str(dut.q_free.value)
    await Timer(5, unit="ns")
    transitions = []
    for value in (1, 0):
        dut.d_free.value = value
        await Timer(1, unit="ns")
        early = str(pin.value)
        await Timer(3, unit="ns")
        transitions.append(early + str(pin.value))
    with open("observed.txt", "a") as measurements:
        print(state, *transitions, file=measurements)
"""


@needs_real_toolchain
@pytest.mark.parametrize(
    "escaped, delay, transitions",
    [(False, 3, "01 10"), (True, 3, "01 10"), (True, 0, "11 00")],
    ids=["ordinary", "renamed", "zero_delay_control"],
)
def test_released_pin_retains_annotated_delay(tmp_path, escaped, delay, transitions):
    request = _stage(tmp_path, _state(seeds=[1, 2, 3]), escaped=escaped, delay=delay)
    instance = ALIAS if escaped else "f_free"
    (tmp_path / "test_tb.py").write_text(PROBE.replace("FLOP", instance))
    report = run_functional_verification(request)
    assert report["status"] == "pass", report["tests"]
    assert report["environment"]["sdf"]["annotated"]
    assert report["environment"]["initial_state"]["seeds"] == [1, 2, 3]
    if escaped:
        assert report["environment"]["sdf"]["renamed"] == [
            {"from": "g[0].sub.ff", "to": ALIAS}
        ]
    text = _shim(tmp_path)
    assert f"force {DUT}.{instance}.D = klt_bit_1;" in text
    assert f"release {DUT}.{instance}.D;" in text
    observed = tmp_path / ".klt/functional-verification/observed.txt"
    assert observed.read_text().splitlines() == [
        f"{bit} {transitions}" for bit in (1, 1, 0)
    ]
