"""Reference-side recovery of a deck's spiral-inductor class (issue #6726).

``klt extract`` writes an extracted ``inductor`` as an ``X`` subcircuit-call
card (``XD_$1 a b vsubs inductor PARAMS:``): it has no native SPICE element
letter, and its third terminal is the substrate or well. Read back through a
plain ``kdb.NetlistSpiceReader`` that card degrades to an abstract circuit
compared by name, invisible to the device census -- the same gap #1942 closed
for MoM capacitors. :func:`inductor_classes_for_deck` reads the owning deck's
``inductors`` table and :func:`recover_inductor_x_card` turns a card naming
one of those classes, with the class's three nets, back into a real device of
:func:`klayout_tools.extract_inductors.inductor_device_class` -- the class the
extraction side registers -- so both sides of a compare agree on the shape.

IHP's own reference CDL writes the same device as a model-bearing three-node
``L`` card, which ``kdb.NetlistSpiceReader`` rejects before any delegate runs;
that spelling is not recovered here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .netlist_class_registry import shared_device_class

if TYPE_CHECKING:
    import klayout.db as kdb

    from .decks import ExtractionDeck


def inductor_classes_for_deck(deck: ExtractionDeck | None) -> dict[str, str]:
    """``{<UPPER-CASED class name>: <canonical class name>}`` for every spiral
    inductor class ``deck`` declares -- the table
    :func:`~klayout_tools.netlist_capacitor_recovery
    .make_capacitor_class_recovery_reader`'s ``inductor_classes`` parameter
    wants. ``None`` (no deck resolved for this side of the compare) returns
    ``{}``: the ``X`` card then degrades to the abstract-circuit fallback,
    like any other unrecognised card."""
    if deck is None:
        return {}
    return {inductor.name.upper(): inductor.name for inductor in deck.inductors}


def recover_inductor_x_card(
    circuit: kdb.Circuit,
    name: str,
    model: str,
    nets: list,
    inductor_lookup: Mapping[str, str],
    registry: dict[str, kdb.DeviceClass],
) -> bool:
    """Recover one ``X`` card naming a deck inductor class as a real device of
    that class. Reports whether the card was handled; a name match whose net
    count is not the class's terminal count reports ``False`` (never seen in
    practice) so the caller falls through to the default handler rather than
    misconnecting a terminal. All cards naming one class share a single
    ``DeviceClass`` object (``shared_device_class``)."""
    # Lazy, like ``netlist_capacitor_recovery``'s own ``from .extract import``:
    # this text-level module should not pull in the extraction stack at import.
    from .extract_inductors import inductor_device_class

    class_name = inductor_lookup.get(model.upper())
    if class_name is None:
        return False
    device_class = shared_device_class(
        circuit.netlist(),
        class_name,
        lambda: inductor_device_class(class_name),
        registry,
    )
    terminals = device_class.terminal_definitions()
    if len(nets) != len(terminals):
        return False
    device = circuit.create_device(device_class, name)
    for net, terminal in zip(nets, terminals, strict=True):
        device.connect_terminal(terminal.name, net)
    return True
