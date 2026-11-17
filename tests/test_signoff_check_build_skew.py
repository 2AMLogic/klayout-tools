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
        # `unknown` (committed report predates the `build` block) is silent
        # too: there is nothing to name, so the text gate narrows to the two
        # states above.
        (None, None),
    ],
)
def test_text_line(capsys, build, needle):
    render_rerun_drift(_check_signoff_report("r.json", _report(build), _report()))
    out = capsys.readouterr().out
    if needle is None:
        assert "build_skew" not in out
    else:
        assert "build_skew:" in out and needle in out


def test_cli_check_text_output_omits_doc_drift_for_the_other_verbs(capsys):
    """`render_rerun_drift` is shared with five other verbs' `--rerun`, whose
    payloads carry no `doc_drift` key -- their output must stay byte-identical
    to what it was before issue #2526."""
    render_rerun_drift(
        {
            "report": "committed.json",
            "status": "drifted",
            "drift": [{"field": "violation_count", "committed": 0, "fresh": 2}],
        }
    )

    assert capsys.readouterr().out == (
        "report: committed.json\n"
        "status: drifted\n"
        "\n"
        "drift:\n"
        "  violation_count:\n"
        "      committed: 0\n"
        "      fresh:     2\n"
    )
