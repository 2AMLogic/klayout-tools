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
HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
needs_icarus = pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog/vvp not installed")

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
