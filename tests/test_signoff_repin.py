"""Issue #5527: ``klt signoff --repin`` re-pins a manifest's stale hashes.

The regenerate-then-re-pin step after an attestation inventory changes used to
end in a hand edit of the block manifest's ``content_hash``; forgetting it
silently downgraded the item to ``unmet``/``stale_evidence``. These tests
cover the mechanical half and, just as importantly, what it refuses to touch.
"""

from __future__ import annotations

import json
import sys

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_repin import (
    SKIP_INPUT_NOT_VERIFIED,
    SKIP_INVALID_EVIDENCE,
    SKIP_UNRESOLVED_EVIDENCE,
)


def _write_inventory(repo, rows):
    """An item-8 attestation inventory citing ``rows``; returns its path."""
    inventory = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 8,
        "report": "report.md",
        "spec_rows": list(rows),
        "citations": {row: ["sim.json"] for row in rows},
        "envelope_checks": {"sim.json": {"argv": [sys.executable, "-c", "pass"]}},
    }
    path = repo / "inventory.json"
    path.write_text(json.dumps(inventory))
    return path


@pytest.fixture
def attested(tmp_path):
    """A repo with a passing item-8 envelope and a manifest pinning it.

    Returns ``(repo, inventory_path, envelope_path, manifest_path)``; the
    manifest is freshly pinned, so ``--manifest`` grades item 8 ``met``.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "report.md").write_text("# characterization\n")
    (repo / "sim.json").write_text(json.dumps({"status": "pass"}))
    inventory = _write_inventory(repo, ["gain"])
    envelope = repo / "env.json"
    assert _attest(inventory, envelope) == 0
    manifest = repo / "manifest.json"
    _write_manifest(
        manifest,
        {"8": {"file": str(envelope), "content_hash": _input_hash(envelope)}},
    )
    return repo, inventory, envelope, manifest


def _attest(inventory, envelope):
    return main(
        [
            "signoff",
            "--attest",
            "8",
            "--inventory",
            str(inventory),
            "-o",
            str(envelope),
            "--format",
            "json",
        ]
    )


def _write_manifest(path, evidence, **extra):
    path.write_text(
        json.dumps(
            {"block": "example", "kind": "analog", "evidence": evidence, **extra},
            indent=2,
        )
        + "\n"
    )


def _input_hash(envelope):
    return json.loads(envelope.read_text())["provenance"]["input"]["content_hash"]


def _item(manifest, item_id=8):
    report = build_tier_report(json.loads(manifest.read_text()))
    return next(row for row in report["items"] if row["id"] == item_id)


def _repin(manifest, capsys, *extra):
    capsys.readouterr()  # drop any --attest output this test already produced
    code = main(
        ["signoff", "--repin", "--manifest", str(manifest), "--format", "json", *extra]
    )
    return code, json.loads(capsys.readouterr().out)


def test_repin_closes_the_regenerate_then_repin_gap(attested, capsys):
    """The reporter's sequence: edit inventory, re-attest, re-pin, re-grade."""
    repo, inventory, envelope, manifest = attested
    assert _item(manifest)["status"] == "met"

    _write_inventory(repo, ["gain", "bw"])
    assert _attest(inventory, envelope) == 0
    stale = _item(manifest)
    assert (stale["status"], stale["reason"]) == ("unmet", "stale_evidence")
    old_pin = json.loads(manifest.read_text())["evidence"]["8"]["content_hash"]

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["status"] == "repinned"
    assert payload["changed_count"] == 1
    assert payload["skipped"] == []
    (change,) = payload["changed"]
    assert change["item"] == "8"
    assert change["index"] is None
    assert change["file"] == str(envelope)
    assert change["old_content_hash"] == old_pin
    assert change["new_content_hash"] == _input_hash(envelope)

    assert _item(manifest)["status"] == "met"


def test_stale_pin_is_skipped_when_the_artifact_is_not_readable_here(attested, capsys):
    """Stale pin, but the grader could not re-derive the live hash.

    The envelope was regenerated (so the pin really is stale), but the
    inventory it anchors to is gone, leaving ``input_verified`` null -- the
    live hash is then the envelope's own say-so, and copying that into the
    manifest is exactly what would defeat the staleness gate.
    """
    repo, inventory, envelope, manifest = attested
    _write_inventory(repo, ["gain", "bw"])
    assert _attest(inventory, envelope) == 0
    inventory.unlink()
    before = manifest.read_bytes()

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["status"] == "unchanged"
    assert payload["changed"] == []
    (skip,) = payload["skipped"]
    assert skip["item"] == "8"
    assert skip["reason"] == SKIP_INPUT_NOT_VERIFIED
    assert manifest.read_bytes() == before


def test_a_non_regenerated_envelope_is_not_papered_over(attested, capsys):
    """Inventory edited, envelope NOT regenerated: --repin is a no-op.

    The pin still matches the envelope's own (now outdated) claim, so there
    is no stale pin to fix -- and --repin must not invent one. The remedy is
    to re-run ``--attest``, which this command deliberately does not do.
    """
    repo, _inventory, _envelope, manifest = attested
    _write_inventory(repo, ["gain", "bw"])
    before = manifest.read_bytes()

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["status"] == "unchanged"
    assert (payload["changed"], payload["skipped"]) == ([], [])
    assert manifest.read_bytes() == before


def test_unpinned_entry_is_never_given_a_pin(attested, capsys):
    """A bare-string entry and a dict with no ``content_hash`` stay unpinned."""
    _repo, _inventory, envelope, manifest = attested
    _write_manifest(manifest, {"8": str(envelope), "4": {"file": str(envelope)}})
    before = manifest.read_bytes()

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert (payload["changed"], payload["skipped"]) == ([], [])
    assert manifest.read_bytes() == before


def test_a_kind_the_grader_does_not_rehash_is_skipped(attested, capsys):
    """A pinned ``drc`` report whose artifact does not resolve here.

    ``input_verified`` is not ``True``, so the pin -- the manifest author's
    own claim about which layout revision was checked -- is left intact.
    """
    repo, _inventory, _envelope, manifest = attested
    drc = repo / "drc.json"
    drc.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "drc",
                "status": "pass",
                "violations": [],
                "checks": [{"name": "a", "violations": 0}],
                "provenance": {
                    "input": {
                        "path": "nowhere/layout.gds",
                        "content_hash": "sha256:" + "b" * 64,
                    }
                },
            }
        )
    )
    _write_manifest(
        manifest, {"3": {"file": str(drc), "content_hash": "sha256:" + "a" * 64}}
    )
    before = manifest.read_bytes()

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["changed"] == []
    (skip,) = payload["skipped"]
    assert skip["item"] == "3"
    assert skip["reason"] == SKIP_INPUT_NOT_VERIFIED
    assert manifest.read_bytes() == before


def test_unresolvable_and_malformed_pinned_entries_are_reported(attested, capsys):
    """A pinned entry the grader cannot resolve is named, not silently kept."""
    repo, _inventory, _envelope, manifest = attested
    _write_manifest(
        manifest,
        {
            "3": {
                "file": str(repo / "absent.json"),
                "content_hash": "sha256:" + "a" * 64,
            },
            "4": {"content_hash": "sha256:" + "c" * 64},
        },
    )
    before = manifest.read_bytes()

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["changed"] == []
    assert {row["item"]: row["reason"] for row in payload["skipped"]} == {
        "3": SKIP_UNRESOLVED_EVIDENCE,
        "4": SKIP_INVALID_EVIDENCE,
    }
    assert manifest.read_bytes() == before


def test_a_clean_manifest_is_not_rewritten_at_all(attested, capsys):
    """Nothing stale: bytes and mtime are untouched, so no spurious diff."""
    _repo, _inventory, _envelope, manifest = attested
    before = manifest.read_bytes()
    before_mtime = manifest.stat().st_mtime_ns

    code, payload = _repin(manifest, capsys)
    assert code == 0
    assert payload["status"] == "unchanged"
    assert payload["changed_count"] == 0
    assert manifest.read_bytes() == before
    assert manifest.stat().st_mtime_ns == before_mtime


def test_rewrite_touches_only_the_changed_hash_lines(attested, capsys):
    """Byte-level: a 2-space-indented manifest differs only in the hash."""
    repo, inventory, envelope, manifest = attested
    before = manifest.read_text().splitlines()
    _write_inventory(repo, ["gain", "bw"])
    assert _attest(inventory, envelope) == 0

    code, payload = _repin(manifest, capsys)
    assert code == 0
    after = manifest.read_text().splitlines()
    assert len(before) == len(after)
    differing = [
        i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b
    ]
    assert len(differing) == 1
    assert "content_hash" in after[differing[0]]
    assert payload["changed"][0]["new_content_hash"] in after[differing[0]]
    assert manifest.read_text().endswith("\n")


def test_multi_citation_list_members_are_repinned_by_index(attested, capsys):
    """An item-5-shaped list entry: the record names which member moved."""
    repo, inventory, envelope, manifest = attested
    other = repo / "env2.json"
    other.write_text(envelope.read_text())
    _write_manifest(
        manifest,
        {
            "8": [
                {"file": str(other), "content_hash": _input_hash(other)},
                {"file": str(envelope), "content_hash": _input_hash(envelope)},
            ]
        },
    )
    _write_inventory(repo, ["gain", "bw"])
    assert _attest(inventory, envelope) == 0

    code, payload = _repin(manifest, capsys)
    assert code == 0
    (change,) = payload["changed"]
    assert (change["item"], change["index"]) == ("8", 1)
    evidence = json.loads(manifest.read_text())["evidence"]["8"]
    assert evidence[1]["content_hash"] == _input_hash(envelope)
    assert evidence[0]["content_hash"] == _input_hash(other)


def test_text_mode_prints_the_same_outcome(attested, capsys):
    repo, inventory, envelope, manifest = attested
    _write_inventory(repo, ["gain", "bw"])
    assert _attest(inventory, envelope) == 0
    capsys.readouterr()

    assert main(["signoff", "--repin", "--manifest", str(manifest)]) == 0
    out = capsys.readouterr().out
    assert "re-pinned item 8" in out
    assert "1 re-pinned, 0 skipped (rewritten)" in out


def test_text_mode_says_nothing_to_do(attested, capsys):
    _repo, _inventory, _envelope, manifest = attested
    capsys.readouterr()
    assert main(["signoff", "--repin", "--manifest", str(manifest)]) == 0
    assert "nothing to re-pin (not rewritten)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="no-manifest"),
        pytest.param(["--manifest", "-"], id="stdin-manifest"),
        pytest.param(["--manifest", "m.json", "--fleet", "f.json"], id="fleet"),
        pytest.param(["--manifest", "m.json", "--attest", "9"], id="attest"),
        pytest.param(["--manifest", "m.json", "--check", "r.json"], id="check"),
        pytest.param(["--manifest", "m.json", "--describe-grader"], id="describe"),
        pytest.param(["--manifest", "m.json", "e.json"], id="envelope-files"),
    ],
)
def test_usage_errors_return_the_usage_exit_code(extra, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["signoff", "--repin", "--format", "json", *extra]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["command"] == "signoff"
    assert "--repin" in error["message"]


def test_unreadable_manifest_is_an_application_error(tmp_path, capsys):
    missing = tmp_path / "absent.json"
    assert (
        main(["signoff", "--repin", "--manifest", str(missing), "--format", "json"])
        == 1
    )
    assert (
        "could not be read" in json.loads(capsys.readouterr().err)["error"]["message"]
    )


def test_non_object_manifest_is_refused(tmp_path, capsys):
    manifest = tmp_path / "m.json"
    manifest.write_text("[]")
    assert (
        main(["signoff", "--repin", "--manifest", str(manifest), "--format", "json"])
        == 1
    )
    assert (
        "must be a JSON object"
        in json.loads(capsys.readouterr().err)["error"]["message"]
    )
