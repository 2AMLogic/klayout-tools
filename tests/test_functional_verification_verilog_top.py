"""``testbench.verilog_top``: a self-checking Verilog bench run without cocotb
(issue #4083). Request-validation tests are pure; the integration tests need
only ``iverilog``/``vvp``."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_ICARUS as needs_icarus
from klayout_tools._provenance import _combined_content_hash
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


def _stage_sdf_units(
    tmp_path: Path,
    *,
    sdf_timescale: str | None = "1ns",
    delay: str = "0.5",
    leaf_timescale: str | None = "1ns/1ps",
    sdf_text: str | None = None,
) -> str:
    """The example's SDF request with the SDF's ``TIMESCALE`` and both of
    its delays rewritten, and the leaf model's own `` `timescale ``. ``None``
    removes the SDF header's ``TIMESCALE`` / the leaf's directive. ``sdf_text``
    replaces the whole SDF."""
    request = _stage(tmp_path, "request-sdf.json")
    if sdf_text is None:
        original = "  (TIMESCALE 1ns)\n"
        header = "" if sdf_timescale is None else f"  (TIMESCALE {sdf_timescale})\n"
        sdf_text = (tmp_path / "and2.sdf").read_text().replace(original, header)
        sdf_text = sdf_text.replace("(0.5:0.5:0.5)", f"({delay}:{delay}:{delay})")
    (tmp_path / "and2.sdf").write_text(sdf_text)
    declaration = "`timescale 1ns/1ps\n"
    leaf = "`timescale " + leaf_timescale + "\n" if leaf_timescale else ""
    cell = tmp_path / "and_cell.v"
    cell.write_text(cell.read_text().replace(declaration, leaf))
    return request


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


def test_env_is_rejected_with_verilog_top(tmp_path):
    request = _edit(_stage(tmp_path), env={"X": "1"})
    with pytest.raises(FunctionalVerificationError, match="request.env"):
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
    assert report["environment"]["results_xml"] == {
        "path": None,
        "scope": "absent",
        "sha256": None,
    }


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
    # Issue #7983: the stored verdict states which timing it is evidence
    # for -- the SDF's own TIMESCALE and the unit its delays were applied in.
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ns", "1ns")
    assert (
        sdf["file"]["sha256"]
        == "sha256:" + hashlib.sha256((tmp_path / "and2.sdf").read_bytes()).hexdigest()
    )
    source_paths = [
        str(tmp_path / name) for name in ("and_cell.v", "and2.v", "tb_and2.v")
    ]
    assert report["provenance"]["input"] == {
        "content_hash": _combined_content_hash(source_paths),
        "role": "source",
    }


@needs_icarus
def test_integration_sdf_ps_file_passes_once_the_model_declares_ps(tmp_path):
    """The curator's third fixture variant: the physically equivalent ``1ps``
    file passes on the same request once only the leaf's unit is ``1ps``."""
    request = _stage_sdf_units(
        tmp_path, sdf_timescale="1ps", delay="500", leaf_timescale="1ps/1ps"
    )
    report = run_functional_verification(request)
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert sdf["annotated"] is True
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ps", "1ps")


@needs_icarus
def test_integration_sdf_unrelated_module_unit_does_not_invalidate(tmp_path):
    """Only modules receiving a retained delay are compared: a source that
    declares another unit, for a module no delay lands in, changes nothing."""
    request = _stage_sdf_units(tmp_path)
    (tmp_path / "unused.v").write_text(
        "`timescale 1ps/1ps\nmodule unused_cell; endmodule\n"
    )
    doc = json.loads(Path(request).read_text())
    doc["sources"].append("unused.v")
    Path(request).write_text(json.dumps(doc))
    report = run_functional_verification(request)
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ns", "1ns")


@needs_icarus
def test_integration_sdf_interconnect_mode_compares_only_retained_delays(tmp_path):
    """``entries: "interconnect"`` drops every IOPATH, so the ``1ps`` leaf no
    longer receives a delay and its unit is no longer compared; the retained
    INTERCONNECT uses the ``1ns`` annotation module, which matches the SDF."""
    request = _stage_sdf_units(
        tmp_path,
        leaf_timescale="1ps/1ps",
        sdf_text="""\
(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "and2")
  (DIVIDER .)
  (TIMESCALE 1ns)
  (CELL (CELLTYPE "and2") (INSTANCE)
    (DELAY (ABSOLUTE (INTERCONNECT a u1.A (0.5:0.5:0.5)))))
  (CELL (CELLTYPE "and_cell") (INSTANCE u1)
    (DELAY (ABSOLUTE (IOPATH A Y (0.5:0.5:0.5)))))
)
""",
    )
    sdf_option = {"file": "and2.sdf", "corner": "typ", "entries": "interconnect"}
    request = _edit(request, **{"options.sdf": sdf_option})
    report = run_functional_verification(request)
    assert report["status"] == "pass"
    sdf = report["environment"]["sdf"]
    assert (sdf["timescale"], sdf["time_unit"]) == ("1ns", "1ns")
    assert sdf["dropped"]["iopath_interconnect_only"]["count"] == 1


@needs_icarus
@pytest.mark.parametrize(
    ("sdf_timescale", "delay", "y_event"),
    [("1ps", "100", "105.000"), ("1ns", "0.100", "5.100")],
    ids=["ps-file-on-ns-model", "same-delay-in-ns"],
)
def test_icarus_applies_sdf_delays_in_the_module_unit(
    tmp_path, sdf_timescale, delay, y_event
):
    """The simulator fact the pre-build refusal rests on (issue #7983's first
    table row and its control), on raw ``iverilog``/``vvp`` with no klt in
    between: the same 100 ps path delay, written as ``100`` in a ``1ps`` file,
    lands at 105 ns on a ``1ns/1ps`` leaf, and as ``0.100`` in a ``1ns`` file
    at 5.1 ns. Icarus ignores the file's TIMESCALE."""
    (tmp_path / "u.v").write_text(
        "`timescale 1ns/1ps\n"
        "module leaf(input a, output y);\n"
        "  specify (a => y) = 0; endspecify\n"
        "  buf (y, a);\n"
        "endmodule\n"
        "`timescale 1ns/1ps\n"
        "module tb;\n"
        "  reg a = 0; wire y;\n"
        "  leaf u(.a(a), .y(y));\n"
        "  initial begin #5 a = 1; #200 $finish; end\n"
        '  always @(y) $display("evt t=%0.3f a=%b y=%b", $realtime, a, y);\n'
        "endmodule\n"
        'module annot(); initial $sdf_annotate("d.sdf", tb.u); endmodule\n'
    )
    (tmp_path / "d.sdf").write_text(
        '(DELAYFILE (SDFVERSION "3.0")\n'
        f" (TIMESCALE {sdf_timescale})\n"
        ' (CELL (CELLTYPE "leaf") (INSTANCE)\n'
        f"  (DELAY (ABSOLUTE (IOPATH a y ({delay}:{delay}:{delay}))))))\n"
    )
    subprocess.run(
        ["iverilog", "-g2012", "-gspecify", "-o", "u.vvp", "u.v"],
        cwd=tmp_path,
        check=True,
    )
    ran = subprocess.run(
        ["vvp", "u.vvp"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    # The input rises at 5 ns; the output event is what the SDF delay moved.
    assert ran.stdout.splitlines()[:2] == [
        "evt t=0.000 a=0 y=0",
        f"evt t={y_event} a=1 y=1",
    ]


@needs_icarus
def test_integration_sdf_that_fails_to_annotate_still_raises(tmp_path):
    request = _stage(tmp_path, "request-sdf.json")
    sdf = tmp_path / "and2.sdf"
    sdf.write_text(sdf.read_text().replace("INSTANCE u1", "INSTANCE nosuch"))
    with pytest.raises(FunctionalVerificationError, match="did not fully apply"):
        run_functional_verification(str(request))


# -- testbench.plusargs (issue #5006) ----------------------------------------


@pytest.mark.parametrize(
    "value,match",
    [
        ("+a=1", "array of strings"),
        ([1], r"plusargs\[0\] must be a non-empty string"),
        ([""], r"plusargs\[0\] must be a non-empty string"),
        (["+ok", "bin=x"], r"plusargs\[1\] must start with '\+'"),
    ],
)
def test_plusargs_validation(tmp_path, value, match):
    request = _edit(_stage(tmp_path), **{"testbench.plusargs": value})
    with pytest.raises(FunctionalVerificationError, match=match):
        run_functional_verification(request)


# `testbench.plusargs` with `testbench.module` is no longer an unknown
# field: issue #6906 accepts and forwards it on the cocotb path. Its
# validation, reservation, forwarding, and echo are covered by
# tests/test_functional_verification_plusargs.py.


@needs_icarus
def test_integration_plusargs_forwarded_and_echoed(tmp_path):
    _stage(tmp_path)
    _rewrite_bench(
        tmp_path,
        '    if (errors == 0) $display("PASS");',
        '    if (errors == 0 && $test$plusargs("go") && '
        '$value$plusargs("file=%s", f)) $display("PASS %0s", f);',
    )
    _rewrite_bench(
        tmp_path, "integer errors = 0;", "integer errors = 0; reg [255:0] f;"
    )
    absent = run_functional_verification(str(tmp_path / "request-rtl.json"))
    assert absent["status"] == "fail"
    assert absent["environment"]["plusargs"] == []
    request = _edit(
        tmp_path / "request-rtl.json",
        **{"testbench.plusargs": ["+go", "+file=a b=c"]},
    )
    report = run_functional_verification(request)
    assert report["status"] == "pass"
    assert report["environment"]["plusargs"] == ["+go", "+file=a b=c"]
