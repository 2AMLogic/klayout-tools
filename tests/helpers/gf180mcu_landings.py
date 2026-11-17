"""Landing conductors for gf180mcu cut fixtures (issue #5915).

gf180mcu's single-population cut-enclosure rules are `require_containment`
(issue #2726), so a fixture that draws a bare Contact/Via1-Via4 -- written
before that opt-in to test the cut's own width/space/enclosure behaviour --
now also reports the cut as an orphan under the matching ``metalN.enclosing.*``
rule. :func:`landed` rewrites such a fixture with a generous landing on every
conductor the deck requires, so those tests keep isolating what they test.

A conductor layer the fixture already draws is left alone: tests that
deliberately under-cover a cut with the conductor under test keep exactly the
geometry they wrote, and only the *opposite* landing is supplied.
"""

from __future__ import annotations

from pathlib import Path

import klayout.db as kdb

from klayout_tools.decks import get_deck

#: Contact and Via1-Via4: the cut layers whose landings `require_containment`.
_CUTS = frozenset({(33, 0), (35, 0), (38, 0), (40, 0), (41, 0)})


def _landing_layers() -> dict[tuple[int, int], list[tuple[int, int]]]:
    landings: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for rule in get_deck("gf180mcu"):
        if rule.require_containment and rule.other_layer in _CUTS:
            landings.setdefault(rule.other_layer, []).append(rule.layer)
    return landings


def landed(path: str | Path, pad_nm: int = 900) -> str:
    """Path of a copy of the GDS at `path` with each cut landed on its conductors."""
    layout = kdb.Layout()
    layout.read(str(path))
    top = layout.top_cell()
    pad = round(pad_nm * 0.001 / layout.dbu)
    drawn = {
        (info.layer, info.datatype)
        for info in layout.layer_infos()
        if not top.bbox_per_layer(layout.layer(info)).empty()
    }
    for cut, conductors in _landing_layers().items():
        if cut not in drawn:
            continue
        region = (
            kdb.Region(top.begin_shapes_rec(layout.layer(*cut))).sized(pad).merged()
        )
        for conductor in conductors:
            if conductor not in drawn:
                top.shapes(layout.layer(*conductor)).insert(region)
    out = Path(path).with_name(Path(path).stem + "_landed.gds")
    layout.write(str(out))
    return str(out)
