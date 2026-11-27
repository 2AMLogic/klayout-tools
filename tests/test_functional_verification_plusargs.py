"""`testbench.plusargs` on the cocotb path (issue #6906).

The Verilog-bench form gained the field in issue #5006; this file covers the
cocotb (`testbench.module`) counterpart: shape validation (the identical
messages), the `klt_init_seed` reservation, the merge with
`options.initial_state` random seeds, the `environment.plusargs` echo, the
`options.sdf` and `--mutations` paths, offline `--check` validation, and
(when cocotb + iverilog are installed) a real Icarus DUT that reads its ROM
image through `$value$plusargs`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner
from helpers.tool_probes import SKIP_NO_COCOTB, SKIP_NO_ICARUS
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from klayout_tools.functional_verification_check import (
    check_functional_verification_report,
)
from test_functional_verification import (
    _MUTATION_BASELINE_XML,
    _MUTATION_DUT_RTL,
    _MUTATION_KILLED_XML,
    _RESULTS_XML_WITH_SKIP,
    _TESTBENCH,
    _base_request,
    _ContentAwareFakeRunner,
    _setup_inputs,
    _stub_mutation_runner,
    _stub_runner,
    _write,
    _write_mutation_proposals,
    _write_request,
)
from test_functional_verification_initial_state import MODELS, NETLIST

EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "functional-verification"
    / "verilog-tb"
)


def _plusargs_request(tmp_path: Path, plusargs, **request_overrides) -> str:
    _setup_inputs(tmp_path)
    request = _base_request(
        testbench={"module": "test_gcd", "testcase": None, "plusargs": plusargs}
    )
    request.update(request_overrides)
    return _write_request(tmp_path / "request.json", request)


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,match",
    [
        # A bare string is one argv element short of the contract: the field
        # is an array, one element per plusarg.
        ("+a=1", "request.testbench.plusargs must be an array of strings"),
        ([1], r"request.testbench.plusargs\[0\] must be a non-empty string"),
        ([""], r"request.testbench.plusargs\[0\] must be a non-empty string"),
        # Missing '+': the entry would land on vvp's argv as a plain flag or
        # file argument, not a plusarg.
        (
            ["+ok", "bin=x"],
            r"request.testbench.plusargs\[1\] must start with '\+'",
        ),
    ],
)
def test_bad_shapes_match_the_verilog_path_messages(tmp_path, value, match):
    request = _plusargs_request(tmp_path, value)
    with pytest.raises(FunctionalVerificationError, match=match):
        run_functional_verification(request)


def test_absent_field_resolves_to_the_empty_list(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, FakeCocotbRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert runner.test_kwargs["plusargs"] == []
    assert report["environment"]["plusargs"] == []


# -- the klt_init_seed reservation --------------------------------------------


@pytest.mark.parametrize("entry", ["+klt_init_seed", "+klt_init_seed=7"])
@pytest.mark.parametrize("with_initial_state", [False, True])
def test_reserved_name_is_a_request_error(tmp_path, with_initial_state, entry):
    options = (
        {"initial_state": {"mode": "x", "netlist": "gcd.v"}}
        if with_initial_state
        else {"coverage": False, "timescale": ["1ns", "1ps"]}
    )
    request = _plusargs_request(tmp_path, [entry], options=options)
    with pytest.raises(
        FunctionalVerificationError,
        match=r"request.testbench.plusargs\[0\] uses the klt-reserved name "
        "'klt_init_seed'",
    ):
        run_functional_verification(request)


def test_a_longer_name_that_contains_the_reserved_one_is_fine(tmp_path, monkeypatch):
    """The reservation is on the *name* (text after `+` up to the first
    `=`), not a substring: `klt_init_seed_extra` is a different name."""
    request = _plusargs_request(tmp_path, ["+klt_init_seed_extra=1"])
    _stub_runner(monkeypatch, FakeCocotbRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request)

    assert report["environment"]["plusargs"] == ["+klt_init_seed_extra=1"]


@SKIP_NO_COCOTB
@SKIP_NO_ICARUS
def test_verilog_bench_path_keeps_no_reservation(tmp_path):
    """Decision from the curation: the Verilog-bench path has no reserved
    names (`options.initial_state` is refused there), so `+klt_init_seed`
    stays an ordinary bench plusarg on that path."""
    for entry in EXAMPLE_DIR.iterdir():
        shutil.copy(entry, tmp_path / entry.name)
    request_path = tmp_path / "request-rtl.json"
    doc = json.loads(request_path.read_text())
    doc["testbench"]["plusargs"] = ["+klt_init_seed=5"]
    request_path.write_text(json.dumps(doc))

    report = run_functional_verification(str(request_path))

    assert report["status"] == "pass"
    assert report["environment"]["plusargs"] == ["+klt_init_seed=5"]


# -- forwarding and the echo --------------------------------------------------


class _RecordingRunner(FakeCocotbRunner):
    """Records every `test()` call's kwargs, for the per-seed assertions."""

    def __init__(self, results_xml_text):
        super().__init__(results_xml_text)
        self.test_calls: list[dict] = []

    def test(self, **kwargs):
        self.test_calls.append(kwargs)
        super().test(**kwargs)


def test_stubbed_run_forwards_each_entry_as_one_argv_element(tmp_path, monkeypatch):
    # A value containing `=` and a space must survive as ONE list element:
    # the entries are forwarded verbatim (no shell, no re-splitting).
    entries = ["+go", "+file=a b=c", "+x=1"]
    request = _plusargs_request(tmp_path, entries)
    runner = _stub_runner(monkeypatch, _RecordingRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request)

    assert runner.test_calls[0]["plusargs"] == entries
    assert report["environment"]["plusargs"] == entries


def test_report_environment_gains_only_the_plusargs_key(tmp_path, monkeypatch):
    """A request without the field must render `environment.plusargs: []`
    and change nothing else: two runs of the same request (stubbed, same
    fixed results.xml) differ only by the added key. The three transcript
    envelopes carry per-run file hashes, so their hashes are compared for
    path/scope only."""
    _setup_inputs(tmp_path)
    plain = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _RecordingRunner(_RESULTS_XML_WITH_SKIP))
    without = run_functional_verification(plain)

    doc = json.loads(Path(plain).read_text())
    doc["testbench"]["plusargs"] = ["+go"]
    Path(plain).write_text(json.dumps(doc))
    with_plusargs = run_functional_verification(plain)

    def _strip_hashes(environment):
        stripped = dict(environment)
        for key in ("results_xml", "test_log", "build_log"):
            stripped[key] = {k: v for k, v in stripped[key].items() if k != "sha256"}
        return stripped

    assert without["environment"]["plusargs"] == []
    assert with_plusargs["environment"]["plusargs"] == ["+go"]
    with_env = _strip_hashes(with_plusargs["environment"])
    without_env = _strip_hashes(without["environment"])
    assert with_env.pop("plusargs") == ["+go"]
    assert without_env.pop("plusargs") == []
    assert with_env == without_env


def test_stubbed_initial_state_random_merges_user_entries_before_the_seed(
    tmp_path, monkeypatch
):
    """Each per-seed run receives the user entries followed by its own
    `+klt_init_seed=<n>` -- the reservation above guarantees the two never
    collide on one name."""
    (tmp_path / "models.v").write_text(MODELS, encoding="utf-8")
    (tmp_path / "net.v").write_text(NETLIST, encoding="utf-8")
    _write(tmp_path / "test_tb.py", "# stub\n")
    request = _write_request(
        tmp_path / "request.json",
        {
            "engine": "icarus",
            "sources": ["models.v", "net.v"],
            "hdl_toplevel": "top",
            "testbench": {
                "module": "test_tb",
                "plusargs": ["+lut=rom.hex"],
            },
            "options": {
                "initial_state": {
                    "mode": "random",
                    "seeds": [3, 4],
                    "netlist": "net.v",
                }
            },
        },
    )
    runner = _stub_runner(monkeypatch, _RecordingRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request)

    assert [call["plusargs"] for call in runner.test_calls] == [
        ["+lut=rom.hex", "+klt_init_seed=3"],
        ["+lut=rom.hex", "+klt_init_seed=4"],
    ]
    assert report["environment"]["plusargs"] == ["+lut=rom.hex"]


def test_stubbed_trace_run_forwards_plusargs_and_waves_together(tmp_path, monkeypatch):
    """`options.trace` rides the same Icarus argv (cocotb appends its own
    `-fst`/`-none` flags to the plusargs list internally), so the user
    entries must still be forwarded -- the merge is cocotb's, not ours."""
    request = _plusargs_request(
        tmp_path, ["+go"], options={"trace": True, "timescale": ["1ns", "1ps"]}
    )
    runner = _stub_runner(
        monkeypatch, FakeCocotbRunner(_RESULTS_XML_WITH_SKIP, trace_bytes=b"fst")
    )

    report = run_functional_verification(request)

    assert runner.test_kwargs["waves"] is True
    assert runner.test_kwargs["plusargs"] == ["+go"]
    assert report["environment"]["plusargs"] == ["+go"]


def test_stubbed_sdf_run_forwards_plusargs(tmp_path, monkeypatch):
    """Plusargs are a run-time argument, so the SDF path (same
    `_run_spec_test` call, only the elaboration root changes) forwards them
    with no second mechanism."""
    _setup_inputs(tmp_path)
    _write(
        tmp_path / "route.sdf",
        '(DELAYFILE\n  (SDFVERSION "3.0")\n  (DESIGN "gcd")\n'
        "  (DIVIDER .)\n  (TIMESCALE 1ns)\n)\n",
    )
    request = _plusargs_request(
        tmp_path,
        ["+lut=rom.hex"],
        options={"coverage": False, "sdf": {"file": "route.sdf"}},
    )
    runner = _stub_runner(monkeypatch, _RecordingRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request)

    assert runner.test_calls[0]["plusargs"] == ["+lut=rom.hex"]
    assert report["environment"]["plusargs"] == ["+lut=rom.hex"]


# -- `--mutations` --------------------------------------------------------------


class _PlusargsLoggingRunner(_ContentAwareFakeRunner):
    """Appends the forwarded plusargs to each run's `log_file`, so they are
    observable even for `--mutations` variants: those run inside a forked
    child process, and an in-memory list would stay in the child."""

    def test(self, **kwargs):
        super().test(**kwargs)
        with open(kwargs["log_file"], "a", encoding="utf-8") as handle:
            handle.write("PLUSARGS:" + json.dumps(kwargs.get("plusargs")) + "\n")


def test_mutations_forward_plusargs_to_the_baseline_and_every_variant(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").mkdir()
    _write(tmp_path / "dut.v", _MUTATION_DUT_RTL)
    _write(tmp_path / "test_dut.py", _TESTBENCH)
    request = _write_request(
        tmp_path / "request.json",
        _base_request(
            sources=["dut.v"],
            hdl_toplevel="dut",
            testbench={"module": "test_dut", "plusargs": ["+lut=rom.hex"]},
        ),
    )
    proposals_path = _write_mutation_proposals(tmp_path)
    _stub_mutation_runner(
        monkeypatch,
        _PlusargsLoggingRunner(
            _MUTATION_BASELINE_XML, _MUTATION_KILLED_XML, "wire a = 9;"
        ),
    )

    report = run_functional_verification_with_mutations(request, proposals_path)

    assert report["status"] == "pass"
    assert report["environment"]["plusargs"] == ["+lut=rom.hex"]
    out = tmp_path / ".klt" / "functional-verification"
    baseline_log = (out / "test_icarus.log").read_text()
    assert 'PLUSARGS:["+lut=rom.hex"]' in baseline_log
    variant_logs = sorted((out / "mutants").glob("mutant_*/test_icarus.log"))
    assert variant_logs, "the mutation run must have tested at least one variant"
    for log in variant_logs:
        assert 'PLUSARGS:["+lut=rom.hex"]' in log.read_text()


# -- `--check` (offline validation) ---------------------------------------------


def test_check_validates_the_field_offline(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, FakeCocotbRunner(_RESULTS_XML_WITH_SKIP))
    report = run_functional_verification(request)
    report_path = tmp_path / "fv.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    # A well-formed entry passes validation (the request hash then drifts,
    # which is the check's own business, not a plusargs refusal).
    doc = json.loads(Path(request).read_text())
    doc["testbench"]["plusargs"] = ["+go"]
    Path(request).write_text(json.dumps(doc))
    result = check_functional_verification_report(str(report_path), request)
    assert result["mode"] == "check"

    # A reserved entry is exit-1 material offline, before any simulator.
    doc["testbench"]["plusargs"] = ["+klt_init_seed=7"]
    Path(request).write_text(json.dumps(doc))
    with pytest.raises(
        FunctionalVerificationError, match="klt-reserved name 'klt_init_seed'"
    ):
        check_functional_verification_report(str(report_path), request)


# -- real Icarus ------------------------------------------------------------------

_LUT_DUT = """\
module lut_dut (input wire [3:0] addr, output reg [7:0] data);
  reg [7:0] rom [0:15];
  // Wide enough for a real path: `$value$plusargs("%s", ...)` stores the
  // string right-aligned in the vector, so 256 bits would truncate it to
  // its last 32 characters.
  reg [2047:0] lut_file;
  reg loaded;
  initial begin
    loaded = 0;
    if ($value$plusargs("lut=%s", lut_file)) begin
      $readmemh(lut_file, rom);
      loaded = 1;
    end
  end
  always @(*) begin
    if (loaded) data = rom[addr];
    else data = 8'hxx;
  end
endmodule
"""

_LUT_TESTBENCH = """\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_rom_loaded(dut):
    for addr, want in ((0, 0x11), (1, 0x22), (15, 0xEE)):
        dut.addr.value = addr
        await Timer(1, units="ns")
        got = int(dut.data.value)
        assert got == want, f"rom[{addr}] = {got:#04x}, want {want:#04x}"
"""

_ROM_HEX = "\n".join(["11", "22"] + ["00"] * 13 + ["EE"]) + "\n"


def _stage_lut(tmp_path: Path) -> str:
    _write(tmp_path / "lut_dut.v", _LUT_DUT)
    _write(tmp_path / "test_lut.py", _LUT_TESTBENCH)
    _write(tmp_path / "rom.hex", _ROM_HEX)
    return _write_request(
        tmp_path / "request.json",
        {
            "engine": "icarus",
            "sources": ["lut_dut.v"],
            "hdl_toplevel": "lut_dut",
            "testbench": {"module": "test_lut"},
        },
    )


@SKIP_NO_COCOTB
@SKIP_NO_ICARUS
def test_integration_plusarg_fed_rom_fails_without_and_passes_with(tmp_path):
    """The gap this issue closes: a DUT that loads its ROM through
    `$value$plusargs` runs unedited -- it fails without the plusarg (the
    data stays `x`) and passes with an absolute path."""
    request = _stage_lut(tmp_path)

    absent = run_functional_verification(request)
    assert absent["status"] == "fail"
    assert absent["environment"]["plusargs"] == []

    doc = json.loads(Path(request).read_text())
    doc["testbench"]["plusargs"] = [f"+lut={tmp_path / 'rom.hex'}"]
    Path(request).write_text(json.dumps(doc))
    absolute = run_functional_verification(request)
    assert absolute["status"] == "pass"
    assert absolute["environment"]["plusargs"] == [f"+lut={tmp_path / 'rom.hex'}"]


@SKIP_NO_COCOTB
@SKIP_NO_ICARUS
def test_integration_plusarg_relative_to_the_output_dir(tmp_path):
    """The cocotb simulator cwd is `output_dir` (default
    `<request_dir>/.klt/functional-verification`), so a committed,
    checkout-free request uses a path relative to that directory."""
    request = _stage_lut(tmp_path)

    doc = json.loads(Path(request).read_text())
    # Two levels up from .klt/functional-verification is the request dir.
    doc["testbench"]["plusargs"] = ["+lut=../../rom.hex"]
    Path(request).write_text(json.dumps(doc))

    report = run_functional_verification(request)

    assert report["status"] == "pass"
    assert report["environment"]["plusargs"] == ["+lut=../../rom.hex"]
