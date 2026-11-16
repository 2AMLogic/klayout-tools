"""``--attest 1 --check`` names a pre-v0.9.0 envelope ``schema-mismatch`` (#5730).

The legacy envelope is minted by rewriting a current one's
``provenance.input`` to the singular shape, so no old ``klt`` is needed."""

from __future__ import annotations

import json

from klayout_tools.signoff_attest import build_attestation
from klayout_tools.signoff_attest_input import legacy_binding_detail
from test_signoff import _attest_cli, _attest_repo, _write_inventory


def _legacy(tmp_path, klt_version="0.8.0"):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    envelope = build_attestation(1, inventory)
    binding = envelope["provenance"]["input"]
    inv_rel = "attest/item1.json"
    envelope["provenance"]["input"] = {
        "path": {"path": inv_rel, "scope": "repo"},
        "content_hash": "sha256:" + "0" * 64,
    }
    assert "paths" in binding
    if klt_version is None:
        envelope["provenance"].pop("klt_version", None)
    else:
        envelope["provenance"]["klt_version"] = klt_version
    committed = repo / "evidence" / "legacy.json"
    committed.write_text(json.dumps(envelope, indent=2) + "\n")
    return repo, inventory, committed


def _check(inventory, committed, capsys, *extra):
    code = _attest_cli(
        1, inventory, "--check", str(committed), "--format", "json", *extra
    )
    return code, json.loads(capsys.readouterr().out)


def test_legacy_shape_is_schema_mismatch_and_still_fails(tmp_path, capsys):
    _repo, inventory, committed = _legacy(tmp_path)
    code, out = _check(inventory, committed, capsys)
    assert code == 3
    assert out["status"] == "schema-mismatch"
    assert out["legacy_binding"] is True
    detail = out["legacy_binding_detail"]
    assert detail["attested_klt_version"] == "0.8.0"
    assert detail["checking_klt_version"]
    assert "v0.9.0" in detail["boundary"] and "#4764" in detail["boundary"]
    assert "regenerate" in detail["remedy"] and "pin" in detail["remedy"]
    assert out["fresh_status"] == "pass"


def test_text_prints_remedy_before_drift_block(tmp_path, capsys):
    _repo, inventory, committed = _legacy(tmp_path)
    assert _attest_cli(1, inventory, "--check", str(committed)) == 3
    text = capsys.readouterr().out
    assert "status: schema-mismatch" in text
    assert text.index("legacy_binding:") < text.index("drift:")


def test_missing_attesting_version_still_renders(tmp_path, capsys):
    _repo, inventory, committed = _legacy(tmp_path, klt_version=None)
    code, out = _check(inventory, committed, capsys)
    assert code == 3
    assert out["legacy_binding_detail"]["attested_klt_version"] is None
    assert "unknown" in out["legacy_binding_detail"]["remedy"]


def test_cooccurring_real_drift_stays_drifted(tmp_path, capsys):
    repo, inventory, committed = _legacy(tmp_path)
    envelope = json.loads(committed.read_text())
    envelope["attestation"]["checks"][0]["passed"] = not envelope["attestation"][
        "checks"
    ][0]["passed"]
    committed.write_text(json.dumps(envelope, indent=2) + "\n")
    code, out = _check(inventory, committed, capsys)
    assert code == 3
    assert out["status"] == "drifted"
    assert "legacy_binding" not in out


def test_current_shape_unchanged_matches(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)
    output = repo / "evidence" / "item1.json"
    assert _attest_cli(1, inventory, "-o", str(output)) == 0
    capsys.readouterr()
    code, out = _check(inventory, output, capsys)
    assert code == 0
    assert out["status"] == "match" and out["fresh_status"] == "pass"
    assert "legacy_binding" not in out


def test_item2_path_to_paths_is_never_legacy():
    drift = [{"field": "provenance.input.path"}, {"field": "provenance.input.paths"}]
    committed = {
        "t1_item": 2,
        "provenance": {"input": {"path": {}, "content_hash": "x"}},
    }
    assert legacy_binding_detail(committed, {}, drift) is None


def test_no_path_nor_paths_or_new_shape_does_not_fire():
    drift = [{"field": "provenance.input.content_hash"}]
    for binding in ({"content_hash": "x"}, {"paths": [], "content_hash": "x"}):
        committed = {"t1_item": 1, "provenance": {"input": binding}}
        assert legacy_binding_detail(committed, {}, drift) is None
