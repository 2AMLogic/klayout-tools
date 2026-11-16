"""`klt drc --check` deck resolution (issue #5709): packaged-deck fallback,
the `--deck-file` override, and the `deck_not_found` diagnostic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import klayout_tools.drc_report_check as rc
from klayout_tools._provenance import content_hash
from klayout_tools.cli import main
from klayout_tools.cli.output import render_check_text
from klayout_tools.decks import asap7_rules
from klayout_tools.drc import DrcError, check_drc_report, rerun_drc_report

DECK = str(asap7_rules.DECK_PATH)
GONE = "/nonexistent/python3.13/site-packages/klayout_tools/decks/asap7.drc"


def _report(tmp_path: Path, *, deck=GONE, deck_hash=None, name="asap7.drc") -> str:
    layout = tmp_path / "top.gds"
    layout.write_bytes(b"gds-bytes")
    doc = {
        "engine": "klayout",
        "file": str(layout),
        "deck": deck,
        "provenance": {
            "input": {"content_hash": content_hash(str(layout))},
            "deck": {
                "name": name,
                "content_hash": deck_hash or content_hash(DECK),
            },
        },
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _deck_check(result):
    return next(c for c in result["checks"] if c["field"].endswith("deck.content_hash"))


def test_packaged_fallback_matches(tmp_path):
    result = check_drc_report(_report(tmp_path))
    assert result["status"] == "match"
    assert "deck_not_found" not in _deck_check(result)


def test_packaged_fallback_never_masks_a_changed_deck(tmp_path):
    result = check_drc_report(_report(tmp_path, deck_hash="sha256:" + "0" * 64))
    check = _deck_check(result)
    assert result["status"] == "drifted"
    assert check["actual"] == content_hash(DECK)
    assert "deck_not_found" not in check


def test_recorded_path_that_exists_is_used_as_is(tmp_path):
    other = tmp_path / "mine.drc"
    other.write_text("# deck")
    report = _report(tmp_path, deck=str(other), deck_hash=content_hash(str(other)))
    assert check_drc_report(report)["status"] == "match"


def test_unresolvable_deck_is_a_failed_check_with_diagnostic(tmp_path):
    result = check_drc_report(_report(tmp_path, name="vendor_runset.drc"))
    check = _deck_check(result)
    assert (result["status"], check["match"], check["actual"]) == (
        "drifted",
        False,
        None,
    )
    assert check["deck_not_found"]["name"] == "vendor_runset.drc"
    assert check["deck_not_found"]["resolved"] == GONE
    assert "skipped" not in result


@pytest.mark.parametrize("name", ["../asap7.drc", "a/asap7.drc", "asap7.py", "", None])
def test_packaged_lookup_rejects_non_basenames(name):
    assert rc._packaged_deck_path(name) is None


def test_deck_file_override_wins_and_is_not_a_noop(tmp_path):
    report = _report(tmp_path, name="vendor_runset.drc")
    ok = check_drc_report(report, deck_path=DECK)
    assert ok["status"] == "match"
    bad = tmp_path / "other.drc"
    bad.write_text("# different")
    result = check_drc_report(report, deck_path=str(bad))
    assert result["status"] == "drifted"
    assert _deck_check(result)["actual"] == content_hash(str(bad))


def test_override_beats_existing_recorded_path(tmp_path):
    recorded = tmp_path / "rec.drc"
    recorded.write_text("# recorded")
    report = _report(
        tmp_path, deck=str(recorded), deck_hash=content_hash(str(recorded))
    )
    assert check_drc_report(report, deck_path=DECK)["status"] == "drifted"


def test_cli_deck_file_and_text_rendering(tmp_path, capsys):
    report = _report(tmp_path, name="vendor_runset.drc")
    assert (
        main(["drc", "--check", report, "--deck-file", DECK, "--format", "json"]) == 0
    )
    capsys.readouterr()
    assert main(["drc", "--check", report, "--format", "text"]) == 3
    out = capsys.readouterr().out
    assert "deck not found" in out and "--deck-file" in out


def test_render_check_text_lists_lookup(tmp_path, capsys):
    render_check_text(check_drc_report(_report(tmp_path, name="v.drc")))
    assert "packaged name 'v.drc'" in capsys.readouterr().out


def test_rerun_without_resolvable_deck_is_a_clean_error(tmp_path):
    with pytest.raises(DrcError, match="deck not found"):
        rerun_drc_report(_report(tmp_path, name="vendor_runset.drc"))


def test_rerun_deck_file_rejected_for_curated(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"file": "x.gds", "deck": "sky130"}))
    with pytest.raises(DrcError, match="--deck-file"):
        rerun_drc_report(str(path), input_path="x.gds", deck_path=DECK)


def test_crosscheck_native_side_resolves_packaged_deck(tmp_path):
    inner = json.loads(Path(_report(tmp_path)).read_text())
    path = tmp_path / "cc.json"
    path.write_text(json.dumps({"mode": "crosscheck", "reports": {"native": inner}}))
    result = check_drc_report(str(path))
    assert result["status"] == "match"
    assert _deck_check(result)["field"] == "reports.native.provenance.deck.content_hash"
