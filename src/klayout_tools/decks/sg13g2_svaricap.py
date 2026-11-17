"""The ``SVaricap`` carve-out of sg13g2's ``NW.c`` rule (issue #5442).

Kept out of ``sg13g2.py`` (file-size ratchet); that module applies
:func:`apply` to ``DECK`` alongside its other post-construction hooks.

IHP's ``NW.c`` does not measure NWell enclosure of *every* P+ Activ: it
first drops whole P+ Activ polygons that interact with a text-recognised
``SVaricap`` varactor (``sg13g2_maximal.drc`` line 1996,
``lNw_c = PActLV.ext_not(PActLV.ext_interacting(SVaricap))``). #5212
documented that deviation as a ``runset_note``; this module models it, so
a stream instantiating the PDK's own varactor is no longer reported
against a threshold the foundry never applies to it.

The recogniser itself is pure deck data -- the drawn layers and the label
string, nothing geometric. ``klayout_tools.drc_text_exempt`` holds the
derivation and its correspondence with the runset, line by line.

Scope: ``nwell.enclosing.pactiv.1`` only. The sibling rules whose upstream
operands carry their own SVaricap / ``schottky_nbl1`` / ``scr1`` exclusions
(``nwell.enclosing.pactiv.hv.1``, ``nwell.space.*``,
``activ.enclosing.cont.1``) use different operators and different
recognised devices; they are untouched and still held to the ordinary
(stricter) threshold.
"""

from __future__ import annotations

import dataclasses

from ..drc_text_exempt import TextMarkerDeviceSpec
from . import DrcRule

#: ``NW.c``'s recognised varactor, transcribed from IHP-Open-PDK
#: ``5cccb161f7492697cfa52eb14dc03beb00bdca9e`` (the ``v0.3.0`` tag).
SVARICAP = TextMarkerDeviceSpec(
    activ=(1, 0),  # Activ.drawing
    gate_poly=(5, 0),  # GatPoly.drawing
    n_implant=(7, 0),  # nSD.drawing
    n_implant_block=(7, 21),  # nSD.block
    p_implant=(14, 0),  # pSD.drawing
    well=(31, 0),  # NWell.drawing
    buried_layer=(32, 0),  # nBuLay.drawing
    text_layer=(63, 0),  # TEXT_0 (`source.labels("63/0")`)
    label="SVaricap",
)

#: Rule id -> recogniser, plus the disclosure appended to that rule's
#: ``description`` so ``klt drc --list-rules`` (and every violation record)
#: states the carve-out rather than leaving it implicit.
EXEMPTIONS: dict[str, TextMarkerDeviceSpec] = {
    "nwell.enclosing.pactiv.1": SVARICAP,
}

_DISCLOSURE = ", excluding text-recognised SVaricap varactors (as PDK NW.c does)"


def apply(deck: list[DrcRule]) -> None:
    """Attach the recogniser(s) to the matching rules of ``deck``, in place."""
    deck[:] = [
        dataclasses.replace(
            rule,
            text_exempt=EXEMPTIONS[rule.id],
            description=rule.description + _DISCLOSURE,
        )
        if rule.id in EXEMPTIONS
        else rule
        for rule in deck
    ]
