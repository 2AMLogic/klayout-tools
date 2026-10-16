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
