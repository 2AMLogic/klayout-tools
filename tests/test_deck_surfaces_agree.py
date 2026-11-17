"""The deck-listing surfaces agree about which decks `klt drc` can run
(issue #3783)."""

from __future__ import annotations

import re

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import (
    UnknownDeckError,
    deck_provides,
    drc_deck_names,
    get_deck,
)
from klayout_tools.decks import sg13g2_cont_grid as sg13g2_cont_grid_module
from klayout_tools.decks.history import deck_info


def _help(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit):
        main([*argv, "--help"])
    return re.sub(r"\s+", " ", capsys.readouterr().out)


def test_drc_help_lists_only_curated_decks(capsys: pytest.CaptureFixture[str]) -> None:
    text = _help(capsys, "drc")
    listed = re.search(r"currently: ([\w, ]+?)\)", text)
    assert listed is not None
    names = [n.strip() for n in listed.group(1).split(",")]
    assert names == drc_deck_names()
    for name in names:
        assert get_deck(name)
    assert "asap7" not in names
    assert "--engine klayout --deck-file" in text


def test_layout_metrics_help_excludes_asap7(capsys: pytest.CaptureFixture[str]) -> None:
    text = _help(capsys, "layout-metrics")
    drc_part = text.split("drc.violation_count field (currently:")[1].split(")")[0]
    assert "asap7" not in drc_part


def test_deck_info_provides() -> None:
    by_name = {d["deck"]: d for d in deck_info()["decks"]}
    assert by_name["asap7"]["provides"] == ["extraction", "klayout_drc"]
    assert by_name["sky130"]["provides"] == ["curated_drc", "extraction"]
    for name, entry in by_name.items():
        assert entry["provides"] == deck_provides(name)
        if "curated_drc" in entry["provides"]:
            assert get_deck(name)


def test_asap7_rejected_with_route_hint() -> None:
    with pytest.raises(UnknownDeckError, match=r"--engine klayout --deck-file"):
        get_deck("asap7")


def test_nonexistent_deck_keeps_unknown_message() -> None:
    with pytest.raises(UnknownDeckError, match=r"unknown deck 'nope'"):
        get_deck("nope")


def test_deck_rules_asap7_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["deck", "rules", "--deck", "asap7", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert "--engine klayout --deck-file" in captured.out + captured.err


#: deck -> the exact `DrcRule.scope` token set its rules declare (issue
#: #5723). Frozen on purpose: a new or changed scope token must land as a
#: reviewed diff here instead of silently widening `coverage.deck_scope`.
#: A scope token names an upstream DRM section or rule-id family, never a
#: layer -- sky130's `"x"` is its genuine `x.*` offgrid family (sky130.lydrc
#: OFFGRID block), gf180mcu's `"OFFGRID"` its `<layer>_OFFGRID` ids, and
#: sg13g2's `"Offgrid"` its `cont_drw_Offgrid` id (an earlier `"x"` there was
#: a copy-paste from sky130, naming nothing in sg13g2).
_EXPECTED_DECK_SCOPES: dict[str, frozenset[str]] = {
    "gf180mcu": frozenset(
        {
            "10.4.2 MIM Option B",
            "10.7 DRC_BJT Mark Layer",
            "7.12 Contact",
            "7.13 Metaln",
            "7.14 Vian",
            "7.15 MetalTop",
            "7.4 Nwell",
            "7.5 Comp",
            "7.7 Poly2",
            "7.8 Nplus",
            "7.9 Pplus",
            "9.1 Bond Pad",
            "EF",
            "ESD",
            "GR",
            "MDN",
            "MDP",
            "OFFGRID",
            "SB",
        }
    ),
    "sg13cmos5l": frozenset(
        {
            "5.1 NWell",
            "5.16 Metal1",
            "5.17 Metaln",
            "5.19 Via1",
            "5.20 Vian",
            "5.21 TopVia1",
            "5.22 TopMetal1",
            "5.5 Activ",
            "5.8 GatPoly",
            "Via1 upper-landing containment (curated)",
            "Via2 upper-landing containment (curated)",
            "Via3 upper-landing containment (curated)",
        }
    ),
    "sg13g2": frozenset(
        {
            "Act",
            "Cnt",
            "Gat",
            "M1",
            "M2",
            "M3",
            "M4",
            "M5",
            "MIM",
            "NW",
            "Offgrid",
            "TM1",
            "TM2",
            "TV1",
            "TV2",
            "V1",
            "V2",
            "V3",
            "V4",
            "pSD",
        }
    ),
    "sky130": frozenset(
        {
            "cap2m",
            "capm",
            "ct",
            "difftap",
            "li",
            "licon",
            "m1",
            "m2",
            "m3",
            "m4",
            "m5",
            "npc",
            "nsdm",
            "nwell",
            "poly",
            "psdm",
            "rpm",
            "urpm",
            "via",
            "via2",
            "via3",
            "via4",
            "x",
        }
    ),
}


def test_deck_scope_tokens_match_frozen_expected_sets() -> None:
    """Every curated deck's scope-token set equals its checked-in expected
    set (#5723): a token that appears, disappears or changes fails here
    rather than reaching a published `coverage.deck_scope`."""
    assert set(drc_deck_names()) == set(_EXPECTED_DECK_SCOPES)
    for name in drc_deck_names():
        declared = frozenset(rule.scope for rule in get_deck(name) if rule.scope)
        assert declared == _EXPECTED_DECK_SCOPES[name], (
            f"{name}: declared {sorted(declared)}, "
            f"expected {sorted(_EXPECTED_DECK_SCOPES[name])}"
        )


def test_sg13g2_cont_ongrid_scope_is_offgrid_family() -> None:
    """`cont.ongrid.1`'s `scope` names sg13g2's upstream offgrid rule family
    (`cont_drw_Offgrid`, `rule_decks/geometry/3_1_offgrid.drc`) -- not the
    `"x"` copy-pasted from sky130's own legitimate `x.*` family (#5723), and
    not `"Cnt"`, which would claim the `Cnt.a`-`Cnt.d` section from
    `feol/5_14_cont.drc` that this deck does not implement. A scope token
    names an upstream DRM section or rule-id family, never a layer."""
    assert sg13g2_cont_grid_module.CONT_ONGRID_RULE.scope == "Offgrid"
    scopes = {rule.scope for rule in get_deck("sg13g2") if rule.scope}
    assert "x" not in scopes
    assert "Offgrid" in scopes
