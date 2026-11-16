"""Issue #5903: ``unmet_reason_counts`` rolls up why T1 items are unmet.

Lives beside ``tests/test_signoff.py`` rather than in it (that file is at its
size-ratchet ceiling), reusing its ``_manifest`` / ``_write`` fixtures.
"""

from __future__ import annotations

import json

from klayout_tools.signoff import build_tier_report, check_tier_report
from klayout_tools.signoff_unmet_reasons import unmet_reason_counts
from test_signoff import _manifest, _write


def _row(tier, status, reason=None):
    return {"tier": tier, "status": status, "reason": reason}


def test_counts_group_unmet_t1_items_by_reason():
    items = [
        _row("T1", "unmet", "no_evidence"),
        _row("T1", "unmet", "no_evidence"),
        _row("T1", "unmet", "no_evidence"),
        _row("T1", "unmet", "stale_evidence"),
        _row("T1", "met"),
    ]
    assert unmet_reason_counts(items) == {
        "unmet_reason_counts": {"no_evidence": 3, "stale_evidence": 1}
    }


def test_key_is_absent_when_no_t1_item_is_unmet():
    assert unmet_reason_counts([_row("T1", "met"), _row("T1", "met")]) == {}
    assert unmet_reason_counts([_row("T1", "not_applicable")]) == {}


def test_ladder_rows_and_not_applicable_are_not_counted():
    items = [
        _row("T1", "not_applicable"),
        _row("T2", "unmet", "tier_not_supported"),
        _row("T3", "unmet", "tier_not_supported"),
        _row("T1", "unmet", "stale_evidence"),
    ]
    assert unmet_reason_counts(items) == {"unmet_reason_counts": {"stale_evidence": 1}}


def test_report_counts_sum_to_the_unmet_t1_items():
    report = build_tier_report(_manifest(kind="mixed-signal"))
    counts = report["unmet_reason_counts"]
    unmet_t1 = [
        i for i in report["items"] if i["tier"] == "T1" and i["status"] == "unmet"
    ]
    assert report["t1_item_count"] == 22
    assert counts == {"no_evidence": 22}
    assert sum(counts.values()) == len(unmet_t1)
    assert "tier_not_supported" not in counts


def test_check_ignores_the_field_on_a_report_committed_before_it_existed(tmp_path):
    manifest = _manifest()
    committed = build_tier_report(manifest)
    del committed["unmet_reason_counts"]
    path = _write(tmp_path, "committed.json", committed)

    result = check_tier_report(path, manifest)

    assert result["status"] == "match"
    assert result["doc_drift"] is False
    assert "unmet_reason_counts" in result["fresh"]


def test_check_still_reports_a_real_status_change_as_drift(tmp_path):
    manifest = _manifest()
    committed = build_tier_report(manifest)
    committed["items"][0]["reason"] = "stale_evidence"
    path = tmp_path / "committed.json"
    path.write_text(json.dumps(committed))

    assert check_tier_report(str(path), manifest)["status"] == "drifted"
