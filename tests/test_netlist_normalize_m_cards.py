"""Plain ``M`` cards naming a PDK simulation model (issue #3882): under a
``deck`` the model token is rewritten to the deck's device class."""

from __future__ import annotations

import pytest

from klayout_tools.netlist_normalize import normalize_reference_netlist


def test_sg13g2_m_cards_map_to_deck_classes():
    text = (
        "MX1 y a vss vss sg13_lv_nmos w=0.5u l=0.13u\n"
        "MX2 y a vdd vdd SG13_LV_PMOS w=1u l=0.13u\n"
    )
    out = normalize_reference_netlist(text, deck="sg13g2")
    assert "MX1 y a vss vss nfet w=0.5u l=0.13u" in out
    assert "MX2 y a vdd vdd pfet w=1u l=0.13u" in out


@pytest.mark.parametrize(
    ("deck", "model", "cls"),
    [
        ("sky130", "sky130_fd_pr__nfet_01v8", "nfet"),
        ("gf180mcu", "pfet_03v3", "pfet"),
    ],
)
def test_other_decks_map(deck, model, cls):
    out = normalize_reference_netlist(f"M1 d g s b {model} w=1u l=1u\n", deck=deck)
    assert f"M1 d g s b {cls} w=1u l=1u" in out


def test_unknown_model_passes_through():
    text = "M1 d g s b some_other_model w=1u l=1u\nM2 d g s b nfet w=1u l=1u\n"
    assert normalize_reference_netlist(text, deck="sg13g2") == text


def test_no_deck_leaves_m_cards_untouched():
    text = "M1 d g s b sg13_lv_nmos w=1u l=1u\n"
    assert normalize_reference_netlist(text) == text


def test_x_card_output_unchanged_alongside_m_cards():
    x = "XM1 d g s b nfet_03v3 L=0.5u W=1u\n"
    only_x = normalize_reference_netlist(x, deck="gf180mcu")
    mixed = normalize_reference_netlist(
        x + "M2 d g s b nfet_03v3 w=1u l=1u\n", deck="gf180mcu"
    )
    assert mixed.startswith(only_x)
