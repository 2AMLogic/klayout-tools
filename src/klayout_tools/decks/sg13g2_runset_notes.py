"""Runset-correspondence notes for the curated sg13g2 deck (issue #5212).

Kept out of ``sg13g2.py`` (file-size ratchet); that module applies
:func:`apply` to ``DECK`` with a single line. The notes surface as
``runset_correspondence`` in a ``klt drc`` report, only for rules that fired.
"""

from __future__ import annotations

import dataclasses

from . import DrcRule

_IHP_OPEN_PDK_COMMIT = "5cccb161f7492697cfa52eb14dc03beb00bdca9e"  # v0.3.0 tag
_MAXIMAL_RUNSET = "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/sg13g2_maximal.drc"

RUNSET_NOTES: dict[str, dict[str, object]] = {
    "nwell.enclosing.pactiv.1": {
        "implemented_by_pdk_runset": True,
        "runset_file": _MAXIMAL_RUNSET,
        "note": (
            "PDK rule NW.c first removes P+ Activ that interacts with a "
            "text-recognised SVaricap (`lNw_c = PActLV.ext_not("
            "PActLV.ext_interacting(SVaricap))`, SVaricap = NWell under an "
            '"SVaricap" text label on a GatPoly stack); this deck checks every '
            "P+ Activ unconditionally, so findings whose source_cell is a PDK "
            "SVaricap device are expected to be absent from the runset "
            "(deck over-report, not a real enclosure violation)."
        ),
    },
}


def apply(deck: list[DrcRule]) -> None:
    """Attach ``runset_note`` to the noted rules of ``deck``, in place."""
    deck[:] = [
        dataclasses.replace(
            rule,
            runset_note={**RUNSET_NOTES[rule.id], "pdk_revision": _IHP_OPEN_PDK_COMMIT},
        )
        if rule.id in RUNSET_NOTES
        else rule
        for rule in deck
    ]
