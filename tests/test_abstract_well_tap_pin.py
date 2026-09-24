"""A macro's own well tie survives ``--abstract-cells`` (issue #2398).

``--abstract-cells`` erases every device-recognition layer inside a matched
cell, which includes the well-tie ``tap``. When the macro's *only* external
well access is a tap drawn inside it (tap -> contact -> local metal stack ->
landing pad), erasing that tap cuts the restored well island (#1911/#2082)
off from the parent's routing: a ``well_label``-declared body pin then binds
to an isolated, single-terminal net the parent never reaches, while a flat
extraction of the same layout resolves it onto the parent's supply net.

Covered here for both tap mechanisms an ``ExtractionDeck`` supports: a deck
with a drawn ``tap`` layer (``sky130``) and one that *derives* its tie
geometry from implant layers (``sg13cmos5l``'s ``tap_nplus`` inside
``NWell``) -- the two code paths that produce the well-tie cover. The
deliberately-unrouted control asserts the fix restores only real drawn
continuity: a macro whose landing pad no parent wire reaches must still
resolve its body pin onto an island, never onto the supply rail.

Complements ``tests/test_abstract_well_connectivity.py``, which covers the
*other* well-continuity shape: one well spanning several abutted abstracted
instances, tied through a tap drawn in the **parent** (never erased).
"""

import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract

#: ``(layer, datatype)`` per role, per deck under test -- keyed so one
#: layout builder serves both the drawn-tap and derived-tap decks.
_LAYERS = {
    "sky130": {
        "nwell": (64, 20),
        "well_label": (64, 5),
        "active": (65, 20),
        "tap": (65, 44),
        "poly": (66, 20),
        "contact": (66, 44),
        "metal0": (67, 20),
        "metal0_label": (67, 5),
        "via0": (67, 44),
        "metal1": (68, 20),
        "metal1_label": (68, 5),
    },
    "sg13cmos5l": {
        "nwell": (31, 0),
        "well_label": (31, 2),
        "active": (1, 0),
        # No drawn tap layer: an `nSD`-covered Activ shape inside NWell is
        # the well tie `extract.py` derives (issue #1084).
        "tap": (7, 0),
        "poly": (5, 0),
        "contact": (6, 0),
        "metal0": (8, 0),
        "metal0_label": (8, 2),
        "via0": (19, 0),
        "metal1": (10, 0),
        "metal1_label": (10, 2),
    },
}


def _macro_layout(deck: str, *, routed: bool = True) -> kdb.Layout:
    """A macro whose only external well access is a tap drawn inside it.

    ``routed=False`` leaves the parent's supply wire short of the macro's own
    landing pad -- the control for "restore drawn continuity, never invent
    it".
    """
    layers = _LAYERS[deck]
    layout = kdb.Layout()
    layout.dbu = 0.001
    macro = layout.create_cell("mylib__macro")
    top = layout.create_cell("top")

    def draw(target, role, box):
        target.shapes(layout.layer(*layers[role])).insert(kdb.Box(*box))

    def label(target, role, text, x, y):
        target.shapes(layout.layer(*layers[role])).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(macro, "nwell", (0, 0, 6000, 6000))
    if deck == "sky130":
        # A real PMOS inside the well gives flat extraction a body-net control.
        draw(macro, "active", (1000, 1000, 3000, 2000))
        draw(macro, "poly", (1800, 800, 2200, 2200))
        for x, pin in [(1400, "A"), (2600, "Y")]:
            draw(macro, "contact", (x - 100, 1400, x + 100, 1600))
            draw(macro, "metal0", (x - 200, 1300, x + 200, 1700))
            label(macro, "metal0_label", pin, x, 1500)

    # The well tie, and the macro's only path from it to the outside world:
    # tap -> contact -> first metal -> via -> landing pad on the next metal.
    draw(macro, "tap", (4000, 1000, 4600, 1600))
    if deck != "sky130":
        # Derived-tap deck: the tie is an implant-covered Activ shape, so the
        # active layer carries the same footprint as the implant marker.
        draw(macro, "active", (4000, 1000, 4600, 1600))
    draw(macro, "contact", (4200, 1200, 4400, 1400))
    draw(macro, "metal0", (4100, 1100, 4500, 4000))
    draw(macro, "via0", (4200, 3700, 4400, 3900))
    draw(macro, "metal1", (4000, 3600, 4600, 4000))
    # The body pin itself: a well-label text over the macro's own well.
    label(macro, "well_label", "VPB", 5200, 5200)

    top.insert(kdb.CellInstArray(macro.cell_index(), kdb.Trans(0, 0)))
    supply_left = 4000 if routed else 7000
    draw(top, "metal1", (supply_left, 3600, 12000, 4000))
    label(top, "metal1_label", "VPWR", 10000, 3800)
    if deck == "sky130":
        for x, pin in [(1400, "a"), (2600, "y")]:
            draw(top, "via0", (x - 100, 1400, x + 100, 1600))
            draw(top, "metal1", (x - 300, 1200, x + 300, 1800))
            label(top, "metal1_label", pin, x, 1500)
    return layout


def _extract(tmp_path, deck, *, abstract, routed=True):
    name = f"{deck}-{'routed' if routed else 'unrouted'}"
    path = tmp_path / f"{name}.gds"
    _macro_layout(deck, routed=routed).write(str(path))
    return run_extract(
        str(path),
        deck,
        output=str(tmp_path / f"{name}-{'abstract' if abstract else 'flat'}.spice"),
        abstract_cell_patterns=("mylib__*",) if abstract else (),
    )


def _instance_pin_net(extraction, cell, instance, pin):
    """The node the emitted ``X<instance>`` card binds ``cell``'s ``pin`` to.

    Read off the netlist text rather than the JSON so the assertion is made
    against the artifact a downstream ``klt lvs`` compare actually consumes.
    """
    text = Path(extraction["netlist_path"]).read_text()
    subckt = re.search(rf"(?m)^\.SUBCKT {re.escape(cell)}((?: \S+)*)\s*$", text)
    assert subckt is not None, f"no .SUBCKT {cell} in:\n{text}"
    pin_names = subckt.group(1).split()
    card = re.search(
        rf"(?m)^X{re.escape(instance)}((?: \S+)+) {re.escape(cell)}\s*$", text
    )
    assert card is not None, f"no X{instance} card in:\n{text}"
    nodes = card.group(1).split()
    assert len(nodes) == len(pin_names), f"arity mismatch in:\n{text}"
    return nodes[pin_names.index(pin)]


def _net_names(extraction):
    return {net["name"] for net in extraction["nets"]}


@pytest.mark.parametrize("deck", ["sky130", "sg13cmos5l"])
def test_abstracted_internal_well_tap_binds_body_pin_to_parent_supply(tmp_path, deck):
    flat = _extract(tmp_path, deck, abstract=False)
    # Flat control: the macro's own well label and the parent's supply label
    # land on one net, so the drawn tap stack really does connect them.
    assert any(set(name.split("|")) >= {"VPB", "VPWR"} for name in _net_names(flat)), (
        _net_names(flat)
    )
    if deck == "sky130":
        assert flat["unbiased_pmos_body_nets"] == []
        bodies = {device["nets"]["b"] for device in flat["devices"]}
        assert len(bodies) == 1 and "VPWR" in next(iter(bodies))

    abstract = _extract(tmp_path, deck, abstract=True)
    assert abstract["device_count"] == 0
    assert (
        _instance_pin_net(abstract, "mylib__macro", "mylib__macro_0", "VPB") == "VPWR"
    )


@pytest.mark.parametrize("deck", ["sky130", "sg13cmos5l"])
def test_abstracted_unrouted_well_pad_stays_off_the_supply_net(tmp_path, deck):
    flat = _extract(tmp_path, deck, abstract=False, routed=False)
    assert not any(
        set(name.split("|")) >= {"VPB", "VPWR"} for name in _net_names(flat)
    ), _net_names(flat)

    abstract = _extract(tmp_path, deck, abstract=True, routed=False)
    bound = _instance_pin_net(abstract, "mylib__macro", "mylib__macro_0", "VPB")
    assert "VPWR" not in bound, bound
