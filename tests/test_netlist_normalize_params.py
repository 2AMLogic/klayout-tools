"""Parametrised subckt-call references (issue #2953): identifier / simple
expression values in ``L``/``W`` resolve against the netlist's ``.subckt``
header defaults and ``.param`` lines before ``_parse_um`` reads them."""

from __future__ import annotations

import pytest

from klayout_tools.netlist_normalize import (
    NormalizeError,
    normalize_reference_netlist,
)


def test_identifier_resolves_from_subckt_header_default():
    text = (
        ".subckt cell a b vdd gnd lacc=0.5u wacc=2u\n"
        "XM1 a b gnd gnd nfet_03v3 L=lacc W=wacc\n"
        ".ends\n"
    )
    out = normalize_reference_netlist(text, deck="gf180mcu")
    assert "M1 a b gnd gnd nfet L=0.5U W=2U" in out


def test_identifier_resolves_from_param_line():
    text = ".param lp=0.5u\nXM1 d g s b nfet_03v3 L=lp W=1u\n"
    out = normalize_reference_netlist(text, deck="gf180mcu")
    assert "L=0.5U W=1U" in out


@pytest.mark.parametrize("expr", ["'w0*2'", "{w0*2}", "w0*2", "(w0+w0)"])
def test_arithmetic_expression_resolves(expr):
    text = f".param w0=1u\nXM1 d g s b nfet_03v3 L=0.5u W={expr}\n"
    out = normalize_reference_netlist(text, deck="gf180mcu")
    assert "W=2U" in out


def test_subckt_default_shadows_other_subckt_and_instance_name_matches_default():
    text = (
        ".subckt a x y w=1u\nXM1 x y y y nfet_03v3 L=0.5u W=w\n.ends\n"
        ".subckt b x y w=3u\nXM1 x y y y nfet_03v3 L=0.5u W=w\n.ends\n"
    )
    out = normalize_reference_netlist(text, deck="gf180mcu")
    assert "W=1U" in out and "W=3U" in out


def test_sibling_call_parameter_is_not_a_symbol():
    # `wx` exists only as another call parameter, which SPICE does not scope.
    with pytest.raises(NormalizeError, match="'wx'"):
        normalize_reference_netlist(
            "XM1 d g s b nfet_03v3 L=0.5u W=wx wx=4u\n", deck="gf180mcu"
        )


def test_bare_literal_geometry_style_unchanged_for_resolved_bare_value():
    text = ".param wb=1\nXM1 d g s b sky130_fd_pr__nfet_01v8 L=0.15 W=wb\n"
    out = normalize_reference_netlist(text, deck="sky130")
    assert "L=0.15U W=1U" in out


def test_parasitic_expressions_do_not_break_conversion():
    text = (
        ".param wp=1u\n"
        "XM1 d g s b nfet_03v3 L=0.5u W=wp ad='wp*0.18u' as='int(wp)' pd=2*wp\n"
    )
    out = normalize_reference_netlist(text, deck="gf180mcu")
    assert "W=1U" in out and "ad=" not in out


def test_unresolved_symbol_names_parameter_and_symbol():
    with pytest.raises(NormalizeError) as excinfo:
        normalize_reference_netlist(
            "XM1 d g s b nfet_03v3 L=lacc W=1u\n", deck="gf180mcu"
        )
    message = str(excinfo.value)
    assert "'L'" in message and "'lacc'" in message


def test_function_call_expression_rejected_loudly():
    with pytest.raises(NormalizeError, match="not a plain numeric literal"):
        normalize_reference_netlist(
            ".param w0=1u\nXM1 d g s b nfet_03v3 L=0.5u W='sqrt(w0)'\n",
            deck="gf180mcu",
        )


def test_circular_definition_rejected():
    with pytest.raises(NormalizeError, match="'a'"):
        normalize_reference_netlist(
            ".param a=b b=a\nXM1 d g s b nfet_03v3 L=a W=1u\n", deck="gf180mcu"
        )


def test_sky130_diode_subckt_call_converts_every_deck_class():
    from klayout_tools.decks.sky130 import EXTRACTION_DECK

    assert EXTRACTION_DECK.diodes
    for diode in EXTRACTION_DECK.diodes:
        text = f"X1 an ca sky130_fd_pr__{diode.name} area=1 perim=4\n"
        out = normalize_reference_netlist(text, deck="sky130")
        assert f"D1 an ca {diode.name} A=1P P=4U" in out, diode.name
        # Auto-resolution (no deck) hits the same curated table.
        auto = normalize_reference_netlist(
            f"X1 an ca sky130_fd_pr__{diode.name} area=1p perim=4u\n"
        )
        assert f"D1 an ca {diode.name} A=1P P=4U" in auto, diode.name


def test_sky130_diode_subckt_call_area_only_pj_and_mult():
    out = normalize_reference_netlist(
        "X1 an ca sky130_fd_pr__diode_pd2nw_05v5 area=2\n", deck="sky130"
    )
    assert "D1 an ca diode_pd2nw_05v5 A=2P" in out and " P=" not in out
    out = normalize_reference_netlist(
        "XD2 an ca sky130_fd_pr__diode_pd2nw_05v5 area=2 pj=6\n", deck="sky130"
    )
    assert "D2 an ca diode_pd2nw_05v5 A=2P P=6U" in out
    with pytest.raises(NormalizeError, match="multi"):
        normalize_reference_netlist(
            "X1 an ca sky130_fd_pr__diode_pd2nw_05v5 area=2 perim=4 mult=2\n",
            deck="sky130",
        )


_OVERRIDE_REF = (
    ".subckt cell a b SCALE=1\n"
    "XM1 a b b b sky130_fd_pr__nfet_01v8 L=1 W=2 nf=1 m={SCALE}\n"
    ".ends\n"
    ".subckt top a b\n"
    "x1 a b cell SCALE=%s\n"
    ".ends\n"
)


def test_call_site_override_of_header_param_is_rejected():
    with pytest.raises(NormalizeError, match=r"M=\{SCALE\}.*SCALE.*x1.*4"):
        normalize_reference_netlist(_OVERRIDE_REF % "4", deck="sky130")


def test_call_site_override_is_case_insensitive():
    text = _OVERRIDE_REF.replace("SCALE=%s", "scale=4")
    with pytest.raises(NormalizeError, match="overrides"):
        normalize_reference_netlist(text, deck="sky130")


def test_call_site_override_through_param_chain_is_rejected():
    text = _OVERRIDE_REF.replace("m={SCALE}", "m={SCALE*2}") % "4"
    with pytest.raises(NormalizeError, match="SCALE"):
        normalize_reference_netlist(text, deck="sky130")


def test_call_passing_default_value_converts():
    normalize_reference_netlist(_OVERRIDE_REF % "1", deck="sky130")


def test_call_omitting_param_converts():
    text = _OVERRIDE_REF.replace(" SCALE=%s", "")
    normalize_reference_netlist(text, deck="sky130")


def test_override_of_unused_param_converts():
    text = (
        ".subckt cell a b SCALE=1 other=2\n"
        "XM1 a b b b sky130_fd_pr__nfet_01v8 L=1 W=2 nf=1 m={SCALE}\n"
        ".ends\n"
        ".subckt top a b\nx1 a b cell other=3\n.ends\n"
    )
    normalize_reference_netlist(text, deck="sky130")
