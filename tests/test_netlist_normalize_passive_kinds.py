"""``device_map`` inductor/varactor kinds and the aggregated unknown-subcircuit
refusal (issue #3206)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.netlist_normalize import (
    NormalizeError,
    normalize_reference_netlist,
)

_MAP = {
    "ind_sub": {"kind": "inductor", "class": "inductor3"},
    "var_4t": {"kind": "varactor", "class": "varactor4"},
}


def test_inductor_and_varactor_normalize_to_class_labelled_x_cards():
    text = "XL1 p1 p2 sub ind_sub W=10u\nXV1 a b w sub var_4t\n"
    out = normalize_reference_netlist(text, device_map=_MAP)
    assert "XL1 p1 p2 sub inductor3\n" in out
    assert "XV1 a b w sub varactor4\n" in out


def test_normalized_card_is_readable_with_right_pin_count(tmp_path):
    out = normalize_reference_netlist("XL1 p1 p2 sub ind_sub\n", device_map=_MAP)
    path = tmp_path / "n.sp"
    path.write_text(out)
    netlist = kdb.Netlist()
    netlist.read(str(path), kdb.NetlistSpiceReader())
    circuit = netlist.circuit_by_name("INDUCTOR3")
    assert circuit is not None and circuit.pin_count() == 3


@pytest.mark.parametrize("card", ["XL1 p1 p2 ind_sub", "XL1 p1 p2 sub extra ind_sub"])
def test_wrong_terminal_count_is_refused(card):
    with pytest.raises(NormalizeError, match="has 3 terminals"):
        normalize_reference_netlist(card + "\n", device_map=_MAP)


def test_geometry_override_refused_for_new_kinds():
    bad = {"x": {"kind": "inductor", "class": "ind", "width_um": 1.0}}
    with pytest.raises(NormalizeError, match="only supported for kind"):
        normalize_reference_netlist("X1 a b c x\n", device_map=bad)


def test_all_unknown_subcircuits_are_listed():
    text = (
        "XA a b c d mystery_a L=1u W=1u\n"
        "XB a b c d mystery_b L=1u W=1u\n"
        "XC a b c d mystery_a L=1u W=1u\n"
    )
    with pytest.raises(NormalizeError) as err:
        normalize_reference_netlist(text, deck="sky130")
    msg = str(err.value)
    assert "'mystery_a'" in msg and "'mystery_b'" in msg
    assert msg.count("'mystery_a'") == 1


def test_single_unknown_subcircuit_keeps_original_message():
    with pytest.raises(NormalizeError, match="subcircuit 'mystery_a' is not a known"):
        normalize_reference_netlist("XA a b c d mystery_a L=1u W=1u\n", deck="sky130")
