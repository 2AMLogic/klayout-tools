"""Landing-pad helpers for sky130 cut-layer DRC fixtures (issue #5737).

Eight sky130 `enclosing` rules carry `require_containment=True` (issue
#5737): every cut of a single-population layer must lie fully inside that
rule's conductor, and a cut whose conductor is absent from the stream is
reported too. A fixture that draws a cut to exercise a width/space rule (or
an unrelated enclosing/separation rule) therefore has to draw the flipped
rules' landings as well, or the fixture reports cuts it never meant to
test -- the same accommodation gf180mcu's #2726 fixtures already make (see
`helpers/gf180mcu_landings.py` and `tests/golden_deck/generate_golden_deck.py`'s
`_add_cut_landings`).

The conductor sides per cut layer are read off the deck's own
`require_containment` rows rather than copied here, so the table cannot drift
from `decks/sky130.py`: `met3.enclosing.via3.1` and `met4.enclosing.via4.1`
stay `False` (a capm via3 / capm2 via4 lands on its cap plate, not on the
next metal down), so a capm-stack fixture needs only the one landing the
flipped rule asks for.
"""

from __future__ import annotations

import functools

import klayout.db as kdb

from klayout_tools.decks import get_deck

#: Grown around a fixture's cuts on every landing pad; clears every flipped
#: rule's margin with room to spare (the largest is met5/via4's 310 dbu) and
#: keeps each pad well above its conductor's own width minimum.
_PAD_DBU = 2000

#: Enclosure the li1 landing in :func:`insert_licon_with_li1_landing` draws
#: around a contact cut: li.5's own published 0.08 um projection enclosure,
#: the value the curated `li1.enclosing.licon1.1` approximates at its
#: zero-margin floor.
LI1_LANDING_ENCLOSURE_UM = 0.08


@functools.cache
def _landings() -> dict[tuple[int, int], tuple[tuple[int, int], ...]]:
    """`(layer, datatype)` of each cut -> the conductors the deck requires."""
    landings: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for rule in get_deck("sky130"):
        if rule.require_containment and rule.other_layer is not None:
            landings.setdefault(rule.other_layer, []).append(rule.layer)
    return {cut: tuple(conductors) for cut, conductors in landings.items()}


def _pad(
    layout: kdb.Layout,
    top: kdb.Cell,
    conductor: tuple[int, int],
    around_box: kdb.Box,
) -> None:
    top.shapes(layout.layer(*conductor)).insert(around_box.enlarged(_PAD_DBU, _PAD_DBU))


def landed_cuts(
    layout: kdb.Layout,
    top: kdb.Cell,
    cut_layer_idx: int,
    boxes: list[kdb.Box],
) -> None:
    """Insert `boxes` as cuts on `cut_layer_idx`, covered by their landings.

    One pad spanning all the cuts is drawn per required conductor -- unless
    the fixture already draws that conductor itself (the rule under test's
    own geometry stays the test subject; only the not-under-test landings
    the fixture omitted get synthesized).
    """
    for box in boxes:
        top.shapes(cut_layer_idx).insert(box)
    info = layout.get_info(cut_layer_idx)
    bbox = kdb.Box()
    for box in boxes:
        bbox += box
    for conductor in _landings().get((info.layer, info.datatype), ()):
        if boxes and top.shapes(layout.layer(*conductor)).size() == 0:
            _pad(layout, top, conductor, bbox)


def insert_licon_with_li1_landing(
    layout: kdb.Layout,
    top: kdb.Cell,
    licon_idx: int,
    li1_idx: int,
    x_um: float,
    y_um: float,
    half_um: float,
    dbu: float,
) -> None:
    """Draw one `licon1` contact square at `(x_um, y_um)` plus its li1 pad.

    The pad encloses the cut by `LI1_LANDING_ENCLOSURE_UM` on every side --
    li.5's own published projection enclosure, the value the curated
    `li1.enclosing.licon1.1` approximates at its zero-margin floor. Measured
    clean across every generator/params case the #461/#781 tests cover; a
    full `contact_region_um` pad (0.10 um per side) is not -- at an unraised
    #461 gate landing pad it leaves a 0.15 um gap to the generator's S/D
    local-metal pads, under `li1.space.1`'s 0.17 um floor.
    """
    half_dbu = round(half_um / dbu)
    pad_half_dbu = round((half_um + LI1_LANDING_ENCLOSURE_UM) / dbu)
    x_dbu = round(x_um / dbu)
    y_dbu = round(y_um / dbu)
    top.shapes(licon_idx).insert(
        kdb.Box(
            x_dbu - half_dbu,
            y_dbu - half_dbu,
            x_dbu + half_dbu,
            y_dbu + half_dbu,
        )
    )
    top.shapes(li1_idx).insert(
        kdb.Box(
            x_dbu - pad_half_dbu,
            y_dbu - pad_half_dbu,
            x_dbu + pad_half_dbu,
            y_dbu + pad_half_dbu,
        )
    )
