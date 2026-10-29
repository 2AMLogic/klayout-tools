"""Narrow-width sky130 nFET fallback in the model-binding writer (issue #4575)."""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.decks import get_extraction_deck
from klayout_tools.pdk_models import (
    create_model_binding_delegate,
    resolve_device_bindings,
)

SPECIAL = "sky130_fd_pr__special_nfet_01v8"
SCALABLE = "sky130_fd_pr__nfet_01v8"


def _write(width_um: float, deck_name: str = "sky130", family: str = "sky130"):
    deck = get_extraction_deck(deck_name)
    bindings = resolve_device_bindings(deck_name, family, deck)
    netlist = kdb.Netlist()
    cls = kdb.DeviceClassMOS4Transistor()
    cls.name = deck.nfet_class
    netlist.add(cls)
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    device = circuit.create_device(cls, "M1")
    device.set_parameter("L", 0.15)
    device.set_parameter("W", width_um)
    for terminal in ("S", "G", "D", "B"):
        net = circuit.create_net(terminal.lower() + "n")
        device.connect_terminal(terminal, net)
    delegate = create_model_binding_delegate(bindings)
    writer = kdb.NetlistSpiceWriter(delegate)
    writer.use_net_names = True
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "n.spice")
        netlist.write(path, writer, "t")
        with open(path) as handle:
            text = handle.read()
    return text, delegate


@pytest.mark.parametrize("width", [0.36, 0.419])
def test_narrow_nfet_uses_special_subckt(width):
    text, delegate = _write(width)
    assert f" {SPECIAL} " in text
    assert len(delegate.substitution_notes) == 1
    note = delegate.substitution_notes[0]
    assert SPECIAL in note and str(width) in note


@pytest.mark.parametrize("width", [0.42, 0.65])
def test_nfet_at_or_above_bin_edge_unchanged(width):
    text, delegate = _write(width)
    assert f" {SCALABLE} " in text
    assert SPECIAL not in text
    assert delegate.substitution_notes == []


def test_other_pdk_has_no_fallback():
    deck = get_extraction_deck("gf180mcu")
    bindings = resolve_device_bindings("gf180mcu", "gf180mcu", deck)
    assert all(b.narrow_fallback is None for b in bindings.values())
