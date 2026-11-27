"""The Icarus Python runtime pre-flight (issue #8015) against real tools.

Needs ``iverilog``/``vvp`` and cocotb; skipped otherwise. The stubbed tier in
``test_functional_verification_python_runtime.py`` fixes what a probe result
means; this one establishes that the result is *true*. The probe is compared
with what a disposable cocotb test observes from inside its own ``vvp``, then
the checked-in examples are run through the verb as a user would.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from io import StringIO
from pathlib import Path

import pytest

from helpers.tool_probes import HAVE_ICARUS, SKIP_NO_COCOTB
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_python_runtime as rt
from klayout_tools.functional_verification import run_functional_verification

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_VVP = shutil.which("vvp")
EXAMPLE_DIR = REPO_ROOT / "examples" / "functional-verification"
needs_icarus = pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog/vvp not installed")
_BANNER = re.compile(r"Using Python (\S+) interpreter")


def _probe(module_dirs: list[str]) -> dict:
    return rt._measure_vvp_python(
        runner_module=fv._import_runner(),
        module_dirs=module_dirs,
        env={},
        timescale=("1ns", "1ps"),
        test_dir=module_dirs[0],  # any existing directory will do
    )


@SKIP_NO_COCOTB
@needs_icarus
def test_probe_measures_what_a_disposable_cocotb_test_observes(tmp_path, monkeypatch):
    """On a healthy installation the probe's two facts -- the version
    ``Py_GetVersion`` reports and the object supplying it -- match an
    independent observation made inside another ``vvp`` the verb launched."""
    (tmp_path / "gcd.v").write_text("module gcd(input wire clk); endmodule\n")
    shutil.copyfile(
        REPO_ROOT / "tests" / "helpers" / "python_runtime_observer.py",
        tmp_path / "klt_observer.py",
    )
    observation = tmp_path / "observation.json"
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "engine": "icarus",
                "sources": ["gcd.v"],
                "hdl_toplevel": "gcd",
                "testbench": {"module": "klt_observer"},
                "env": {"OBSERVATION": str(observation)},
            }
        )
    )

    report = run_functional_verification(str(tmp_path / "request.json"))

    assert report["status"] == "pass", report["tests"]
    seen = json.loads(observation.read_text())
    measured = _probe([str(tmp_path)])
    assert measured["status"] == "measured", measured
    assert measured["py_get_version"] == seen["version"]
    assert rt._release(measured["py_get_version"]) == tuple(sys.version_info)
    if seen["mapped"] is not None:  # Linux
        assert Path(measured["library"]).samefile(seen["mapped"])
    else:
        assert measured["library"] is None


@pytest.fixture
def measurements(monkeypatch):
    outcomes = []
    measure = rt._measure_vvp_python

    def record(**kwargs):
        outcome = measure(**kwargs)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(rt, "_measure_vvp_python", record)
    return outcomes


@SKIP_NO_COCOTB
@needs_icarus
def test_gcd_worked_example_runs_past_the_probe(tmp_path, monkeypatch, measurements):
    """The probe measures klt's own runtime here, so the ordinary run
    happens -- whatever build version cocotb's banner names, which is the
    wheel's compile-time ``PY_VERSION`` and routinely differs."""
    for name in ("gcd.v", "test_gcd.py", "request.json"):
        shutil.copyfile(EXAMPLE_DIR / name, tmp_path / name)

    caller_file = tmp_path / "caller.json"
    caller_file.write_text('{"owner": "caller"}')
    monkeypatch.setenv("KLT_PYTHON_RUNTIME_PROBE_RESULT", str(caller_file))

    report = run_functional_verification(str(tmp_path / "request.json"))

    assert report["status"] == "fail"
    assert (
        report["test_count"],
        report["passed_count"],
        report["failed_count"],
        report["skipped_count"],
    ) == (3, 2, 1, 0)
    transcript = Path(report.local_paths["test_log"]).read_text(errors="replace")
    banner = _BANNER.search(transcript)
    assert banner is not None, transcript
    # The run above proceeded on the runtime's say-so: the probe measured
    # klt's own interpreter, whichever build version the banner named.
    assert caller_file.read_text() == '{"owner": "caller"}'
    [measured] = measurements
    assert measured["status"] == "measured", measured
    assert rt._release(measured["py_get_version"]) == rt._klt_release()
    assert rt._release(banner.group(1)) is not None


@SKIP_NO_COCOTB
@needs_icarus
@pytest.mark.parametrize("selector", ["COCOTB_TEST_FILTER", "COCOTB_TESTCASE"])
@pytest.mark.parametrize("source", ["request", "ambient"])
def test_user_test_selection_preserves_the_probe(
    tmp_path, monkeypatch, measurements, selector, source
):
    for name in ("gcd.v", "test_gcd.py"):
        shutil.copyfile(EXAMPLE_DIR / name, tmp_path / name)
    request = json.loads((EXAMPLE_DIR / "request.json").read_text())
    testcase = "test_gcd_known_pairs"
    if source == "request":
        request["env"] = {selector: testcase}
    else:
        monkeypatch.setenv(selector, testcase)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))

    report = run_functional_verification(str(request_path))

    [measured] = measurements
    assert measured["status"] == "measured", measured
    assert rt._release(measured["py_get_version"]) == rt._klt_release()
    assert report["status"] == "pass"
    assert [test["name"] for test in report["tests"]] == [testcase]


@SKIP_NO_COCOTB
@needs_icarus
@pytest.mark.parametrize("setting", ["COCOTB_USER_COVERAGE", "COVERAGE"])
@pytest.mark.parametrize("source", ["request", "ambient"])
def test_user_coverage_report_after_probe(
    tmp_path, monkeypatch, measurements, setting, source
):
    coverage = pytest.importorskip("coverage")
    for name in ("gcd.v", "test_gcd.py"):
        shutil.copyfile(EXAMPLE_DIR / name, tmp_path / name)
    request = json.loads((EXAMPLE_DIR / "request.json").read_text())
    data_file = tmp_path / ".coverage"
    monkeypatch.setenv("COVERAGE_FILE", str(data_file))
    if source == "ambient":
        monkeypatch.setenv(setting, "1")
    else:
        request["env"] = {setting: "1"}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))

    report = run_functional_verification(str(request_path))

    [measured] = measurements
    assert measured["status"] == "measured", measured
    assert (report["passed_count"], report["failed_count"]) == (2, 1)
    user_coverage = coverage.Coverage(data_file=data_file, config_file=False)
    user_coverage.load()
    output = StringIO()
    user_coverage.report(file=output)
    assert "test_gcd.py" in output.getvalue()
    assert rt.PROBE_MODULE not in output.getvalue()


@needs_icarus
def test_verilog_bench_bypasses_the_probe(tmp_path, monkeypatch):
    """A pure-Verilog bench loads no Python; the probe must not launch."""
    for entry in (EXAMPLE_DIR / "verilog-tb").iterdir():
        shutil.copyfile(entry, tmp_path / entry.name)
    monkeypatch.setattr(
        rt, "_measure_vvp_python", lambda **kwargs: pytest.fail("probe ran")
    )

    report = run_functional_verification(str(tmp_path / "request-rtl.json"))

    assert report["status"] == "pass"


def _recording_vvp(directory: Path, label: str, log: Path) -> None:
    """A ``vvp`` that logs which launcher was chosen and from which working
    directory, then hands over to the real Icarus."""
    directory.mkdir(parents=True)
    wrapper = directory / "vvp"
    wrapper.write_text(
        f'#!/bin/sh\necho "{label} $(pwd -P)" >> "{log}"\nexec "{REAL_VVP}" "$@"\n'
    )
    wrapper.chmod(0o755)


@SKIP_NO_COCOTB
@needs_icarus
def test_probe_and_user_run_select_the_same_vvp_launcher(tmp_path, monkeypatch):
    """A relative ``PATH`` entry resolves from the simulator's working
    directory. With an output-relative ``tools/vvp`` ahead of an absolute
    fallback, the probe and the user's run must pick the same one -- else a
    wrapper that chooses libpython makes the pre-flight measure a runtime the
    run never loads."""
    for name in ("gcd.v", "test_gcd.py", "request.json"):
        shutil.copyfile(EXAMPLE_DIR / name, tmp_path / name)
    out, log = tmp_path / "out", tmp_path / "launches.log"
    _recording_vvp(out / "tools", "output-relative", log)
    _recording_vvp(tmp_path / "fallback", "fallback", log)
    path = ["tools", str(tmp_path / "fallback"), os.environ["PATH"]]
    monkeypatch.setenv("PATH", os.pathsep.join(path))

    run_functional_verification(str(tmp_path / "request.json"), output_dir=str(out))

    launches = [line.split(" ", 1) for line in log.read_text().splitlines()]
    simulations = [(label, cwd) for label, cwd in launches if cwd == str(out.resolve())]
    # The probe's simulator, then the user's: the same wrapper, from the same place.
    assert simulations == [("output-relative", str(out.resolve()))] * 2, launches
