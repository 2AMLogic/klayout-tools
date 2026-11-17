"""Issue #5571: ``klt signoff --manifest`` resolves the item-2 stream
producer's nested ``attestation.inventory.schema_version`` into the tier
record's ``inventory_schema_version`` disclosure -- the flat key still wins
when an envelope carries both shapes, and a malformed nested block yields
``null`` without failing the row. Split from ``test_signoff.py`` /
``test_signoff_attest_item2.py`` (size ratchet); reuses their fixtures."""

from __future__ import annotations

import pytest

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import build_attestation
from klayout_tools.signoff_attest_stream import build_stream_attestation
from test_signoff import _attest_repo, _item, _manifest, _write, _write_inventory
from test_signoff_attest_item2 import _PASS, _bind, _inv, _stream


def test_manifest_resolves_a_nested_only_inventory_block(tmp_path):
    """Issue #5571: an envelope in the item-2 stream producer's nested-only
    shape (`attestation.inventory.schema_version`, no flat key) binds with
    the real version -- already-committed evidence needs no re-attest."""
    repo = _attest_repo(tmp_path)
    envelope = build_attestation(9, _write_inventory(repo, 9))
    attestation = envelope["attestation"]
    attestation["inventory"] = {
        "path": envelope["provenance"]["input"]["path"],
        "content_hash": envelope["provenance"]["input"]["content_hash"],
        "schema_version": 1,
    }
    del attestation["inventory_schema_version"]

    evidence = _write(repo / "evidence", "item9.json", envelope)
    result = build_tier_report(
        _manifest(
            evidence={
                "9": {
                    "file": evidence,
                    "content_hash": envelope["provenance"]["input"]["content_hash"],
                }
            }
        )
    )

    item = _item(result, 9)
    assert item["status"] == "met"
    record = item["citation"]["artifact_binding"]["attestation"]
    assert record["inventory_schema_version"] == 1


def test_manifest_prefers_the_flat_inventory_schema_version_key(tmp_path):
    """Issue #5571: when an envelope carries both shapes, the flat
    `inventory_schema_version` key wins (items 1/8/9/10 are unchanged)."""
    repo = _attest_repo(tmp_path)
    envelope = build_attestation(9, _write_inventory(repo, 9))
    attestation = envelope["attestation"]
    attestation["inventory"] = {
        "path": envelope["provenance"]["input"]["path"],
        "content_hash": envelope["provenance"]["input"]["content_hash"],
        "schema_version": 1,
    }
    attestation["inventory_schema_version"] = 7

    evidence = _write(repo / "evidence", "item9.json", envelope)
    result = build_tier_report(
        _manifest(
            evidence={
                "9": {
                    "file": evidence,
                    "content_hash": envelope["provenance"]["input"]["content_hash"],
                }
            }
        )
    )

    item = _item(result, 9)
    assert item["status"] == "met"
    record = item["citation"]["artifact_binding"]["attestation"]
    assert record["inventory_schema_version"] == 7


def test_item2_with_declared_checks_binds_the_nested_version(tmp_path):
    """Issue #5571: an item-2 envelope from `--attest 2 --inventory` binds
    with the inventory's real schema version, and the row stays `met`."""
    repo, stream = _stream(tmp_path)
    inv = _inv(repo, [_PASS])
    env = build_stream_attestation(stream, inventory_path=inv)
    assert env["status"] == "pass"
    assert env["attestation"]["inventory"]["path"]["path"] == "evidence/item2-inv.json"

    item = _bind(repo, env)
    assert item["status"] == "met"
    assert (
        item["citation"]["artifact_binding"]["attestation"]["inventory_schema_version"]
        == 1
    )


def test_item2_without_inventory_binds_null(tmp_path):
    """Issue #5571: no declared-checks inventory still discloses `null`."""
    repo, stream = _stream(tmp_path)
    env = build_stream_attestation(stream)
    assert "inventory" not in env["attestation"]

    item = _bind(repo, env)
    assert item["status"] == "met"
    assert (
        item["citation"]["artifact_binding"]["attestation"]["inventory_schema_version"]
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-mapping",
        [1, 2],
        {"path": {"path": "evidence/x.json", "scope": "repo"}},
    ],
)
def test_malformed_nested_inventory_block_yields_null(tmp_path, bad):
    """Issue #5571: a malformed `attestation.inventory` cannot raise and is
    never graded -- the disclosure resolves to `null`."""
    repo, stream = _stream(tmp_path)
    env = build_stream_attestation(stream)
    env["attestation"]["inventory"] = bad
    item = _bind(repo, env)
    assert item["status"] == "met"
    assert (
        item["citation"]["artifact_binding"]["attestation"]["inventory_schema_version"]
        is None
    )
