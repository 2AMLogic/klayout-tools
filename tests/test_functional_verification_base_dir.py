"""`klt functional-verification --base-dir` tests (issue #5673).

Split out of ``tests/test_functional_verification.py`` for the per-file
size ratchet: that module is already baselined over the 500-line hard
limit, so these tests live in their own module instead of growing it.
Shared fixtures and fake runners are imported from the parent module
(same pattern as ``test_characterize_keep_going.py`` et al.).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from klayout_tools import functional_verification as fv
from klayout_tools.functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from test_functional_verification import (
    _GCD_RTL,
    _MUTATION_BASELINE_XML,
    _MUTATION_DUT_RTL,
    _MUTATION_KILLED_XML,
    _RESULTS_XML_WITH_FAILURE,
    _TESTBENCH,
    _base_request,
    _ContentAwareFakeRunner,
    _FakeRunner,
    _mutation_proposals_doc,
    _stub_mutation_runner,
    _stub_runner,
    _write,
    _write_proposals,
    _write_request,
)

# --------------------------------------------------------------------------- #
# --base-dir: archived-request anchor override (issue #5673)
# --------------------------------------------------------------------------- #


def _write_archived_layout(tmp_path: Path) -> tuple[Path, str]:
    """Issue #5673's motivating layout: the design lives under
    ``<tmp_path>/proj`` and the request is archived under
    ``proj/records/r1/`` -- the paths inside the request are relative to the
    project root, not to the record directory the file sits in. Returns
    ``(project root, request path)``."""
    base = tmp_path / "proj"
    (base / "rtl").mkdir(parents=True)
    _write(base / "rtl" / "gcd.v", _GCD_RTL)
    _write(base / "test_gcd.py", _TESTBENCH)
    (base / "records" / "r1").mkdir(parents=True)
    request_path = _write_request(
        base / "records" / "r1" / "request.json",
        _base_request(sources=["rtl/gcd.v"]),
    )
    return base, request_path


def test_load_request_arg_base_dir_overrides_file_anchor(tmp_path):
    base, request_path = _write_archived_layout(tmp_path)
    _, default_anchor = fv.load_request_arg(request_path)
    assert Path(default_anchor) == (base / "records" / "r1").resolve()
    _, anchor = fv.load_request_arg(request_path, base_dir=str(base))
    assert Path(anchor) == base.resolve()


def test_load_request_arg_base_dir_overrides_stdin_and_inline_cwd(
    tmp_path, monkeypatch
):
    """`--base-dir` overrides the cwd default on the stdin/inline forms too --
    the same override code path every form shares."""
    base, _ = _write_archived_layout(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    monkeypatch.setattr(
        "sys.stdin", __import__("io").StringIO(json.dumps(_base_request()))
    )
    _, anchor = fv.load_request_arg("-", base_dir=str(base))
    assert Path(anchor) == base.resolve()
    _, anchor = fv.load_request_arg(json.dumps(_base_request()), base_dir=str(base))
    assert Path(anchor) == base.resolve()


def test_base_dir_relative_resolves_against_cwd(tmp_path, monkeypatch):
    base, request_path = _write_archived_layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    _, anchor = fv.load_request_arg(request_path, base_dir="proj")
    assert Path(anchor) == base.resolve()


def test_base_dir_missing_or_not_a_directory_is_an_error(tmp_path):
    """A `--base-dir` that does not exist or is not a directory is exit 1
    through the standard error envelope -- never a silent fallback to the
    request's own directory."""
    base, request_path = _write_archived_layout(tmp_path)
    missing = tmp_path / "no-such-dir"
    expected = re.escape(f"base directory '{missing}' is not a directory")
    with pytest.raises(FunctionalVerificationError, match=expected):
        run_functional_verification(request_path, base_dir=str(missing))
    with pytest.raises(FunctionalVerificationError, match="is not a directory"):
        fv.load_request_arg(request_path, base_dir=str(base / "rtl" / "gcd.v"))


def test_archived_request_without_base_dir_fails(tmp_path):
    """The gap, reproduced: the same document that runs from the project root
    cannot resolve its sources from the record directory it is archived in."""
    _, request_path = _write_archived_layout(tmp_path)
    with pytest.raises(
        FunctionalVerificationError, match="RTL source not found: rtl/gcd.v"
    ):
        run_functional_verification(request_path)


def test_archived_request_with_base_dir_runs(tmp_path, monkeypatch):
    base, request_path = _write_archived_layout(tmp_path)
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))

    report = run_functional_verification(request_path, base_dir=str(base))

    assert report["status"] == "fail"
    assert runner.build_kwargs["sources"] == [str((base / "rtl" / "gcd.v").resolve())]
    assert runner.test_kwargs["test_module"] == "test_gcd"
    # The default artifact directory follows the anchor too, matching the
    # stdin form rather than carving out an exception.
    results_xml = Path(report.local_paths["results_xml"]).resolve()
    assert results_xml.parent.parent.parent == base.resolve()


def test_base_dir_equivalent_to_stdin_form_from_cwd(tmp_path, monkeypatch):
    """The whole feature in one statement: a file request run with
    `--base-dir D` resolves every relative path exactly as the same document
    piped on stdin with `cwd=D`."""
    base, request_path = _write_archived_layout(tmp_path)
    document = Path(request_path).read_text(encoding="utf-8")

    runner_flag = _FakeRunner(_RESULTS_XML_WITH_FAILURE)
    _stub_runner(monkeypatch, runner_flag)
    report_flag = run_functional_verification(request_path, base_dir=str(base))

    monkeypatch.chdir(base)
    runner_stdin = _FakeRunner(_RESULTS_XML_WITH_FAILURE)
    _stub_runner(monkeypatch, runner_stdin)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(document))
    report_stdin = run_functional_verification("-")

    assert runner_flag.build_kwargs["sources"] == runner_stdin.build_kwargs["sources"]
    assert (
        Path(report_flag.local_paths["results_xml"]).resolve()
        == Path(report_stdin.local_paths["results_xml"]).resolve()
    )
    assert (
        Path(report_flag.local_paths["test_log"]).resolve()
        == Path(report_stdin.local_paths["test_log"]).resolve()
    )


def test_base_dir_anchors_every_relative_path_field(tmp_path):
    """Every request-relative field resolves against `--base-dir`: sources,
    testbench.search_path, options.includes, options.sdf.file,
    options.initial_state.netlist and options.source_defines keys."""
    base = tmp_path / "proj"
    for sub in ("rtl", "tb", "inc", "sdf", "net", "records/r1"):
        (base / sub).mkdir(parents=True)
    _write(base / "rtl" / "gcd.v", _GCD_RTL)
    _write(base / "tb" / "test_gcd.py", _TESTBENCH)
    _write(base / "sdf" / "gcd.sdf", "(DELAYFILE)\n")
    _write(base / "net" / "gcd_netlist.v", "module gcd(); endmodule\n")
    request_path = _write_request(
        base / "records" / "r1" / "request.json",
        _base_request(
            sources=["rtl/gcd.v"],
            testbench={"module": "test_gcd", "search_path": "tb"},
            options={
                "coverage": False,
                "includes": ["inc"],
                "sdf": {"file": "sdf/gcd.sdf"},
                "initial_state": {"mode": "x", "netlist": "net/gcd_netlist.v"},
                "source_defines": {"rtl/gcd.v": {"FUNCTIONAL": None}},
            },
        ),
    )

    doc, anchor = fv.load_request_arg(request_path, base_dir=str(base))
    assert Path(anchor) == base.resolve()

    sources = fv._resolve_sources(doc["sources"], anchor)
    assert sources == [str((base / "rtl" / "gcd.v").resolve())]

    module, module_dir, _testcase = fv._resolve_testbench(doc["testbench"], anchor)
    assert module == "test_gcd"
    assert module_dir == str((base / "tb").resolve())

    (
        _coverage,
        _timescale,
        _seed,
        _defines,
        _build_args,
        includes,
        sdf,
        _trace,
    ) = fv._resolve_options(doc["options"], "icarus", anchor)
    assert includes == [str((base / "inc").resolve())]
    assert sdf["file"] == str((base / "sdf" / "gcd.sdf").resolve())

    initial_state = fv._resolve_initial_state(doc["options"], "icarus", anchor)
    assert initial_state["netlist"] == str((base / "net" / "gcd_netlist.v").resolve())

    source_defines = fv._resolve_source_defines(doc["options"], sources, anchor)
    assert source_defines == {
        str((base / "rtl" / "gcd.v").resolve()): {"FUNCTIONAL": None}
    }


def test_base_dir_leaves_absolute_request_paths_alone(tmp_path):
    """Absolute paths inside the request are unaffected by `--base-dir` --
    the override replaces the anchor, not the absoluteness check."""
    base, _ = _write_archived_layout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write(elsewhere / "gcd.v", _GCD_RTL)
    request_path = _write_request(
        elsewhere / "request.json",
        _base_request(sources=[str(elsewhere / "gcd.v")]),
    )
    doc, anchor = fv.load_request_arg(request_path, base_dir=str(base))
    assert fv._resolve_sources(doc["sources"], anchor) == [
        str((elsewhere / "gcd.v").resolve())
    ]


def test_base_dir_outdir_still_wins_and_resolves_against_cwd(tmp_path, monkeypatch):
    base, request_path = _write_archived_layout(tmp_path)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_FAILURE))
    monkeypatch.chdir(tmp_path)

    report = run_functional_verification(
        request_path, output_dir="out", base_dir=str(base)
    )

    assert (
        Path(report.local_paths["results_xml"]).resolve()
        == (tmp_path / "out" / "results_icarus.xml").resolve()
    )


def test_mutations_honour_base_dir(tmp_path, monkeypatch):
    """`--base-dir` + `--mutations`: the shape impossible on stdin today,
    because `--mutations` consumes stdin before the baseline re-reads it."""
    base = tmp_path / "proj"
    (base / "rtl").mkdir(parents=True)
    _write(base / "rtl" / "dut.v", _MUTATION_DUT_RTL)
    _write(base / "test_dut.py", _TESTBENCH)
    (base / "records" / "r1").mkdir(parents=True)
    request_path = _write_request(
        base / "records" / "r1" / "request.json",
        _base_request(
            sources=["rtl/dut.v"],
            hdl_toplevel="dut",
            testbench={"module": "test_dut", "testcase": None},
        ),
    )
    doc = _mutation_proposals_doc()
    doc["scope"] = ["rtl/dut.v"]
    for proposal in doc["proposals"]:
        proposal["file"] = "rtl/dut.v"
    proposals_path = _write_proposals(tmp_path / "proposals.json", doc)

    _stub_mutation_runner(
        monkeypatch,
        _ContentAwareFakeRunner(
            _MUTATION_BASELINE_XML, _MUTATION_KILLED_XML, "wire a = 9;"
        ),
    )

    report = run_functional_verification_with_mutations(
        request_path, proposals_path, base_dir=str(base)
    )

    assert report["status"] == "pass"
    assert report["mutation_testing"]["proposal_count"] == 2
