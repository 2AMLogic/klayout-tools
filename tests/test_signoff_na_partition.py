"""A mixed-signal item 6 digital NA declaration covers its assigned spec rows."""

import pytest

from klayout_tools.signoff import build_tier_report
from test_signoff import _item6, _na_entry


def grade(tmp_path, digital, analog, *, kind="mixed-signal", statement_ids=("logic",)):
    spec = {"status": "ratified", "rows": [{"id": "logic"}, {"id": "bias", "sigma": 1}]}
    statement = {
        "rows": [
            {"id": i, "class": "functional", "reason": "checked by logic tests"}
            for i in statement_ids
        ]
    }
    return build_tier_report(
        {
            "kind": kind,
            "block": "mixed",
            "evidence": {"6.digital": _na_entry(tmp_path, spec, statement)},
            "spec_rows": {"6.digital": digital, "6.analog": analog},
        }
    )


def test_digital_statement_checked_within_whole_spec_assignment(tmp_path):
    report = grade(tmp_path, [{"id": "logic"}], [{"id": "bias"}])
    digital = _item6(report, "digital")
    assert digital["status"] == "not_applicable"
    assert digital["not_applicable"]["spec"]["row_count"] == 2
    assert [r["id"] for r in digital["not_applicable"]["statement"]["rows"]] == [
        "logic"
    ]
    assert _item6(report, "analog")["status"] == "unmet"


@pytest.mark.parametrize(
    "analog", [[{"id": "typo"}], [{"id": "bias"}, {"id": "absent"}], "malformed"]
)
def test_partition_assignment_must_cover_exactly_the_ratified_spec(tmp_path, analog):
    row = _item6(grade(tmp_path, [{"id": "logic"}], analog), "digital")
    assert (row["status"], row["reason"]) == ("unmet", "invalid_evidence")


def test_statistical_row_on_both_partitions_is_still_scanned(tmp_path):
    report = grade(tmp_path, [{"id": "logic"}, {"id": "bias"}], [{"id": "bias"}])
    assert _item6(report, "digital")["reason"] == "not_applicable_statistical_row"


def test_digital_statement_cannot_claim_analog_only_rows(tmp_path):
    report = grade(
        tmp_path, [{"id": "logic"}], [{"id": "bias"}], statement_ids=["logic", "bias"]
    )
    assert _item6(report, "digital")["reason"] == "not_applicable_statement_incomplete"


@pytest.mark.parametrize(
    "digital",
    [[{"id": "logic", "gap": "not closed"}], [{"id": "logic", "ratified": False}]],
)
def test_digital_row_dispositions_still_block_na(tmp_path, digital):
    assert (
        _item6(grade(tmp_path, digital, [{"id": "bias"}]), "digital")["reason"]
        == "invalid_evidence"
    )


def test_partition_scope_requires_a_mixed_signal_manifest(tmp_path):
    report = grade(tmp_path, [{"id": "logic"}], [{"id": "bias"}], kind="digital")
    assert _item6(report)["reason"] == "invalid_evidence"
