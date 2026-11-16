"""Item 6 ``not_applicable``: markdown spec table + reviewed assertion (issue #4793)."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_signoff import _item6, _na_entry, _na_report, _sha

_MD_SPEC = (
    "# Target spec\r\n\r\nStatus: Ratified\r\n\r\n"
    "| Param | Metric | Min |\r\n|:--|---|--:|\r\n"
    "| fmax  | fmax_mhz | 100 |\r\n| setup | setup_slack_ns | 0 |\r\n\r\ntrailing\r\n"
)


def _md_na_entry(tmp_path, md=_MD_SPEC, statement=None, id_column="Param", **ref_kw):
    spec_path = tmp_path / "spec.md"
    spec_path.write_bytes(md.encode())
    entry = _na_entry(tmp_path, statement=statement)
    entry["not_applicable"]["spec"] = {
        "file": str(spec_path),
        "content_hash": _sha(spec_path),
        "format": "markdown-table",
        "id_column": id_column,
        **ref_kw,
    }
    return entry


def test_item6_na_markdown_spec_is_granted_and_format_echoed(tmp_path):
    item = _item6(_na_report(tmp_path, _md_na_entry(tmp_path)))
    assert item["status"] == "not_applicable"
    spec = item["not_applicable"]["spec"]
    assert (spec["format"], spec["id_column"], spec["row_count"]) == (
        "markdown-table",
        "Param",
        2,
    )


def test_item6_na_json_spec_block_has_no_format_key(tmp_path):
    item = _item6(_na_report(tmp_path, _na_entry(tmp_path)))
    assert "format" not in item["not_applicable"]["spec"]


@pytest.mark.parametrize(
    ("md", "id_column", "reason", "problem"),
    [
        (
            _MD_SPEC.replace("Ratified", "Draft"),
            "Param",
            "not_applicable_spec_unratified",
            None,
        ),
        (
            _MD_SPEC.replace("Status: Ratified", ""),
            "Param",
            "invalid_evidence",
            "Status",
        ),
        (_MD_SPEC, "Nope", "invalid_evidence", "Nope"),
        (_MD_SPEC.replace("| setup ", "| fmax  "), "Param", "invalid_evidence", None),
        (
            "Status: ratified\n\n| Param | M |\n|---|---|\n",
            "Param",
            "not_applicable_statement_incomplete",
            None,
        ),
        (_MD_SPEC.replace("| fmax  |", "|  |"), "Param", "invalid_evidence", None),
    ],
)
def test_item6_na_markdown_spec_refusals(tmp_path, md, id_column, reason, problem):
    item = _item6(_na_report(tmp_path, _md_na_entry(tmp_path, md, id_column=id_column)))
    assert item["status"] == "unmet"
    assert item["reason"] == reason
    if problem:
        assert problem in item["detail"]["problem"]


@pytest.mark.parametrize(
    "md",
    [
        _MD_SPEC.replace("Metric", "Offset"),
        _MD_SPEC.replace("fmax_mhz", "3σ fmax"),
    ],
)
def test_item6_na_markdown_statistical_row_is_refused(tmp_path, md):
    item = _item6(_na_report(tmp_path, _md_na_entry(tmp_path, md)))
    assert item["reason"] == "not_applicable_statistical_row"
    assert item["detail"]["row"] == "fmax"


def test_item6_na_markdown_edit_after_hash_is_stale(tmp_path):
    entry = _md_na_entry(tmp_path)
    Path(entry["not_applicable"]["spec"]["file"]).write_text(_MD_SPEC + "\nedit\n")
    item = _item6(_na_report(tmp_path, entry))
    assert item["reason"] == "stale_evidence"
    assert item["detail"]["document"] == "spec"


def test_item6_na_markdown_needs_id_column_and_known_format(tmp_path):
    entry = _md_na_entry(tmp_path)
    del entry["not_applicable"]["spec"]["id_column"]
    assert _item6(_na_report(tmp_path, entry))["reason"] == "invalid_evidence"
    entry = _md_na_entry(tmp_path, format="yaml")
    assert _item6(_na_report(tmp_path, entry))["reason"] == "invalid_evidence"


def _reviewed_statement(flag):
    row = {"id": "fmax", "class": "no sigma or yield bound applies", "reason": "STA"}
    if flag:
        row["reviewed_not_statistical"] = True
    return {"rows": [row]}


_ONE_ROW_SPEC = {"status": "ratified", "rows": [{"id": "fmax", "metric": "fmax_mhz"}]}


def test_item6_na_reviewed_flag_waives_class_text_hit_and_is_echoed(tmp_path):
    entry = _na_entry(tmp_path, _ONE_ROW_SPEC, _reviewed_statement(True))
    item = _item6(_na_report(tmp_path, entry))
    assert item["status"] == "not_applicable"
    assert item["not_applicable"]["statement"]["rows"][0]["reviewed_not_statistical"]


def test_item6_na_class_text_hit_still_refused_without_flag(tmp_path):
    entry = _na_entry(tmp_path, _ONE_ROW_SPEC, _reviewed_statement(False))
    item = _item6(_na_report(tmp_path, entry))
    assert item["reason"] == "not_applicable_statistical_row"


def test_item6_na_reviewed_flag_waives_spec_text_but_not_keys(tmp_path):
    text_spec = {
        "status": "ratified",
        "rows": [{"id": "fmax", "note": "no yield bound"}],
    }
    ok = _item6(
        _na_report(tmp_path, _na_entry(tmp_path, text_spec, _reviewed_statement(True)))
    )
    assert ok["status"] == "not_applicable"
    key_spec = {"status": "ratified", "rows": [{"id": "fmax", "sigma": 3}]}
    bad = _item6(
        _na_report(tmp_path, _na_entry(tmp_path, key_spec, _reviewed_statement(True)))
    )
    assert bad["reason"] == "not_applicable_statistical_row"
    assert bad["detail"]["field"] == "sigma"
