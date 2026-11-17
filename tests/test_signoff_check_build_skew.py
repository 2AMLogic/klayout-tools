"""``klt signoff --check`` report-only ``build_skew`` (issues #5868, #5852)."""

from __future__ import annotations

import pytest

from klayout_tools.cli import main
from klayout_tools.cli.output import render_rerun_drift
from klayout_tools.signoff import _check_signoff_report, build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    _manifest,
    _write,
)

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


# --------------------------------------------------------------------------- #
# The `unverifiable` state (issue #5852): `_build_drift`'s absence-of-evidence
# rule (issue #4223) applied to the `build` comparison -- a side whose
# identity cannot be resolved (`+unknown`, a source-tarball install) must not
# turn the report-only signal into an assertion the facts cannot support.
# --------------------------------------------------------------------------- #


def test_two_unresolvable_identities_are_unverifiable_not_same():
    """The false-`"same"` this state exists to fix: two `+unknown` builds
    that agree on every recorded field used to read as `"same"`, but
    neither side can resolve its identity, so nothing is established."""
    tarball = {**_BUILD, "version": "0.9.0.dev0+unknown"}
    result = _check_signoff_report("r.json", _report(tarball), _report(tarball))
    assert result["build_skew"]["state"] == "unverifiable"
    assert result["build_skew"]["fields"] == []
    assert result["status"] == "match"


def test_committed_tarball_identity_is_unverifiable_not_build_only():
    """A `+unknown` committed side whose recorded fields all differ from the
    running build's: the difference is *named* in `fields`, but the state
    says `unverifiable` -- `build_only` would assert "rendered by a
    different klt build" when the committed side records no git facts to
    support that."""
    committed = _report({**_BUILD, "version": "0.5.0+unknown", "is_release": None})
    result = _check_signoff_report("r.json", committed, _report())
    assert result["build_skew"]["state"] == "unverifiable"
    assert {f["field"] for f in result["build_skew"]["fields"]} >= {
        "build.version",
        "build.is_release",
    }
    assert result["status"] == "match"


def test_running_tarball_identity_is_unverifiable():
    """The mirror case: the *running* build is the `+unknown` one -- a
    tarball install re-checking a report committed by a real build."""
    fresh = _report({**_BUILD, "version": "0.9.1.dev0+unknown"})
    result = _check_signoff_report("r.json", _report(), fresh)
    assert result["build_skew"]["state"] == "unverifiable"
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
        # `unverifiable` prints its one line too -- with the recorded field
        # names when they differ, without them when both sides agree.
        ({**_BUILD, "version": "0.9.0+unknown"}, "build identity unverifiable"),
        (
            {**_BUILD, "version": "0.9.0+unknown", "git_commit": "bbb"},
            "build identity unverifiable",
        ),
        (_BUILD, None),
        # `unknown` (committed report predates the `build` block) is silent
        # too: there is nothing to name, so the text gate narrows to the
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


def test_cli_check_reports_a_dev_built_report_without_failing(tmp_path, capsys):
    """End to end: a report committed by an unreleased dev build re-checked
    by a different build still exits `0` (`status: match`) -- and the text
    render names the skew once, field *names* only, never the values (the
    same convention as `doc_drift_fields`)."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest = _manifest(evidence={"3": drc_path})
    manifest_path = _write(tmp_path, "manifest.json", manifest)
    fresh = build_tier_report(manifest)
    dev = dict(fresh["build"])
    dev["version"] = "0.9.0.dev1+g0000000000000000000000000000000000000000"
    dev["is_release"] = not fresh["build"]["is_release"]
    dev["grading_ruleset_id"] = "sha256:another-grading-ruleset"
    committed_path = _write(tmp_path, "committed.json", {**fresh, "build": dev})

    exit_code = main(
        ["signoff", "--manifest", str(manifest_path), "--check", str(committed_path)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "status: match" in out
    assert "build_skew: graded under a different ruleset" in out
    for field in ("build.version", "build.is_release", "build.grading_ruleset_id"):
        assert field in out
    # Names only -- the values stay in the JSON payload.
    assert "sha256:another-grading-ruleset" not in out

    # The same report re-committed by the same install is quiet: no line.
    same_path = _write(tmp_path, "same.json", fresh)
    exit_code = main(
        ["signoff", "--manifest", str(manifest_path), "--check", str(same_path)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "build_skew" not in out
