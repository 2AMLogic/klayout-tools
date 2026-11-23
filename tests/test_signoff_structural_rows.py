"""Tests for the ``structural`` disposition of ``spec_rows[]`` rows (#6985).

A row whose claim is a design constant the bench itself wires in, or a
structural property checked by inspecting a deck or layout, is not a measured
quantity: no producible envelope can carry a covering measurement, so the row
could only be asserted through some envelope's ``rows`` (a false claim) or
declared a ``gap`` -- and a gap reads identically whether the row is
unevidenced or evidenced by a real structural check the grader cannot see.
The ``structural`` disposition makes such a row representable and distinct:
it names the checked artifact, freshness-pinned by content hash, without
pretending a measurement covers it. It never counts as coverage -- the
roll-up is deliberately unchanged (a declared structural row keeps the item
``unmet`` / ``partial_coverage``, exactly like a justified gap; operator
ruling family of #5858).

Modelled on ``test_signoff_justified_gaps.py``, mirroring its fixture
conventions: hand-built JSON envelopes, no external corpus.
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
# The `structural` disposition (issue #6985): a citable evidence form for a
# row whose claim is a design constant or a structural property, not a
# measured quantity. The artifact is hash-checked at grading time; the row is
# never covered, never a gap, and keeps the item `unmet` / `partial_coverage`.
# --------------------------------------------------------------------------- #


def _artifact(tmp_path, name="sim/tb_ota.spice") -> dict:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("* tb_ota.spice\nVref vdda 0 0.9\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file": str(path), "content_hash": f"sha256:{digest}"}


def _structural(
    artifact: dict,
    claim="reference is the 0.9 V source `Vref` wired in tb_ota.spice, "
    "not a measured node",
) -> dict:
    return {"id": "vref_const", "structural": {"claim": claim, "artifact": artifact}}


def test_item5_structural_row_is_listed_apart_and_still_partial_coverage(tmp_path):
    a = _write(tmp_path, "a.json", SIM_PASS_ENVELOPE)
    rows = [{"id": "tdec"}, _structural(_artifact(tmp_path))]

    item = _item5({"5": [{"file": a, "rows": ["tdec"]}]}, rows)

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["detail"]["declared_gaps"] == []
    assert item["detail"]["uncovered_rows"] == []
    assert "justified_gaps" not in item["detail"]
    structural = item["detail"]["structural_rows"][0]
    assert structural["id"] == "vref_const"
    assert structural["claim"].startswith("reference is the 0.9 V source")
    assert structural["artifact"]["content_hash"].startswith("sha256:")
    assert item["detail"]["row_sources"] == {"tdec": "asserted"}


def test_structural_row_without_evidence_is_partial_coverage_never_met(tmp_path):
    item = _item5({}, [_structural(_artifact(tmp_path))])

    assert item["status"] == "unmet"
    assert item["reason"] == "partial_coverage"
    assert item["citation"] is None
    assert item["detail"]["uncovered_rows"] == []
    assert [r["id"] for r in item["detail"]["structural_rows"]] == ["vref_const"]


def test_structural_row_keeps_t1_blocked(tmp_path):
    report = build_tier_report(
        _manifest(spec_rows={"5": [_structural(_artifact(tmp_path))]})
    )

    assert report["tier"] is None
    item5 = next(i for i in report["items"] if i["id"] == 5)
    assert item5["status"] == "unmet"


def test_structural_artifact_unreadable_names_row_and_file(tmp_path):
    missing = _artifact(tmp_path)
    os.remove(missing["file"])

    item = _item5({}, [_structural(missing)])

    assert item["status"] == "unmet"
    assert item["reason"] == "structural_artifact_unreadable"
    assert item["detail"]["row"] == "vref_const"
    assert item["detail"]["document"] == "artifact"
    assert item["detail"]["file"] == missing["file"]


def test_structural_artifact_stale_reports_both_hashes(tmp_path):
    stale = _artifact(tmp_path)
    Path(stale["file"]).write_text("* an edit after the hash was taken\n")

    item = _item5({}, [_structural(stale)])

    assert item["status"] == "unmet"
    assert item["reason"] == "structural_artifact_stale"
    assert item["detail"]["expected_content_hash"] == stale["content_hash"]
    assert item["detail"]["actual_content_hash"].startswith("sha256:")
    assert item["detail"]["actual_content_hash"] != stale["content_hash"]


def test_plain_gap_control_renders_under_declared_gaps(tmp_path):
    rows = [
        {
            "id": "vref_const",
            "gap": "reference is a bench constant, not a measured node",
        }
    ]

    item = _item5({}, rows)

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["declared_gaps"] == [
        {
            "id": "vref_const",
            "gap": "reference is a bench constant, not a measured node",
        }
    ]
    assert "structural_rows" not in item["detail"]


def test_structural_and_gap_together_is_invalid_evidence(tmp_path):
    row = {
        **_structural(_artifact(tmp_path)),
        "gap": "also a gap",
    }

    item = _item5({}, [row])

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"


def test_structural_with_empty_claim_is_invalid_evidence(tmp_path):
    item = _item5({}, [_structural(_artifact(tmp_path), claim="")])

    assert item["reason"] == "invalid_evidence"


def test_structural_without_artifact_is_invalid_evidence(tmp_path):
    row = {"id": "vref_const", "structural": {"claim": "wired in by the bench"}}

    item = _item5({}, [row])

    assert item["reason"] == "invalid_evidence"


def test_structural_with_malformed_artifact_is_invalid_evidence(tmp_path):
    row = {
        "id": "vref_const",
        "structural": {
            "claim": "wired in by the bench",
            "artifact": {"file": "sim/tb_ota.spice"},
        },
    }

    item = _item5({}, [row])

    assert item["reason"] == "invalid_evidence"


def test_structural_with_unknown_key_is_invalid_evidence(tmp_path):
    row = {
        "id": "vref_const",
        "structural": {
            "claim": "wired in by the bench",
            "artifact": _artifact(tmp_path),
            "issue": "#6985",
        },
    }

    item = _item5({}, [row])

    assert item["reason"] == "invalid_evidence"


def test_structural_non_dict_is_invalid_evidence(tmp_path):
    row = {"id": "vref_const", "structural": "wired in by the bench"}

    item = _item5({}, [row])

    assert item["reason"] == "invalid_evidence"


def test_citation_naming_a_structural_row_is_invalid_evidence(tmp_path):
    rows = [_structural(_artifact(tmp_path))]

    item = _item5(
        {
            "5": [
                {
                    "file": _write(tmp_path, "a.json", SIM_PASS_ENVELOPE),
                    "rows": ["vref_const"],
                }
            ]
        },
        rows,
    )

    assert item["reason"] == "invalid_evidence"


def test_partition_qualified_structural_row_resolves(tmp_path):
    manifest = _manifest(
        evidence={}, spec_rows={"5.analog": [_structural(_artifact(tmp_path))]}
    )
    report = build_tier_report(manifest)

    item5 = next(i for i in report["items"] if i["id"] == 5)
    assert item5["reason"] == "partial_coverage"
    assert [r["id"] for r in item5["detail"]["structural_rows"]] == ["vref_const"]


def test_structural_row_on_ratified_false_row_is_not_unratified(tmp_path):
    row = {"id": "vref_const", "ratified": False, **_structural(_artifact(tmp_path))}

    item = _item5({}, [row])

    assert item["reason"] == "partial_coverage"
    assert item["detail"]["unratified_rows"] == []
    assert [r["id"] for r in item["detail"]["structural_rows"]] == ["vref_const"]


def test_manifest_without_structural_rows_renders_byte_identically(tmp_path):
    manifest = _manifest(
        evidence={},
        spec_rows={"5": [{"id": "tdec"}, {"id": "zin", "gap": "later"}]},
    )
    report = build_tier_report(manifest)

    assert "structural_rows" not in report
    detail = next(i for i in report["items"] if i["id"] == 5)["detail"]
    assert sorted(detail) == [
        "declared_gaps",
        "failed_rows",
        "row_sources",
        "uncovered_rows",
        "unratified_rows",
    ]
    assert all(r["state"] != "structural" for r in report["spec_rows"])


def test_spec_row_table_record_state_is_structural(tmp_path):
    a = _write(tmp_path, "a.json", SIM_PASS_ENVELOPE)
    manifest = _manifest(
        evidence={"5": [{"file": a, "rows": ["tdec"]}]},
        spec_rows={"5": [{"id": "tdec"}, _structural(_artifact(tmp_path))]},
    )
    report = build_tier_report(manifest)

    record = next(r for r in report["spec_rows"] if r["id"] == "vref_const")

    assert record["state"] == "structural"
    assert record["gap"] is None
    assert record["claim"].startswith("reference is the 0.9 V source")
    assert record["artifact"]["content_hash"].startswith("sha256:")
    assert record["limits"] is None
    assert record["sources"] == []
    assert next(r for r in report["spec_rows"] if r["id"] == "tdec")["state"] == (
        "unattributable"
    )


def test_item6_structural_row_is_listed_apart(tmp_path):
    manifest = _manifest(evidence={})
    manifest["spec_rows"] = {"6": [_structural(_artifact(tmp_path))]}

    item = next(i for i in build_tier_report(manifest)["items"] if i["id"] == 6)

    assert item["reason"] == "partial_coverage"
    assert [r["id"] for r in item["detail"]["structural_rows"]] == ["vref_const"]


def test_structural_row_text_output(tmp_path, capsys):
    artifact = _artifact(tmp_path)
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            _manifest(
                evidence={},
                spec_rows={
                    "5": [
                        _structural(artifact),
                        {
                            **_structural(artifact),
                            "id": "device_ratio",
                            "structural": {
                                "claim": "unit-sized input pair, per the deck",
                                "artifact": artifact,
                            },
                        },
                    ]
                },
            )
        )
    )

    assert main(["signoff", "--manifest", str(manifest), "--format", "text"]) == 3
    out = capsys.readouterr().out
    assert "structural: vref_const" in out
    assert f"artifact: {artifact['file']}" in out
    assert f"content_hash={artifact['content_hash']}" in out
    assert "[STRCT] vref_const" in out
