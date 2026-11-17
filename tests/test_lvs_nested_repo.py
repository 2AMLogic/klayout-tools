"""Issue #5631: one repo root per LVS report, so a nested-repo input round-trips."""

import json
import os

import pytest

from klayout_tools.env_provenance import find_repo_root
from klayout_tools.env_provenance_walk import common_repo_root
from klayout_tools.lvs import check_lvs_report, rerun_lvs_report, run_lvs

_SPICE = ".SUBCKT top A Y VSS\nR1 A Y 1k\n.ENDS\n"


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _roots(paths):
    return common_repo_root(paths, find_repo_root=find_repo_root)


def _report(cwd, layout_rel, ref_rel):
    request = cwd / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": layout_rel, "top": "top"},
                "reference": {"netlist": ref_rel, "top": "top"},
            }
        )
    )
    report = cwd / "evidence" / "r.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(run_lvs(str(request))))
    return report


def test_common_repo_root_cases(tmp_path):
    outer = _repo(tmp_path / "outer")
    inner = _repo(outer / "vendored")
    sibling = _repo(tmp_path / "sibling")
    for repo in (outer, inner, sibling):
        (repo / "f").write_text("x")
    (tmp_path / "loose").mkdir()
    (tmp_path / "loose" / "f").write_text("x")
    real = os.path.realpath
    assert _roots([outer / "f", inner / "f"]) == real(outer)
    assert _roots([inner / "f", outer / "f"]) == real(outer)
    assert _roots([outer / "f", sibling / "f"]) == real(outer)
    assert _roots([sibling / "f", outer / "f"]) == real(sibling)
    assert _roots([tmp_path / "loose" / "f", inner / "f"]) == real(inner)
    assert _roots([tmp_path / "loose" / "f", None]) is None
    assert _roots([]) is None


@pytest.mark.parametrize("gitfile", [False, True])
def test_nested_reference_round_trips(tmp_path, monkeypatch, gitfile):
    outer = _repo(tmp_path / "outer")
    inner = outer / "vendored"
    inner.mkdir()
    if gitfile:  # submodule-style `.git` file
        (inner / ".git").write_text("gitdir: ../.git/modules/vendored\n")
    else:
        (inner / ".git").mkdir()
    (outer / "layout.spice").write_text(_SPICE)
    (inner / "ref.spice").write_text(_SPICE)
    monkeypatch.chdir(outer)
    report = _report(outer, "layout.spice", "vendored/ref.spice")
    data = json.loads(report.read_text())
    assert data["layout"] == {"path": "layout.spice", "scope": "repo"}
    assert data["reference"] == {"path": "vendored/ref.spice", "scope": "repo"}
    monkeypatch.chdir(tmp_path)
    assert check_lvs_report(str(report))["status"] == "match"
    assert rerun_lvs_report(str(report))["status"] == "match"


def test_inverse_nesting_round_trips(tmp_path, monkeypatch):
    outer = _repo(tmp_path / "outer")
    inner = _repo(outer / "vendored")
    (inner / "layout.spice").write_text(_SPICE)
    (outer / "ref.spice").write_text(_SPICE)
    monkeypatch.chdir(outer)
    report = _report(outer, "vendored/layout.spice", "ref.spice")
    data = json.loads(report.read_text())
    assert data["layout"] == {"path": "vendored/layout.spice", "scope": "repo"}
    assert data["reference"] == {"path": "ref.spice", "scope": "repo"}
    assert check_lvs_report(str(report))["status"] == "match"


def test_external_layout_keeps_repo_reference(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "layout.spice").write_text(_SPICE)
    (repo / "ref.spice").write_text(_SPICE)
    monkeypatch.chdir(repo)
    report = _report(repo, str(loose / "layout.spice"), "ref.spice")
    data = json.loads(report.read_text())
    assert data["layout"]["scope"] == "external"
    assert data["reference"] == {"path": "ref.spice", "scope": "repo"}
