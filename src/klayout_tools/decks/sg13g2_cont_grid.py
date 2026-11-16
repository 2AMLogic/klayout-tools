"""``cont.ongrid.1`` for the curated sg13g2 deck (issue #5231).

Kept out of ``sg13g2.py`` (file-size ratchet); that module applies
:func:`apply` to ``DECK`` with a single line.
"""

from __future__ import annotations

from . import DrcRule, RuleProvenance

CONT_ONGRID_RULE = DrcRule(
    id="cont.ongrid.1",
    description="Cont vertices must be on the 0.005um manufacturing grid",
    layer=(6, 0),  # Cont.drawing
    check="ongrid",
    threshold_dbu=0,  # unused by "ongrid" -- see grid_um
    grid_um=0.005,
    # geometry/3_1_offgrid.drc: `offgrid_check(layer, name)` ->
    # "3.1 cont_drw_Offgrid: ... on a drawing grid of 5 nm"
    scope="x",
    provenance=RuleProvenance(
        source_repo="IHP-GmbH/IHP-Open-PDK",
        source_path="ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/geometry/3_1_offgrid.drc",
        rule_id="cont_drw_Offgrid",
        commit="5cccb161f7492697cfa52eb14dc03beb00bdca9e",  # v0.3.0 tag
    ),
    voltage_independent=True,
)


def apply(deck: list[DrcRule]) -> None:
    """Insert ``cont.ongrid.1`` right after ``gatpoly.enclosing.cont.1``."""
    at = next(i for i, r in enumerate(deck) if r.id == "gatpoly.enclosing.cont.1")
    deck.insert(at + 1, CONT_ONGRID_RULE)
