"""`signoff --check`'s report-only `build_provenance` field (issue #5852).

Split out of `test_signoff.py` when that file hit the file-size ratchet's
ceiling: these tests reuse its envelope fixtures through cross-file imports
(the `test_extract`/`test_artifact_store` precedent), so the ratchet is paid
by moving, not by deleting coverage.
"""

from __future__ import annotations

from klayout_tools import signoff as signoff_module
from klayout_tools.cli import main
from klayout_tools.signoff import (
    build_fleet_report,
    build_tier_report,
    check_fleet_report,
    check_tier_report,
)
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    _committed_from_other_provisioning_route,
    _drc_evidence_beside_its_layout,
    _manifest,
    _write,
)


# --------------------------------------------------------------------------- #
# `--check` vs. the `build` block itself (issue #5852): excluded from the
# verdict (issue #2249) but never invisible -- the response reports,
# additively and report-only, whether the committed report's build identity
# differs from the running one, so "committed by a dev build, re-checked by
# the pinned release" is answerable from the same JSON.
# --------------------------------------------------------------------------- #


#: `report["build"]` as an *unreleased dev build of another commit* would
#: have written it (issue #5852): the same shape, a different identity on
#: exactly `version`/`is_release`/`grading_ruleset_id` -- never `+unknown`,
#: so the two sides stay comparable.
def _dev_build_block(report: dict) -> dict:
    build = dict(report["build"])
    build["version"] = "0.9.0.dev1+g0000000000000000000000000000000000000000"
    build["is_release"] = not build["is_release"]
    build["grading_ruleset_id"] = "sha256:another-grading-ruleset"
    return build


def test_check_verifies_a_report_predating_the_build_block(tmp_path):
    """A report committed before issue #2176 (no `build` key at all) still
    verifies on its graded content -- an excluded path is skipped whether or
    not either side carries it, so its absence is not one spurious drift
    entry."""
    drc_path, _ = _drc_evidence_beside_its_layout(tmp_path)
    manifest = _manifest(evidence={"3": drc_path})
    fresh = build_tier_report(manifest)
    legacy = {key: value for key, value in fresh.items() if key != "build"}
    committed_path = _write(tmp_path, "committed.json", legacy)

    result = check_tier_report(committed_path, manifest)

    assert result["status"] == "match"
    assert result["drift"] == []
    # Issue #5852: no `build` block at all is `unverifiable` -- absence of
    # evidence is never `different` -- with nulls echoed for the committed
    # side and the running build still identified.
    assert result["build_provenance"]["state"] == "unverifiable"
    assert result["build_provenance"]["differing_fields"] == []
    assert result["build_provenance"]["committed"] == {
        "version": None,
        "is_release": None,
        "grading_ruleset_id": None,
    }


def test_check_reports_build_provenance_same_for_the_same_build(tmp_path):
    """The quiet case: a report re-committed by the very install that
    rendered it (identical `build` block) answers `same`, nothing
    differing, verdict untouched."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest = _manifest(evidence={"3": drc_path})
    fresh = build_tier_report(manifest)
    committed_path = _write(tmp_path, "committed.json", fresh)

    result = check_tier_report(committed_path, manifest)

    assert result["status"] == "match"
    assert result["build_provenance"] == {
        "state": "same",
        "differing_fields": [],
        "committed": {
            "version": fresh["build"]["version"],
            "is_release": fresh["build"]["is_release"],
            "grading_ruleset_id": fresh["build"]["grading_ruleset_id"],
        },
        "running": {
            "version": fresh["build"]["version"],
            "is_release": fresh["build"]["is_release"],
            "grading_ruleset_id": fresh["build"]["grading_ruleset_id"],
        },
    }


def test_check_reports_build_provenance_different_without_failing(tmp_path):
    """The case the field exists for: a committed report graded by an
    unreleased dev build, re-checked by a different build. The state says
    `different` and names the differing identity fields -- and *nothing else
    moves*: `status` stays `"match"` (exit-0 territory) and `drift` stays
    empty."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest = _manifest(evidence={"3": drc_path})
    fresh = build_tier_report(manifest)
    committed_path = _write(
        tmp_path, "committed.json", {**fresh, "build": _dev_build_block(fresh)}
    )

    result = check_tier_report(committed_path, manifest)

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["build_provenance"]["state"] == "different"
    assert result["build_provenance"]["differing_fields"] == [
        "version",
        "is_release",
        "grading_ruleset_id",
    ]
    assert result["build_provenance"]["committed"] == {
        "version": "0.9.0.dev1+g0000000000000000000000000000000000000000",
        "is_release": not fresh["build"]["is_release"],
        "grading_ruleset_id": "sha256:another-grading-ruleset",
    }
    assert result["build_provenance"]["running"] == {
        "version": fresh["build"]["version"],
        "is_release": fresh["build"]["is_release"],
        "grading_ruleset_id": fresh["build"]["grading_ruleset_id"],
    }


def test_check_build_provenance_unverifiable_for_a_tarball_running_build(
    tmp_path, monkeypatch
):
    """The `+unknown` side may be the *running* build -- a tarball install
    re-checking a report committed by a real one. Every identity field
    differs, but the running side could not resolve its own identity, so
    none of that is established: `unverifiable`, never `different` (the
    same absence-of-evidence rule `_build_drift` applies, issue #4223)."""
    monkeypatch.setattr(
        signoff_module,
        "_build_identity_fields",
        lambda: (
            ("version", "0.9.1.dev0+unknown"),
            ("package_version", "0.9.1"),
            ("git_commit", None),
            ("git_tag", None),
            ("dirty", None),
            ("is_release", None),
            ("grading_ruleset_id", "sha256:tarball-ruleset"),
        ),
    )
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest = _manifest(evidence={"3": drc_path})
    fresh = build_tier_report(manifest)
    assert "+unknown" in fresh["build"]["version"]
    committed_path = _write(
        tmp_path,
        "committed.json",
        {
            **fresh,
            "build": {
                "version": "0.9.0",
                "package_version": "0.9.0",
                "git_commit": "f" * 40,
                "git_tag": "v0.9.0",
                "dirty": False,
                "is_release": True,
                "grading_ruleset_id": "sha256:released-ruleset",
            },
        },
    )

    result = check_tier_report(committed_path, manifest)

    assert result["build_provenance"]["state"] == "unverifiable"
    assert result["build_provenance"]["differing_fields"] == []
    assert result["build_provenance"]["committed"]["version"] == "0.9.0"
    assert result["build_provenance"]["running"] == {
        "version": "0.9.1.dev0+unknown",
        "is_release": None,
        "grading_ruleset_id": "sha256:tarball-ruleset",
    }


def test_check_build_provenance_unverifiable_echoes_a_tarball_committed_side(
    tmp_path,
):
    """The mirror of the tarball test: the `+unknown` side is the
    *committed* report (`_committed_from_other_provisioning_route`, the
    byte-different-but-equal premise of issue #2249). The committed side's
    recorded identity is echoed field-by-field, `unverifiable` -- absence of
    evidence is never `different` -- and the verdict still says `match`."""
    drc_path, _ = _drc_evidence_beside_its_layout(tmp_path)
    manifest = _manifest(evidence={"3": drc_path})
    fresh = build_tier_report(manifest)
    committed_path = _committed_from_other_provisioning_route(
        tmp_path, fresh, "committed.json"
    )

    result = check_tier_report(committed_path, manifest)

    assert result["status"] == "match"
    assert result["build_provenance"]["state"] == "unverifiable"
    assert result["build_provenance"]["differing_fields"] == []
    assert result["build_provenance"]["committed"] == {
        "version": "0.5.0+unknown",
        "is_release": None,
        "grading_ruleset_id": "sha256:whatever-this-install-reads",
    }
    assert result["build_provenance"]["running"] == {
        "version": fresh["build"]["version"],
        "is_release": fresh["build"]["is_release"],
        "grading_ruleset_id": fresh["build"]["grading_ruleset_id"],
    }


def test_check_fleet_carries_build_provenance(tmp_path):
    """`--fleet --check` reports the same field: a roll-up carries exactly
    one top-level `build` block, so the one comparison covers it."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    fleet = {"blocks": [{"block": "b1", "kind": "analog", "evidence": {"3": drc_path}}]}
    fresh = build_fleet_report(fleet)
    committed_path = _write(
        tmp_path, "fleet-committed.json", {**fresh, "build": _dev_build_block(fresh)}
    )

    result = check_fleet_report(committed_path, fleet)

    assert result["status"] == "match"
    assert result["build_provenance"]["state"] == "different"
    assert result["build_provenance"]["differing_fields"] == [
        "version",
        "is_release",
        "grading_ruleset_id",
    ]


def test_cli_check_text_prints_build_provenance_without_failing(tmp_path, capsys):
    """`--format text`: one `build_provenance: <state>` line, a
    `build differs:` list of the field *names* only when `different`, and a
    `different` state with otherwise matching evidence still exits `0`."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest = _manifest(evidence={"3": drc_path})
    manifest_path = _write(tmp_path, "manifest.json", manifest)
    fresh = build_tier_report(manifest)
    committed_path = _write(
        tmp_path, "committed.json", {**fresh, "build": _dev_build_block(fresh)}
    )

    exit_code = main(
        ["signoff", "--manifest", manifest_path, "--check", committed_path]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "status: match" in out
    assert "build_provenance: different" in out
    assert "build differs:" in out
    for field in ("version", "is_release", "grading_ruleset_id"):
        assert f"\n  {field}" in out
    # Names only, the same convention as `doc_drift_fields` -- the values
    # are in the JSON payload.
    assert "sha256:another-grading-ruleset" not in out

    same_path = _write(tmp_path, "same.json", fresh)
    exit_code = main(["signoff", "--manifest", manifest_path, "--check", same_path])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "build_provenance: same" in out
    assert "build differs:" not in out
