"""sky130 front-end spacing rules ``difftap.3`` / ``poly.2`` (issue #4282)."""

from __future__ import annotations

import klayout.db as db
import pytest

from klayout_tools.decks import sky130 as deck_mod
from klayout_tools.drc import run_drc

DIFF = (65, 20)
TAP = (65, 44)
POLY = (66, 20)


def _layout(layer, boxes):
    ly = db.Layout()
    ly.dbu = 0.001
    cell = ly.create_cell("t")
    for b in boxes:
        cell.shapes(ly.layer(*layer)).insert(db.Box(*b))
    return ly


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(__name__ + "._DIR", tmp_path, raising=False)


_DIR = None
_N = [0]


def _ids(ly):
    _N[0] += 1
    path = _DIR / f"t{_N[0]}.gds"
    ly.write(str(path))
    return set(run_drc(str(path), "sky130")["rule_counts"])


def test_diff_spacing_violation_and_minimum():
    assert "diff.space.1" in _ids(
        _layout(DIFF, [(0, 0, 500, 500), (700, 0, 1200, 500)])
    )
    assert "diff.space.1" not in _ids(
        _layout(DIFF, [(0, 0, 500, 500), (770, 0, 1270, 500)])
    )


def test_tap_spacing_violation_and_minimum():
    assert "tap.space.1" in _ids(_layout(TAP, [(0, 0, 500, 500), (700, 0, 1200, 500)]))
    assert "tap.space.1" not in _ids(
        _layout(TAP, [(0, 0, 500, 500), (770, 0, 1270, 500)])
    )


def test_poly_spacing_violation_and_minimum():
    assert "poly.space.1" in _ids(
        _layout(POLY, [(0, 0, 150, 1000), (300, 0, 450, 1000)])
    )
    assert "poly.space.1" not in _ids(
        _layout(POLY, [(0, 0, 150, 1000), (360, 0, 510, 1000)])
    )


def test_abutting_diff_is_merged_not_flagged():
    assert "diff.space.1" not in _ids(
        _layout(DIFF, [(0, 0, 500, 500), (500, 0, 1000, 500)])
    )


def test_unmodeled_list_sorted_and_discloses_omitted_spacing():
    rules = list(deck_mod.UNMODELED_RULES)
    assert rules == sorted(rules)
    assert len(rules) == len(set(rules))
    for rid in ("poly.3", "li.7", "dnwell.2", "licon.13", "capm.2b"):
        assert rid in rules
    assert "difftap.3" not in rules and "poly.2" not in rules
