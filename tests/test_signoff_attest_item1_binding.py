"""``klt signoff --attest 1`` binds its full input set (issue #4764).

Item 1 pins every source, every derived file and the inventory itself through
``provenance.input.paths`` so editing any member makes the manifest row stale.
Items 9 and 10 keep the single-inventory binding (see ``test_signoff.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import signoff as signoff_module
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import build_attestation
from test_signoff import (
    _attest_cli,
    _attest_repo,
    _item,
    _manifest,
    _write_inventory,
)


def _pinned_manifest(repo: Path, envelope: dict) -> dict:
    return _manifest(
        evidence={
            "1": {
                "file": str(repo / "evidence" / "item1.json"),
                "content_hash": envelope["provenance"]["input"]["content_hash"],
            }
        }
    )


def _attested(tmp_path) -> tuple[Path, str, dict, dict]:
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    envelope = build_attestation(1, inventory)
    (repo / "evidence" / "item1.json").write_text(json.dumps(envelope, indent=2))
    return repo, inventory, envelope, _pinned_manifest(repo, envelope)


def test_envelope_binds_sources_derived_and_inventory(tmp_path):
    repo = _attest_repo(tmp_path)
    envelope = build_attestation(1, _write_inventory(repo, 1))

    binding = envelope["provenance"]["input"]
    names = [m["path"]["path"] for m in binding["paths"]]
    assert names == sorted(names)
    assert {"attest/item1.json", "design/ota.sch", "netlist/ota.spice"} <= set(names)
    assert "path" not in binding
    assert binding["content_hash"] == signoff_module.input_set_digest(
        (m["path"]["path"], m["content_hash"]) for m in binding["paths"]
    )


def test_cli_envelope_is_met_and_refused_on_the_other_items(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    output = repo / "evidence" / "item1.json"
    assert _attest_cli(1, _write_inventory(repo, 1), "-o", str(output)) == 0
    capsys.readouterr()
    pin = json.loads(output.read_text())["provenance"]["input"]["content_hash"]

    entry = {"file": str(output), "content_hash": pin}
    result = build_tier_report(_manifest(evidence={str(i): entry for i in (1, 9, 10)}))

    bound = _item(result, 1)
    assert bound["status"] == "met"
    binding = bound["citation"]["artifact_binding"]
    assert binding["t1_item"] == 1
    assert binding["path"] is None  # set binding
    assert "attest/item1.json" in [m["path"]["path"] for m in binding["members"]]
    assert binding["input_verified"] is True
    assert binding["attestation"]["producer"] == "klt signoff --attest"
    for other in (9, 10):
        replayed = _item(result, other)
        assert replayed["status"] == "unmet"
        assert replayed["reason"] == "wrong_item"


def test_untouched_attested_tree_stays_met(tmp_path):
    repo, _, _, manifest = _attested(tmp_path)

    graded = _item(build_tier_report(manifest, repo_root=str(repo)), 1)

    assert graded["status"] == "met"
    assert "detail" not in graded or not graded["detail"]
    members = graded["citation"]["artifact_binding"]["members"]
    assert {m["status"] for m in members} == {"verified"}


@pytest.mark.parametrize("path", ["design/ota.sch", "netlist/ota.spice"])
def test_editing_an_attested_source_or_derived_file_is_stale(tmp_path, path):
    repo, _, _, manifest = _attested(tmp_path)

    (repo / path).write_text("edited after attestation\n")
    graded = _item(build_tier_report(manifest, repo_root=str(repo)), 1)

    assert graded["status"] == "unmet"
    assert graded["reason"] == "stale_evidence"
    members = graded["detail"]["artifact_binding"]["members"]
    assert [
        (m["path"]["path"], m["status"]) for m in members if m["status"] != "verified"
    ] == [(path, "stale")]


def test_deleting_an_attested_file_is_unverifiable_provenance(tmp_path):
    repo, _, _, manifest = _attested(tmp_path)

    (repo / "design" / "bias.sch").unlink()
    graded = _item(build_tier_report(manifest, repo_root=str(repo)), 1)

    assert graded["status"] == "unmet"
    assert graded["reason"] == "unverifiable_provenance"
    members = graded["detail"]["artifact_binding"]["members"]
    assert [
        (m["path"]["path"], m["status"]) for m in members if m["status"] != "verified"
    ] == [("design/bias.sch", "unverifiable")]


@pytest.mark.parametrize("edit", ["design/ota.sch", "netlist/ota.spice", "inventory"])
def test_digest_changes_when_any_member_is_edited(tmp_path, edit):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    before = build_attestation(1, inventory)
    assert before == build_attestation(1, inventory)  # byte-stable

    target = Path(inventory) if edit == "inventory" else repo / edit
    target.write_text(target.read_text() + "\n")
    after = build_attestation(1, inventory)

    assert (
        after["provenance"]["input"]["content_hash"]
        != before["provenance"]["input"]["content_hash"]
    )
