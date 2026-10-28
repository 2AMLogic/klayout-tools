"""``klt signoff --attest 1`` with a ``recorded`` derived freshness record
(issue #4531)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "rtl").mkdir()
    (repo / "netlist").mkdir()
    (repo / "rtl/top.v").write_text("module top; endmodule\n")
    (repo / "rtl/alu.v").write_bytes(b"module alu;\r\nendmodule\r\n")
    (repo / "netlist/top.v").write_text("// synthesized\n")
    return repo


def _inventory(repo: Path, **recorded_overrides) -> dict:
    recorded = {
        "derived_sha256": _sha(repo / "netlist/top.v"),
        "sources": {
            "rtl/top.v": _sha(repo / "rtl/top.v"),
            "rtl/alu.v": _sha(repo / "rtl/alu.v"),
        },
    }
    recorded.update(recorded_overrides)
    return {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 1,
        "sources": ["rtl/top.v", "rtl/alu.v"],
        "derived": [
            {"path": "netlist/top.v", "generator": "yosys", "recorded": recorded}
        ],
    }


def _attest(repo: Path, inventory: dict) -> dict:
    path = repo / "inv.json"
    path.write_text(json.dumps(inventory))
    return build_attestation(1, str(path))


def _freshness(envelope: dict) -> dict:
    (check,) = [
        c for c in envelope["attestation"]["checks"] if c["role"] == "freshness"
    ]
    return check


def test_matching_record_passes(tmp_path):
    repo = _repo(tmp_path)
    envelope = _attest(repo, _inventory(repo))
    check = _freshness(envelope)
    assert check["check"] == "recorded_hash"
    assert check["passed"] is True
    assert "mismatches" not in check
    assert envelope["status"] == "pass"


@pytest.mark.parametrize("victim", ["netlist/top.v", "rtl/top.v", "rtl/alu.v"])
def test_changed_file_fails_naming_path_and_digests(tmp_path, victim):
    repo = _repo(tmp_path)
    inventory = _inventory(repo)
    old = _sha(repo / victim)
    (repo / victim).write_text("changed\n")
    envelope = _attest(repo, inventory)
    check = _freshness(envelope)
    assert check["passed"] is False
    assert check["mismatches"] == [
        {"path": victim, "expected": old, "actual": _sha(repo / victim)}
    ]
    assert envelope["status"] != "pass"


def test_missing_source_fails(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inventory(repo)
    (repo / "rtl/alu.v").unlink()
    check = _freshness(_attest(repo, inventory))
    assert check["passed"] is False
    assert check["mismatches"][0]["path"] == "rtl/alu.v"
    assert check["mismatches"][0]["actual"] is None


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda e: e.update(check={"argv": [sys.executable, "-c", "pass"]}),
            "exactly one",
        ),
        (lambda e: e.pop("recorded"), "exactly one"),
        (lambda e: e["recorded"].update(extra=1), "unknown field"),
        (lambda e: e["recorded"].update(sources={}), "non-empty"),
        (lambda e: e["recorded"].update(derived_sha256="abc"), "64-character"),
        (lambda e: e["recorded"].update(derived_sha256="A" * 64), "lowercase"),
        (lambda e: e["recorded"].update(sources={"rtl/top.v": "zz"}), "64-character"),
        (lambda e: e["recorded"].update(sources={"rtl/other.v": "0" * 64}), "not in"),
    ],
)
def test_validation_refusals(tmp_path, mutate, message):
    repo = _repo(tmp_path)
    inventory = _inventory(repo)
    mutate(inventory["derived"][0])
    with pytest.raises(AttestationError, match=message):
        _attest(repo, inventory)


def test_derived_also_listed_as_source_refused(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inventory(repo)
    inventory["sources"].append("netlist/top.v")
    with pytest.raises(AttestationError, match="more than once"):
        _attest(repo, inventory)


def test_check_reports_no_drift_then_drift(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inventory(repo)
    envelope = _attest(repo, inventory)
    committed = repo / "committed.json"
    committed.write_text(render_envelope(envelope))
    inv_path = str(repo / "inv.json")
    clean = check_attestation(1, inv_path, str(committed))
    assert clean["status"] == "match"
    assert clean["fresh_status"] == "pass"
    (repo / "rtl/top.v").write_text("module top2; endmodule\n")
    drifted = check_attestation(1, inv_path, str(committed))
    assert drifted["status"] != "match"
