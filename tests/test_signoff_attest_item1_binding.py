"""``klt signoff --attest 1`` binds its full input set (issue #4764).

Item 1 pins every source, every derived file and the inventory itself through
``provenance.input.paths`` so editing any member makes the manifest row stale.
Items 9 and 10 keep the single-inventory binding (see ``test_signoff.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import signoff as signoff_module
from klayout_tools._provenance import sha256_file
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


def test_envelope_hash_pin_explains_what_to_pin(tmp_path):
    repo, _inventory, envelope, manifest = _attested(tmp_path)
    evidence = repo / "evidence" / "item1.json"
    manifest["evidence"]["1"]["content_hash"] = "sha256:" + sha256_file(str(evidence))
    item = _item(build_tier_report(manifest), 1)
    assert item["reason"] == "stale_evidence"
    detail = item["detail"]
    assert detail["hash_check"]["match"] is False
    assert detail["pin_is_envelope_file_hash"] is True
    assert "attest/item1.json" in detail["compared_against"]
    assert "not the hash of the evidence envelope file" in detail["hint"]


def test_other_stale_pin_has_hint_without_envelope_flag(tmp_path):
    repo, _inventory, _envelope, manifest = _attested(tmp_path)
    manifest["evidence"]["1"]["content_hash"] = "sha256:" + "0" * 64
    detail = _item(build_tier_report(manifest), 1)["detail"]
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"].startswith("content_hash must be")


# --- Issue #5640: the item-1 coverage floor -------------------------------


def _narrow_to_inventory(repo: Path, inventory: str, envelope: dict) -> dict:
    """Rewrite ``provenance.input`` to the pre-0.9.0 producer's shape: the
    inventory file bound alone, no ``paths`` key. The attestation block --
    which enumerates every member the envelope was made about -- is left
    intact, so the envelope carries the proof of its own narrowing."""
    envelope["provenance"]["input"] = {
        "path": {"path": "attest/item1.json", "scope": "repo"},
        "content_hash": "sha256:" + sha256_file(inventory),
    }
    return envelope


def _grade_narrow(tmp_path, repo: Path, envelope: dict, **kwargs) -> dict:
    (repo / "evidence" / "item1.json").write_text(json.dumps(envelope, indent=2))
    manifest = _pinned_manifest(repo, envelope)
    return _item(build_tier_report(manifest, **kwargs), 1)


def test_pre_090_item1_envelope_grades_provenance_narrower(tmp_path):
    repo, inventory, envelope, _ = _attested(tmp_path)
    _narrow_to_inventory(repo, inventory, envelope)

    graded = _grade_narrow(tmp_path, repo, envelope, repo_root=str(repo))

    assert graded["status"] == "unmet"
    assert graded["reason"] == "provenance_narrower_than_attested"
    detail = graded["detail"]
    assert detail["missing"] == ["provenance.input.paths"]
    present = [
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "present" and c.get("path")
    ]
    assert detail["attested_members"] == len(present) == 3
    assert detail["bound_paths"] == 0


def test_narrow_envelope_reason_does_not_need_the_repo(tmp_path):
    """The floor is a shape check: it fires without ``--repo-root`` too,
    before any member re-hash could depend on the filesystem."""
    repo, inventory, envelope, _ = _attested(tmp_path)
    _narrow_to_inventory(repo, inventory, envelope)

    graded = _grade_narrow(tmp_path, repo, envelope)

    assert graded["reason"] == "provenance_narrower_than_attested"


def test_handwritten_narrow_envelope_without_attestation_is_unchanged(tmp_path):
    """A hand-written item-1 generic envelope enumerates nothing, so the
    floor has nothing to compare against and the old single-path grading
    path applies unchanged."""
    repo, inventory, envelope, _ = _attested(tmp_path)
    del envelope["attestation"]
    _narrow_to_inventory(repo, inventory, envelope)

    graded = _grade_narrow(tmp_path, repo, envelope, repo_root=str(repo))

    assert graded["status"] == "met"
    assert graded["citation"]["artifact_binding"]["input_verified"] is True
    assert "members" not in graded["citation"]["artifact_binding"]


def test_envelope_with_only_non_present_checks_is_not_narrow(tmp_path):
    """An attestation whose ``checks[]`` holds only command/freshness entries
    enumerates no file, so there is no breadth to lose."""
    repo, inventory, envelope, _ = _attested(tmp_path)
    envelope["attestation"]["checks"] = [
        c for c in envelope["attestation"]["checks"] if c["check"] != "present"
    ]
    assert envelope["attestation"]["checks"]
    _narrow_to_inventory(repo, inventory, envelope)

    graded = _grade_narrow(tmp_path, repo, envelope, repo_root=str(repo))

    assert graded["status"] == "met"
    assert graded["citation"]["artifact_binding"]["input_verified"] is True


def test_malformed_paths_is_not_reported_as_narrowing(tmp_path):
    """A present-but-empty ``paths`` is a malformed binding, not a narrowed
    one: the existing ``unanchored_evidence`` refusal applies and the floor
    must not shadow it."""
    repo, inventory, envelope, _ = _attested(tmp_path)
    envelope["provenance"]["input"] = {
        "paths": [],
        "content_hash": "sha256:" + sha256_file(inventory),
    }

    graded = _grade_narrow(tmp_path, repo, envelope, repo_root=str(repo))

    assert graded["status"] == "unmet"
    assert graded["reason"] == "unanchored_evidence"
    assert graded["detail"]["missing"] == ["provenance.input.path"]


def test_floor_is_scoped_to_item_1(tmp_path):
    """Items 2, 9 and 10 bind their inventory (or stream) alone by design --
    an envelope with the narrow shape never trips the floor for them."""
    from klayout_tools.signoff_evidence_pin import (
        REASON_PROVENANCE_NARROWER,
        generic_binding_check,
        provenance_narrowing,
    )

    repo, inventory, envelope, _ = _attested(tmp_path)
    _narrow_to_inventory(repo, inventory, envelope)
    assert provenance_narrowing(envelope) == {"attested_members": 3, "bound_paths": 0}

    spec = {"content_hash": "sha256:" + "a" * 64}
    for item in (2, 9, 10):
        scoped = dict(envelope, t1_item=item)
        assert generic_binding_check(item, scoped, spec, True, True) == (None, [])
    assert generic_binding_check(1, envelope, spec, True, True) == (
        REASON_PROVENANCE_NARROWER,
        ["provenance.input.paths"],
    )
