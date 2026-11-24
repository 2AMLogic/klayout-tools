"""Shared plain width and spacing lookup for ring and composition geometry."""

from dataclasses import replace

import pytest

from klayout_tools import decks
from klayout_tools.decks import plain_rules
from klayout_tools.decks.rules import DerivedLayer


@pytest.mark.parametrize("family", ["sky130", "gf180mcu", "sg13g2", "sg13cmos5l"])
def test_lookup_matches_every_layer_of_the_curated_deck(family):
    rules = decks.get_deck(family)
    unit = decks.get_nominal_dbu(family)
    for layer in {rule.layer for rule in rules}:
        for check in ("width", "space"):
            matching = (
                rule
                for rule in rules
                if rule.layer == layer
                and rule.check == check
                and rule.other_layer is None
                and rule.derived_layer is None
            )
            first_max = max(matching, key=lambda rule: rule.threshold_dbu, default=None)
            expected = (
                (first_max.threshold_dbu * unit, first_max.id) if first_max else None
            )
            assert plain_rules.largest_plain_rule_um(family, layer, check) == expected


def test_unknown_family_and_missing_layer():
    assert plain_rules.largest_plain_rule_um("unknown", (68, 20), "width") is None
    assert plain_rules.largest_plain_rule_um("sky130", None, "width") is None


def test_first_equal_threshold_wins_and_nonplain_rules_are_excluded(monkeypatch):
    rule = next(rule for rule in decks.get_deck("sky130") if rule.id == "met1.width.1")
    derived = DerivedLayer(base=rule.layer, sized_by_um=1)
    rules = [
        replace(rule, id="cross", other_layer=(69, 20), threshold_dbu=400),
        replace(rule, id="derived", derived_layer=derived, threshold_dbu=500),
        replace(rule, id="small", threshold_dbu=10),
        replace(rule, id="first", threshold_dbu=30),
        replace(rule, id="second", threshold_dbu=30),
    ]
    monkeypatch.setattr(decks, "get_deck", lambda _: rules)
    assert plain_rules.largest_plain_rule_um("sky130", rule.layer, "width") == (
        30 * decks.get_nominal_dbu("sky130"),
        "first",
    )
