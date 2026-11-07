"""Issue #5124: ``klt signoff --attest`` prints the manifest entry to cite."""

from __future__ import annotations

import json
import sys

from klayout_tools.cli import main


def _inventory(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "report.md").write_text("# characterization\n")
    (repo / "sim.json").write_text(json.dumps({"status": "pass"}))
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 8,
        "report": "report.md",
        "spec_rows": ["gain"],
        "citations": {"gain": ["sim.json"]},
        "envelope_checks": {
            "sim.json": {"argv": [sys.executable, "-c", "pass"]},
        },
    }
    path = repo / "inventory.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def test_json_manifest_entry_matches_envelope_hash(tmp_path, capsys):
    inventory = _inventory(tmp_path)
    out = tmp_path / "env.json"
    argv = ["signoff", "--attest", "8", "--inventory", inventory]
    assert main([*argv, "-o", str(out), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    data = payload.get("data", payload)
    entry = data["manifest_entry"]
    assert entry["file"] == str(out)
    assert entry["content_hash"] == data["provenance"]["input"]["content_hash"]
    assert "manifest_entry" not in json.loads(out.read_text())


def test_text_prints_manifest_entry(tmp_path, capsys):
    inventory = _inventory(tmp_path)
    out = tmp_path / "env.json"
    argv = ["signoff", "--attest", "8", "--inventory", inventory, "-o", str(out)]
    assert main(argv) == 0
    text = capsys.readouterr().out
    assert f"manifest entry: {{file: {out}, content_hash: sha256:" in text
