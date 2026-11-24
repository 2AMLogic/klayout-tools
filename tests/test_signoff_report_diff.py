"""Tests for `klt signoff --diff-reports` (issue #6850): the checklist-surface
comparison of two previously committed tier reports.

The mode exists for the reviewer who has the two report *files* and nothing
else -- no manifest, no evidence, no working grading build -- so the
fixtures render real reports through `build_tier_report`, commit them to
disk, and then mutate the saved JSON the way two snapshots from different
checklist versions actually differ (a reworded doc, a gained/lost/renumbered
item), never re-grading anything.

Lives in its own file rather than `tests/test_signoff.py` because that file
sits at its per-file size-ratchet ceiling (the same reason
`tests/test_signoff_generator_batch.py` is split out); the shared fixtures
are imported from `test_signoff`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import build_fleet_report, build_tier_report
from klayout_tools.signoff_report_diff import diff_tier_reports
from test_signoff import DRC_CLEAN_ENVELOPE, _manifest, _write

# `build_tier_report` resolves the running build's identity by shelling out
# to `git` -- the same collision `tests/test_signoff.py` solves with this
# fixture (see tests/conftest.py).
pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _row(report: dict, *, tier: str, item_id, partition=None) -> dict:
    """The one row of `report` matching an identity -- the test-side spelling
    of the `(tier, id, partition)` match key the mode itself uses."""
    return next(
        row
        for row in report["items"]
        if row["tier"] == tier
        and row["id"] == item_id
        and row.get("partition") == partition
    )


def _mutated(report: dict, edit) -> dict:
    """A deep copy of `report` with `edit` (mutating its copy) applied -- two
    snapshots of one render, differing exactly as `edit` makes them."""
    snapshot = json.loads(json.dumps(report))
    edit(snapshot)
    return snapshot


def _saved(tmp_path, name: str, report: dict) -> str:
    return _write(tmp_path, name, report)


def test_compares_two_saved_reports_with_the_manifest_and_evidence_absent(
    tmp_path, capsys
):
    """AC: the comparison reads only the supplied reports -- the manifest and
    the evidence the render graded are deleted before the run, and the
    unchanged pair compares as an exactly-empty movement."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(evidence={"3": drc_path})
    )
    rendered = build_tier_report(_manifest(evidence={"3": drc_path}))
    old_path = _saved(tmp_path, "old.json", rendered)
    new_path = _saved(tmp_path, "new.json", rendered)
    Path(manifest_path).unlink()
    Path(drc_path).unlink()

    exit_code = main(
        ["signoff", "--diff-reports", old_path, new_path, "--format", "json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == {
        "schema_version": 1,
        "mode": "diff-reports",
        "old_report": old_path,
        "new_report": new_path,
        "changed": False,
        "source": [],
        "items": {"changed": [], "added": [], "removed": []},
    }


def test_names_old_and_new_values_for_a_source_path_and_hash_change(tmp_path):
    """AC: a changed `source_doc` (the doc came from somewhere else) and the
    `source_doc_content_hash` that pins what it said are both reported, with
    both values, in that order."""
    old = build_tier_report(_manifest())
    new = _mutated(
        old,
        lambda r: r.update(
            source_doc="vendored.md", source_doc_content_hash="sha256:new"
        ),
    )

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )

    assert result["changed"] is True
    assert result["source"] == [
        {
            "field": "source_doc",
            "old": "docs/design-evidence-tiers.md",
            "new": "vendored.md",
        },
        {
            "field": "source_doc_content_hash",
            "old": old["source_doc_content_hash"],
            "new": "sha256:new",
        },
    ]
    assert result["items"] == {"changed": [], "added": [], "removed": []}


def test_names_old_and_new_values_for_a_wording_edit(tmp_path):
    """AC: a reworded row reports the row's identity and each moved field
    (`title`/`text`/`notes` -- the same three `--check` calls the
    checklist's own wording), with both values -- the answer a reviewer
    re-reading two committed verdicts needs, not just a boolean."""
    old = build_tier_report(_manifest())
    row = _row(old, tier="T1", item_id=7)

    def edit(snapshot):
        target = _row(snapshot, tier="T1", item_id=7)
        target["title"] = "Post-layout verification (retitled)"
        target["text"] = "the spec suite re-run, reworded (issue #6850)"
        target["notes"] = ["a new note"]

    new = _mutated(old, edit)

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )

    assert result["items"]["changed"] == [
        {
            "tier": "T1",
            "id": 7,
            "partition": None,
            "field": "title",
            "old": row["title"],
            "new": "Post-layout verification (retitled)",
        },
        {
            "tier": "T1",
            "id": 7,
            "partition": None,
            "field": "text",
            "old": row["text"],
            "new": "the spec suite re-run, reworded (issue #6850)",
        },
        {
            "tier": "T1",
            "id": 7,
            "partition": None,
            "field": "notes",
            "old": row["notes"],
            "new": ["a new note"],
        },
    ]
    assert result["items"]["added"] == []
    assert result["items"]["removed"] == []


def test_identifies_an_insertion_and_a_removal_once_with_their_content(tmp_path):
    """AC: a row the new checklist gained (or the old one lost, here in the
    middle of the list, where an index-based diff would misalign) is
    identified exactly once, carrying the checklist content itself."""
    old = build_tier_report(_manifest())
    gained = {
        **json.loads(json.dumps(_row(old, tier="T1", item_id=11))),
        "id": 12,
        "title": "Item 12 (gained)",
        "text": "a brand-new requirement",
        "notes": ["note"],
    }

    def edit(snapshot):
        middle = len(snapshot["items"]) // 2
        snapshot["items"].insert(middle, gained)

    new = _mutated(old, edit)

    def drop(snapshot):
        snapshot["items"].remove(_row(snapshot, tier="T1", item_id=5))

    lost = _mutated(old, drop)
    lost_row = _row(old, tier="T1", item_id=5)

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )
    assert result["items"]["added"] == [
        {
            "tier": "T1",
            "id": 12,
            "partition": None,
            "title": "Item 12 (gained)",
            "text": "a brand-new requirement",
            "notes": ["note"],
        }
    ]
    assert result["items"]["removed"] == []

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "lost.json", lost)
    )
    assert result["items"]["removed"] == [
        {
            "tier": "T1",
            "id": 5,
            "partition": None,
            "title": lost_row["title"],
            "text": lost_row["text"],
            "notes": lost_row["notes"],
        }
    ]
    assert result["items"]["added"] == []


def test_reordering_rows_alone_yields_an_empty_comparison(tmp_path):
    """AC: the match is by row identity, not list position -- reversing
    `items[]` wholesale is not checklist movement."""
    old = build_tier_report(_manifest())
    new = _mutated(old, lambda r: r["items"].reverse())

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )

    assert result == {
        "schema_version": 1,
        "mode": "diff-reports",
        "old_report": _saved(tmp_path, "old.json", old),
        "new_report": _saved(tmp_path, "new.json", new),
        "changed": False,
        "source": [],
        "items": {"changed": [], "added": [], "removed": []},
    }


def test_verdict_only_and_build_only_changes_yield_an_empty_comparison(tmp_path):
    """AC: what the *grading* said (statuses, counts, citations) and which
    install rendered the report are not checklist changes -- the mode is the
    complement of `--check`, and must stay that way."""
    old = build_tier_report(_manifest(evidence={}))

    def verdicts(snapshot):
        row = _row(snapshot, tier="T1", item_id=3)
        row["status"] = "met"
        row["reason"] = None
        snapshot["t1_met_count"] += 1
        snapshot["tier"] = "T1"

    def build_only(snapshot):
        snapshot["build"] = {**snapshot["build"], "version": "9.9.9+gdeadbeef"}

    for edit in (verdicts, build_only):
        new = _mutated(old, edit)
        result = diff_tier_reports(
            _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
        )
        assert result["changed"] is False, edit.__name__
        assert result["items"] == {"changed": [], "added": [], "removed": []}


def test_edits_to_one_partition_of_a_mixed_signal_pair_name_only_that_row(tmp_path):
    """AC: the analog and digital rows of a mixed-signal pair share a tier
    and numeric id -- the partition completes the key, so editing one side
    names only that side."""
    old = build_tier_report(_manifest(kind="mixed-signal"))
    reworded = "digital-side guidance, reworded"

    def edit(snapshot):
        _row(snapshot, tier="T1", item_id=5, partition="digital")["text"] = reworded

    new = _mutated(old, edit)

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )

    assert result["items"]["changed"] == [
        {
            "tier": "T1",
            "id": 5,
            "partition": "digital",
            "field": "text",
            "old": _row(old, tier="T1", item_id=5, partition="digital")["text"],
            "new": reworded,
        }
    ]


def test_edits_to_a_null_id_ladder_row_name_only_that_tier(tmp_path):
    """AC: the T2-T4 ladder rows carry a null id -- distinguished by tier, so
    editing one names exactly that row."""
    old = build_tier_report(_manifest())
    reworded = "T3 claim, reworded"

    def edit(snapshot):
        _row(snapshot, tier="T3", item_id=None)["text"] = reworded

    new = _mutated(old, edit)

    result = diff_tier_reports(
        _saved(tmp_path, "old.json", old), _saved(tmp_path, "new.json", new)
    )

    assert result["items"]["changed"] == [
        {
            "tier": "T3",
            "id": None,
            "partition": None,
            "field": "text",
            "old": _row(old, tier="T3", item_id=None)["text"],
            "new": reworded,
        }
    ]


def test_refuses_an_unreadable_report_with_a_diagnostic(tmp_path, capsys):
    """AC: an unreadable file is a failure to run (exit 1), never an empty
    or partial comparison."""
    report = build_tier_report(_manifest())
    good = _saved(tmp_path, "good.json", report)
    missing = str(tmp_path / "nope.json")
    bad = str(tmp_path / "bad.json")
    Path(bad).write_text("{not json")

    for broken in (missing, bad):
        exit_code = main(
            ["signoff", "--diff-reports", good, broken, "--format", "json"]
        )
        assert exit_code == 1
        message = json.loads(capsys.readouterr().err)["error"]["message"]
        assert "nope.json" in message or "not valid JSON" in message


def test_refuses_a_non_tier_report(tmp_path, capsys):
    """AC: a fleet roll-up (or any JSON object a tier-report mode did not
    produce) is refused as a wrong file, not diffed into a wall of
    movement -- the same refusal `--check` makes, reusing its loader."""
    fleet = build_fleet_report({"blocks": [_manifest()]})
    fleet_path = _saved(tmp_path, "fleet.json", fleet)
    tier_path = _saved(tmp_path, "tier.json", build_tier_report(_manifest()))

    exit_code = main(
        ["signoff", "--diff-reports", fleet_path, tier_path, "--format", "json"]
    )

    assert exit_code == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "carries no 'items' key" in message
    assert "--diff-reports expects a report this mode produced" in message


def test_refuses_an_ambiguous_duplicate_row_key(tmp_path, capsys):
    """AC: two rows with the same `(tier, id, partition)` identity cannot be
    matched unambiguously -- guessing would fabricate a comparison, so the
    report is refused."""
    report = build_tier_report(_manifest())

    def duplicate(snapshot):
        snapshot["items"].append(
            json.loads(json.dumps(_row(snapshot, tier="T1", item_id=3)))
        )

    doubled = _mutated(report, duplicate)
    old_path = _saved(tmp_path, "old.json", report)
    new_path = _saved(tmp_path, "new.json", doubled)

    exit_code = main(
        ["signoff", "--diff-reports", old_path, new_path, "--format", "json"]
    )

    assert exit_code == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "two rows with the identity T1 #3" in message


def test_refuses_an_inventory_with_an_items_array(tmp_path, capsys):
    path = _saved(tmp_path, "inventory.json", {"items": [{"id": "sky130"}]})
    assert main(["signoff", "--diff-reports", path, path, "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "inventory.json" in message and "tier report" in message


def test_compares_reports_before_build_and_checklist_hash_were_recorded(tmp_path):
    report = build_tier_report(_manifest())
    report.pop("build")
    report.pop("source_doc_content_hash")
    for row in report["items"]:
        row.pop("partition", None)
    path = _saved(tmp_path, "historical.json", report)
    assert not diff_tier_reports(path, path)["changed"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("tier", []), ("id", {"measure": 3}), ("id", True), ("partition", [])],
)
def test_refuses_malformed_row_identity(tmp_path, capsys, field, value):
    original = build_tier_report(_manifest())
    good = _saved(tmp_path, "good.json", original)
    original["items"][0][field] = value
    bad = _saved(tmp_path, "bad.json", original)

    assert main(["signoff", "--diff-reports", good, bad, "--format", "json"]) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "bad.json" in message and "items[0]" in message


def test_refuses_the_flags_of_every_other_mode(tmp_path, capsys):
    """The mode is mutually exclusive with every grading flag, by name, so
    none is ever silently ignored."""
    old_path = _saved(tmp_path, "old.json", build_tier_report(_manifest()))
    new_path = _saved(tmp_path, "new.json", build_tier_report(_manifest()))
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    for extra in (
        ["--manifest", manifest_path],
        ["--check", old_path],
        ["--describe-grader"],
        ["--inventory", "inventory.json"],
        ["--require-build-match"],
        ["--skip-local-only"],
        ["--input", "design.gds"],
        ["--generator", "example"],
        ["--regenerated-hash", "sha256:deadbeef"],
        ["--hash-kind", "bytes"],
        ["-o", str(tmp_path / "out.json")],
    ):
        argv = [
            "signoff",
            "--diff-reports",
            old_path,
            new_path,
            *extra,
            "--format",
            "json",
        ]
        exit_code = main(argv)
        assert exit_code == 1, extra
        assert (
            "--diff-reports cannot be combined with"
            in json.loads(capsys.readouterr().err)["error"]["message"]
        )


def test_exit_code_is_zero_whether_or_not_anything_moved(tmp_path, capsys):
    """AC: a completed comparison exits 0 including when it reports
    movement -- the movement is the answer, not a failure -- and the text
    rendering says which of the two it was."""
    old = build_tier_report(_manifest())
    old_path = _saved(tmp_path, "old.json", old)
    new = _mutated(
        old, lambda r: _row(r, tier="T2", item_id=None).__setitem__("text", "reworded")
    )
    new_path = _saved(tmp_path, "new.json", new)

    exit_code = main(["signoff", "--diff-reports", old_path, old_path])
    assert exit_code == 0
    assert "checklist: unchanged" in capsys.readouterr().out

    exit_code = main(["signoff", "--diff-reports", old_path, new_path])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "checklist: changed" in out
    assert "T2 #-" in out  # the null-id ladder row, by its text label
