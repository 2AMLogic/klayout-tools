"""Stored FV metadata must not gain post-layout credit through JSON truthiness."""

import json

import pytest

from klayout_tools.signoff import build_signoff, build_tier_report


def _passing_regression():
    """Complete counts also satisfy the stricter FV execution contract."""
    return {
        "schema_version": 1,
        "status": "pass",
        "test_count": 3,
        "passed_count": 2,
        "failed_count": 0,
        "skipped_count": 1,
        "tests": [
            {"name": "first", "status": "passed"},
            {"name": "second", "status": "passed"},
            {"name": "optional", "status": "skipped"},
        ],
        "environment": {"sdf": {"annotated": True, "corner": "typ"}},
    }


def _assert_annotation_credit(tmp_path, envelope, *, annotated, corner="typ"):
    # Both public consumers parse the saved JSON, as with old or external
    # producers. Only optional annotation metadata varies across these cases.
    path = tmp_path / "functional.json"
    path.write_text(json.dumps(envelope, allow_nan=False), encoding="utf-8")
    plain = build_signoff([str(path)])
    assert plain["status"] == "pass"
    check = plain["checks"][0]
    assert check["passed"] is True
    assert check["kind"] == "functional-verification"
    assert check["detail"]["sdf_annotated"] is annotated
    assert check["detail"]["sdf_corner"] == corner
    assert check["detail"]["passed_count"] == 2
    assert check["detail"]["skipped_count"] == 1

    tier = build_tier_report(
        {
            "block": "sdf-domain",
            "kind": "digital",
            "evidence": {"5": str(path), "7": str(path)},
        }
    )
    items = {item["id"]: item for item in tier["items"]}
    assert items[5]["status"] == "met"
    assert items[5]["citation"]["kind"] == "functional-verification"
    assert items[7]["status"] == ("met" if annotated else "unmet")
    assert items[7]["reason"] == (None if annotated else "not_post_layout")
    if annotated:
        assert items[7]["citation"]["kind"] == "functional-verification"
    else:
        assert items[7]["citation"] is None


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        "",
        "false",
        "true",
        " ",
        0,
        1,
        -1,
        0.0,
        1.0,
        [],
        [False],
        {},
        {"failed": True},
    ],
)
def test_annotation_credit_requires_json_true(tmp_path, value):
    envelope = _passing_regression()
    envelope["environment"]["sdf"]["annotated"] = value
    _assert_annotation_credit(tmp_path, envelope, annotated=value is True)


@pytest.mark.parametrize("field", ["environment", "sdf"])
@pytest.mark.parametrize(
    "value", [None, True, False, "", "malformed", 0, 1, 1.0, [], [True], {}]
)
def test_malformed_metadata_container_cannot_qualify(tmp_path, field, value):
    envelope = _passing_regression()
    parent = envelope if field == "environment" else envelope["environment"]
    parent[field] = value
    _assert_annotation_credit(tmp_path, envelope, annotated=False, corner=None)


@pytest.mark.parametrize("field", ["environment", "sdf", "annotated"])
def test_missing_annotation_metadata_cannot_qualify(tmp_path, field):
    envelope = _passing_regression()
    parent = envelope
    if field != "environment":
        parent = parent["environment"]
    if field == "annotated":
        parent = parent["sdf"]
    del parent[field]
    _assert_annotation_credit(
        tmp_path,
        envelope,
        annotated=False,
        corner="typ" if field == "annotated" else None,
    )
