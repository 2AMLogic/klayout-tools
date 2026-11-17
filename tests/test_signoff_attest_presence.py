"""Presence-only ``hygiene`` entries (issue #5615).

A ``{"path", "attest": "presence"}`` entry omits ``content_hash`` from its
``present`` check, so editing the file is neither ``--check`` drift nor a
``stale_evidence`` manifest row. The option is item 10 ``hygiene`` only.
"""

from __future__ import annotations

import hashlib
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

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / "README.md").write_text("# readme\n")
    (repo / "CLAUDE.md").write_text("managed v1\n")
    (repo / "tb.spice").write_text("* tb\n")
    return repo


def _inv(repo: Path, item: int = 10, **fields) -> str:
    inventory: dict = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": item,
        **fields,
    }
    if item == 10:
        inventory.setdefault("checks", [{"name": "ok", "argv": _OK}])
    path = repo / "inv.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _hygiene_inv(repo: Path, hygiene) -> str:
    return _inv(repo, hygiene=hygiene)


def _checks(envelope: dict) -> dict[str, dict]:
    return {
        c["path"]: c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "present"
    }


_MIXED = ["README.md", {"path": "CLAUDE.md", "attest": "presence"}]


def test_bare_string_entries_are_unchanged(tmp_path):
    repo = _repo(tmp_path)
    check = _checks(build_attestation(10, _hygiene_inv(repo, ["README.md"])))[
        "README.md"
    ]
    digest = hashlib.sha256(b"# readme\n").hexdigest()
    assert check == {
        "check": "present",
        "role": "hygiene",
        "path": "README.md",
        "passed": True,
        "content_hash": f"sha256:{digest}",
    }


def test_explicit_content_entry_equals_bare_string(tmp_path):
    repo = _repo(tmp_path)
    bare = build_attestation(10, _hygiene_inv(repo, ["README.md"]))
    explicit = build_attestation(
        10, _hygiene_inv(repo, [{"path": "README.md", "attest": "content"}])
    )
    assert _checks(bare) == _checks(explicit)


def test_presence_entry_omits_content_hash_and_is_self_describing(tmp_path):
    repo = _repo(tmp_path)
    checks = _checks(build_attestation(10, _hygiene_inv(repo, _MIXED)))
    assert "content_hash" in checks["README.md"]
    assert "attest" not in checks["README.md"]
    assert checks["CLAUDE.md"] == {
        "check": "present",
        "role": "hygiene",
        "path": "CLAUDE.md",
        "passed": True,
        "attest": "presence",
    }
    assert "content_hash" not in checks["CLAUDE.md"]


def test_presence_entry_still_fails_on_a_missing_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").unlink()
    envelope = build_attestation(10, _hygiene_inv(repo, _MIXED))
    check = _checks(envelope)["CLAUDE.md"]
    assert check["passed"] is False
    assert "content_hash" not in check
    assert envelope["status"] == "fail"


def _committed(repo: Path, inventory: str) -> str:
    path = repo / "evidence" / "item10.json"
    path.write_text(render_envelope(build_attestation(10, inventory)))
    return str(path)


def test_editing_a_presence_file_keeps_check_at_match(tmp_path):
    repo = _repo(tmp_path)
    inventory = _hygiene_inv(repo, _MIXED)
    committed = _committed(repo, inventory)
    (repo / "CLAUDE.md").write_text("managed v2, resynced\n")

    result = check_attestation(10, inventory, committed)

    assert result["status"] == "match", result.get("drift")


def test_editing_a_content_file_still_drifts(tmp_path):
    repo = _repo(tmp_path)
    inventory = _hygiene_inv(repo, _MIXED)
    committed = _committed(repo, inventory)
    (repo / "README.md").write_text("# readme, edited\n")

    result = check_attestation(10, inventory, committed)

    assert result["status"] != "match"
    assert any(e["field"].endswith(".content_hash") for e in result["drift"])


def test_flipping_content_to_presence_is_drift_until_reminted(tmp_path):
    repo = _repo(tmp_path)
    committed = _committed(repo, _hygiene_inv(repo, ["README.md", "CLAUDE.md"]))
    flipped = _hygiene_inv(repo, _MIXED)

    result = check_attestation(10, flipped, committed)

    assert result["status"] != "match"


def test_presence_only_entry_that_is_the_inventory_itself(tmp_path):
    repo = _repo(tmp_path)
    inventory = _hygiene_inv(
        repo, ["README.md", {"path": "inv.json", "attest": "presence"}]
    )
    envelope = build_attestation(10, inventory)
    assert _checks(envelope)["inv.json"]["passed"] is True
    assert "content_hash" not in _checks(envelope)["inv.json"]


# --- grading (mechanism 3: --manifest re-hash, #4197) ----------------------- #


def test_manifest_row_stays_met_after_editing_a_presence_file(tmp_path):
    repo = _repo(tmp_path)
    inventory = _hygiene_inv(repo, _MIXED)
    envelope_path = repo / "evidence" / "item10.json"
    envelope_path.write_text(json.dumps(build_attestation(10, inventory), indent=2))
    digest = hashlib.sha256(Path(inventory).read_bytes()).hexdigest()
    manifest = {
        "block": "demo",
        "kind": "analog",
        "evidence": {
            "10": {"file": str(envelope_path), "content_hash": f"sha256:{digest}"}
        },
    }

    def row() -> dict:
        report = build_tier_report(manifest, repo_root=str(repo))
        return next(i for i in report["items"] if i["id"] == 10)

    assert row()["status"] == "met"
    (repo / "CLAUDE.md").write_text("managed v2, resynced\n")
    assert row()["status"] == "met"
    (repo / "README.md").write_text("# readme, edited\n")
    graded = row()
    assert graded["status"] == "unmet"
    assert graded["reason"] == "stale_evidence"


# --- validation -------------------------------------------------------------- #


def test_presence_refused_on_item1_sources(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        item=1,
        sources=[{"path": "tb.spice", "attest": "presence"}],
        derived=[],
    )
    with pytest.raises(AttestationError, match="item 10 'hygiene'"):
        build_attestation(1, inventory)


def test_attest_refused_on_item9_testbenches(tmp_path):
    repo = _repo(tmp_path)
    inventory = _inv(
        repo,
        item=9,
        testbenches=[{"path": "tb.spice", "attest": "presence"}],
        cold_start={"argv": _OK},
    )
    with pytest.raises(AttestationError, match="attest"):
        build_attestation(9, inventory)


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({"path": "README.md", "attest": "hash"}, "must be one of"),
        ({"path": "README.md", "attest": True}, "must be one of"),
        ({"path": "README.md", "attest": "presence", "extra": 1}, "extra"),
        ({"attest": "presence"}, "must declare 'path'"),
    ],
)
def test_malformed_mapping_entries_are_refused(tmp_path, entry, needle):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=needle):
        build_attestation(10, _hygiene_inv(repo, [entry]))


def test_duplicate_path_is_refused_through_the_mapping_form(tmp_path):
    repo = _repo(tmp_path)
    inventory = _hygiene_inv(
        repo, ["README.md", {"path": "README.md", "attest": "presence"}]
    )
    with pytest.raises(AttestationError, match="more than once"):
        build_attestation(10, inventory)


def test_escape_and_symlink_refused_through_the_mapping_form(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError):
        build_attestation(
            10, _hygiene_inv(repo, [{"path": "../x", "attest": "presence"}])
        )
    (repo / "link.md").symlink_to(repo / "README.md")
    with pytest.raises(AttestationError, match="symlink"):
        build_attestation(
            10, _hygiene_inv(repo, [{"path": "link.md", "attest": "presence"}])
        )
