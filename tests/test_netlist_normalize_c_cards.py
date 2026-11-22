"""Foundry-style valueless ``C`` cards (issue #4729), and the gf180mcu MiM
capacitor's PDK ``fF`` spellings (issue #7031)."""

import pytest

from klayout_tools.netlist_normalize import NormalizeError, convert_reference_netlist


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


#: The PDK's own MIM capacitor densities, in both reference-netlist
#: spellings: foundry CDLs write ``fF`` (mixed case), the ngspice models
#: define the case-folded ``ff`` ``.subckt`` names.
_MIM_DENSITIES = ("1f0", "1f5", "2f0")
_MIM_SPELLINGS = ("fF", "ff")
_MIM_GEOMETRY = "c_length=20u c_width=20u"


@pytest.mark.parametrize("density", _MIM_DENSITIES)
@pytest.mark.parametrize("spelling", _MIM_SPELLINGS)
@pytest.mark.parametrize("card_letter", ("C", "X"))
def test_mim_ff_spelling_converts_like_its_curated_twin(density, spelling, card_letter):
    # Issue #7031: a card naming the PDK's current spelling converts to the
    # exact same plain-element card as the curated m4m5 spelling.
    alias = f"cap_mim_{density}{spelling}"
    curated = f"cap_mim_{density}_m4m5_noshield"
    converted = convert_reference_netlist(
        f"{card_letter}1 a b {alias} {_MIM_GEOMETRY} m=1\n", deck="gf180mcu"
    ).text
    twin = convert_reference_netlist(
        f"{card_letter}1 a b {curated} {_MIM_GEOMETRY} m=1\n", deck="gf180mcu"
    ).text
    assert converted == twin
    assert f"cap_mim_{density}_m4m5_noshield" in converted


@pytest.mark.parametrize("density", _MIM_DENSITIES)
def test_mim_ff_spelling_lands_on_the_same_density_class(density):
    converted = convert_reference_netlist(
        f"C1 a b cap_mim_{density}fF {_MIM_GEOMETRY} m=1\n", deck="gf180mcu"
    ).text
    assert f"cap_mim_{density}_m4m5_noshield" in converted
    for other in _MIM_DENSITIES:
        if other != density:
            assert f"cap_mim_{other}" not in converted


def test_mim_ff_spelling_carries_geometry_and_expands_m():
    text = "C1 a b cap_mim_2f0fF c_length=20u c_width=20u m=2\n"
    converted = convert_reference_netlist(text, deck="gf180mcu").text
    assert (
        converted
        == convert_reference_netlist(
            "C1 a b cap_mim_2f0_m4m5_noshield c_length=20u c_width=20u m=2\n",
            deck="gf180mcu",
        ).text
    )
    lines = converted.strip().splitlines()
    assert len(lines) == 2  # m=2 expands to two parallel cards
    assert all("A=400P" in line and "P=80U" in line for line in lines)


def test_mim_ff_binding_is_independent_of_the_mim_cap_option():
    # The reference-side binding is by name, so selecting a different layout
    # flavour (issue #1151) must not change how a `2f0fF` card converts.
    text = "C1 a b cap_mim_2f0fF c_length=20u c_width=20u m=1\n"
    with_option = convert_reference_netlist(
        text,
        deck="gf180mcu",
        deck_options={"mim_cap": "cap_mim_1f0_m4m5_noshield"},
    ).text
    assert with_option == convert_reference_netlist(text, deck="gf180mcu").text


def test_mim_aliases_never_reach_the_extraction_write_direction():
    from klayout_tools.decks import get_extraction_deck
    from klayout_tools.pdk_models import resolve_device_bindings

    deck = get_extraction_deck("gf180mcu")
    bindings = resolve_device_bindings("gf180mcu", "gf180mcuA", deck)
    capacitors = {n: b for n, b in bindings.items() if b.kind == "capacitor"}
    assert "cap_mim_2f0_m4m5_noshield" in capacitors
    for name, binding in capacitors.items():
        assert binding.subckt == name  # curated identity, never an fF alias
        assert not name.endswith(("fF", "ff"))


def test_unknown_mim_density_still_errors_and_lists_the_aliases():
    with pytest.raises(NormalizeError) as excinfo:
        convert_reference_netlist(
            "X1 a b cap_mim_3f0fF c_length=20u c_width=20u\n", deck="gf180mcu"
        )
    message = str(excinfo.value)
    assert "not a known device" in message
    assert "cap_mim_2f0fF" in message
