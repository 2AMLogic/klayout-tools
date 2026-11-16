"""``klt signoff --check`` report-only ``build_skew`` (issue #5868)."""

from __future__ import annotations

import pytest

from klayout_tools.cli.output import render_rerun_drift
from klayout_tools.signoff import _check_signoff_report

_BUILD = {
    "version": "0.9.0",
    "package_version": "0.9.0",
    "git_commit": "aaa",
    "git_tag": "v0.9.0",
    "dirty": False,
    "is_release": True,
    "grading_ruleset_id": "sha256:one",
}


def _report(build=_BUILD, status="met"):
    report = {"items": [{"id": "1", "tier": "T1", "partition": None, "status": status}]}
    if build is not None:
        report["build"] = build
    return report


def test_same_build_is_same():
    result = _check_signoff_report("r.json", _report(), _report())
    assert result["build_skew"] == {"state": "same", "fields": []}


def test_build_only_difference():
    committed = _report({**_BUILD, "git_commit": "bbb", "is_release": False})
    result = _check_signoff_report("r.json", committed, _report())
    skew = result["build_skew"]
    assert skew["state"] == "build_only"
    assert {f["field"] for f in skew["fields"]} == {
        "build.git_commit",
        "build.is_release",
    }
    assert result["status"] == "match"
    assert result["drift"] == []


def test_ruleset_difference():
    committed = _report({**_BUILD, "grading_ruleset_id": "sha256:two"})
    result = _check_signoff_report("r.json", committed, _report())
    assert result["build_skew"]["state"] == "ruleset"
    assert result["status"] == "match"


def test_committed_without_build_is_unknown():
    result = _check_signoff_report("r.json", _report(build=None), _report())
    assert result["build_skew"] == {"state": "unknown", "fields": []}
    assert result["status"] == "match"


def test_skew_does_not_mask_real_drift():
    committed = _report({**_BUILD, "git_commit": "bbb"}, status="unmet")
    result = _check_signoff_report("r.json", committed, _report())
    assert result["status"] == "drifted"
    assert result["build_skew"]["state"] == "build_only"


@pytest.mark.parametrize(
    ("build", "needle"),
    [
        ({**_BUILD, "git_commit": "bbb"}, "different klt build"),
        ({**_BUILD, "grading_ruleset_id": "x"}, "different ruleset"),
        (_BUILD, None),
    ],
)
def test_text_line(capsys, build, needle):
    render_rerun_drift(_check_signoff_report("r.json", _report(build), _report()))
    out = capsys.readouterr().out
    if needle is None:
        assert "build_skew" not in out
    else:
        assert "build_skew:" in out and needle in out
