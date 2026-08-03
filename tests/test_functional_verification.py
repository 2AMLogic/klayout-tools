"""Tests for `klt functional-verification` and the
`klayout_tools.functional_verification` library.

Three tiers, mirroring `tests/test_synthesize.py`'s structure:

- **Request/library unit tests** exercise `load_request`/`load_request_arg`/
  `run_functional_verification`'s validation paths directly. Every one of
  them runs *before* cocotb is imported, so they pass on a machine (or CI
  runner) with no cocotb, no Icarus, and no Verilator.
- **`results.xml` parsing tests** run against the exact XML fragments
  captured live in `docs/design/cocotb-verification-spike.md` section 4, plus
  a **stubbed-runner** tier that drives `run_functional_verification` end to
  end with `_import_runner` replaced by a fake `cocotb_tools.runner` module —
  covering the full response shape, both exit codes, the `timescale`-on-both-
  calls gotcha (spike section 3b), coverage post-processing, and the
  failure/edge paths (build error, no `results.xml`, zero registered tests).
- **Integration tests** (`@pytest.mark.skipif` when cocotb or the simulator
  binary is missing) run the *real* GCD worked example from
  `examples/functional-verification/` through the verb against **both**
  `engine: "icarus"` and `engine: "verilator"`, asserting the spike's own
  live-captured `TESTS=3 PASS=2 FAIL=1 SKIP=0` outcome and (Verilator only)
  its line/toggle/branch/expr coverage numbers.

  These are **skipped, not failed, in CI**: cocotb caps at Python <= 3.13 and
  neither `iverilog` nor `verilator` is installed on the CI runners today
  (the same posture `test_synthesize.py` takes toward `yosys` + a real
  sky130 install). They run — and pass — on any machine with cocotb plus the
  simulator.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.cli import main
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    load_request,
    parse_results_xml,
    run_functional_verification,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "functional-verification"

# --------------------------------------------------------------------------- #
# Fixtures captured live in docs/design/cocotb-verification-spike.md section 4
# --------------------------------------------------------------------------- #

_RESULTS_XML_WITH_SKIP = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_gcd_known_pairs" classname="test_gcd" \
file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="45" \
time="0.005598783493041992" sim_time_ns="520.0" ratio_time="92877.31891155304" />
    <testcase name="test_gcd_random_pairs" classname="test_gcd" \
file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="58" \
time="0.03472900390625" sim_time_ns="4720.0" ratio_time="135909.45518453428" />
    <testcase name="test_gcd_deliberately_wrong_expectation" classname="test_gcd" \
file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="79" time="0" \
sim_time_ns="0" ratio_time="0">
      <skipped />
    </testcase>
  </testsuite>
</testsuites>
"""

_RESULTS_XML_WITH_FAILURE = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
    <testcase name="test_gcd_known_pairs" classname="test_gcd" \
time="0.005598783493041992" sim_time_ns="520.0" />
    <testcase name="test_gcd_random_pairs" classname="test_gcd" \
time="0.03472900390625" sim_time_ns="4720.0" />
    <testcase name="test_gcd_deliberately_wrong_expectation" classname="test_gcd" \
file="/private/tmp/gcd-cocotb-spike/test_gcd.py" lineno="79" \
time="0.0025670528411865234" sim_time_ns="120.0" ratio_time="46746.21342992477">
      <failure error_type="AssertionError" \
error_msg="gcd(48, 18): got 6, want 999 (deliberate failure)&#10;assert 6 == 999" />
    </testcase>
  </testsuite>
</testsuites>
"""

_RESULTS_XML_EMPTY = """\
<testsuites name="results">
  <testsuite name="all" package="all">
    <property name="random_seed" value="1785780800" />
  </testsuite>
</testsuites>
"""

_GCD_RTL = "module gcd(input wire clk); endmodule\n"
_TESTBENCH = "# stub testbench module -- never imported by these tests\n"


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request), encoding="utf-8")
    return str(path)


def _base_request(**overrides) -> dict:
    request = {
        "engine": "icarus",
        "sources": ["gcd.v"],
        "hdl_toplevel": "gcd",
        "testbench": {"module": "test_gcd", "testcase": None},
        "options": {"coverage": False, "timescale": ["1ns", "1ps"]},
    }
    request.update(overrides)
    return request


def _setup_inputs(tmp_path: Path) -> None:
    """The RTL + testbench files a request's own validation requires to
    exist. Their *contents* never matter to the stubbed tiers -- no real
    simulator ever reads them."""
    _write(tmp_path / "gcd.v", _GCD_RTL)
    _write(tmp_path / "test_gcd.py", _TESTBENCH)


# --------------------------------------------------------------------------- #
# `load_request` / `load_request_arg`
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(FunctionalVerificationError, match="file not found"):
        load_request(str(tmp_path / "nope.json"))


def test_load_request_directory(tmp_path):
    with pytest.raises(FunctionalVerificationError, match="not a file"):
        load_request(str(tmp_path))


def test_load_request_invalid_json(tmp_path):
    path = _write(tmp_path / "request.json", "{not json")
    with pytest.raises(FunctionalVerificationError, match="not valid JSON"):
        load_request(path)


def test_load_request_not_an_object(tmp_path):
    path = _write(tmp_path / "request.json", "[1, 2, 3]")
    with pytest.raises(FunctionalVerificationError, match="must contain a JSON object"):
        load_request(path)


@pytest.mark.parametrize("field", ["sources", "hdl_toplevel", "testbench"])
def test_load_request_missing_required_field(tmp_path, field):
    request = _base_request()
    del request[field]
    path = _write_request(tmp_path / "request.json", request)
    with pytest.raises(
        FunctionalVerificationError, match=f"missing required field: {field}"
    ):
        load_request(path)


def test_load_request_arg_inline_json(tmp_path, monkeypatch):
    """The inline-JSON form resolves relative paths against the cwd, the same
    way `klt lvs`'s own inline request does -- this is what lets `klt eval`
    hand this verb an inline request object."""
    _setup_inputs(tmp_path)
    monkeypatch.chdir(tmp_path)
    request, base_dir = fv.load_request_arg(json.dumps(_base_request()))
    assert request["hdl_toplevel"] == "gcd"
    assert Path(base_dir) == tmp_path


def test_load_request_arg_stdin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.stdin", __import__("io").StringIO(json.dumps(_base_request()))
    )
    request, _ = fv.load_request_arg("-")
    assert request["testbench"]["module"] == "test_gcd"


def test_load_request_arg_garbage_string(tmp_path):
    with pytest.raises(FunctionalVerificationError, match="neither an existing file"):
        fv.load_request_arg("not-a-file-or-json")


# --------------------------------------------------------------------------- #
# request validation (all of it runs before cocotb is ever imported)
# --------------------------------------------------------------------------- #


def test_unsupported_engine(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(engine="questa")
    )
    with pytest.raises(
        FunctionalVerificationError, match="unsupported engine 'questa'"
    ):
        run_functional_verification(request_path)


def test_sources_must_be_nonempty_array(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request(sources=[]))
    with pytest.raises(FunctionalVerificationError, match="non-empty array"):
        run_functional_verification(request_path)


def test_sources_entries_must_be_strings(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request(sources=[7]))
    with pytest.raises(FunctionalVerificationError, match="non-empty strings"):
        run_functional_verification(request_path)


def test_missing_rtl_source_is_a_clean_error(tmp_path):
    """A malformed/missing RTL source path fails with exit 1 and no envelope
    -- never a crash (issue #422's edge-case list)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )
    with pytest.raises(
        FunctionalVerificationError, match="RTL source not found: nope.v"
    ):
        run_functional_verification(request_path)


def test_unreadable_rtl_source(tmp_path):
    _setup_inputs(tmp_path)
    source_path = tmp_path / "gcd.v"
    os.chmod(source_path, 0o000)
    try:
        request_path = _write_request(tmp_path / "request.json", _base_request())
        with pytest.raises(
            FunctionalVerificationError, match="could not read RTL source"
        ):
            run_functional_verification(request_path)
    finally:
        os.chmod(source_path, 0o644)


def test_hdl_toplevel_must_be_nonempty_string(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(hdl_toplevel="")
    )
    with pytest.raises(FunctionalVerificationError, match="hdl_toplevel must be"):
        run_functional_verification(request_path)


def test_testbench_must_be_object(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(testbench="test_gcd")
    )
    with pytest.raises(
        FunctionalVerificationError, match="request.testbench must be a JSON object"
    ):
        run_functional_verification(request_path)


def test_testbench_module_must_be_a_module_name_not_a_path(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(testbench={"module": "test_gcd.py"})
    )
    with pytest.raises(
        FunctionalVerificationError, match="must be a Python module name, not a path"
    ):
        run_functional_verification(request_path)


def test_testbench_module_file_must_exist(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    with pytest.raises(FunctionalVerificationError, match="testbench module not found"):
        run_functional_verification(request_path)


@pytest.mark.parametrize("testcase", [7, [], [""], ""])
def test_testcase_shape_validation(tmp_path, testcase):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(testbench={"module": "test_gcd", "testcase": testcase}),
    )
    with pytest.raises(FunctionalVerificationError, match="testcase"):
        run_functional_verification(request_path)


def test_options_must_be_object(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options="coverage")
    )
    with pytest.raises(
        FunctionalVerificationError, match="request.options must be a JSON object"
    ):
        run_functional_verification(request_path)


def test_coverage_must_be_boolean(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"coverage": "yes"})
    )
    with pytest.raises(
        FunctionalVerificationError, match="options.coverage must be a boolean"
    ):
        run_functional_verification(request_path)


def test_coverage_with_icarus_is_rejected(tmp_path):
    """`options.coverage: true` + `engine: "icarus"` is an error (exit 1),
    never a silent no-op -- Icarus has no coverage path (spike section 1)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="icarus", options={"coverage": True}),
    )
    with pytest.raises(
        FunctionalVerificationError,
        match="options.coverage requires engine 'verilator'",
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize(
    "timescale", [["1ns"], ["1ns", "1ps", "1fs"], [1, 2], ["1ns", ""], "1ns"]
)
def test_timescale_shape_validation(tmp_path, timescale):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"timescale": timescale})
    )
    with pytest.raises(
        FunctionalVerificationError, match="timescale must be a \\[unit, precision\\]"
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("random_seed", [True, False, "1785780800", 1.5, [1]])
def test_random_seed_must_be_an_integer(tmp_path, random_seed):
    """`bool` is deliberately rejected too -- `isinstance(True, int)` is
    `True` in Python, so a naive `isinstance(..., int)` check would silently
    accept `options.random_seed: true` as seed `1`."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"random_seed": random_seed})
    )
    with pytest.raises(
        FunctionalVerificationError, match="random_seed must be an integer"
    ):
        run_functional_verification(request_path)


def test_missing_cocotb_is_an_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cocotb_tools", None)
    with pytest.raises(FunctionalVerificationError, match="cocotb is not installed"):
        fv._import_runner()


# --------------------------------------------------------------------------- #
# `results.xml` parsing -- the spike's own captured fixtures
# --------------------------------------------------------------------------- #


def test_parse_results_xml_skipped_entry(tmp_path):
    path = _write(tmp_path / "results.xml", _RESULTS_XML_WITH_SKIP)
    tests = parse_results_xml(path)

    assert [test["status"] for test in tests] == ["passed", "passed", "skipped"]
    assert tests[0]["name"] == "test_gcd_known_pairs"
    assert tests[0]["sim_time_ns"] == 520.0
    assert tests[0]["real_time_s"] == pytest.approx(0.005598783493041992)
    # `error_type`/`error_message` appear only on failures.
    assert "error_type" not in tests[0]
    assert "error_type" not in tests[2]


def test_parse_results_xml_failure_entry(tmp_path):
    path = _write(tmp_path / "results.xml", _RESULTS_XML_WITH_FAILURE)
    tests = parse_results_xml(path)

    assert [test["status"] for test in tests] == ["passed", "passed", "failed"]
    failed = tests[2]
    assert failed["error_type"] == "AssertionError"
    assert failed["error_message"].startswith("gcd(48, 18): got 6, want 999")
    assert failed["sim_time_ns"] == 120.0


def test_parse_results_xml_missing_file(tmp_path):
    with pytest.raises(FunctionalVerificationError, match="produced no results file"):
        parse_results_xml(str(tmp_path / "results.xml"))


def test_parse_results_xml_unparseable(tmp_path):
    path = _write(tmp_path / "results.xml", "<testsuites")
    with pytest.raises(FunctionalVerificationError, match="not parseable XML"):
        parse_results_xml(path)


def test_parse_results_xml_tolerates_missing_timing_attributes(tmp_path):
    path = _write(
        tmp_path / "results.xml",
        '<testsuites><testsuite name="all">'
        '<testcase name="t" time="not-a-number" /></testsuite></testsuites>',
    )
    tests = parse_results_xml(path)
    assert tests == [
        {"name": "t", "status": "passed", "sim_time_ns": None, "real_time_s": None}
    ]


# --------------------------------------------------------------------------- #
# `_extract_random_seed_property` -- the `environment.random_seed` source
# --------------------------------------------------------------------------- #


def test_extract_random_seed_property_reads_the_captured_value(tmp_path):
    """The exact `random_seed` value `docs/design/cocotb-verification-spike.md`
    section 4 captured live from a real run."""
    path = _write(tmp_path / "results.xml", _RESULTS_XML_WITH_SKIP)
    assert fv._extract_random_seed_property(path) == 1785780800


def test_extract_random_seed_property_missing_property_is_none(tmp_path):
    path = _write(
        tmp_path / "results.xml",
        '<testsuites><testsuite name="all">'
        '<testcase name="t" /></testsuite></testsuites>',
    )
    assert fv._extract_random_seed_property(path) is None


def test_extract_random_seed_property_missing_file_is_none(tmp_path):
    assert fv._extract_random_seed_property(str(tmp_path / "nope.xml")) is None


def test_extract_random_seed_property_unparseable_file_is_none(tmp_path):
    path = _write(tmp_path / "results.xml", "<testsuites")
    assert fv._extract_random_seed_property(path) is None


def test_extract_random_seed_property_non_integer_value_is_none(tmp_path):
    path = _write(
        tmp_path / "results.xml",
        '<testsuites><testsuite name="all">'
        '<property name="random_seed" value="not-an-int" />'
        "</testsuite></testsuites>",
    )
    assert fv._extract_random_seed_property(path) is None


# --------------------------------------------------------------------------- #
# Stubbed cocotb runner: full `run_functional_verification` without cocotb
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """Stands in for a `cocotb_tools.runner.Runner`, recording the exact
    kwargs the library passed and writing whatever `results.xml` the test
    asked for."""

    def __init__(
        self,
        results_xml_text,
        *,
        build_exc=None,
        test_exc=None,
        coverage_dat_text=None,
    ):
        self.results_xml_text = results_xml_text
        self.build_exc = build_exc
        self.test_exc = test_exc
        # When set, `test()` writes a fresh `coverage.dat` into `test_dir` --
        # standing in for Verilator flushing coverage data as part of *this*
        # run, the way `results_xml_text` stands in for `results.xml`.
        self.coverage_dat_text = coverage_dat_text
        self.build_kwargs = None
        self.test_kwargs = None

    def build(self, **kwargs):
        self.build_kwargs = kwargs
        Path(kwargs["build_dir"]).mkdir(parents=True, exist_ok=True)
        with open(kwargs["log_file"], "w", encoding="utf-8") as handle:
            handle.write("fake build log\nELABORATION DETAIL\n")
        if self.build_exc is not None:
            raise self.build_exc

    def test(self, **kwargs):
        self.test_kwargs = kwargs
        with open(kwargs["log_file"], "w", encoding="utf-8") as handle:
            handle.write("fake test log\n")
        if self.results_xml_text is not None:
            with open(kwargs["results_xml"], "w", encoding="utf-8") as handle:
                handle.write(self.results_xml_text)
        if self.coverage_dat_text is not None:
            coverage_dat = os.path.join(kwargs["test_dir"], "coverage.dat")
            with open(coverage_dat, "w", encoding="utf-8") as handle:
                handle.write(self.coverage_dat_text)
        if self.test_exc is not None:
            raise self.test_exc


def _stub_runner(monkeypatch, runner: _FakeRunner, *, engines=("icarus", "verilator")):
    def get_runner(name):
        assert name in engines
        return runner

    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=get_runner)
    )
    monkeypatch.setattr(fv, "_engine_version", lambda engine: "13.0")
    monkeypatch.setattr(fv, "_cocotb_version", lambda: "2.0.1")
    return runner


def test_stubbed_run_reports_the_full_contract_shape(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    report = run_functional_verification(request_path)

    assert report["schema_version"] == 1
    assert report["engine"] == "icarus"
    assert report["hdl_toplevel"] == "gcd"
    assert report["testbench"] == "test_gcd"
    assert report["status"] == "fail"
    assert report["test_count"] == 3
    assert report["passed_count"] == 2
    assert report["failed_count"] == 1
    assert report["skipped_count"] == 0
    assert report["coverage"] is None
    assert report["environment"] == {
        "engine": "icarus",
        "engine_version": "13.0",
        "cocotb_version": "2.0.1",
        "results_xml": report["environment"]["results_xml"],
        # `_RESULTS_XML_WITH_FAILURE` carries the exact `random_seed` value
        # the spike captured live (section 4) -- this is the authoritative
        # echo, read back from `results.xml` itself.
        "random_seed": 1785780800,
    }
    results_xml = report["environment"]["results_xml"]
    assert os.path.isabs(results_xml)
    assert os.path.isfile(results_xml)
    assert ".klt/functional-verification" in results_xml.replace(os.sep, "/")

    # The build/test kwargs the contract's gotchas are about.
    assert runner.build_kwargs["timescale"] == ("1ns", "1ps")
    assert runner.test_kwargs["timescale"] == ("1ns", "1ps")
    assert runner.build_kwargs["always"] is True
    assert runner.build_kwargs["build_args"] == []
    assert runner.build_kwargs["hdl_toplevel"] == "gcd"
    assert runner.test_kwargs["test_module"] == "test_gcd"
    assert runner.test_kwargs["testcase"] is None
    # No `options.random_seed` in the request -- cocotb generates its own,
    # so `Runner.test()`'s `seed` kwarg is left unset (`None`).
    assert runner.test_kwargs["seed"] is None


def test_stubbed_run_counts_skipped_tests_separately(tmp_path, monkeypatch):
    """The spike's own section-4 finding: `get_results()` would report
    `num_tests=3, num_failures=0` here, hiding the skip. The contract reports
    2 passed + 1 skipped."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert report["status"] == "pass"
    assert (report["test_count"], report["passed_count"], report["skipped_count"]) == (
        3,
        2,
        1,
    )


def test_stubbed_run_default_engine_and_timescale(tmp_path, monkeypatch):
    """`engine` and `options` are both optional: the default engine is
    `icarus` and the default timescale is `["1ns", "1ps"]` (spike section 7)."""
    _setup_inputs(tmp_path)
    request = _base_request()
    del request["engine"]
    del request["options"]
    request_path = _write_request(tmp_path / "request.json", request)
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert report["engine"] == "icarus"
    assert runner.build_kwargs["timescale"] == ("1ns", "1ps")
    assert runner.test_kwargs["timescale"] == ("1ns", "1ps")


def test_stubbed_run_passes_testcase_filter_through(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            testbench={"module": "test_gcd", "testcase": ["test_gcd_known_pairs"]}
        ),
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.test_kwargs["testcase"] == ["test_gcd_known_pairs"]


def test_stubbed_run_pins_random_seed_end_to_end(tmp_path, monkeypatch):
    """`options.random_seed` -> `Runner.test()`'s own `seed` kwarg (which
    cocotb turns into `COCOTB_RANDOM_SEED` in the simulator subprocess's
    environment) -> echoed back in `environment.random_seed` -- issue #423's
    request-field-to-response-echo reproducibility chain, full stack."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"coverage": False, "random_seed": 42}),
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    report = run_functional_verification(request_path)

    assert runner.test_kwargs["seed"] == 42
    # The stubbed `results.xml` fixture's own `<property>` value is echoed
    # back verbatim -- this module never trusts the *request* value as the
    # response's `random_seed`, only what `results.xml` itself reports (the
    # same "results.xml is the only source of truth" discipline the rest of
    # this contract already follows).
    assert report["environment"]["random_seed"] == 1785780800


def test_stubbed_run_without_random_seed_leaves_seed_kwarg_unset(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.test_kwargs["seed"] is None


def test_stubbed_run_swallows_the_simulators_exit_code(tmp_path, monkeypatch):
    """A `SystemExit` out of `Runner.test()` means only "the simulator
    returned nonzero" -- the verdict still comes from `results.xml` (spike
    section 4's central finding)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(
        monkeypatch,
        _FakeRunner(_RESULTS_XML_WITH_FAILURE, test_exc=SystemExit(1)),
    )

    report = run_functional_verification(request_path)

    assert report["status"] == "fail"
    assert report["failed_count"] == 1


def test_stubbed_run_without_results_xml_is_an_error(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(None))

    with pytest.raises(FunctionalVerificationError, match="produced no results file"):
        run_functional_verification(request_path)


def test_stubbed_run_ignores_a_stale_results_xml(tmp_path, monkeypatch):
    """A previous run's `results.xml` must never be reported as this run's
    evidence when the current run produces none."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    stale_dir = tmp_path / ".klt" / "functional-verification"
    stale_dir.mkdir(parents=True)
    _write(stale_dir / "results_icarus.xml", _RESULTS_XML_WITH_SKIP)
    _stub_runner(monkeypatch, _FakeRunner(None))

    with pytest.raises(FunctionalVerificationError, match="produced no results file"):
        run_functional_verification(request_path)


def test_stubbed_run_zero_registered_tests(tmp_path, monkeypatch):
    """A testbench with no `@cocotb.test()` functions verified nothing, so it
    is a failed-to-run error (exit 1) -- never a vacuous `status: "pass"`
    that would hand `klt eval` a `valid: true` for an unchecked design. No
    divide-by-zero, no empty-report success."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_EMPTY))

    with pytest.raises(FunctionalVerificationError, match="registered no tests"):
        run_functional_verification(request_path)


def test_stubbed_build_failure_reports_the_engine_log_tail(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            build_exc=subprocess.CalledProcessError(1, ["iverilog"]),
        ),
    )

    with pytest.raises(
        FunctionalVerificationError,
        match=r"icarus build/elaboration failed \(exit 1\).*ELABORATION DETAIL",
    ):
        run_functional_verification(request_path)


def test_stubbed_missing_simulator_binary(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())

    def get_runner(name):
        raise FileNotFoundError("no such file: iverilog")

    monkeypatch.setattr(
        fv, "_import_runner", lambda: types.SimpleNamespace(get_runner=get_runner)
    )

    with pytest.raises(
        FunctionalVerificationError, match="could not initialise the 'icarus' simulator"
    ):
        run_functional_verification(request_path)


# --------------------------------------------------------------------------- #
# Coverage post-processing (Verilator only)
# --------------------------------------------------------------------------- #

_COVERAGE_SUMMARY = """\
Coverage Summary:
  line      : 100.0% (  6/  6)
  toggle    : 60.0% (102/170)
  branch    : 100.0% (  4/  4)
  expr      : 100.0% (  5/  5)
  fsm_state : 0.0% (  0/  0)
  fsm_arc   : 0.0% (  0/  0)
"""


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_verilator_coverage(monkeypatch, *, summary=_COVERAGE_SUMMARY, returncode=0):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "verilator_coverage"
        if cmd[1] == "--write-info":
            with open(cmd[2], "w", encoding="utf-8") as handle:
                handle.write("TN:verilator_coverage\nSF:gcd.v\nDA:15,538\n")
            return _FakeCompleted(returncode=returncode)
        return _FakeCompleted(returncode=returncode, stdout=summary)

    monkeypatch.setattr(fv.subprocess, "run", fake_run)


def test_stubbed_coverage_run_populates_the_coverage_block(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="verilator", options={"coverage": True}),
    )
    # Verilator writes coverage.dat into the simulation's working directory
    # as part of `test()` -- the fake runner mirrors that here (rather than
    # the file existing before the run) so the staleness-removal step ahead
    # of `_run_test()` doesn't delete it out from under this test.
    runner = _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_FAILURE,
            coverage_dat_text="# fake verilator coverage\n",
        ),
    )
    _stub_verilator_coverage(monkeypatch)

    report = run_functional_verification(request_path)

    assert runner.build_kwargs["build_args"] == ["--coverage", "--trace"]
    coverage = report["coverage"]
    assert coverage["line_pct"] == 100.0
    assert coverage["toggle_pct"] == 60.0
    assert coverage["branch_pct"] == 100.0
    assert coverage["expr_pct"] == 100.0
    assert coverage["info_path"].endswith("coverage.info")
    assert os.path.isfile(coverage["info_path"])


def test_coverage_requested_but_no_coverage_dat(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="verilator", options={"coverage": True}),
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    with pytest.raises(FunctionalVerificationError, match="produced no coverage.dat"):
        run_functional_verification(request_path)


def test_stubbed_run_ignores_a_stale_coverage_dat(tmp_path, monkeypatch):
    """A previous run's `coverage.dat` must never be reported as this run's
    coverage evidence when the current run's test process crashes after
    `results.xml` is written but before a fresh `coverage.dat` is flushed --
    mirrors `test_stubbed_run_ignores_a_stale_results_xml` above, for the
    equivalent `coverage.dat` staleness guard."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="verilator", options={"coverage": True}),
    )
    output_dir = tmp_path / ".klt" / "functional-verification"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "coverage.dat", "# stale verilator coverage from a prior run\n")
    # The stubbed runner writes results.xml (the simulation completed) but
    # never writes a fresh coverage.dat, simulating the crash window between
    # results.xml being flushed and coverage.dat being flushed.
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    with pytest.raises(FunctionalVerificationError, match="produced no coverage.dat"):
        run_functional_verification(request_path)


def test_coverage_tool_failure_is_an_error(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="verilator", options={"coverage": True}),
    )
    _stub_runner(
        monkeypatch,
        _FakeRunner(_RESULTS_XML_WITH_SKIP, coverage_dat_text="# fake\n"),
    )

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: verilator_coverage")

    monkeypatch.setattr(fv.subprocess, "run", fake_run)

    with pytest.raises(
        FunctionalVerificationError, match="could not launch verilator_coverage"
    ):
        run_functional_verification(request_path)


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_pass_exits_zero_json(tmp_path, monkeypatch, capsys):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    exit_code = main(["functional-verification", request_path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pass"
    assert out["skipped_count"] == 1


def test_cli_test_failure_exits_three(tmp_path, monkeypatch, capsys):
    """`status: "fail"` -> exit 3, the `valid: false` half of the `klt eval`
    boundary (spike section 8) -- deliberately not exit 1, which means
    "never ran"."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    exit_code = main(["functional-verification", request_path, "--format", "json"])

    assert exit_code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "fail"
    assert out["tests"][2]["error_type"] == "AssertionError"


def test_cli_text_format(tmp_path, monkeypatch, capsys):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    exit_code = main(["functional-verification", request_path])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "status: fail" in out
    assert "[failed] test_gcd_deliberately_wrong_expectation" in out
    assert "AssertionError" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_error_exits_one_with_json_error(tmp_path, capsys):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )

    exit_code = main(["functional-verification", request_path, "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "functional-verification"
    assert "RTL source not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, capsys):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(sources=["nope.v"])
    )

    exit_code = main(["functional-verification", request_path])

    assert exit_code == 1
    assert capsys.readouterr().err.startswith("klt functional-verification:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["functional-verification"])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# `klt eval` boundary: status "pass"/"fail" -> valid true/false
# --------------------------------------------------------------------------- #


def _eval_descriptor(request_path: str) -> dict:
    return {
        "gates": [
            {
                "check": "functional-verification",
                "name": "functional",
                "args": {"request": request_path},
            }
        ],
        "objective": {
            "check": "functional-verification",
            "metric": "failed_count",
            "polarity": "minimize",
            "args": {"request": request_path},
        },
    }


def test_eval_consumes_a_passing_run_as_valid_true(tmp_path, monkeypatch):
    from klayout_tools.eval import run_eval

    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    result = run_eval(json.dumps(_eval_descriptor(request_path)))

    assert result["valid"] is True
    assert result["gates"][0] == {
        "check": "functional-verification",
        "name": "functional",
        "status": "pass",
        "exit_code": 0,
        "count": 0,
    }
    assert result["objective"]["value"] == 0


def test_eval_consumes_a_failing_run_as_valid_false(tmp_path, monkeypatch):
    from klayout_tools.eval import run_eval

    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    result = run_eval(json.dumps(_eval_descriptor(request_path)))

    assert result["valid"] is False
    assert result["gates"][0]["status"] == "fail"
    assert result["gates"][0]["exit_code"] == 3
    assert result["gates"][0]["count"] == 1


def test_eval_surfaces_a_failed_to_run_verification_as_an_error(tmp_path, monkeypatch):
    """A run that never produced evidence must reach `klt eval` as its own
    exit 1 (EvalError), never as a `valid: false` score (issue #387)."""
    from klayout_tools.eval import EvalError, run_eval

    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(None))

    with pytest.raises(EvalError, match="failed to run"):
        run_eval(json.dumps(_eval_descriptor(request_path)))


# --------------------------------------------------------------------------- #
# Integration: real cocotb + a real simulator (skipped when either is absent)
# --------------------------------------------------------------------------- #


def _have_cocotb() -> bool:
    try:
        import cocotb_tools.runner  # noqa: F401
    except Exception:
        return False
    return True


HAVE_COCOTB = _have_cocotb()
HAVE_SIMULATOR = {
    "icarus": shutil.which("iverilog") is not None,
    "verilator": shutil.which("verilator") is not None
    and shutil.which("verilator_coverage") is not None,
}


def _stage_example(tmp_path: Path, request_name: str) -> str:
    """Copy the checked-in worked example into `tmp_path` so the run's own
    `.klt/functional-verification/` artifacts never land in the repo."""
    for name in ("gcd.v", "test_gcd.py", request_name):
        shutil.copy(EXAMPLE_DIR / name, tmp_path / name)
    return str(tmp_path / request_name)


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_gcd_worked_example(tmp_path):
    """The GCD worked example (spike section 6) end to end against real
    cocotb + Icarus: `TESTS=3 PASS=2 FAIL=1 SKIP=0`, exit 3."""
    request_path = _stage_example(tmp_path, "request.json")

    report = run_functional_verification(request_path)

    assert report["engine"] == "icarus"
    assert report["status"] == "fail"
    assert (
        report["test_count"],
        report["passed_count"],
        report["failed_count"],
        report["skipped_count"],
    ) == (3, 2, 1, 0)
    failed = [test for test in report["tests"] if test["status"] == "failed"]
    assert failed[0]["name"] == "test_gcd_deliberately_wrong_expectation"
    assert failed[0]["error_type"] == "AssertionError"
    assert "want 999" in failed[0]["error_message"]
    assert report["coverage"] is None
    assert report["environment"]["cocotb_version"]
    assert report["environment"]["engine_version"]
    assert os.path.isfile(report["environment"]["results_xml"])


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["verilator"],
    reason="verilator/verilator_coverage is not installed on this machine",
)
def test_integration_real_verilator_gcd_worked_example_with_coverage(tmp_path):
    """The *same* testbench against Verilator reproduces the identical
    pass/fail structure (the spike's own differential-oracle check) and adds
    the coverage block Icarus cannot produce."""
    request_path = _stage_example(tmp_path, "request-verilator-coverage.json")

    report = run_functional_verification(request_path)

    assert report["engine"] == "verilator"
    assert report["status"] == "fail"
    assert (
        report["test_count"],
        report["passed_count"],
        report["failed_count"],
        report["skipped_count"],
    ) == (3, 2, 1, 0)

    coverage = report["coverage"]
    assert coverage is not None
    assert coverage["line_pct"] == 100.0
    assert coverage["branch_pct"] == 100.0
    assert coverage["expr_pct"] == 100.0
    # Toggle coverage is deliberately partial: the random test only drives
    # values up to 500 through a 16-bit datapath (spike section 6).
    assert 0.0 < coverage["toggle_pct"] < 100.0
    assert os.path.isfile(coverage["info_path"])
    with open(coverage["info_path"], encoding="utf-8") as handle:
        assert handle.readline().startswith("TN:")


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_testcase_filter_passes(tmp_path):
    """Filtering out the deliberately-failing test flips the same example to
    `status: "pass"` / exit 0 -- the `valid: true` half of the gate."""
    _stage_example(tmp_path, "request.json")
    request = json.loads((tmp_path / "request.json").read_text())
    request["testbench"]["testcase"] = [
        "test_gcd_known_pairs",
        "test_gcd_random_pairs",
    ]
    request_path = _write_request(tmp_path / "request-filtered.json", request)

    report = run_functional_verification(request_path)

    assert report["status"] == "pass"
    assert report["failed_count"] == 0
    assert report["passed_count"] == 2
    # cocotb still lists the filtered-out test, as a skip -- the exact
    # `get_results()` undercount the contract's own counts avoid.
    assert report["skipped_count"] == 1
    assert report["test_count"] == 3


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_random_seed_is_pinned_and_echoed(tmp_path):
    """`options.random_seed` pinned end to end against a real cocotb +
    Icarus run: the pinned value reaches `COCOTB_RANDOM_SEED` (via
    `Runner.test()`'s own `seed` parameter) and is echoed back verbatim in
    `environment.random_seed`, read from `results.xml`'s own
    `<property name="random_seed">` -- issue #423's `random_seed`
    reproducibility acceptance criterion, exercised against the real
    toolchain rather than the stubbed runner."""
    request_path = _stage_example(tmp_path, "request.json")
    request = json.loads(Path(request_path).read_text())
    request["options"]["random_seed"] = 424242
    request_path = _write_request(tmp_path / "request-seeded.json", request)

    report = run_functional_verification(request_path)

    assert report["environment"]["random_seed"] == 424242
    # Same pass/fail structure the unseeded worked example produces -- pinning
    # a seed changes cocotb's own randomized-test bookkeeping, never this
    # design's deterministic (per-test-fixed-seed) stimulus or verdict.
    assert (
        report["test_count"],
        report["passed_count"],
        report["failed_count"],
        report["skipped_count"],
    ) == (3, 2, 1, 0)


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_same_seed_reproduces_identical_results(tmp_path):
    """Two runs of the same seeded request produce identical `tests[]`
    content -- issue #423's own edge-case test plan ("confirm two CI runs of
    the same seeded request produce identical `tests[]`
    ordering/timing-independent results"). Compares everything except the
    inherently timing-dependent `real_time_s` (wall-clock, never contracted
    to be reproducible)."""
    request_path = _stage_example(tmp_path, "request.json")
    request = json.loads(Path(request_path).read_text())
    request["options"]["random_seed"] = 13
    request_path = _write_request(tmp_path / "request-seeded.json", request)

    def _strip_timing(report):
        return [
            {key: value for key, value in test.items() if key != "real_time_s"}
            for test in report["tests"]
        ]

    first = run_functional_verification(request_path)
    second = run_functional_verification(request_path)

    assert first["environment"]["random_seed"] == 13
    assert second["environment"]["random_seed"] == 13
    assert _strip_timing(first) == _strip_timing(second)


# Sanity: `subprocess` really is the module this file's coverage stubs patch
# (guards against a future refactor silently making them a no-op).
def test_functional_verification_uses_stdlib_subprocess():
    assert fv.subprocess is subprocess
