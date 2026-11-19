"""Cited item-8 paths must be recognised envelopes (#5688).

A cited path that is prose, malformed JSON, or JSON without `schema_version`
fails the attestation even when its `envelope_checks` command exits `0`.
Split from `tests/test_signoff_attest_item8.py`, which sits at the per-file
size ratchet's 500-line limit (#2766); the inventory fixtures are imported
from it, exactly as `tests/test_signoff_attest_item8_gzip.py` does.
"""

from __future__ import annotations

import json

import pytest

from test_signoff_attest_item8 import _PASS, _analog_inventory, _attest, _repo


def test_cited_prose_file_fails_even_with_a_true_command(tmp_path):
    repo = _repo(tmp_path)
    (repo / "evidence" / "NOTES.txt").write_text("looks fine to me\n")
    inventory = _analog_inventory(
        spec_rows=["gain"],
        citations={"gain": ["evidence/NOTES.txt"]},
        envelope_checks={"evidence/NOTES.txt": _PASS},
    )
    envelope = _attest(repo, inventory)
    assert envelope["status"] == "fail"
    checks = envelope["attestation"]["checks"]
    env = [c for c in checks if c["check"] == "envelope"]
    assert env == [
        {
            "check": "envelope",
            "role": "evidence",
            "path": "evidence/NOTES.txt",
            "kind": None,
            "passed": False,
        }
    ]
    assert [c["passed"] for c in checks if c["check"] == "citation"] == [False]


def test_real_generic_envelope_passes_and_records_kind(tmp_path):
    envelope = _attest(_repo(tmp_path), _analog_inventory())
    assert envelope["status"] == "pass"
    kinds = {
        c["path"]: c["kind"]
        for c in envelope["attestation"]["checks"]
        if c["check"] == "envelope"
    }
    assert kinds == {
        "evidence/sim_tt.json": "generic",
        "evidence/sim_ss.json": "generic",
    }


@pytest.mark.parametrize(
    "text", ["{not json", "[1, 2]", json.dumps({"status": "pass"})]
)
def test_malformed_or_schemaless_citation_fails(tmp_path, text):
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_tt.json").write_text(text)
    envelope = _attest(repo, _analog_inventory())
    assert envelope["status"] == "fail"
    assert ("envelope", "evidence") in [
        (c["check"], c["role"])
        for c in envelope["attestation"]["checks"]
        if not c["passed"]
    ]


def test_envelope_check_text_line():
    from klayout_tools.signoff_attest_text import attestation_check_line

    base = {"check": "envelope", "role": "evidence", "path": "a.json"}
    assert (
        attestation_check_line({**base, "kind": "generic", "passed": True})
        == "[PASS] evidence a.json envelope (generic)"
    )
    assert attestation_check_line({**base, "kind": None, "passed": False}) == (
        "[FAIL] evidence a.json envelope: not a recognised envelope"
    )
