"""Foundry-style valueless ``C`` cards (issue #4729)."""

from klayout_tools.netlist_normalize import convert_reference_netlist


def test_valueless_c_card_is_rewritten_like_the_x_call():
    text = "C_X1 n1 n2 cap_nmos_03v3 W=50u L=50u M=2\n"
    c_form = convert_reference_netlist(text, deck="gf180mcu").text
    x_form = convert_reference_netlist(
        "X_X1 n1 n2 cap_nmos_03v3 W=50u L=50u M=2\n", deck="gf180mcu"
    ).text
    assert c_form == x_form
    assert "C_X1_m0 n1 n2 0 cap_nmos_03v3" in c_form


def test_valued_c_card_is_left_unchanged():
    text = "C2 a b 1p\n"
    assert convert_reference_netlist(text, deck="gf180mcu").text == text


def test_c_card_with_unknown_model_is_left_unchanged():
    text = "C3 a b mystery_cap W=1u L=1u\n"
    assert convert_reference_netlist(text, deck="gf180mcu").text == text
