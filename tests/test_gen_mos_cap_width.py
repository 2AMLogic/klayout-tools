"""Width-floor fallbacks for a family with a MOS-capacitor marker."""

from dataclasses import replace

import klayout.db as kdb
import pytest

from klayout_tools import decks
from klayout_tools.gen import GenError
from klayout_tools.gen_layer_params_checks import resolve_mos_cap_mark


def test_mos_cap_keeps_marker_when_deck_is_unavailable(monkeypatch):
    def unavailable_deck(family):
        raise decks.UnknownDeckError(family)

    monkeypatch.setattr(decks, "get_deck", unavailable_deck)

    marker = resolve_mos_cap_mark("gf180mcu", {"mos_cap": True, "w_um": 1.0}, None)

    assert (marker.layer, marker.datatype) == (166, 5)


@pytest.mark.parametrize(
    ("marker_threshold_dbu", "voltage_flavor_mark", "width", "rule_id"),
    [
        pytest.param(None, None, 0.21, r"comp\.width\.1", id="missing-marker-rule"),
        pytest.param(100, (55, 0), 0.29, r"comp\.width\.mv\.1", id="wider-oxide-rule"),
    ],
)
def test_mos_cap_uses_oxide_floor(
    monkeypatch, marker_threshold_dbu, voltage_flavor_mark, width, rule_id
):
    rules = decks.get_deck("gf180mcu")
    marker_rule = next(rule for rule in rules if rule.id == "comp.width.moscap.1")
    rules = [rule for rule in rules if rule.id != marker_rule.id]
    if marker_threshold_dbu is not None:
        rules.append(replace(marker_rule, threshold_dbu=marker_threshold_dbu))
    monkeypatch.setattr(decks, "get_deck", lambda _: rules)
    oxide_mark = kdb.LayerInfo(*voltage_flavor_mark) if voltage_flavor_mark else None

    with pytest.raises(GenError, match=rf"deck rule '{rule_id}'"):
        resolve_mos_cap_mark("gf180mcu", {"mos_cap": True, "w_um": width}, oxide_mark)
