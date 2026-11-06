"""``klt drc --list-rules`` threshold_um exposure (issue #5059)."""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import get_deck, get_nominal_dbu


def _run_drc_json(capsys, argv: list[str]) -> tuple[int, dict]:
    capsys.readouterr()
    code = main(["drc", *argv, "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu", "sg13g2", "sg13cmos5l"])
def test_list_rules_exposes_thresholds_in_um(capsys, deck_name):
    """Issue #5059: each rule carries threshold_um/threshold_max_um."""
    code, listing = _run_drc_json(capsys, ["--deck", deck_name, "--list-rules"])

    assert code == 0
    dbu = get_nominal_dbu(deck_name)
    assert listing["nominal_dbu_um"] == dbu
    by_id = {r["id"]: r for r in listing["rules"]}
    non_distance = {
        "area",
        "density",
        "antenna",
        "ongrid",
        "angle",
        "comp_coverage",
        "tap_distance",
    }
    for rule in get_deck(deck_name):
        entry = by_id[rule.id]
        assert "threshold_um" in entry and "threshold_max_um" in entry
        if rule.check in non_distance:
            assert entry["threshold_um"] is None
            assert entry["threshold_max_um"] is None
        else:
            assert entry["threshold_um"] == pytest.approx(rule.threshold_dbu * dbu)
            if rule.threshold_max_dbu is None:
                assert entry["threshold_max_um"] is None
            else:
                assert entry["threshold_max_um"] == pytest.approx(
                    rule.threshold_max_dbu * dbu
                )
