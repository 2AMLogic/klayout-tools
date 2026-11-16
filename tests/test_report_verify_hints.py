"""Issue #4795: diagnostics that explain a stale pin / unresolved --check input."""

from __future__ import annotations

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


def test_stale_pin_without_input_provenance_is_unchanged():
    detail = stale_pin({"content_hash": "sha256:a"}, {"content_hash": "sha256:b"})
    assert set(detail) == {"content_hash_pin", "hash_check"}
