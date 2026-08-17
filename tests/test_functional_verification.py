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

from helpers.cocotb_fakes import FakeCocotbRunner as _FakeRunner
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


def test_testbench_search_path_must_be_a_nonempty_string(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(testbench={"module": "test_gcd", "search_path": ""}),
    )
    with pytest.raises(
        FunctionalVerificationError, match="search_path must be a non-empty string"
    ):
        run_functional_verification(request_path)


def test_testbench_search_path_relative_resolves_against_request_dir(tmp_path):
    """`testbench.search_path` follows the same absolute-or-relative-to-the-
    request convention `sources` entries already use (issue #1003) -- a
    testbench that lives in a sibling directory is found without being
    copied or symlinked next to the request."""
    shared = tmp_path / "shared-testbenches"
    shared.mkdir()
    _write(shared / "test_gcd.py", _TESTBENCH)
    module, module_dir, _testcase = fv._resolve_testbench(
        {"module": "test_gcd", "search_path": "shared-testbenches"}, str(tmp_path)
    )
    assert module == "test_gcd"
    assert module_dir == str(shared.resolve())


def test_testbench_search_path_absolute(tmp_path):
    _write(tmp_path / "gcd.v", _GCD_RTL)
    shared = tmp_path / "elsewhere"
    shared.mkdir()
    _write(shared / "test_gcd.py", _TESTBENCH)
    module, module_dir, _testcase = fv._resolve_testbench(
        {"module": "test_gcd", "search_path": str(shared)}, str(tmp_path)
    )
    assert module == "test_gcd"
    assert module_dir == str(shared.resolve())


def test_testbench_search_path_missing_module_names_resolved_location(tmp_path):
    """The error names the resolved `search_path` location, not always
    `request_dir` (issue #1003's third acceptance criterion)."""
    _write(tmp_path / "gcd.v", _GCD_RTL)
    (tmp_path / "search-here").mkdir()
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(testbench={"module": "test_gcd", "search_path": "search-here"}),
    )
    with pytest.raises(
        FunctionalVerificationError,
        match=(
            r"testbench module not found: expected '.*search-here.*test_gcd\.py'"
            r".*request\.testbench\.search_path"
        ),
    ):
        run_functional_verification(request_path)


def test_testbench_search_path_omitted_resolves_next_to_request(tmp_path):
    """Omitting `search_path` is fully additive -- behavior is byte-for-byte
    unchanged from before this key existed."""
    _setup_inputs(tmp_path)
    module, module_dir, _testcase = fv._resolve_testbench(
        {"module": "test_gcd"}, str(tmp_path)
    )
    assert module == "test_gcd"
    assert module_dir == str(tmp_path.resolve())


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


@pytest.mark.parametrize("defines", ["USE_POWER_PINS", ["USE_POWER_PINS"], 7])
def test_defines_must_be_object(tmp_path, defines):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"defines": defines})
    )
    with pytest.raises(
        FunctionalVerificationError,
        match=r"options.defines must be a JSON object of string -> string\|null",
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("value", [7, 1.5, True, [], {}])
def test_defines_values_must_be_string_or_null(tmp_path, value):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"defines": {"FUNCTIONAL": value}}),
    )
    with pytest.raises(
        FunctionalVerificationError, match=r"options.defines\['FUNCTIONAL'\]"
    ):
        run_functional_verification(request_path)


def test_defines_keys_must_be_nonempty_strings(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"defines": {"": None}})
    )
    with pytest.raises(
        FunctionalVerificationError, match="options.defines keys must be"
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("build_args", ["--foo", [7], [""], {"a": 1}])
def test_build_args_shape_validation(tmp_path, build_args):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"build_args": build_args})
    )
    with pytest.raises(
        FunctionalVerificationError,
        match="options.build_args must be an array of non-empty strings",
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("includes", ["cells", [7], [""], {"a": 1}])
def test_includes_shape_validation(tmp_path, includes):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"includes": includes})
    )
    with pytest.raises(
        FunctionalVerificationError,
        match="options.includes must be an array of non-empty strings",
    ):
        run_functional_verification(request_path)


def test_includes_directory_must_exist(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"includes": ["no-such-dir"]})
    )
    with pytest.raises(
        FunctionalVerificationError,
        match="include directory not found: no-such-dir",
    ):
        run_functional_verification(request_path)


def test_parameters_must_be_object(tmp_path):
    """A list (or any non-object) `request.parameters` is a clear validation
    error -- not a raw exception surfacing from the cocotb runner."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(parameters=["WIDTH", 16])
    )
    with pytest.raises(
        FunctionalVerificationError, match="request.parameters must be a JSON object"
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("value", [[16], {"nested": 1}, None])
def test_parameters_values_must_be_scalars(tmp_path, value):
    """Each `request.parameters` value must be a scalar (int/float/string/
    bool) -- an array, object, or null is rejected up front."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(parameters={"WIDTH": value})
    )
    with pytest.raises(FunctionalVerificationError, match="must be a scalar"):
        run_functional_verification(request_path)


def test_missing_cocotb_is_an_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cocotb_tools", None)
    with pytest.raises(FunctionalVerificationError, match="cocotb is not installed"):
        fv._import_runner()


# --------------------------------------------------------------------------- #
# `_check_cocotb_abi_compatibility` -- the WHEEL-tag pre-flight check
# (issue #1103): `import cocotb_tools.runner` succeeds fine even when the
# installed cocotb wheel's compiled ABI tag doesn't match the running
# interpreter -- the mismatch only manifests once the simulator subprocess
# `dlopen`s cocotb's VPI module. These tests use a synthetic distribution
# double rather than a genuinely ABI-mismatched cocotb install.
# --------------------------------------------------------------------------- #


class _FakeCocotbDistribution:
    """Stands in for `importlib.metadata.Distribution` -- just enough surface
    for `_check_cocotb_abi_compatibility` to read a synthetic `WHEEL` file."""

    def __init__(self, wheel_text):
        self._wheel_text = wheel_text

    def read_text(self, filename):
        assert filename == "WHEEL"
        return self._wheel_text


def _stub_cocotb_distribution(monkeypatch, wheel_text, *, version="2.0.1"):
    monkeypatch.setattr(
        fv.importlib.metadata,
        "distribution",
        lambda name: _FakeCocotbDistribution(wheel_text),
    )
    monkeypatch.setattr(fv.importlib.metadata, "version", lambda name: version)


def test_cocotb_abi_mismatch_is_an_actionable_error(monkeypatch):
    """A `cp27` wheel tag can never match this repo's `requires-python >=
    3.10` floor -- a deliberately unmistakable mismatch, so the test doesn't
    depend on which CPython minor version actually runs it."""
    _stub_cocotb_distribution(
        monkeypatch,
        "Wheel-Version: 1.0\nTag: cp27-cp27mu-manylinux_2_17_x86_64\n",
        version="2.0.1",
    )

    with pytest.raises(
        FunctionalVerificationError,
        match=r"cocotb 2\.0\.1 is installed as a cp27-cp27mu-manylinux_2_17_x86_64 "
        r"wheel but klt is running on CPython",
    ):
        fv._check_cocotb_abi_compatibility()


def test_cocotb_abi_match_raises_nothing(monkeypatch):
    """The matching case must be silent -- this check is a pre-flight guard,
    not a report."""
    interpreter_tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    tag = f"{interpreter_tag}-{interpreter_tag}-manylinux_2_17_x86_64"
    _stub_cocotb_distribution(monkeypatch, f"Wheel-Version: 1.0\nTag: {tag}\n")

    fv._check_cocotb_abi_compatibility()  # must not raise


@pytest.mark.parametrize(
    "wheel_text",
    [
        "",
        "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n",  # no `Tag:` lines
        "not even WHEEL-shaped metadata",
    ],
)
def test_cocotb_abi_check_degrades_gracefully_on_malformed_wheel_metadata(
    monkeypatch, wheel_text
):
    """Missing/malformed `WHEEL` metadata must skip the check, never crash
    `klt` -- the same discipline `_engine_version()`/`_cocotb_version()`
    already follow for their own probes."""
    _stub_cocotb_distribution(monkeypatch, wheel_text)

    fv._check_cocotb_abi_compatibility()  # must not raise


def test_cocotb_abi_check_degrades_gracefully_when_cocotb_is_not_installed(
    monkeypatch,
):
    """`importlib.metadata.distribution("cocotb")` raising
    `PackageNotFoundError` (or any other lookup failure) must not itself
    crash `klt` -- callers reach this check only after `_import_runner()`
    already succeeded, but the check must not assume that invariant."""

    def _raise(name):
        raise fv.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(fv.importlib.metadata, "distribution", _raise)

    fv._check_cocotb_abi_compatibility()  # must not raise


def test_cocotb_abi_check_runs_before_the_build_step(tmp_path, monkeypatch):
    """The pre-flight check (issue #1103) must reject an ABI-mismatched
    cocotb before the runner is ever invoked -- not surface as a confusing
    downstream build/test failure four layers down."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    _stub_cocotb_distribution(
        monkeypatch, "Wheel-Version: 1.0\nTag: cp27-cp27mu-manylinux_2_17_x86_64\n"
    )

    with pytest.raises(FunctionalVerificationError, match="cp27-cp27mu"):
        run_functional_verification(request_path)

    assert runner.build_kwargs is None


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


# --------------------------------------------------------------------------- #
# `_scan_interpreter_mismatch_diagnostics` -- the transcript-scan fallback
# (issue #1103), modeled directly on `_scan_sdf_diagnostics`.
# --------------------------------------------------------------------------- #


def test_scan_interpreter_mismatch_diagnostics_finds_the_gpi_marker(tmp_path):
    log = _write(
        tmp_path / "test.log",
        "INFO  gpi ... Using Python 3.12.10 interpreter at <venv>/bin/python\n"
        "ERROR gpi ... Unexpected sys.executable value (expected "
        "'<venv>/bin/python', got '.../vvp')\n",
    )
    result = fv._scan_interpreter_mismatch_diagnostics(log)
    assert result is not None
    assert "Unexpected sys.executable value" in result


def test_scan_interpreter_mismatch_diagnostics_finds_the_embed_init_marker(tmp_path):
    log = _write(tmp_path / "test.log", "FATAL: _embed_init_python failed\n")
    result = fv._scan_interpreter_mismatch_diagnostics(log)
    assert result is not None
    assert "_embed_init_python" in result


def test_scan_interpreter_mismatch_diagnostics_none_when_absent(tmp_path):
    log = _write(tmp_path / "test.log", "fake test log\nTESTS=3 PASS=3 FAIL=0\n")
    assert fv._scan_interpreter_mismatch_diagnostics(log) is None


def test_scan_interpreter_mismatch_diagnostics_tolerates_a_missing_log(tmp_path):
    """Never raises -- a missing/unreadable transcript contributes nothing,
    the same posture `_scan_sdf_diagnostics`/`_log_tail` already take."""
    assert fv._scan_interpreter_mismatch_diagnostics(str(tmp_path / "nope.log")) is None


def test_scan_interpreter_mismatch_diagnostics_checks_every_log_in_order(tmp_path):
    build_log = _write(tmp_path / "build.log", "fake build log\n")
    test_log = _write(
        tmp_path / "test.log",
        "ERROR gpi ... Unexpected sys.executable value (mismatch)\n",
    )
    result = fv._scan_interpreter_mismatch_diagnostics(build_log, test_log)
    assert result is not None
    assert "Unexpected sys.executable value" in result


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
        # Additive (issue #1002): `null` on any run without `options.sdf`,
        # so an unannotated verdict is never mistakable for an annotated one.
        "sdf": None,
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


def test_stubbed_run_honors_testbench_search_path(tmp_path, monkeypatch):
    """Two requests, in different directories, both reach the *same* shared
    testbench file via `testbench.search_path` -- the motivating workflow
    from issue #1003 (one unmodified testbench, several views of a design)."""
    shared = tmp_path / "shared-testbenches"
    shared.mkdir()
    _write(shared / "test_gcd.py", _TESTBENCH)

    rtl_dir = tmp_path / "rtl-check"
    rtl_dir.mkdir()
    _write(rtl_dir / "gcd.v", _GCD_RTL)
    rtl_request_path = _write_request(
        rtl_dir / "request.json",
        _base_request(
            testbench={"module": "test_gcd", "search_path": "../shared-testbenches"}
        ),
    )

    netlist_dir = tmp_path / "netlist-check"
    netlist_dir.mkdir()
    _write(netlist_dir / "gcd.v", _GCD_RTL)
    netlist_request_path = _write_request(
        netlist_dir / "request.json",
        _base_request(
            testbench={"module": "test_gcd", "search_path": "../shared-testbenches"}
        ),
    )

    for request_path in (rtl_request_path, netlist_request_path):
        runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
        # `sys.path`/`PYTHONPATH` resolution is only in effect for the
        # duration of `Runner.test()` (`_run_test`'s try/finally) -- capture
        # it from inside the fake `test()` call, not after
        # `run_functional_verification` has already returned.
        captured_sys_path: list[str] = []
        original_test = runner.test

        def _capturing_test(
            *, _original=original_test, _captured=captured_sys_path, **kwargs
        ):
            _captured.append(list(sys.path))
            return _original(**kwargs)

        monkeypatch.setattr(runner, "test", _capturing_test)

        report = run_functional_verification(request_path)

        assert report["testbench"] == "test_gcd"
        assert runner.test_kwargs["test_module"] == "test_gcd"
        # Both requests' testbench resolution used the one shared directory,
        # not either request's own directory.
        assert str(shared.resolve()) in captured_sys_path[0]


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


def test_stubbed_run_forwards_parameters_to_build_and_test(tmp_path, monkeypatch):
    """`request.parameters` -> both `Runner.build(parameters=...)` and
    `Runner.test(parameters=...)`, forwarded verbatim -- cocotb's own
    per-simulator backend (Icarus `-P`, Verilator `-G`) does the flag
    translation, so this module only needs to pass the mapping through."""
    _setup_inputs(tmp_path)
    parameters = {"WIDTH": 8, "ENABLE": True, "NAME": "x", "RATIO": 1.5}
    request_path = _write_request(
        tmp_path / "request.json", _base_request(parameters=parameters)
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["parameters"] == parameters
    assert runner.test_kwargs["parameters"] == parameters


def test_stubbed_run_without_parameters_forwards_an_empty_mapping(
    tmp_path, monkeypatch
):
    """No `request.parameters` in the request -- an empty mapping is
    forwarded to both calls, matching today's no-override behavior exactly
    (fully backward compatible with existing committed requests)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["parameters"] == {}
    assert runner.test_kwargs["parameters"] == {}


def test_stubbed_run_forwards_defines_and_includes_to_build(tmp_path, monkeypatch):
    """`options.defines`/`options.includes` reach `Runner.build()` verbatim --
    the generic mechanism this issue adds in place of the "defines file
    listed first in `sources`" preprocessing-order workaround."""
    _setup_inputs(tmp_path)
    include_dir = tmp_path / "cells"
    include_dir.mkdir()
    defines = {"USE_POWER_PINS": None, "FUNCTIONAL": "1"}
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"defines": defines, "includes": ["cells"]}),
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["defines"] == defines
    assert runner.build_kwargs["includes"] == [str(include_dir.resolve())]


def test_stubbed_run_without_defines_or_includes_forwards_empty(tmp_path, monkeypatch):
    """No `options.defines`/`options.includes`/`options.build_args` in the
    request -- `Runner.build()` receives empty defaults, byte-identical to
    today's behavior for existing requests (regression guard)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["defines"] == {}
    assert runner.build_kwargs["includes"] == []
    assert runner.build_kwargs["build_args"] == []


def test_stubbed_run_forwards_build_args(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"build_args": ["-Wall", "-Wno-timescale"]}),
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["build_args"] == ["-Wall", "-Wno-timescale"]


def test_stubbed_coverage_run_composes_build_args_with_coverage_args(
    tmp_path, monkeypatch
):
    """`options.coverage: true` + `options.build_args` -- the effective args
    are `COVERAGE_BUILD_ARGS + options.build_args`, in that order, so a
    user-supplied flag can still override a coverage default (see
    `_resolve_build_args`'s docstring and this module's merge-order note)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            engine="verilator",
            options={"coverage": True, "build_args": ["--assert"]},
        ),
    )
    runner = _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            coverage_dat_text="# fake verilator coverage\n",
        ),
    )
    _stub_verilator_coverage(monkeypatch)

    run_functional_verification(request_path)

    assert runner.build_kwargs["build_args"] == ["--coverage", "--trace", "--assert"]


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


def test_stubbed_run_without_results_xml_keeps_the_original_message_when_clean(
    tmp_path, monkeypatch
):
    """Regression guard (issue #1103's own edge case): a genuinely missing
    `results.xml` with *no* matching transcript diagnostic must keep the
    original, unaugmented "did not run to completion" message -- not an
    unsupported probable-cause guess."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(None))

    with pytest.raises(FunctionalVerificationError) as excinfo:
        run_functional_verification(request_path)

    assert "did not run to completion" in str(excinfo.value)
    assert "probable cause" not in str(excinfo.value)


def test_stubbed_run_without_results_xml_names_an_interpreter_mismatch(
    tmp_path, monkeypatch
):
    """The transcript-scan fallback (issue #1103): when a run produces no
    `results.xml` *and* the test transcript carries cocotb's own ABI-mismatch
    signature (`libembed`'s "Unexpected sys.executable value"), that line is
    folded into the raised error as a probable cause -- the catch-all for
    whatever the WHEEL-tag pre-flight check doesn't cover."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            None,
            extra_test_log=(
                "INFO  gpi ... Using Python 3.12.10 interpreter at "
                "<venv>/bin/python\n"
                "ERROR gpi ... Unexpected sys.executable value (expected "
                "'<venv>/bin/python', got '.../vvp')\n"
            ),
        ),
    )

    with pytest.raises(
        FunctionalVerificationError, match="produced no results file"
    ) as excinfo:
        run_functional_verification(request_path)

    assert "probable cause" in str(excinfo.value)
    assert "Unexpected sys.executable value" in str(excinfo.value)


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
# `options.sdf` (issue #1002): SDF back-annotation through Icarus's own
# `$sdf_annotate`, per `docs/design/sdf-annotate-feasibility-spike.md`.
# --------------------------------------------------------------------------- #

_MINIMAL_SDF = """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "gcd")
  (DIVIDER .)
  (TIMESCALE 1ns)
)
"""


def _sdf_request(tmp_path: Path, **sdf_fields) -> str:
    """A `_base_request()` carrying an `options.sdf` block plus the SDF file
    it names (request validation resolves and reads it)."""
    _setup_inputs(tmp_path)
    _write(tmp_path / "route.sdf", _MINIMAL_SDF)
    sdf = {"file": "route.sdf"}
    sdf.update(sdf_fields)
    return _write_request(
        tmp_path / "request.json",
        _base_request(options={"coverage": False, "sdf": sdf}),
    )


def test_sdf_option_must_be_an_object(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"sdf": "route.sdf"})
    )
    with pytest.raises(
        FunctionalVerificationError, match="options.sdf must be a JSON object"
    ):
        run_functional_verification(request_path)


def test_sdf_option_on_verilator_is_a_request_error(tmp_path):
    """`options.sdf` + `engine: "verilator"` is rejected up front, never a
    silent no-op -- the exact posture `options.coverage` + `icarus` already
    takes. Verilator has no `$sdf_annotate` path at all (spike section 4.1),
    so silently ignoring the block would report a zero-delay verdict as if it
    were an annotated one."""
    _setup_inputs(tmp_path)
    _write(tmp_path / "route.sdf", _MINIMAL_SDF)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(engine="verilator", options={"sdf": {"file": "route.sdf"}}),
    )
    with pytest.raises(
        FunctionalVerificationError, match="options.sdf requires engine 'icarus'"
    ):
        run_functional_verification(request_path)


def test_sdf_option_rejects_unknown_fields(tmp_path):
    """A typo'd key would otherwise degrade to a silently different run (the
    wrong corner, or no annotation)."""
    _setup_inputs(tmp_path)
    _write(tmp_path / "route.sdf", _MINIMAL_SDF)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"sdf": {"file": "route.sdf", "corners": "max"}}),
    )
    with pytest.raises(
        FunctionalVerificationError, match="unknown field\\(s\\): corners"
    ):
        run_functional_verification(request_path)


def test_sdf_option_file_must_be_a_nonempty_string(tmp_path):
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json", _base_request(options={"sdf": {}})
    )
    with pytest.raises(
        FunctionalVerificationError, match="options.sdf.file must be a non-empty string"
    ):
        run_functional_verification(request_path)


def test_sdf_option_missing_file_is_a_request_error(tmp_path):
    """An unopenable SDF is `SDF WARNING: ... Skipping this annotation.` at
    run time -- non-fatal to `vvp`, so it must be caught here instead."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"sdf": {"file": "nope.sdf"}}),
    )
    with pytest.raises(FunctionalVerificationError, match="SDF file not found"):
        run_functional_verification(request_path)


@pytest.mark.parametrize("corner", ["fast", "TYP", "", 1, None])
def test_sdf_option_corner_must_be_min_typ_or_max(tmp_path, corner):
    request_path = _sdf_request(tmp_path, corner=corner)
    with pytest.raises(
        FunctionalVerificationError, match="options.sdf.corner must be one of"
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("functional_value", [None, "1", ""])
def test_sdf_with_a_functional_define_is_a_request_error(
    tmp_path, monkeypatch, functional_value
):
    """`options.sdf` + a `FUNCTIONAL` define asks for two incompatible things
    (issue #1004): a PDK's FUNCTIONAL cell models are the zero-delay branch of
    the same `ifdef that guards the timing models, so they carry none of the
    `specify` blocks an SDF's IOPATH entries annotate. At best the annotation
    matches nothing; on Icarus 12.0 the run silently mis-simulates. Either way
    the verdict is quietly wrong, so it is rejected up front."""
    _setup_inputs(tmp_path)
    _write(tmp_path / "route.sdf", _MINIMAL_SDF)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            options={
                "sdf": {"file": "route.sdf"},
                "defines": {"FUNCTIONAL": functional_value},
            }
        ),
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    with pytest.raises(
        FunctionalVerificationError,
        match="options.sdf cannot be combined with a 'FUNCTIONAL' define",
    ):
        run_functional_verification(request_path)


def test_functional_define_without_sdf_is_untouched(tmp_path, monkeypatch):
    """Regression guard: `FUNCTIONAL` on its own is a perfectly ordinary
    zero-delay gate-level run and stays one -- the rejection is about the
    *combination*, not about the define."""
    _setup_inputs(tmp_path)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"defines": {"FUNCTIONAL": None}}),
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert report["environment"]["sdf"] is None
    assert runner.build_kwargs["defines"] == {"FUNCTIONAL": None}


@pytest.mark.parametrize("version", ["12.0", "11.0", "12.0 (stable)"])
def test_sdf_on_a_pre_13_icarus_is_a_request_error(tmp_path, monkeypatch, version):
    """`-ginterconnect` does not exist before Icarus 13.0 (issue #1004, found
    live on Ubuntu noble's 12.0 package): `iverilog` fails the build with
    "Unknown/Unsupported Language generation interconnect" and exit 255, four
    layers below this API. Since a post-route SDF's net delays are entirely
    INTERCONNECT entries, that host cannot serve the request at all -- so it
    is rejected up front, naming the actual constraint."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    monkeypatch.setattr(fv, "_engine_version", lambda engine: version)

    with pytest.raises(
        FunctionalVerificationError, match="requires Icarus Verilog 13.0 or newer"
    ):
        run_functional_verification(request_path)


@pytest.mark.parametrize("version", ["13.0", "13.0 (stable)", "14.0", None, "??"])
def test_sdf_engine_capability_gate_admits_13_and_the_unknowable(version):
    """The gate is a courtesy, not a lock: an unresolvable or unparsable
    version string must not block a run the build itself would have served
    fine (and whose real failures the transcript scan still catches)."""
    fv._check_sdf_engine_capability(version)


def test_stubbed_sdf_generates_the_annotate_shim_with_an_absolute_path(
    tmp_path, monkeypatch
):
    """The load-bearing trick (spike section 2.1): a cocotb regression's
    `hdl_toplevel` *is* the DUT, so the `$sdf_annotate` call rides in a
    generated second elaboration root. The embedded path must be **absolute**
    -- `vvp` resolves a relative one against its own working directory, one
    `SDF WARNING` away from a silent zero-delay run.

    The scope argument is the DUT nested under the generated pass-through
    wrapper (`klt_sdf_dut_wrapper.klt_sdf_dut`), not `hdl_toplevel` directly
    -- issue #1056's fix for a top-level-port-attached `INTERCONNECT` entry,
    which Icarus cannot resolve against a module elaborated as its own `-s`
    root."""
    request_path = _sdf_request(tmp_path)
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    shim = tmp_path / ".klt" / "functional-verification" / "klt_sdf_annotate.v"
    assert shim.is_file()
    text = shim.read_text()
    assert "module klt_sdf_annotate();" in text
    assert (
        f'$sdf_annotate("{tmp_path / "route.sdf"}", '
        "klt_sdf_dut_wrapper.klt_sdf_dut);" in text
    )

    wrapper = tmp_path / ".klt" / "functional-verification" / "klt_sdf_dut_wrapper.v"
    assert wrapper.is_file()
    wrapper_text = wrapper.read_text()
    assert "module klt_sdf_dut_wrapper" in wrapper_text
    assert "gcd klt_sdf_dut" in wrapper_text

    # ...and both generated files are elaborated alongside the design's own
    # sources, wrapper before shim.
    assert runner.build_kwargs["sources"][-2:] == [str(wrapper), str(shim)]
    assert str(tmp_path / "gcd.v") in runner.build_kwargs["sources"]
    # The wrapper -- not the real DUT -- is the Verilog toplevel actually
    # elaborated (`-s`) and the one cocotb's own `dut.<port>` handle binds
    # to; the real DUT keeps its own name for the response's own echo.
    assert runner.build_kwargs["hdl_toplevel"] == "klt_sdf_dut_wrapper"


def test_stubbed_sdf_appends_the_required_build_args(tmp_path, monkeypatch):
    """All four flags the spike found mandatory, plus the corner selection:
    `-gspecify` (without it Icarus drops the annotation *silently*),
    `-ginterconnect` (without it every INTERCONNECT entry fails), `-s
    klt_sdf_annotate` (elaborate the shim as a second root), and `-T <corner>`
    (Icarus's compile-time min/typ/max selection)."""
    request_path = _sdf_request(tmp_path, corner="max")
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    run_functional_verification(request_path)

    assert runner.build_kwargs["build_args"] == [
        "-gspecify",
        "-ginterconnect",
        "-s",
        "klt_sdf_annotate",
        "-T",
        "max",
    ]


def test_stubbed_sdf_defaults_to_the_typ_corner(tmp_path, monkeypatch):
    request_path = _sdf_request(tmp_path)
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert runner.build_kwargs["build_args"][-2:] == ["-T", "typ"]
    assert report["environment"]["sdf"] == {
        "file": str(tmp_path / "route.sdf"),
        "corner": "typ",
        "annotated": True,
        "partial": False,
        "dropped": {},
    }


def test_stubbed_sdf_absolute_file_path_is_accepted_verbatim(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    sdf_path = _write(tmp_path / "elsewhere.sdf", _MINIMAL_SDF)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"sdf": {"file": sdf_path, "corner": "min"}}),
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    report = run_functional_verification(request_path)

    assert report["environment"]["sdf"]["file"] == sdf_path


def test_stubbed_sdf_fails_loud_on_an_sdf_error_in_the_transcript(
    tmp_path, monkeypatch
):
    """The single highest-value behaviour here (spike section 3.3): Icarus
    reports every SDF failure mode and **carries on**, `vvp` exits 0, and
    cocotb duly reports the zero-delay verdict as a pass. The run must fail
    on the transcript, not be trusted because it produced a green
    `results.xml` -- which this fixture deliberately does."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            extra_test_log=(
                "SDF ERROR: route.sdf:2976: Unable to match ModPath D -> Q in "
                "gcd._647_\n"
            ),
        ),
    )

    with pytest.raises(
        FunctionalVerificationError,
        match="did not fully apply.*Unable to match ModPath",
    ):
        run_functional_verification(request_path)


def test_stubbed_sdf_scans_the_build_transcript_too(tmp_path, monkeypatch):
    """`SDF WARNING` lines can land in either transcript -- an unopenable
    file is reported at run time, but `$sdf_annotate` argument diagnostics
    come out of elaboration."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            extra_build_log=(
                'SDF WARNING: shim.v:2: Unable to open SDF file "route.sdf". '
                "Skipping this annotation.\n"
            ),
        ),
    )

    with pytest.raises(FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(request_path)


def test_stubbed_sdf_fails_on_the_omitted_annotation_warning(tmp_path, monkeypatch):
    """Not an `SDF`-prefixed line at all, and the most dangerous one there is
    (spike section 3.1): with `-gspecify` missing, Icarus drops the
    annotation as an ordinary compile `warning:` and runs at zero delay."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            extra_build_log=(
                "shim.v:4: warning: Omitting $sdf_annotate() since specify "
                "blocks and interconnects are being omitted.\n"
            ),
        ),
    )

    with pytest.raises(FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(request_path)


def test_stubbed_sdf_tolerates_the_benign_timingcheck_warning(tmp_path, monkeypatch):
    """Icarus implements SDF delays but not SDF timing checks (spike section
    3.4), and a real `write_sdf` emits TIMINGCHECK sections -- so failing on
    this class would reject every real post-route SDF. The delays it *does*
    apply are unaffected.

    Issue #1102: `annotated: true` alone cannot tell "every check applied"
    apart from "every TIMINGCHECK was dropped" -- both report it identically.
    `partial`/`dropped` make that distinction machine-readable."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            extra_test_log="SDF WARNING: route.sdf:7: TIMINGCHECK not supported.\n",
        ),
    )

    report = run_functional_verification(request_path)

    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is True
    assert sdf["dropped"].keys() == {"timingcheck"}
    assert sdf["dropped"]["timingcheck"]["count"] == 1
    assert sdf["dropped"]["timingcheck"]["reason"]  # non-empty explanation


def test_stubbed_sdf_counts_timingcheck_drops_across_both_transcripts(
    tmp_path, monkeypatch
):
    """The dropped-diagnostic count is a sum across build and test
    transcripts -- `TIMINGCHECK` lines can land in either, the same as the
    actionable-diagnostic scan they sit alongside (issue #1102)."""
    request_path = _sdf_request(tmp_path)
    _stub_runner(
        monkeypatch,
        _FakeRunner(
            _RESULTS_XML_WITH_SKIP,
            extra_build_log="SDF WARNING: route.sdf:3: TIMINGCHECK not supported.\n",
            extra_test_log=(
                "SDF WARNING: route.sdf:7: TIMINGCHECK not supported.\n"
                "SDF WARNING: route.sdf:11: TIMINGCHECK not supported.\n"
            ),
        ),
    )

    report = run_functional_verification(request_path)

    dropped = report["environment"]["sdf"]["dropped"]
    assert dropped["timingcheck"]["count"] == 3


def test_sdf_diagnostics_are_only_scanned_on_annotated_runs(tmp_path, monkeypatch):
    """Regression guard: a request with no `options.sdf` behaves exactly as
    it did before this feature existed -- no shim, no build args, no
    transcript gate (even against a transcript that would trip it)."""
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(
        monkeypatch,
        _FakeRunner(_RESULTS_XML_WITH_SKIP, extra_test_log="SDF ERROR: bogus\n"),
    )

    report = run_functional_verification(request_path)

    assert report["status"] == "pass"
    assert report["environment"]["sdf"] is None
    assert runner.build_kwargs["build_args"] == []
    assert not (
        tmp_path / ".klt" / "functional-verification" / "klt_sdf_annotate.v"
    ).exists()


# --------------------------------------------------------------------------- #
# `_parse_toplevel_ports` / `_write_sdf_dut_wrapper` (issue #1056): the
# top-level-port-attached `INTERCONNECT` fix's wrapper-generation machinery.
# --------------------------------------------------------------------------- #


def test_parse_toplevel_ports_non_ansi_yosys_style(tmp_path):
    """The convention both `klt synthesize`'s Yosys `write_verilog` and `klt
    place-and-route`'s OpenROAD `write_verilog` emit (verified against
    `tests/corpus/statime/gcd_netlist.v`): a bare-name port list, with
    direction/width declared on a separate line per port."""
    netlist = _write(
        tmp_path / "gcd_netlist.v",
        "module gcd(clk, rst_n, a_in, b_in, done, result);\n"
        "  wire [15:0] a;\n"
        "  input [15:0] a_in;\n"
        "  input [15:0] b_in;\n"
        "  input clk;\n"
        "  output done;\n"
        "  output [15:0] result;\n"
        "  input rst_n;\n"
        "endmodule\n",
    )

    ports = fv._parse_toplevel_ports([netlist], "gcd")

    assert ports == [
        ("input", "", "clk"),
        ("input", "", "rst_n"),
        ("input", "[15:0]", "a_in"),
        ("input", "[15:0]", "b_in"),
        ("output", "", "done"),
        ("output", "[15:0]", "result"),
    ]


def test_parse_toplevel_ports_ansi_style(tmp_path):
    """The convention this module's own hand-authored integration-test
    fixtures use -- direction inline per port."""
    netlist = _write(
        tmp_path / "dly.v",
        "module dly (input wire a, output wire [7:0] y, input clk);\nendmodule\n",
    )

    ports = fv._parse_toplevel_ports([netlist], "dly")

    assert ports == [
        ("input", "", "a"),
        ("output", "[7:0]", "y"),
        ("input", "", "clk"),
    ]


def test_parse_toplevel_ports_mixed_ansi_and_bare_is_a_request_error(tmp_path):
    """Verilog's 'inherit the previous port's direction' rule for a
    partially-ANSI header is not resolved -- fail loud rather than guess."""
    netlist = _write(
        tmp_path / "dut.v",
        "module dut (input a, b, output y);\nendmodule\n",
    )

    with pytest.raises(FunctionalVerificationError, match="mixes ANSI-style ports"):
        fv._parse_toplevel_ports([netlist], "dut")


def test_parse_toplevel_ports_missing_module_is_a_request_error(tmp_path):
    netlist = _write(tmp_path / "other.v", "module other(a, b); endmodule\n")

    with pytest.raises(
        FunctionalVerificationError, match="could not find a 'module gcd\\(...\\)'"
    ):
        fv._parse_toplevel_ports([netlist], "gcd")


def test_parse_toplevel_ports_missing_direction_declaration_is_a_request_error(
    tmp_path,
):
    netlist = _write(
        tmp_path / "dut.v",
        "module dut(a, b);\n  input a;\nendmodule\n",
    )

    with pytest.raises(
        FunctionalVerificationError,
        match="could not find a direction declaration for port 'b'",
    ):
        fv._parse_toplevel_ports([netlist], "dut")


def test_write_sdf_dut_wrapper_forwards_every_port(tmp_path):
    wrapper_path = str(tmp_path / "klt_sdf_dut_wrapper.v")

    fv._write_sdf_dut_wrapper(
        wrapper_path,
        hdl_toplevel="gcd",
        ports=[
            ("input", "", "clk"),
            ("input", "[15:0]", "a_in"),
            ("output", "[15:0]", "result"),
        ],
    )

    text = Path(wrapper_path).read_text()
    assert "module klt_sdf_dut_wrapper (" in text
    assert "input clk" in text
    assert "input [15:0] a_in" in text
    assert "output [15:0] result" in text
    assert "gcd klt_sdf_dut (" in text
    assert ".clk(clk)" in text
    assert ".a_in(a_in)" in text
    assert ".result(result)" in text


def test_stubbed_sdf_with_parameters_is_a_request_error(tmp_path, monkeypatch):
    """cocotb's own Icarus parameter-override syntax is always
    `-P<hdl_toplevel>.<name>=<value>` -- once `hdl_toplevel` becomes the
    generated wrapper (issue #1056), that would silently target the wrapper
    (which declares no parameters) instead of the real DUT, so the
    combination is rejected up front rather than risking a parameter
    override that looks applied but was not."""
    _setup_inputs(tmp_path)
    _write(tmp_path / "route.sdf", _MINIMAL_SDF)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(options={"sdf": {"file": "route.sdf"}}, parameters={"WIDTH": 8}),
    )
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    with pytest.raises(
        FunctionalVerificationError,
        match="request.parameters cannot currently be combined with options.sdf",
    ):
        run_functional_verification(request_path)


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
def test_integration_real_icarus_parameters_override_reaches_elaboration(tmp_path):
    """`request.parameters` end to end against real cocotb + Icarus: the
    `modexp` core elaborates at `WIDTH=8` (its RTL default is `WIDTH=16`),
    and the companion testbench's own `len(dut.result) == 8` assertion
    confirms the override reached the simulator, not just the request/
    response envelope -- issue #610's own acceptance criterion."""
    for name in (
        "modexp.v",
        "test_modexp.py",
        "test_modexp_parameters.py",
        "request-modexp-parameters.json",
    ):
        shutil.copy(EXAMPLE_DIR / name, tmp_path / name)
    request_path = str(tmp_path / "request-modexp-parameters.json")

    report = run_functional_verification(request_path)

    assert report["status"] == "pass"
    assert report["failed_count"] == 0
    assert report["test_count"] == 1


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


# --------------------------------------------------------------------------- #
# Integration: `options.sdf` against real cocotb + real Icarus (issue #1002)
#
# Deliberately PDK-free. The measurement `docs/design/post-route-sta-survey.md`
# section 4.3 asks for -- "does the testbench's own pass/fail outcome change
# once real delay is present?" -- needs a design with a `specify` block and an
# SDF that annotates it, not a standard-cell library. A two-module inverter
# plus a three-line SDF reproduces the whole signal in a tmp dir, so these run
# on any machine with cocotb + `iverilog` (the same posture the worked-example
# integration tests above take) instead of needing a volare PDK install.
# --------------------------------------------------------------------------- #

_SDF_DUT_V = """\
module klt_inv (input wire a, output wire y);
  assign y = ~a;
  specify
    (a => y) = (0.0, 0.0);
  endspecify
endmodule

module dly (input wire a, output wire y);
  klt_inv u_inv (.a(a), .y(y));
endmodule
"""

_SDF_TESTBENCH_PY = '''\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_output_settles_within_two_ns(dut):
    """Passes at zero delay; fails once a >2 ns path delay is annotated."""
    dut.a.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(2, unit="ns")
    assert dut.y.value == 0, f"y={dut.y.value} 2 ns after a rose (expected 0)"
'''


def _sdf_text(iopath_source: str = "a") -> str:
    """A minimal IEEE-1497 SDF annotating `u_inv`'s `a -> y` arc with a
    `min:typ:max` triplet of 1/5/9 ns -- one member either side of the
    testbench's own 2 ns sampling point, so the corner selection is
    observable in the verdict. `iopath_source="nope"` names an arc the cell's
    `specify` block does not declare, reproducing the spike's own `Unable to
    match ModPath` failure mode."""
    return f"""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "dly")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "klt_inv")
    (INSTANCE u_inv)
    (DELAY (ABSOLUTE
      (IOPATH {iopath_source} y (1.000:5.000:9.000) (1.000:5.000:9.000))
    ))
  )
)
"""


def _stage_sdf_design(tmp_path: Path, *, sdf_text: str | None = None) -> None:
    _write(tmp_path / "dly.v", _SDF_DUT_V)
    _write(tmp_path / "test_dly.py", _SDF_TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _sdf_text() if sdf_text is None else sdf_text)


def _sdf_integration_request(tmp_path: Path, name: str, sdf: dict | None) -> str:
    request = {
        "sources": ["dly.v"],
        "hdl_toplevel": "dly",
        "testbench": {"module": "test_dly"},
    }
    if sdf is not None:
        request["options"] = {"sdf": sdf}
    return _write_request(tmp_path / name, request)


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_sdf_annotation_changes_the_verdict(tmp_path):
    """Survey section 4.3's own coverage metric, demonstrated end to end: the
    *same* testbench passes at zero delay and fails once the SDF's real path
    delay is back-annotated. A silently-skipped annotation would leave both
    runs identical, which is exactly what makes this the load-bearing
    assertion for the whole feature."""
    _stage_sdf_design(tmp_path)
    zero_delay = _sdf_integration_request(tmp_path, "request-plain.json", None)
    annotated = _sdf_integration_request(
        tmp_path, "request-sdf.json", {"file": "route.sdf", "corner": "typ"}
    )

    plain_report = run_functional_verification(zero_delay)
    annotated_report = run_functional_verification(annotated)

    assert plain_report["status"] == "pass"
    assert plain_report["environment"]["sdf"] is None
    assert annotated_report["status"] == "fail"
    assert annotated_report["environment"]["sdf"] == {
        "file": str(tmp_path / "route.sdf"),
        "corner": "typ",
        "annotated": True,
        "partial": False,
        "dropped": {},
    }


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
@pytest.mark.parametrize(
    ("corner", "expected_status"),
    [("min", "pass"), ("max", "fail")],
)
def test_integration_real_icarus_sdf_corner_selects_a_triplet_member(
    tmp_path, corner, expected_status
):
    """`options.sdf.corner` -> `iverilog -T` reaches the simulator, against
    one SDF whose every delay is the triplet `(1.000:5.000:9.000)`: the
    testbench samples 2 ns after the input edge, so `min` (1 ns) has settled
    and `max` (9 ns) has not. This is the *compile-time* selection the spike
    established -- passing a corner as an `$sdf_annotate` argument instead
    would be silently ignored by Icarus and both rows would match."""
    _stage_sdf_design(tmp_path)
    request_path = _sdf_integration_request(
        tmp_path, f"request-{corner}.json", {"file": "route.sdf", "corner": corner}
    )

    report = run_functional_verification(request_path)

    assert report["status"] == expected_status
    assert report["environment"]["sdf"]["corner"] == corner


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_broken_sdf_fails_loud_not_silently(tmp_path):
    """The trap, reproduced against the real toolchain: an SDF naming an arc
    the cell does not declare makes Icarus drop that annotation, print `SDF
    ERROR: ... Unable to match ModPath`, and **still exit 0** -- the run's own
    `results.xml` reports a clean pass at zero delay. The transcript scan is
    the only thing between that and a false green."""
    _stage_sdf_design(tmp_path, sdf_text=_sdf_text(iopath_source="nope"))
    request_path = _sdf_integration_request(
        tmp_path, "request-broken.json", {"file": "route.sdf"}
    )

    with pytest.raises(
        FunctionalVerificationError,
        match="did not fully apply.*Unable to match ModPath",
    ):
        run_functional_verification(request_path)

    # The evidence the scan overrode: cocotb's own artifact says "passed".
    results_xml = tmp_path / ".klt" / "functional-verification" / "results_icarus.xml"
    assert "<failure" not in results_xml.read_text()


# --------------------------------------------------------------------------- #
# Integration: a top-level-port-attached `INTERCONNECT` entry (issue #1056).
#
# Before the fix, Icarus's `$sdf_annotate` could not resolve an `INTERCONNECT`
# entry whose endpoint is a bare top-level port -- `INTERCONNECT a inst.pin`
# (a primary input) or `INTERCONNECT inst.pin b` (a primary output) -- even
# with `-ginterconnect` present, because the DUT was elaborated as its own
# `-s` root. A purely internal `INTERCONNECT inst.pin inst.pin` entry always
# resolved fine, which is exactly what made this a silent, design-shape-
# dependent trap: any design with primary-port SDF delay hit it every time.
# --------------------------------------------------------------------------- #

_TOPLEVEL_PORT_SDF_DUT_V = """\
module klt_inv (input wire in, output wire out);
  assign out = ~in;
  specify
    (in => out) = (0.0, 0.0);
  endspecify
endmodule

module klt_buf (input wire in, output wire out);
  assign out = in;
  specify
    (in => out) = (0.0, 0.0);
  endspecify
endmodule

module toplevel_iconn (input wire a, output wire b);
  wire w;
  klt_inv u1 (.in(a), .out(w));
  klt_buf u2 (.in(w), .out(b));
endmodule
"""

_TOPLEVEL_PORT_TESTBENCH_PY = '''\
import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_output_settles_within_two_ns(dut):
    """`b` inverts `a` through u1 (klt_inv) then passes through u2 (klt_buf)
    unchanged: at zero delay `b` tracks `~a` immediately, so this passes;
    once real INTERCONNECT delay on the top-level-port-attached entries is
    annotated, `b` has not yet responded 2 ns after `a` rises and this
    fails."""
    dut.a.value = 0
    await Timer(10, unit="ns")
    dut.a.value = 1
    await Timer(2, unit="ns")
    assert dut.b.value == 0, f"b={dut.b.value} 2 ns after a rose (expected 0)"
'''


def _toplevel_port_sdf_text() -> str:
    """A minimal IEEE-1497 SDF whose top design `CELL` mixes a purely
    internal `INTERCONNECT u1.out u2.in` entry (already resolved before
    issue #1056) with two top-level-port-attached ones -- `INTERCONNECT a
    u1.in` (a primary input) and `INTERCONNECT u2.out b` (a primary output)
    -- the exact entry shape issue #1056 reports, matching a real
    `write_sdf -divider .` top design `CELL` block's own bare-port-name
    convention (`docs/design/sdf-annotate-feasibility-spike.md`)."""
    return """\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "toplevel_iconn")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL
    (CELLTYPE "toplevel_iconn")
    (INSTANCE)
    (DELAY (ABSOLUTE
      (INTERCONNECT a u1.in (1.000:5.000:9.000) (1.000:5.000:9.000))
      (INTERCONNECT u1.out u2.in (0.000:0.000:0.000) (0.000:0.000:0.000))
      (INTERCONNECT u2.out b (0.000:0.000:0.000) (0.000:0.000:0.000))
    ))
  )
)
"""


def _stage_toplevel_port_sdf_design(tmp_path: Path) -> None:
    _write(tmp_path / "toplevel_iconn.v", _TOPLEVEL_PORT_SDF_DUT_V)
    _write(tmp_path / "test_toplevel_iconn.py", _TOPLEVEL_PORT_TESTBENCH_PY)
    _write(tmp_path / "route.sdf", _toplevel_port_sdf_text())


def _toplevel_port_sdf_integration_request(
    tmp_path: Path, name: str, sdf: dict | None
) -> str:
    request = {
        "sources": ["toplevel_iconn.v"],
        "hdl_toplevel": "toplevel_iconn",
        "testbench": {"module": "test_toplevel_iconn"},
    }
    if sdf is not None:
        request["options"] = {"sdf": sdf}
    return _write_request(tmp_path / name, request)


@pytest.mark.skipif(not HAVE_COCOTB, reason="cocotb is not installed on this machine")
@pytest.mark.skipif(
    not HAVE_SIMULATOR["icarus"], reason="iverilog is not installed on this machine"
)
def test_integration_real_icarus_sdf_resolves_a_toplevel_port_interconnect(tmp_path):
    """The load-bearing regression for issue #1056: a real post-route SDF's
    top design `CELL` mixes purely internal `INTERCONNECT inst.pin inst.pin`
    entries with entries touching a top-level port. Before the fix this
    raised `FunctionalVerificationError` (`did not fully apply ... Could not
    find intermodpath!`) for both port-touching entries even though
    `-ginterconnect` was present. The generated pass-through wrapper
    (`_write_sdf_dut_wrapper`) nests the DUT under a new root instead, which
    Icarus resolves cleanly -- verified here by the *same* "does the
    testbench's own verdict change" coverage metric the existing IOPATH
    integration test above uses."""
    _stage_toplevel_port_sdf_design(tmp_path)
    zero_delay = _toplevel_port_sdf_integration_request(
        tmp_path, "request-plain.json", None
    )
    annotated = _toplevel_port_sdf_integration_request(
        tmp_path, "request-sdf.json", {"file": "route.sdf", "corner": "typ"}
    )

    plain_report = run_functional_verification(zero_delay)
    annotated_report = run_functional_verification(annotated)

    assert plain_report["status"] == "pass"
    assert plain_report["environment"]["sdf"] is None
    # This is the assertion that would previously raise
    # FunctionalVerificationError instead of returning a report at all.
    assert annotated_report["status"] == "fail"
    assert annotated_report["environment"]["sdf"] == {
        "file": str(tmp_path / "route.sdf"),
        "corner": "typ",
        "annotated": True,
        "partial": False,
        "dropped": {},
    }


# Sanity: `subprocess` really is the module this file's coverage stubs patch
# (guards against a future refactor silently making them a no-op).
def test_functional_verification_uses_stdlib_subprocess():
    assert fv.subprocess is subprocess
