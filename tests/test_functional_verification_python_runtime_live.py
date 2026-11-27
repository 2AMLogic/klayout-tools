"""The Icarus Python runtime pre-flight (issue #8015) against real tools.

Needs ``iverilog``/``vvp`` and cocotb; skipped otherwise. The stubbed tier in
``test_functional_verification_python_runtime.py`` fixes what a probe result
means; this one establishes that the result is *true*. The probe is compared
with what a disposable cocotb test observes from inside its own ``vvp``, then
the checked-in examples are run through the verb as a user would.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from helpers.tool_probes import HAVE_ICARUS, SKIP_NO_COCOTB
from klayout_tools import functional_verification as fv
from klayout_tools import functional_verification_python_runtime as rt
from klayout_tools.functional_verification import run_functional_verification

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "functional-verification"
needs_icarus = pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog/vvp not installed")
_BANNER = re.compile(r"Using Python (\S+) interpreter")


def _probe(module_dirs: list[str]) -> dict:
    return rt._measure_vvp_python(
        runner_module=fv._import_runner(),
        module_dirs=module_dirs,
        env={},
        timescale=("1ns", "1ps"),
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
    assert Path(measured["library"]).is_file()
    if seen["mapped"] is not None:  # always set on Linux
        assert Path(measured["library"]).samefile(seen["mapped"])


@SKIP_NO_COCOTB
@needs_icarus
def test_gcd_worked_example_runs_past_the_probe(tmp_path):
    """The probe measures klt's own runtime here, so the ordinary run
    happens -- whatever build version cocotb's banner names, which is the
    wheel's compile-time ``PY_VERSION`` and routinely differs."""
    for name in ("gcd.v", "test_gcd.py", "request.json"):
        shutil.copyfile(EXAMPLE_DIR / name, tmp_path / name)

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
    measured = _probe([str(tmp_path)])
    assert rt._release(measured["py_get_version"]) == rt._klt_release()
    assert rt._release(banner.group(1)) is not None


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
