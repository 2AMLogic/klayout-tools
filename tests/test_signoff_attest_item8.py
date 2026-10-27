"""``klt signoff --attest 8 --inventory FILE`` (issue #4311)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)

_PASS = {"argv": [sys.executable, "-c", "pass"]}
_FAIL = {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / "report.md").write_text("# characterization\n")
    for name in ("sim_tt", "sim_ss"):
        (repo / "evidence" / f"{name}.json").write_text(
            json.dumps(
                {"status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}}
            )
        )
    return repo


def _analog_inventory(**overrides) -> dict:
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 8,
        "report": "report.md",
        "spec_rows": ["gain", "bandwidth"],
        "citations": {
            "gain": ["evidence/sim_tt.json"],
            "bandwidth": ["evidence/sim_tt.json", "evidence/sim_ss.json"],
        },
        "envelope_checks": {
            "evidence/sim_tt.json": _PASS,
            "evidence/sim_ss.json": _PASS,
        },
    }
    inventory.update(overrides)
    return inventory


def _digital_inventory(**overrides) -> dict:
    metrics = {
        corner: {
            name: {"envelope": f"evidence/sim_{corner}.json", "field": f"result.{name}"}
            for name in ("fmax", "area", "power")
        }
        for corner in ("tt", "ss")
    }
    return _analog_inventory(corners=["tt", "ss"], metrics=metrics, **overrides)


def _attest(repo: Path, inventory: dict) -> dict:
    path = repo / "inventory.json"
    path.write_text(json.dumps(inventory))
    return build_attestation(8, str(path))


def _failed(envelope: dict) -> list[tuple]:
    return [
        (c["check"], c.get("row") or c.get("target") or c.get("metric") or c["role"])
        for c in envelope["attestation"]["checks"]
        if not c["passed"]
    ]


def _grade(repo: Path, envelope: dict) -> dict:
    env_path = repo / "evidence" / "characterization.json"
    env_path.write_text(render_envelope(envelope))
    manifest = {
        "block": "demo",
        "kind": "analog",
        "evidence": {
            "8": {
                "file": str(env_path),
                "content_hash": envelope["provenance"]["input"]["content_hash"],
            }
        },
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == 8)


def test_analog_pass_shape_and_grades_met_with_disclosure(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _analog_inventory())

    assert envelope["status"] == "pass"
    assert envelope["kind"] == "generic"
    assert envelope["t1_item"] == 8
    assert envelope["attestation"]["hash_algorithm"] == "sha256-raw-bytes"
    item = _grade(repo, envelope)
    assert item["status"] == "met"
    disclosure = item["citation"]["attestation"]
    assert disclosure["tool_produced"] is True
    assert disclosure["producer"] == "klt signoff --attest"
    assert disclosure["check_count"] == envelope["attestation"]["check_count"]


def test_bare_generic_still_grades_with_tool_produced_false(tmp_path):
    repo = _repo(tmp_path)
    bare = {
        "schema_version": 1,
        "kind": "generic",
        "status": "pass",
        "summary": "hand written",
    }
    env_path = repo / "evidence" / "bare.json"
    env_path.write_text(json.dumps(bare))
    report = build_tier_report(
        {"block": "demo", "kind": "analog", "evidence": {"8": {"file": str(env_path)}}}
    )

    item = next(i for i in report["items"] if i["id"] == 8)

    assert item["status"] == "met"
    assert item["citation"]["attestation"] == {"tool_produced": False}


def test_spec_row_without_citation_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory(spec_rows=["gain", "bandwidth", "psrr"])

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert _failed(envelope) == [("citation", "psrr")]


def test_missing_cited_envelope_fails(tmp_path):
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_ss.json").unlink()

    envelope = _attest(repo, _analog_inventory())

    assert envelope["status"] == "fail"
    assert ("citation", "bandwidth") in _failed(envelope)


def test_failing_envelope_check_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["envelope_checks"]["evidence/sim_ss.json"] = _FAIL

    envelope = _attest(repo, inventory)

    assert _failed(envelope) == [("command", "evidence/sim_ss.json")]


def test_digital_metrics_pass_and_are_recorded(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _digital_inventory())

    assert envelope["status"] == "pass"
    metrics = [c for c in envelope["attestation"]["checks"] if c["check"] == "metric"]
    assert len(metrics) == 6
    assert {c["value"] for c in metrics} == {100, 5, 2}


def test_digital_missing_metric_for_one_corner_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    del inventory["metrics"]["ss"]["fmax"]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert _failed(envelope) == [("metric", "fmax")]


def test_digital_non_numeric_metric_field_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    inventory["metrics"]["tt"]["power"]["field"] = "status"

    envelope = _attest(repo, inventory)

    assert _failed(envelope) == [("metric", "power")]


def test_mixed_signal_style_inventory_with_duplicate_citation(tmp_path):
    repo = _repo(tmp_path)
    inventory = _digital_inventory()
    inventory["citations"]["gain"] = ["evidence/sim_tt.json", "evidence/sim_tt.json"]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "pass"
    cited_once = [
        c for c in envelope["attestation"]["checks"] if c["role"] == "evidence"
    ]
    assert len(cited_once) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda inv: inv.pop("report"),
        lambda inv: inv.update(spec_rows=[]),
        lambda inv: inv["citations"].update(unknown=["evidence/sim_tt.json"]),
        lambda inv: inv.pop("envelope_checks"),
        lambda inv: inv["envelope_checks"].pop("evidence/sim_ss.json"),
        lambda inv: inv.update(corners=["tt"]),
        lambda inv: inv.update(typo=1),
        lambda inv: inv["citations"].update(gain=["../outside.json"]),
    ],
)
def test_malformed_inventory_is_refused(tmp_path, mutate):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    mutate(inventory)

    with pytest.raises(AttestationError):
        _attest(repo, inventory)


def test_check_reports_drift_on_cited_envelope_and_row_map(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    envelope = _attest(repo, inventory)
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(envelope))
    inv_path = str(repo / "inventory.json")

    clean = check_attestation(8, inv_path, str(committed))
    assert clean["status"] == "match"
    assert clean["fresh_status"] == "pass"

    (repo / "evidence" / "sim_tt.json").write_text(json.dumps({"status": "pass"}))
    drifted = check_attestation(8, inv_path, str(committed))
    assert drifted["status"] != "match"

    (repo / "evidence" / "sim_tt.json").write_text(
        json.dumps({"status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}})
    )
    inventory["citations"]["gain"] = ["evidence/sim_ss.json"]
    (repo / "inventory.json").write_text(json.dumps(inventory))
    remapped = check_attestation(8, inv_path, str(committed))
    assert remapped["status"] != "match"


def test_report_cited_as_evidence_is_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = _analog_inventory()
    inventory["citations"]["gain"] = ["report.md"]
    inventory["envelope_checks"]["report.md"] = _PASS

    with pytest.raises(AttestationError):
        _attest(repo, inventory)
