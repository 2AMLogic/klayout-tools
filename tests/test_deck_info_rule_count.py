"""Tests for `deck info`'s ``enforced_rule_count`` field and the
curated-deck content-hash label (issue #5843): the library field
(``decks/history.py`` via ``decks/enforced_rules.py``) and its text
rendering (``cli/deck_cmd.py``).

Split out of ``test_deck_history.py`` to hold that module at its
file-size ratchet ceiling. The fixture points ``history._HISTORY_PATH``
at a synthetic table under ``tmp_path`` (the same pattern as
``test_deck_history.py``) so these tests never depend on the checked-in
``_history.json``.
"""

from __future__ import annotations

import json
import re

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import history


@pytest.fixture()
def _history_table(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text(json.dumps({"entries": []}))
    monkeypatch.setattr(history, "_HISTORY_PATH", path)
    return path


def test_deck_info_reports_enforced_rule_count_for_curated_deck(_history_table):
    entry = history.deck_info(name="gf180mcu")["decks"][0]

    # The same count `klt deck rules` reports for the same deck (#5843) --
    # derived here, not hardcoded, so the two surfaces cannot drift apart
    # silently on a PDK bump.
    assert isinstance(entry["enforced_rule_count"], int)
    assert entry["enforced_rule_count"] > 0
    assert entry["enforced_rule_count"] == len(history.deck_rules("gf180mcu")["rules"])


def test_deck_info_enforced_rule_count_null_without_curated_table(_history_table):
    entry = history.deck_info(name="asap7")["decks"][0]

    # Present but null -- never omitted, never 0 (#5843): asap7 has no
    # curated rule table at all, which is a different claim from a rule
    # table that enforces nothing.
    assert "enforced_rule_count" in entry
    assert entry["enforced_rule_count"] is None


def test_cli_info_text_curated_deck_labels_hash_and_enforced_count(capsys):
    exit_code = main(["deck", "info", "--deck", "sky130"])

    assert exit_code == 0
    out = capsys.readouterr().out
    # For a curated deck the module hash covers both roles (#5843) -- the
    # bare "extraction deck" label sent provenance authors hunting for a
    # DRC hash that is this identical value.
    assert "deck content_hash (extraction + curated DRC rule table):" in out
    assert "extraction deck content_hash" not in out
    # The enforced count reads as a pair with the NOT ENFORCED count,
    # directly above it.
    enforced = re.search(r"  enforced_rules: (\d+)", out)
    assert enforced is not None
    assert int(enforced.group(1)) == len(history.deck_rules("sky130")["rules"])
    assert "NOT ENFORCED by this deck:" in out
    assert out.index("enforced_rules:") < out.index("NOT ENFORCED by this deck:")


def test_cli_info_text_asap7_keeps_two_artifact_rendering(capsys):
    exit_code = main(["deck", "info", "--deck", "asap7"])

    assert exit_code == 0
    out = capsys.readouterr().out
    # No curated table -> no enforced count; the two real artifacts still
    # print on their own labelled lines (#5843).
    assert "enforced_rules:" not in out
    assert "extraction deck content_hash:" in out
    assert "drc deck content_hash:" in out
