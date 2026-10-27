"""``testbench.verilog_top``: a self-checking Verilog bench run without cocotb
(issue #4083). Request-validation tests are pure; the integration tests need
only ``iverilog``/``vvp``."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
    run_functional_verification_with_mutations,
)

EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "functional-verification"
    / "verilog-tb"
)
HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
needs_icarus = pytest.mark.skipif(not HAVE_ICARUS, reason="iverilog/vvp not installed")


def _stage(tmp_path: Path, request_name: str = "request-rtl.json") -> Path:
    for entry in EXAMPLE_DIR.iterdir():
        shutil.copy(entry, tmp_path / entry.name)
    return tmp_path / request_name


def _edit(request_path: Path, **updates) -> str:
    doc = json.loads(request_path.read_text())
    for key, value in updates.items():
        if key.startswith("testbench."):
            doc["testbench"][key.split(".", 1)[1]] = value
        elif key.startswith("options."):
            doc.setdefault("options", {})[key.split(".", 1)[1]] = value
        else:
            doc[key] = value
    request_path.write_text(json.dumps(doc))
    return str(request_path)


def _rewrite_bench(tmp_path: Path, old: str, new: str) -> None:
    bench = tmp_path / "tb_and2.v"
    bench.write_text(bench.read_text().replace(old, new))


# -- request validation ------------------------------------------------------


def test_both_module_and_verilog_top_is_rejected(tmp_path):
    request = _edit(_stage(tmp_path), **{"testbench.module": "test_x"})
    with pytest.raises(FunctionalVerificationError, match="not both"):
        run_functional_verification(request)


def test_neither_module_nor_verilog_top_is_rejected(tmp_path):
    request_path = _stage(tmp_path)
    request = _edit(request_path, testbench={"pass_marker": "PASS"})
    with pytest.raises(FunctionalVerificationError, match="testbench.module"):
        run_functional_verification(request)


def test_verilog_top_must_be_declared_in_sources(tmp_path):
    request = _edit(_stage(tmp_path), **{"testbench.verilog_top": "no_such_tb"})
    with pytest.raises(FunctionalVerificationError, match="not a module declared"):
        run_functional_verification(request)


@pytest.mark.parametrize(
    "option,value",
    [
        ("coverage", True),
        ("trace", True),
        ("random_seed", 1),
        ("initial_state", {"mode": "x"}),
    ],
)
def test_cocotb_only_options_are_rejected(tmp_path, option, value):
    request = _edit(_stage(tmp_path), **{f"options.{option}": value})
    with pytest.raises(FunctionalVerificationError, match=f"options.{option}"):
        run_functional_verification(request)


def test_verilator_is_rejected(tmp_path):
    request = _edit(_stage(tmp_path), engine="verilator")
    with pytest.raises(FunctionalVerificationError, match="requires engine 'icarus'"):
        run_functional_verification(request)


def test_sdf_requires_dut_instance(tmp_path):
    request_path = _stage(tmp_path, "request-sdf.json")
    doc = json.loads(request_path.read_text())
    del doc["testbench"]["dut_instance"]
    request_path.write_text(json.dumps(doc))
    with pytest.raises(FunctionalVerificationError, match="dut_instance"):
        run_functional_verification(str(request_path))


def test_dut_instance_without_sdf_is_rejected(tmp_path):
    request = _edit(_stage(tmp_path), **{"testbench.dut_instance": "tb_and2.dut"})
    with pytest.raises(FunctionalVerificationError, match="only applies"):
        run_functional_verification(request)


def test_mutations_rejected_with_verilog_top(tmp_path):
    request = str(_stage(tmp_path))
    proposals = tmp_path / "p.json"
    proposals.write_text(json.dumps({"scope": [], "proposals": []}))
    with pytest.raises(FunctionalVerificationError, match="--mutations"):
        run_functional_verification_with_mutations(request, str(proposals))


# -- real Icarus -------------------------------------------------------------


@needs_icarus
def test_integration_pass_without_sdf(tmp_path):
    report = run_functional_verification(str(_stage(tmp_path)))
    assert report["status"] == "pass"
    assert (report["test_count"], report["passed_count"], report["failed_count"]) == (
        1,
        1,
        0,
    )
    assert report["tests"][0]["name"] == "tb_and2"
    assert report["environment"]["cocotb_version"] is None
    assert report["environment"]["sdf"] is None
    assert Path(report.local_paths["test_log"]).is_file()
    assert report["environment"]["results_xml"] == {"path": None, "scope": "absent"}


@needs_icarus
def test_integration_fail_marker_fails(tmp_path):
    request = _stage(tmp_path)
    _rewrite_bench(tmp_path, "check(1)", "check(0)")
    report = run_functional_verification(str(request))
    assert report["status"] == "fail"
    assert "fail_marker" in report["tests"][0]["error_message"]


@needs_icarus
def test_integration_fatal_fails(tmp_path):
    request = _stage(tmp_path)
    _rewrite_bench(tmp_path, "$finish;", '$fatal(1, "boom");')
    _rewrite_bench(tmp_path, '$display("PASS")', ";")
    report = run_functional_verification(str(request))
    assert report["status"] == "fail"
    assert "exited with status" in report["tests"][0]["error_message"]


@needs_icarus
def test_integration_exit_zero_without_pass_marker_fails(tmp_path):
    request = _stage(tmp_path)
    _rewrite_bench(tmp_path, '$display("PASS")', ";")
    report = run_functional_verification(str(request))
    assert report["status"] == "fail"
    assert "pass_marker" in report["tests"][0]["error_message"]


@needs_icarus
def test_integration_no_pass_marker_uses_exit_status_only(tmp_path):
    request = _stage(tmp_path)
    doc = json.loads(request.read_text())
    del doc["testbench"]["pass_marker"]
    request.write_text(json.dumps(doc))
    _rewrite_bench(tmp_path, '$display("PASS")', ";")
    assert run_functional_verification(str(request))["status"] == "pass"


@needs_icarus
def test_integration_sdf_annotated_run(tmp_path):
    report = run_functional_verification(str(_stage(tmp_path, "request-sdf.json")))
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert sdf["partial"] is False


@needs_icarus
def test_integration_sdf_that_fails_to_annotate_still_raises(tmp_path):
    request = _stage(tmp_path, "request-sdf.json")
    sdf = tmp_path / "and2.sdf"
    sdf.write_text(sdf.read_text().replace("INSTANCE u1", "INSTANCE nosuch"))
    with pytest.raises(FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(str(request))
