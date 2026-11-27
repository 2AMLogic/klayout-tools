"""Compatibility controls for the pre-0.9 item-1 input binding (#5730)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import build_attestation
from test_signoff import _attest_cli, _attest_repo, _write_inventory
from test_signoff_attest_multi_stream import _two

_BINDING_FIELDS = {
    "provenance.input.path",
    "provenance.input.paths",
    "provenance.input.content_hash",
}


def _legacy_item1(repo, version="0.8.0"):
    inventory = _write_inventory(repo, 1)
    envelope = build_attestation(1, inventory)
    provenance = envelope["provenance"]
    # 0.8.x bound just the inventory, whose path and raw digest are both
    # present in the current input set.
    inventory_path = Path(inventory).relative_to(repo).as_posix()
    provenance["input"] = next(
        member
        for member in provenance["input"]["paths"]
        if member["path"]["path"] == inventory_path
    )
    if version is None:
        provenance.pop("klt_version")
    else:
        provenance["klt_version"] = version
    committed = repo / "evidence" / "item1.json"
    committed.write_text(json.dumps(envelope))
    return inventory, committed


@pytest.mark.parametrize("version", ["0.8.0", None])
def test_legacy_item1_binding_retains_drift_and_exit_code(tmp_path, capsys, version):
    inventory, committed = _legacy_item1(_attest_repo(tmp_path), version)
    assert _attest_cli(1, inventory, "--check", str(committed), "--format", "json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "drifted"
    assert result["fresh_status"] == "pass"
    assert {entry["field"] for entry in result["drift"]} == _BINDING_FIELDS


def test_legacy_item1_also_reports_changed_source_bytes(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory, committed = _legacy_item1(repo)
    (repo / "design/ota.sch").write_text("edited after attestation\n")

    assert _attest_cli(1, inventory, "--check", str(committed), "--format", "json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "drifted"
    assert _BINDING_FIELDS <= {entry["field"] for entry in result["drift"]}
    assert any(
        entry.get("check", {}).get("path") == "design/ota.sch"
        and entry["field"].endswith(".content_hash")
        and entry["committed"] != entry["fresh"]
        for entry in result["drift"]
    )


def test_second_item2_stream_is_input_drift(tmp_path, capsys):
    repo, first, second = _two(tmp_path)
    committed = str(repo / "evidence" / "item2.json")
    command = ["signoff", "--attest", "2", "--input", first, "--format", "json"]
    assert main([*command, "-o", committed]) == 0
    capsys.readouterr()

    assert main([*command, "--input", second, "--check", committed]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "drifted"
    assert "shape_mismatch" not in result
    assert _BINDING_FIELDS <= {entry["field"] for entry in result["drift"]}


def test_current_item1_binding_matches(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    committed = str(repo / "evidence" / "item1.json")
    assert _attest_cli(1, inventory, "-o", committed) == 0
    capsys.readouterr()

    assert _attest_cli(1, inventory, "--check", committed, "--format", "json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "match"
    assert result["drift"] == []
    assert "shape_mismatch" not in result
