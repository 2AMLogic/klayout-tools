"""Tests for the justified object form of `spec_rows[].gap` (issue #5599).

A declared gap is pending work, never permanent (operator ruling on #5858,
2026-11-17): the justified form carries a human reason, a hash-bound
rationale artifact and the issue that would close it; a justified gap that
names no issue is reported as a finding. Neither form ever counts as
coverage -- both keep the item `unmet` / `partial_coverage`, and the
free-text form's verdict and `detail` shape are unchanged.

Split out of `test_signoff.py` (whose per-file size ratchet this respects),
mirroring that file's fixture conventions: hand-built JSON envelopes, no
external corpus.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report

SIM_PASS_ENVELOPE = {
    "schema_version": 1,
    "netlist": "design.spice",
    "status": "pass",
    "corner_count": 3,
    "passed": 3,
    "failed": 0,
    "errored": 0,
    "environment": {},
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
        "input": {"content_hash": "sha256:netlistA", "role": "netlist"},
    },
    "measurements": [],
    "corners": [],
}


def _write(tmp_path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _manifest(**overrides) -> dict:
    base = {"block": "demo-block", "kind": "analog", "evidence": {}}
    base.update(overrides)
    return base


def _item5(evidence, spec_rows=None, **overrides) -> dict:
    manifest = _manifest(evidence=evidence, **overrides)
    if spec_rows is not None:
        manifest["spec_rows"] = {"5": spec_rows}
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 5)


# --------------------------------------------------------------------------- #
# Justified declared gaps (issue #5599): the object form of `spec_rows[].gap`.
# A declared gap is pending work, never permanent (operator ruling on #5858,
# 2026-11-17): the justified form carries a human reason, a hash-bound
# rationale artifact and the issue that would close it. Both forms keep the
# item `unmet` / `partial_coverage` -- the roll-up is deliberately unchanged.
# --------------------------------------------------------------------------- #


def _rationale(tmp_path, name="gaps/pvt_ss_125c.md") -> dict:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Decision\n\nNo liberty file ships for this PVT point.\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file": str(path), "content_hash": f"sha256:{digest}"}


def _gap(
    reason="no liberty file ships for this PVT point",
    rationale=...,
    issue="#5858",
) -> dict:
    """The justified object form of `gap`; omit keys by leaving them at the
    `...` sentinel (an absent key, distinct from an explicit `None`)."""
    gap: dict = {"reason": reason}
    if rationale is not ...:
        gap["rationale"] = rationale
    if issue is not ...:
        gap["issue"] = issue
    return gap


def _justified(gap: dict) -> dict:
    return {"id": "pvt_ss_125c", "gap": gap}


def test_item5_justified_gap_is_listed_apart_and_still_partial_coverage(tmp_path):
    a = _write(tmp_path, "a.json", SIM_PASS_ENVELOPE)
    rows = [{"id": "tdec"}, _justified(_gap(rationale=_rationale(tmp_path)))]

    item = _item5({"5": [{"file": a, "rows": ["tdec"]}]}, rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["declared_gaps"] == []
    gap = item["detail"]["justified_gaps"][0]
    assert gap["id"] == "pvt_ss_125c"
    assert gap["reason"] == "no liberty file ships for this PVT point"
    assert gap["rationale"]["content_hash"].startswith("sha256:")
    assert gap["issue"] == "#5858"
    assert item["detail"]["uncovered_rows"] == []


def test_item5_justified_gap_without_evidence_is_partial_coverage_never_met(tmp_path):
    rows = [{"id": "tdec"}, _justified(_gap(rationale=_rationale(tmp_path)))]

    item = _item5({}, rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["citation"] is None
    assert item["detail"]["uncovered_rows"] == ["tdec"]
    assert [g["id"] for g in item["detail"]["justified_gaps"]] == ["pvt_ss_125c"]


def test_t1_justified_gap_count_is_reported_and_omitted_when_zero(tmp_path):
    justified = _manifest(
        evidence={},
        spec_rows={
            "5": [{"id": "tdec"}, _justified(_gap(rationale=_rationale(tmp_path)))]
        },
    )
    report = build_tier_report(justified)
    assert report["t1_justified_gap_count"] == 1

    free_text = _manifest(
        evidence={}, spec_rows={"5": [{"id": "tdec"}, {"id": "zin", "gap": "later"}]}
    )
    report = build_tier_report(free_text)
    assert "t1_justified_gap_count" not in report
    assert "findings" not in report
    detail = next(i for i in report["items"] if i["id"] == 5)["detail"]
    assert sorted(detail) == [
        "declared_gaps",
        "failed_rows",
        "row_sources",
        "uncovered_rows",
        "unratified_rows",
    ]
    assert detail["declared_gaps"] == [{"id": "zin", "gap": "later"}]


def test_justified_gap_without_issue_link_is_reported_as_a_finding(tmp_path):
    manifest = _manifest(
        evidence={},
        spec_rows={"5": [_justified(_gap(rationale=_rationale(tmp_path), issue=None))]},
    )
    report = build_tier_report(manifest)

    assert report["findings"] == [
        {
            "finding": "justified_gap_without_issue_link",
            "item": 5,
            "row": "pvt_ss_125c",
        }
    ]


def test_justified_gap_with_issue_link_emits_no_finding(tmp_path):
    manifest = _manifest(
        evidence={},
        spec_rows={"5": [_justified(_gap(rationale=_rationale(tmp_path)))]},
    )
    report = build_tier_report(manifest)

    assert "findings" not in report


def test_item5_justified_gap_with_missing_rationale_file_names_it(tmp_path):
    missing = _rationale(tmp_path)
    os.remove(missing["file"])
    rows = [_justified(_gap(rationale=missing))]

    item = _item5({}, rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "justified_gap_rationale_unreadable"
    assert item["detail"]["row"] == "pvt_ss_125c"
    assert item["detail"]["document"] == "rationale"
    assert item["detail"]["file"] == missing["file"]


def test_item5_justified_gap_with_stale_rationale_reports_both_hashes(tmp_path):
    stale = _rationale(tmp_path)
    Path(stale["file"]).write_text("\nan edit after the hash was taken\n")
    rows = [_justified(_gap(rationale=stale))]

    item = _item5({}, rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "justified_gap_rationale_stale"
    assert item["detail"]["expected_content_hash"] == stale["content_hash"]
    assert item["detail"]["actual_content_hash"].startswith("sha256:")
    assert item["detail"]["actual_content_hash"] != stale["content_hash"]


def test_item5_object_gap_without_rationale_is_invalid_evidence(tmp_path):
    item = _item5({}, [_justified(_gap(rationale=_rationale(tmp_path)))])

    assert item["reason"] == "partial_coverage"  # control: with rationale it grades

    item = _item5({}, [_justified(_gap())])

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"


def test_item5_object_gap_without_reason_is_invalid_evidence(tmp_path):
    item = _item5({}, [_justified({"rationale": _rationale(tmp_path)})])

    assert item["reason"] == "invalid_evidence"


def test_item5_object_gap_with_non_string_issue_is_invalid_evidence(tmp_path):
    item = _item5(
        {},
        [_justified(_gap(rationale=_rationale(tmp_path), issue=5858))],  # type: ignore[arg-type]
    )

    assert item["reason"] == "invalid_evidence"


def test_item5_object_gap_with_empty_reason_is_invalid_evidence(tmp_path):
    item = _item5({}, [_justified(_gap(reason="", rationale=_rationale(tmp_path)))])

    assert item["reason"] == "invalid_evidence"


def test_item5_malformed_rationale_reference_is_invalid_evidence(tmp_path):
    item = _item5({}, [_justified(_gap(rationale={"file": "gaps/x.md"}))])

    assert item["reason"] == "invalid_evidence"


def test_item5_member_citing_a_justified_gap_row_is_invalid_evidence(tmp_path):
    rationale = _rationale(tmp_path)
    rows = [_justified(_gap(rationale=rationale))]

    item = _item5(
        {
            "5": [
                {
                    "file": _write(tmp_path, "a.json", SIM_PASS_ENVELOPE),
                    "rows": ["pvt_ss_125c"],
                }
            ]
        },
        rows,
    )

    assert item["reason"] == "invalid_evidence"


def test_item5_justified_gap_on_ratified_false_row_is_not_unratified(tmp_path):
    row = {
        "id": "pvt_ss_125c",
        "ratified": False,
        **_justified(_gap(rationale=_rationale(tmp_path))),
    }

    item = _item5({}, [row])

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["unratified_rows"] == []
    assert [g["id"] for g in item["detail"]["justified_gaps"]] == ["pvt_ss_125c"]


def test_two_justified_rows_sharing_one_rationale_are_counted_separately(tmp_path):
    rationale = _rationale(tmp_path)
    rows = [
        _justified(_gap(rationale=rationale)),
        {**_justified(_gap(rationale=rationale)), "id": "pvt_ff_minus40c"},
    ]

    report = build_tier_report(_manifest(evidence={}, spec_rows={"5": rows}))

    assert report["t1_justified_gap_count"] == 2
    assert [
        g["id"]
        for g in next(i for i in report["items"] if i["id"] == 5)["detail"][
            "justified_gaps"
        ]
    ] == ["pvt_ss_125c", "pvt_ff_minus40c"]


def test_partition_qualified_justified_gap_resolves(tmp_path):
    manifest = _manifest(
        evidence={},
        spec_rows={"5.analog": [_justified(_gap(rationale=_rationale(tmp_path)))]},
    )
    report = build_tier_report(manifest)

    assert report["t1_justified_gap_count"] == 1


def test_justified_gap_rationale_outside_the_manifest_directory_resolves(tmp_path):
    # Rationale paths resolve as given (like not_applicable documents): a
    # sibling-directory path is accepted when the hash matches, and a `../`
    # escape is not special-cased.
    outside = tmp_path / "outside"
    outside.mkdir()
    rationale = _rationale(outside, name="decision.md")
    rows = [_justified(_gap(rationale=rationale))]

    item = _item5({}, rows)

    assert item["reason"] == "partial_coverage"


def test_item6_justified_gap_is_listed_apart(tmp_path):
    manifest = _manifest(evidence={})
    manifest["spec_rows"] = {"6": [_justified(_gap(rationale=_rationale(tmp_path)))]}

    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 6)

    assert item["reason"] == "partial_coverage"
    assert [g["id"] for g in item["detail"]["justified_gaps"]] == ["pvt_ss_125c"]


def test_justified_gap_text_output(tmp_path, capsys):
    rationale = _rationale(tmp_path)
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            _manifest(
                evidence={},
                spec_rows={
                    "5": [
                        _justified(_gap(rationale=rationale)),
                        {
                            **_justified(_gap(rationale=rationale, issue=None)),
                            "id": "pvt_ff_minus40c",
                        },
                    ]
                },
            )
        )
    )

    assert main(["signoff", "--manifest", str(manifest), "--format", "text"]) == 3
    out = capsys.readouterr().out
    assert "T1: 0/11 items met, 2 justified gaps" in out
    assert "justified gap: pvt_ss_125c" in out
    assert f"rationale: {rationale['file']}" in out
    assert "issue: #5858" in out
    assert "no issue names what would close this gap" in out
