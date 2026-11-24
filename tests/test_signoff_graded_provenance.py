"""Graded row and group summaries identify the evidence behind their verdict."""

import json
from pathlib import Path

from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_VIOLATIONS_ENVELOPE,
    _drc_evidence_beside_its_layout,
    _item_11,
    _manifest,
    _power_delivery_evidence,
    _write,
)


def _drc_item(entry):
    report = build_tier_report(_manifest(evidence={"3": entry}))
    return next(item for item in report["items"] if item["id"] == 3)


def _provenance(entry):
    return entry["content_hash"], entry["input_verified"]


def test_graded_members_match_the_individual_verification_states(tmp_path):
    reports = []
    for name in ("verified", "changed", "unavailable"):
        directory = tmp_path / name
        directory.mkdir()
        report, layout = _drc_evidence_beside_its_layout(
            directory, layout_bytes=name.encode()
        )
        if name == "changed":
            layout.write_bytes(b"modified")
        if name == "unavailable":
            layout.unlink()
        reports.append(report)

    expected = [_provenance(_drc_item(path)["citation"]) for path in reports]
    assert [verified for _, verified in expected] == [True, False, None]
    assert len({digest for digest, _ in expected}) == 3
    item = _drc_item(reports)
    assert item["status"] == "met"
    assert [_provenance(entry) for entry in item["citations_graded"]] == expected


def test_unmet_and_unreadable_members_disclose_available_evidence(tmp_path):
    failed = _write(tmp_path, "violations.json", DRC_VIOLATIONS_ENVELOPE)
    unavailable = str(tmp_path / "missing.json")
    expected = _provenance(_drc_item(failed)["detail"]["cited_evidence"])
    assert expected[0] is not None

    item = _drc_item([failed, unavailable])
    assert item["status"] == "unmet"
    assert [_provenance(entry) for entry in item["citations_graded"]] == [
        expected,
        (None, None),
    ]


def test_group_grade_uses_the_part_named_first_in_the_manifest(tmp_path):
    erc, lvs = _power_delivery_evidence(tmp_path, kind="analog")
    # Two different envelopes in one file distinguish parts by pointer, including
    # when the first part named in the manifest is the second in canonical order.
    lvs_report = json.loads(Path(lvs).read_text())
    layout = tmp_path / lvs_report["layout"]
    layout.write_bytes(b"LVS layout")
    composite = _write(
        tmp_path,
        "both.json",
        {
            "first": json.loads(Path(erc).read_text()),
            "second": lvs_report,
        },
    )
    first = {"file": composite, "pointer": "/first"}
    second = {"file": composite, "pointer": "/second"}
    groups = [[second, first], [first, second], [str(tmp_path / "missing.json")]]
    item = _item_11(
        build_tier_report(_manifest(kind="analog", evidence={"11": groups}))
    )
    assert [g["status"] for g in item["citations_graded"]] == ["met", "met", "unmet"]
    expected = [_provenance(part) for part in item["citations"][0]["parts"]]
    assert expected[0] != expected[1]
    assert [_provenance(entry) for entry in item["citations_graded"]] == [
        expected[1],
        expected[0],
        (None, None),
    ]
