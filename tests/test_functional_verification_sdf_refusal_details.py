"""Issue #5854: an SDF-annotation refusal carries the drop accounting it
already computed.

Both refusal sites -- the cocotb path's post-run transcript gate and
`testbench.verilog_top`'s `_verilog_top_sdf_gate` -- used to build the merged
per-class drop accounting (`{**sdf_pre_dropped_counts, **scanned}`) and then
discard it on the raise, so the caller whose run refused was exactly the one
who could not read the fidelity accounting. The raised
`FunctionalVerificationError` now carries
`details = {"sdf": {"dropped": ...}}` in the same shape the success path
reports as `environment.sdf.dropped` -- omitted when no class was dropped.

Everything here runs on the stubbed-runner tier or against the gate function
directly; the one integration test needs only `iverilog`/`vvp` (skipped
without them, like its siblings in `test_functional_verification_verilog_top.py`).
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_verilog_top as fvt
from klayout_tools.cli import main
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERILOG_TB_EXAMPLE_DIR = (
    REPO_ROOT / "examples" / "functional-verification" / "verilog-tb"
)

_RESULTS_XML = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_gcd_known_pairs" classname="test_gcd" \
file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="45" \
time="0.005598783493041992" sim_time_ns="520.0" ratio_time="92877.31891155304" />
  </testsuite>
</testsuites>
"""

_GCD_RTL = "module gcd(input wire clk); endmodule\n"
_TESTBENCH = "# stub testbench module -- never imported by these tests\n"

_MINIMAL_SDF = """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "gcd")
  (DIVIDER .)
  (TIMESCALE 1ns)
)
"""

#: One `COND`-operator `IOPATH` (issue #5296's drop class): the pre-run
#: normalization step records `iopath_cond_expression` -- count 1, arcs
#: naming the CLK -> Q cell arc -- into `sdf_pre_dropped_counts`/`..._arcs`
#: before the run starts.
_COND_EXPR_SDF = """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "gcd")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "cellx")
    (INSTANCE u)
    (DELAY (ABSOLUTE
      (COND ~CLK&~D&SETN (IOPATH (posedge CLK) Q (0.1:0.1:0.1) (0.1:0.1:0.1)))
    ))
  )
)
"""

_ACTIONABLE_ERROR_LINE = (
    "SDF ERROR: route.sdf:2976: Unable to match ModPath D -> Q in gcd._647_\n"
)

_TIMINGCHECK_LINE = "SDF WARNING: route.sdf:7: TIMINGCHECK not supported.\n"

_COND_ARCS = [{"celltype": "cellx", "from": "CLK", "to": "Q"}]


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _request(tmp_path: Path, sdf_text: str) -> str:
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _write(tmp_path / "test_gcd.py", _TESTBENCH)
    _write(tmp_path / "route.sdf", sdf_text)
    request = {
        "engine": "icarus",
        "sources": ["gcd.v"],
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd", "testcase": None},
        "options": {
            "coverage": False,
            "timescale": ["1ns", "1ps"],
            "sdf": {"file": "route.sdf"},
        },
    }
    return _write(tmp_path / "request.json", json.dumps(request))


def _stub_runner(monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner) -> None:
    def get_runner(name):
        assert name == "icarus"
        return runner

    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=get_runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")


# -- the cocotb path ---------------------------------------------------------


def test_cocotb_refusal_carries_the_drop_accounting(tmp_path, monkeypatch):
    """A pre-run drop (`iopath_cond_expression`, with arcs) plus a
    post-run benign scan class (`timingcheck`) both survive into the raised
    error's `details`, merged exactly as the success report would have
    merged them."""
    request_path = _request(tmp_path, _COND_EXPR_SDF)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML,
            extra_test_log=_TIMINGCHECK_LINE + _ACTIONABLE_ERROR_LINE,
        ),
    )

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        run_functional_verification(request_path)

    error = excinfo.value
    assert error.code is None  # no new error code (issue #5854's AC)
    dropped = error.details["sdf"]["dropped"]
    assert set(dropped) == {"iopath_cond_expression", "timingcheck"}
    assert dropped["iopath_cond_expression"]["count"] == 1
    assert dropped["iopath_cond_expression"]["reason"]
    assert dropped["iopath_cond_expression"]["arcs"] == _COND_ARCS
    assert dropped["timingcheck"]["count"] == 1
    assert dropped["timingcheck"]["reason"]
    assert "arcs" not in dropped["timingcheck"]


def test_refusal_with_no_dropped_class_has_no_details(tmp_path, monkeypatch):
    """Nothing was dropped (no pre-run step fired, the transcript carried
    only the actionable error) -> the error object has no `details` at all,
    the same shape every other refusal path's error has."""
    request_path = _request(tmp_path, _MINIMAL_SDF)
    _stub_runner(
        monkeypatch, _FakeRunner(_RESULTS_XML, extra_test_log=_ACTIONABLE_ERROR_LINE)
    )

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        run_functional_verification(request_path)

    assert getattr(excinfo.value, "details", None) is None


# -- the CLI error envelope ---------------------------------------------------


def test_cli_error_envelope_carries_sdf_dropped(tmp_path, monkeypatch, capsys):
    request_path = _request(tmp_path, _COND_EXPR_SDF)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML,
            extra_test_log=_TIMINGCHECK_LINE + _ACTIONABLE_ERROR_LINE,
        ),
    )

    exit_code = main(["functional-verification", request_path, "--format", "json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["command"] == "functional-verification"
    assert "did not fully apply" in error["message"]
    assert "code" not in error
    dropped = error["sdf"]["dropped"]
    assert dropped["iopath_cond_expression"]["count"] == 1
    assert dropped["iopath_cond_expression"]["arcs"] == _COND_ARCS
    assert dropped["timingcheck"]["count"] == 1


def test_cli_error_envelope_has_no_sdf_object_when_nothing_dropped(
    tmp_path, monkeypatch, capsys
):
    request_path = _request(tmp_path, _MINIMAL_SDF)
    _stub_runner(
        monkeypatch, _FakeRunner(_RESULTS_XML, extra_test_log=_ACTIONABLE_ERROR_LINE)
    )

    exit_code = main(["functional-verification", request_path, "--format", "json"])

    assert exit_code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert "sdf" not in error


# -- the `testbench.verilog_top` gate -----------------------------------------


def test_verilog_top_gate_refusal_carries_the_drop_accounting(tmp_path):
    build_log = _write(tmp_path / "build_icarus.log", "")
    test_log = _write(
        tmp_path / "test_icarus.log",
        _TIMINGCHECK_LINE + _ACTIONABLE_ERROR_LINE,
    )
    sdf = {"file": "route.sdf", "corner": "typ", "entries": "all"}
    counts = {"iopath_cond_expression": 1}
    arcs = {"iopath_cond_expression": _COND_ARCS}

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        fvt._verilog_top_sdf_gate(sdf, "icarus", build_log, test_log, counts, arcs)

    dropped = excinfo.value.details["sdf"]["dropped"]
    assert set(dropped) == {"iopath_cond_expression", "timingcheck"}
    assert dropped["iopath_cond_expression"]["arcs"] == _COND_ARCS
    assert dropped["timingcheck"]["count"] == 1


def test_verilog_top_gate_without_diagnostics_returns_merged_counts(tmp_path):
    build_log = _write(tmp_path / "build_icarus.log", "")
    test_log = _write(tmp_path / "test_icarus.log", _TIMINGCHECK_LINE)
    sdf = {"file": "route.sdf", "corner": "typ", "entries": "all"}

    merged = fvt._verilog_top_sdf_gate(
        sdf,
        "icarus",
        build_log,
        test_log,
        {"iopath_cond_expression": 1},
        {"iopath_cond_expression": _COND_ARCS},
    )

    assert merged == {"iopath_cond_expression": 1, "timingcheck": 1}


@needs_icarus
def test_verilog_top_integration_refusal_carries_the_drop_accounting(tmp_path):
    """End to end through the real Icarus run (the shape
    `test_functional_verification_verilog_top.py`'s own annotate-failure
    integration test exercises): the pre-run cond-expression drop plus the
    unmatched-instance refusal come out together in `error.details`."""
    request_path = _stage_verilog_tb(tmp_path, "request-sdf.json")
    sdf = tmp_path / "and2.sdf"
    text = sdf.read_text().replace("INSTANCE u1", "INSTANCE nosuch").rstrip()
    assert text.endswith(")")
    sdf.write_text(text[:-1] + _AND2_COND_CELL + ")\n")

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        run_functional_verification(str(request_path))

    dropped = excinfo.value.details["sdf"]["dropped"]
    assert set(dropped) == {"iopath_cond_expression"}
    assert dropped["iopath_cond_expression"]["count"] == 1
    assert dropped["iopath_cond_expression"]["arcs"] == [
        {"celltype": "cellx", "from": "CLK", "to": "Q"}
    ]


_AND2_COND_CELL = (
    '  (CELL (CELLTYPE "cellx") (INSTANCE u)\n'
    "    (DELAY (ABSOLUTE\n"
    "      (COND ~CLK&~D&SETN (IOPATH (posedge CLK) Q "
    "(0.1:0.1:0.1) (0.1:0.1:0.1)))\n"
    "    )))\n"
)


def _stage_verilog_tb(tmp_path: Path, request_name: str) -> Path:
    for entry in VERILOG_TB_EXAMPLE_DIR.iterdir():
        shutil.copy(entry, tmp_path / entry.name)
    return tmp_path / request_name


# -- issue #5769: naming the confirmed-rejected non-zero arcs ------------------
#
# The refusal hint resolves each `SDF ERROR: <file>:<line>: Could not find
# intermodpath!` locator against the files the run actually handed to
# `$sdf_annotate`. Icarus's line counter is cumulative across calls (a
# 16-line safe file plus line 10 of the deferred file is reported as 26),
# so the fixtures below state their line arithmetic in the comments.

_FAN_DUT_V = "module fan (input wire [1:0] a, output wire b);\nendmodule\n"

_FAN_SDF = """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "fan")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "fan")
    (INSTANCE)
    (DELAY (ABSOLUTE
      (INTERCONNECT a[0] u1.in (1.000:5.000:9.000) (1.000:5.000:9.000))
      (INTERCONNECT a[0] u_d.in (1.000:5.000:9.000) (1.000:5.000:9.000))
    ))
  )
)
"""

#: The request SDF above is 14 lines and both entries touch bus port `a`,
#: so the #1619 split defers both: a 14-line safe file (entries cut in
#: place, line count unchanged) then a 14-line deferred file whose first
#: entry sits on its line 10. Icarus reports that rejection as
#: 14 (safe lines) + 10 = line 24 of the deferred file.
_FAN_DEFERRED_LINE = 24


def _intermodpath_line(path: Path, line: int) -> str:
    return f"SDF ERROR: {path}:{line}: Could not find intermodpath!\n"


def test_cocotb_refusal_names_rejected_arcs_and_workaround(tmp_path, monkeypatch):
    """End to end on the stubbed-runner tier (the real-Icarus twin is
    `test_integration_real_icarus_sdf_bus_port_input_fanout_stays_
    unresolvable`): the injected rejection of the non-survivor `a[0]
    u1.in` entry is attributed through the run's own deferred SDF, and
    the message names that arc and the buffer workaround -- never the
    surviving `u_d.in` destination, which annotated and emitted nothing."""
    _write(tmp_path / "fan.v", _FAN_DUT_V)
    _write(tmp_path / "test_fan.py", _TESTBENCH)
    _write(tmp_path / "route.sdf", _FAN_SDF)
    request = {
        "engine": "icarus",
        "sources": ["fan.v"],
        "hdl_toplevel": "fan",
        "testbench": {"module": "test_fan", "testcase": None},
        "options": {
            "coverage": False,
            "timescale": ["1ns", "1ps"],
            "sdf": {"file": "route.sdf"},
        },
    }
    request_path = _write(tmp_path / "request.json", json.dumps(request))
    deferred = (
        tmp_path
        / ".klt"
        / "functional-verification"
        / ("klt_sdf_deferred_bus_ports.sdf")
    )
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML,
            extra_test_log=_intermodpath_line(deferred, _FAN_DEFERRED_LINE),
        ),
    )

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        run_functional_verification(request_path)

    message = str(excinfo.value)
    assert "a[0] → u1.in" in message
    assert "insert a buffer" in message
    assert "u_d.in" not in message
    assert getattr(excinfo.value, "details", None) is None  # no new drop class


def test_refusal_without_resolvable_locator_keeps_the_plain_message(
    tmp_path, monkeypatch
):
    """A diagnostic whose locator names the request's own SDF path (not a
    file the run annotated) attributes nothing: the message keeps its
    exact pre-#5769 text."""
    request_path = _request(tmp_path, _MINIMAL_SDF)
    unresolved = "SDF ERROR: route.sdf:2976: Could not find intermodpath!\n"
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML, extra_test_log=unresolved))

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        run_functional_verification(request_path)

    message = str(excinfo.value)
    assert "Confirmed rejected" not in message
    assert "→" not in message


def test_verilog_top_gate_names_rejected_arcs(tmp_path):
    """The verilog-top refusal site attributes through the same stashed
    annotation context (hand-built here as a real run would stash it)."""
    from klayout_tools import functional_verification_sdf_steps as steps

    annotate_path = tmp_path / "klt_sdf_deferred_bus_ports.sdf"
    _write(annotate_path, _FAN_SDF)
    build_log = _write(tmp_path / "build_icarus.log", "")
    test_log = _write(
        tmp_path / "test_icarus.log",
        _intermodpath_line(annotate_path, 10),  # single call: line is direct
    )
    sdf = {
        "file": "route.sdf",
        "corner": "typ",
        "entries": "all",
        "annotate_paths": [str(annotate_path)],
        "bus_input_ports": {"a"},
    }

    with pytest.raises(
        FunctionalVerificationError, match="did not fully apply"
    ) as excinfo:
        fvt._verilog_top_sdf_gate(sdf, "icarus", build_log, test_log, {}, {})

    message = str(excinfo.value)
    assert "a[0] → u1.in" in message
    assert "u_d.in" not in message
    assert steps._sdf_intermodpath_hint(sdf, []) == ""  # no diagnostic, no arc
