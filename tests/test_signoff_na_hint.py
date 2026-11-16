"""Item 6 ``not_applicable``: statistical-row waiver hint (issue #5057)."""

from __future__ import annotations

from test_signoff import _item6, _na_entry, _na_report


def _monte_carlo_id_spec():
    spec = {"status": "ratified", "rows": [{"id": "monte-carlo"}]}
    statement = {"rows": [{"id": "monte-carlo", "class": "timing", "reason": "x"}]}
    return spec, statement


def test_item6_id_keyword_refusal_carries_waiver_hint(tmp_path):
    spec, statement = _monte_carlo_id_spec()
    item = _item6(_na_report(tmp_path, _na_entry(tmp_path, spec, statement)))

    assert item["status"] == "unmet"
    assert item["reason"] == "not_applicable_statistical_row"
    assert item["detail"]["row"] == "monte-carlo"
    assert item["detail"]["field"] == "id"
    assert "reviewed_not_statistical" in item["detail"]["hint"]


def test_item6_id_keyword_waived_by_reviewed_flag(tmp_path):
    spec, statement = _monte_carlo_id_spec()
    statement["rows"][0]["reviewed_not_statistical"] = True
    item = _item6(_na_report(tmp_path, _na_entry(tmp_path, spec, statement)))

    assert item["status"] == "not_applicable"


def test_item6_key_hit_refusal_has_no_hint(tmp_path):
    spec = {"status": "ratified", "rows": [{"id": "vos", "offset_mv_3sigma": 5}]}
    statement = {"rows": [{"id": "vos", "class": "timing", "reason": "x"}]}
    item = _item6(_na_report(tmp_path, _na_entry(tmp_path, spec, statement)))

    assert item["reason"] == "not_applicable_statistical_row"
    assert "hint" not in item["detail"]
