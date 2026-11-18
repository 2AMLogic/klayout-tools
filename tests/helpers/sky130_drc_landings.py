"""Landing-pad helpers for sky130 cut-layer DRC fixtures (issue #5737).

Eight sky130 `enclosing` rules carry `require_containment=True` (issue
#5737): every cut of a single-population layer must lie fully inside that
rule's conductor, and a cut whose conductor is absent from the stream is
reported too. A fixture that draws a cut to exercise a width/space rule (or
an unrelated enclosing/separation rule) therefore has to draw the flipped
rules' landings as well, or the fixture reports cuts it never meant to
test -- the same accommodation gf180mcu's #2726 fixtures already make (see
`tests/golden_deck/generate_golden_deck.py`'s `_add_cut_landings`).

The conductor sides per cut layer mirror `sky130.py`'s own
`require_containment=True` rows exactly: `met3.enclosing.via3.1` and
`met4.enclosing.via4.1` stay `False` (a capm via3 / capm2 via4 lands on its
cap plate, not on the next metal down), so a capm-stack fixture needs only
the one landing the flipped rule asks for.
"""

from __future__ import annotations

import klayout.db as kdb

#: Conductors the flipped #5737 containment rules require around each
#: single-population cut layer, `(layer, datatype)` -> conductor
#: `(layer, datatype)`s, straight from `decks/sky130.py`.
_SKY130_CUT_CONDUCTOR_LANDINGS: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {
    (66, 44): ((67, 20),),  # licon1 -> li1 (li1.enclosing.licon1.1)
    (67, 44): ((68, 20),),  # mcon -> met1 (met1.enclosing.mcon.1)
    (68, 44): ((68, 20), (69, 20)),  # via -> met1 + met2 (both flipped)
    (69, 44): ((69, 20), (70, 20)),  # via2 -> met2 + met3 (both flipped)
    (70, 44): ((71, 20),),  # via3 -> met4 only (capm via3s keep met3 False)
    (71, 44): ((72, 20),),  # via4 -> met5 only (capm2 via4s keep met4 False)
}

_CONDUCTOR_NAMES: dict[tuple[int, int], str] = {
    (67, 20): "li1.drawing",
    (68, 20): "met1.drawing",
    (69, 20): "met2.drawing",
    (70, 20): "met3.drawing",
    (71, 20): "met4.drawing",
    (72, 20): "met5.drawing",
}

#: Grown around a fixture's cuts on every landing pad; clears every flipped
#: rule's margin with room to spare (the largest is met5/via4's 310 dbu) and
#: keeps each pad well above its conductor's own width minimum.
_PAD_DBU = 2000

#: Enclosure the li1 landing in :func:`insert_licon_with_li1_landing` draws
#: around a contact cut: li.5's own published 0.08 um projection enclosure,
#: the value the curated `li1.enclosing.licon1.1` approximates at its
#: zero-margin floor.
LI1_LANDING_ENCLOSURE_UM = 0.08


def landed_cuts(
    layout: kdb.Layout,
    top: kdb.Cell,
    cut_layer_idx: int,
    boxes: list[kdb.Box],
) -> None:
    """Insert `boxes` as cuts on `cut_layer_idx`, covered by their landings.

    For every conductor the flipped #5737 rules require around this cut
    layer, one pad spanning all the cuts (grown by `_PAD_DBU` on every side)
    is drawn -- unless the fixture already draws that conductor itself (the
    rule under test's own geometry stays the test subject; only the
    not-under-test landings the fixture omitted get synthesized).
    """
    for box in boxes:
        top.shapes(cut_layer_idx).insert(box)
    if not boxes:
        return
    info = layout.get_info(cut_layer_idx)
    landings = _SKY130_CUT_CONDUCTOR_LANDINGS.get((info.layer, info.datatype), ())
    left = min(box.left for box in boxes) - _PAD_DBU
    bottom = min(box.bottom for box in boxes) - _PAD_DBU
    right = max(box.right for box in boxes) + _PAD_DBU
    top_edge = max(box.top for box in boxes) + _PAD_DBU
    for num, datatype in landings:
        idx = layout.layer(num, datatype)
        if top.shapes(idx).size() == 0:
            name = _CONDUCTOR_NAMES[(num, datatype)]
            layout.set_info(idx, kdb.LayerInfo(num, datatype, name))
            top.shapes(idx).insert(kdb.Box(left, bottom, right, top_edge))


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
