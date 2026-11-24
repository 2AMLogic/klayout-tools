"""Functional-verification path envelopes use the request base (#6762, #6779).

A request file uses its parent directory by default. Stdin and inline JSON
default to the invoking directory. A fixed absolute ``--base-dir`` pins the
base for any request form.

Split out of ``tests/test_functional_verification.py`` for the per-file size
ratchet (that module is baselined far over the hard limit); shared fixtures
are imported from it, the pattern ``test_functional_verification_base_dir.py``
uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.functional_verification import (
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from test_functional_verification import (
    _MUTATION_BASELINE_XML,
    _MUTATION_DUT_RTL,
    _MUTATION_KILLED_XML,
    _RESULTS_XML_WITH_SKIP,
    _TESTBENCH,
    _base_request,
    _ContentAwareFakeRunner,
    _FakeRunner,
    _mutation_proposals_doc,
    _setup_inputs,
    _stub_mutation_runner,
    _stub_runner,
    _write,
    _write_proposals,
    _write_request,
)
from test_functional_verification_verilog_top import _stage, needs_icarus

_ARTIFACTS = ("results_xml", "test_log", "build_log")
_OUT = "proj/.klt/functional-verification/"


def _checkout(root: Path, *, git_file: bool = False) -> Path:
    """A directory ``find_repo_root`` recognises (a ``.git`` entry is all it
    checks); ``git_file`` makes it the ``.git`` *file* of a linked worktree."""
    root.mkdir(parents=True, exist_ok=True)
    if git_file:
        (root / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    else:
        (root / ".git").mkdir()
    return root


def _invoking_dirs(tmp_path: Path, proj: Path) -> dict[str, Path]:
    """The directories one request is driven from: inside its own checkout,
    outside every checkout, and inside an unrelated one."""
    driver = tmp_path / "driver"
    driver.mkdir()
    return {
        "own checkout": proj,
        "outside any checkout": driver,
        "unrelated checkout": _checkout(tmp_path / "checkout-b"),
    }


def _envelopes(report: dict) -> dict:
    env = report["environment"]
    return {"sources": env["sources"], **{key: env[key] for key in _ARTIFACTS}}


def _staged_project(tmp_path: Path, *, git_file: bool = False) -> tuple[Path, str]:
    """``checkout-a/proj`` holding a stubbed-cocotb gcd request."""
    proj = _checkout(tmp_path / "checkout-a", git_file=git_file) / "proj"
    proj.mkdir()
    _setup_inputs(proj)
    return proj, _write_request(proj / "request.json", _base_request())


@pytest.mark.parametrize("git_file", [False, True], ids=["git-dir", "linked-worktree"])
def test_envelopes_do_not_depend_on_the_invoking_directory(
    tmp_path, monkeypatch, git_file
):
    """The headline case: request + sources in a checkout, ``klt`` driven from
    inside it, from outside every checkout and from an unrelated one."""
    proj, request = _staged_project(tmp_path, git_file=git_file)
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    runs = {}
    for label, cwd in _invoking_dirs(tmp_path, proj).items():
        monkeypatch.chdir(cwd)
        runs[label] = _envelopes(run_functional_verification(request))

    reference = runs["own checkout"]
    assert reference["sources"][0]["path"] == "proj/gcd.v"
    assert reference["sources"][0]["scope"] == "repo"
    for key in _ARTIFACTS:
        assert reference[key]["scope"] == "repo", key
        assert reference[key]["path"].startswith(_OUT), key
    for label, got in runs.items():
        assert json.dumps(got, sort_keys=True) == json.dumps(
            reference, sort_keys=True
        ), label


def test_request_outside_any_checkout_is_external_never_absolute(tmp_path, monkeypatch):
    loose = tmp_path / "loose"
    loose.mkdir()
    _setup_inputs(loose)
    request = _write_request(loose / "request.json", _base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    for cwd in (tmp_path, _checkout(tmp_path / "checkout-b")):
        monkeypatch.chdir(cwd)
        got = _envelopes(run_functional_verification(request))
        assert got["sources"][0]["path"] is None
        assert got["sources"][0]["scope"] == "external"
        for key in _ARTIFACTS:
            assert (got[key]["path"], got[key]["scope"]) == (None, "external"), key
        assert str(tmp_path) not in json.dumps(got)


def test_output_dir_outside_the_request_checkout_is_external(tmp_path, monkeypatch):
    """``--outdir`` is judged against the request's checkout like any other
    path, so an output directory inside the *invoking* checkout (but outside
    the request's) is ``external`` -- and the same from any cwd."""
    proj, request = _staged_project(tmp_path)
    other = _checkout(tmp_path / "checkout-b")
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))
    monkeypatch.chdir(other)

    got = _envelopes(run_functional_verification(request, str(other / "out")))

    assert got["sources"][0]["scope"] == "repo"
    for key in _ARTIFACTS:
        assert (got[key]["path"], got[key]["scope"]) == (None, "external"), key


def test_base_dir_anchors_the_inline_request_form(tmp_path, monkeypatch):
    """``--base-dir`` re-anchors relative paths for the stdin/inline forms,
    and the envelopes follow it: the cwd plays no part."""
    proj, request = _staged_project(tmp_path)
    inline = json.dumps(json.loads(Path(request).read_text(encoding="utf-8")))
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    runs = []
    for cwd in _invoking_dirs(tmp_path, proj).values():
        monkeypatch.chdir(cwd)
        runs.append(_envelopes(run_functional_verification(inline, base_dir=str(proj))))

    assert runs[0]["sources"][0]["path"] == "proj/gcd.v"
    assert runs[0]["results_xml"]["path"].startswith(_OUT)
    assert runs[1] == runs[0]
    assert runs[2] == runs[0]


def test_inline_request_without_base_dir_follows_the_cwd(tmp_path, monkeypatch):
    """The two layouts give the same inline source different repo-relative
    paths. Outside a checkout, the same source is external (#6779)."""
    flat = _checkout(tmp_path / "checkout-a")
    _setup_inputs(flat)
    nested = _checkout(tmp_path / "checkout-b") / "proj"
    nested.mkdir()
    _setup_inputs(nested)
    inline = json.dumps(_base_request())
    _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    runs = {}
    for label, cwd in {"flat checkout": flat, "nested checkout": nested}.items():
        monkeypatch.chdir(cwd)
        runs[label] = _envelopes(run_functional_verification(inline))

    assert runs["flat checkout"]["sources"][0] == {
        "path": "gcd.v",
        "scope": "repo",
        "sha256": runs["flat checkout"]["sources"][0]["sha256"],
    }
    assert runs["nested checkout"]["sources"][0]["path"] == "proj/gcd.v"
    assert runs["nested checkout"]["sources"][0]["scope"] == "repo"
    assert runs["flat checkout"]["results_xml"]["path"].startswith(
        ".klt/functional-verification/"
    )
    assert runs["nested checkout"]["results_xml"]["path"].startswith(_OUT)
    assert json.dumps(runs["flat checkout"], sort_keys=True) != json.dumps(
        runs["nested checkout"], sort_keys=True
    )

    outside = tmp_path / "outside"
    outside.mkdir()
    _setup_inputs(outside)
    monkeypatch.chdir(outside)
    got = _envelopes(run_functional_verification(inline))
    assert got["sources"][0]["path"] is None
    assert got["sources"][0]["scope"] == "external"
    for key in _ARTIFACTS:
        assert (got[key]["path"], got[key]["scope"]) == (None, "external"), key


@pytest.mark.parametrize(
    ("scope", "envelope_path", "message"),
    [
        ("repo", "proj/gcd.v", "see proj/gcd.v:17"),
        ("external", None, "see gcd.v:17"),
    ],
    ids=["in-a-repo", "outside-every-checkout"],
)
def test_source_envelope_and_failure_message_agree_in_one_report(
    tmp_path, monkeypatch, scope, envelope_path, message
):
    """#6769, the #6762 criterion PR #6766 left open: within a *single*
    report, the envelope's ``sources[0].{path,scope}`` and the failure
    message the #6760/#6761 normalizer renders tell the same story. In a
    repo the diagnostic quotes the envelope path verbatim (``message`` is
    ``see {envelope_path}:17``); outside every repo the envelope says
    ``external``/``null`` (#6762 criterion 2) while the diagnostic falls
    back to a request-dir-relative path -- a deliberate divergence to pin,
    not collapse."""
    if envelope_path is not None:
        proj, request = _staged_project(tmp_path)
        source = proj / "gcd.v"
    else:
        loose = tmp_path / "loose"
        loose.mkdir()
        _setup_inputs(loose)
        request = _write_request(loose / "request.json", _base_request())
        source = loose / "gcd.v"
    xml = (
        f'<testsuite><testcase name="test_gcd">'
        f'<failure message="see {source}:17"/></testcase></testsuite>'
    )
    _stub_runner(monkeypatch, _FakeRunner(xml))
    monkeypatch.chdir(_checkout(tmp_path / "checkout-b"))

    report = run_functional_verification(request)

    assert report["status"] == "fail"
    assert (report["test_count"], report["failed_count"]) == (1, 1)
    rendered = report["environment"]["sources"][0]
    assert (rendered["path"], rendered["scope"]) == (envelope_path, scope)
    (test,) = report["tests"]
    # Exact equality: an absolute prefix or a basename-only rendering of the
    # in-repo case both fail here.
    assert test["error_message"] == message
    assert str(tmp_path) not in test["error_message"]


def test_mutation_log_paths_do_not_depend_on_the_invoking_directory(
    tmp_path, monkeypatch
):
    proj = _checkout(tmp_path / "checkout-a") / "proj"
    proj.mkdir()
    _write(proj / "dut.v", _MUTATION_DUT_RTL)
    _write(proj / "test_dut.py", _TESTBENCH)
    request = _write_request(
        proj / "request.json",
        _base_request(
            sources=["dut.v"],
            hdl_toplevel="dut",
            testbench={"module": "test_dut", "testcase": None},
        ),
    )
    proposals = _write_proposals(tmp_path / "proposals.json", _mutation_proposals_doc())
    _stub_mutation_runner(
        monkeypatch,
        _ContentAwareFakeRunner(
            _MUTATION_BASELINE_XML, _MUTATION_KILLED_XML, "wire a = 9;"
        ),
    )

    runs = {}
    for label, cwd in _invoking_dirs(tmp_path, proj).items():
        monkeypatch.chdir(cwd)
        report = run_functional_verification_with_mutations(request, proposals)
        runs[label] = {
            "log_paths": [m["log_path"] for m in report["mutation_testing"]["results"]],
            "envelopes": _envelopes(report),
        }

    reference = runs["own checkout"]
    assert any(p["scope"] == "repo" for p in reference["log_paths"])
    for p in reference["log_paths"]:
        assert p["scope"] != "repo" or p["path"].startswith("proj/.klt/")
    for label, got in runs.items():
        assert json.dumps(got, sort_keys=True) == json.dumps(
            reference, sort_keys=True
        ), label


@needs_icarus
def test_verilog_top_envelopes_do_not_depend_on_the_invoking_directory(
    tmp_path, monkeypatch
):
    proj = _checkout(tmp_path / "checkout-a") / "proj"
    proj.mkdir()
    request = str(_stage(proj))

    runs = {}
    for label, cwd in _invoking_dirs(tmp_path, proj).items():
        monkeypatch.chdir(cwd)
        runs[label] = _envelopes(run_functional_verification(request))

    reference = runs["own checkout"]
    assert reference["results_xml"]["scope"] == "absent"
    for key in ("test_log", "build_log"):
        assert reference[key]["scope"] == "repo", key
        assert reference[key]["path"].startswith(_OUT), key
    for label, got in runs.items():
        assert json.dumps(got, sort_keys=True) == json.dumps(
            reference, sort_keys=True
        ), label
