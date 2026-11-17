"""Issue #4795: diagnostics that explain a stale pin / unresolved --check input."""

from __future__ import annotations

import hashlib
import os

from klayout_tools._report_verify_hints import input_not_found_entry, stale_pin


def test_input_not_found_relative_path_names_anchor_and_working_dir(
    tmp_path, monkeypatch
):
    pair = tmp_path / "pair"
    pair.mkdir()
    report = pair / "report.json"
    monkeypatch.chdir(tmp_path)
    found = str(pair / "ref.spice")
    entry = input_not_found_entry("ref.spice", str(report), found)
    assert os.path.realpath(entry["resolved_against"]) == os.path.realpath(tmp_path)
    assert (
        os.path.realpath(pair) in os.path.realpath(entry["hint"])
        or str(pair) in (entry["hint"])
    )
    assert entry["found_relative_to_report"] == found
    assert entry["path"] == "ref.spice"


def test_input_not_found_absolute_path_has_no_anchor_or_hint(tmp_path):
    path = str(tmp_path / "gone.gds")
    entry = input_not_found_entry(path, str(tmp_path / "report.json"), None)
    assert entry["resolved_against"] is None
    assert entry["hint"] is None
    assert entry["resolved"] == path


def test_stale_pin_names_the_failed_comparison():
    detail = stale_pin(
        {"content_hash": "sha256:stale-revision"}, {"content_hash": "sha256:layoutA"}
    )
    assert detail["content_hash_pin"] == "stale"
    assert detail["hash_check"] == {
        "field": "provenance.input.content_hash",
        "expected": "sha256:stale-revision",
        "actual": "sha256:layoutA",
        "match": False,
    }


def test_stale_pin_without_input_provenance_gets_the_generic_hint():
    """Issue #5837: even with no path/paths/samples to name, the detail
    explains what ``content_hash`` must be -- never a bare
    ``{content_hash_pin, hash_check}`` with no remedy."""
    detail = stale_pin({"content_hash": "sha256:a"}, {"content_hash": "sha256:b"})
    assert detail["content_hash_pin"] == "stale"
    assert detail["hash_check"]["match"] is False
    assert "compared_against" not in detail
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"] == (
        "content_hash must be the envelope's provenance.input.content_hash "
        "(the recorded input hash), not the hash of the evidence envelope file"
    )


def test_stale_pin_of_envelope_own_hash_is_flagged_without_a_path(tmp_path):
    """Issue #5837: the envelope-own-hash check runs for a path-less
    ``provenance.input`` too -- the pin that names the record, not what the
    record is about, is the mistake the flag exists for."""
    envelope = tmp_path / "drc.json"
    envelope.write_text("{}")
    pin = "sha256:" + hashlib.sha256(envelope.read_bytes()).hexdigest()
    detail = stale_pin(
        {"content_hash": pin},
        {
            "content_hash": "sha256:layoutA",
            "envelope": {"provenance": {"input": {"content_hash": "sha256:layoutA"}}},
            "file": str(envelope),
        },
    )
    assert detail["pin_is_envelope_file_hash"] is True
    assert str(envelope) in detail["hint"]
    assert "itself" in detail["hint"]
    assert "compared_against" not in detail


def test_stale_pin_of_unreadable_evidence_file_no_flag_no_crash(tmp_path):
    detail = stale_pin(
        {"content_hash": "sha256:a"},
        {
            "content_hash": "sha256:b",
            "envelope": {"provenance": {"input": {"content_hash": "sha256:b"}}},
            "file": str(tmp_path / "missing.json"),
        },
    )
    assert "pin_is_envelope_file_hash" not in detail
    assert detail["hint"].startswith("content_hash must be")
