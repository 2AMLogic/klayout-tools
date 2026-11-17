"""Issue #5665: a reference `.SUBCKT` named after a deck device class.

Split out of `tests/test_lvs.py` (which is at its per-file size ceiling) —
see the `#5665` banner below for the defect narrative and
`netlist_capacitor_recovery.py`'s module docstring for the mechanism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


# --------------------------------------------------------------------------- #
# Issue #5665: a reference `.SUBCKT` whose *name* collides with a drawn-
# resistor or MoM-capacitor class in the resolved deck's recovery tables.
# The #1157/#1942 recovery reader claimed such a name unconditionally in
# `wants_subcircuit`, telling KLayout it was a device model -- so the reader
# silently swallowed the matching `.SUBCKT` *definition* and a one-
# subcircuit reference came back with zero circuits ("reference netlist has
# no top circuit"; or, when the file also instantiated it via an `X` card,
# "Not a known element type: 'X'"). A foundry resistor CDL is conventionally
# named after the device it models, so the collision is the common case.
# --------------------------------------------------------------------------- #


#: ``(deck, colliding `.SUBCKT` name, plain-element card, subckt-call card)``.
#: A drawn-resistor class on each of two decks (proving the fix is not
#: deck-keyed) plus sg13g2's MoM capacitor (the `custom_device_classes`
#: lookup rather than the resistor one). The plain-element card carries a
#: real value token so the default reader accepts it without conversion;
#: the subckt-call card is the valueless foundry shape of the original
#: report. The inner card names the colliding class as its model so the
#: surviving device's class binding can be asserted too (the MoM leg has no
#: native element letter, so its card stays a plain valued `C` and no class
#: binding is expected).
_ISSUE_5665_COLLISIONS = [
    pytest.param(
        "sg13g2",
        "rppd",
        "R1 net5 net6 sub 260 rppd l=1.2u w=0.7u",
        "Rp3 net5 net6 sub rppd m=1 l=1.2u w=0.7u",
        True,
        id="sg13g2-rppd",
    ),
    pytest.param(
        "sg13g2",
        "cap_cmomi",
        "C1 a b 1e-15",
        "C1 a b 1e-15",
        False,
        id="sg13g2-cap_cmomi",
    ),
    pytest.param(
        "gf180mcu",
        "ppolyf_u_3k",
        "R1 n1 n2 260 ppolyf_u_3k l=1u w=1u",
        "R1 n1 n2 ppolyf_u_3k w=1u l=1u",
        True,
        id="gf180mcu-ppolyf_u_3k",
    ),
]


@pytest.mark.parametrize("form", ["plain-element", "subckt-call"])
@pytest.mark.parametrize(
    ("deck", "subckt_name", "plain_card", "subckt_call_card", "model_named"),
    _ISSUE_5665_COLLISIONS,
)
def test_reference_subckt_named_after_deck_device_class_survives_the_read(
    tmp_path, form, deck, subckt_name, plain_card, subckt_call_card, model_named
):
    """The recovery reader must not claim a name the reference itself
    defines: the circuit survives (exactly one circuit, one top circuit --
    pre-fix both were zero/empty) and the device inside keeps its class
    binding instead of the whole definition being swallowed."""
    from klayout_tools.lvs import _read_reference_netlist

    card = subckt_call_card if form == "subckt-call" else plain_card
    path = _write(
        tmp_path / f"collide_{deck}_{subckt_name}.cdl",
        f".SUBCKT {subckt_name}\n{card}\n.ENDS {subckt_name}\n",
    )
    netlist = _read_reference_netlist(path, form=form, deck=deck)

    circuits = list(netlist.each_circuit())
    assert len(circuits) == 1  # pre-fix: 0 -- the definition was swallowed
    assert [c.name for c in netlist.top_circuits()] == [subckt_name.upper()]
    devices = list(circuits[0].each_device())
    assert len(devices) == 1
    if model_named:
        # Rescued without downgrading: the model-named card still binds the
        # deck class (uppercased by the SPICE reader), not a generic class.
        assert devices[0].device_class().name == subckt_name.upper()


@pytest.mark.parametrize(
    ("deck", "subckt_name", "plain_card", "subckt_call_card", "model_named"),
    _ISSUE_5665_COLLISIONS,
)
def test_reference_subckt_named_after_deck_device_class_mixed_case(
    tmp_path, deck, subckt_name, plain_card, subckt_call_card, model_named
):
    """The not-claimed-because-defined check is case-insensitive in both
    directions: a mixed-case `.SUBCKT RPpd` definition survives the same
    uppercase-keyed claim (`defined_subckt_names` upper-cases, exactly like
    the lookups do)."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / f"collide_mixed_{deck}.cdl",
        f".SUBCKT {subckt_name.capitalize()}\n{plain_card}\n"
        f".ENDS {subckt_name.capitalize()}\n",
    )
    netlist = _read_reference_netlist(path, deck=deck)

    assert len(list(netlist.each_circuit())) == 1
    assert [c.name for c in netlist.top_circuits()] == [subckt_name.upper()]


def test_reference_subckt_named_after_deck_class_without_deck_unchanged(tmp_path):
    """With no `deck` at all both recovery lookups are empty, so a colliding
    `.SUBCKT rppd` was never claimed -- that read must stay byte-identical
    (the fix is a no-op without a deck)."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "collide_no_deck.cdl",
        ".SUBCKT rppd\nR1 net5 net6 sub 260 rppd l=1.2u w=0.7u\n.ENDS rppd\n",
    )
    netlist = _read_reference_netlist(path)

    assert [c.name for c in netlist.top_circuits()] == ["RPPD"]


def test_reference_defining_and_instantiating_colliding_subckt_parses(tmp_path):
    """The define-plus-`X`-card shape: a reference that *defines* `.SUBCKT
    rppd` and instantiates it via an `X` card is a legitimate hierarchical
    reference -- pre-fix it did not parse at all ("Not a known element type:
    'X'"). Post-fix it reads as a TOP -> RPPD hierarchy with the `X` card a
    real subcircuit call."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "define_and_call.cdl",
        ".SUBCKT rppd A B SUB\n"
        "R1 A B SUB 260 rppd l=1.2u w=0.7u\n"
        ".ENDS rppd\n"
        ".SUBCKT TOP A B SUB\n"
        "X1 A B SUB rppd\n"
        ".ENDS TOP\n",
    )
    netlist = _read_reference_netlist(path, deck="sg13g2")

    top = next(c for c in netlist.top_circuits() if c.name == "TOP")
    calls = list(top.each_subcircuit())
    assert len(calls) == 1
    assert calls[0].circuit_ref().name == "RPPD"


def test_reference_undefined_resistor_x_card_still_recovers_a_device(tmp_path):
    """Regression guard for #1157/#1942: an `X` card naming a drawn-resistor
    class the file does *not* define must still be recovered as a device of
    that class -- the not-claimed check only yields for names the file
    itself defines."""
    from klayout_tools.lvs import _read_reference_netlist

    path = _write(
        tmp_path / "undefined_x.cdl",
        ".SUBCKT TOP A B SUB\nX$1 A B SUB res_high_po r=3627.9776 L=10U W=1U\n"
        ".ENDS TOP\n",
    )
    netlist = _read_reference_netlist(path, deck="sky130")

    top = next(c for c in netlist.top_circuits() if c.name == "TOP")
    devices = list(top.each_device())
    assert len(devices) == 1
    assert devices[0].device_class().name == "res_high_po"
    # And no phantom circuit was synthesised for the undefined call.
    assert [c.name for c in netlist.each_circuit()] == ["TOP"]
