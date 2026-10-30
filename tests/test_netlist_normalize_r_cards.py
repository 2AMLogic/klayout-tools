"""Foundry-style valueless ``R`` cards (issue #4670)."""

from __future__ import annotations

from klayout_tools.netlist_normalize import convert_reference_netlist

CARD = "R_RCG NC GND_VCO GND_VCO ppolyf_u_3k W=1.0u L=5.6u"


def test_valueless_r_card_converted():
    result = convert_reference_netlist(CARD + "\n", deck="gf180mcu")
    out = result.text.strip()
    tokens = out.split()
    assert tokens[:4] == ["R_RCG", "NC", "GND_VCO", "GND_VCO"]
    assert tokens[4] == "0"
    assert tokens[5] == "ppolyf_u_3k"
    assert any(t.startswith("L=") for t in tokens)
    assert any(t.startswith("W=") for t in tokens)
    assert result.placeholder_value_classes == {"ppolyf_u_3k": "resistor"}


def test_model_name_case_insensitive():
    out = convert_reference_netlist(CARD.upper() + "\n", deck="gf180mcu").text
    assert " 0 ppolyf_u_3k " in out


def test_two_terminal_card():
    out = convert_reference_netlist(
        "R1 a b ppolyf_u_3k W=1u L=5u\n", deck="gf180mcu"
    ).text
    assert out.split()[:5] == ["R1", "a", "b", "0", "ppolyf_u_3k"]


def test_valued_card_unchanged():
    text = "R1 a b 1k\n"
    assert convert_reference_netlist(text, deck="gf180mcu").text == text


def test_unknown_model_unchanged():
    text = "R1 a b c mymodel W=1u L=5u\n"
    assert convert_reference_netlist(text, deck="gf180mcu").text == text


def test_no_deck_unchanged():
    assert convert_reference_netlist(CARD + "\n").text == CARD + "\n"


def test_r_card_inside_subckt_converted():
    text = ".subckt top a\n" + CARD + "\n.ends\n"
    assert " 0 ppolyf_u_3k " in convert_reference_netlist(text, deck="gf180mcu").text


def test_plain_element_parse_failure_names_subckt_call_fix(tmp_path):
    import json

    import pytest

    from klayout_tools.lvs import LvsError, run_lvs

    layout = tmp_path / "layout.spice"
    layout.write_text(".subckt top a b\nR1 a b 1k\n.ends\n")
    ref = tmp_path / "ref.spice"
    ref.write_text(".subckt top a b\n" + CARD + "\n.ends\n")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(layout), "top": "top"},
                "reference": {"netlist": str(ref)},
            }
        )
    )
    with pytest.raises(LvsError) as exc_info:
        run_lvs(str(request))
    message = str(exc_info.value)
    assert "Can't find a value for a R, C or L device" in message
    assert 'form: "subckt-call"' in message
    assert "reference.deck" in message
