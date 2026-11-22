"""Per-PDK layout builders for deck-resolved per-landing cut resistance
(issue #6917).

Each builder draws one minimal net whose landings exercise a specific
deck-curation state of ``ParasiticsDeck.cuts`` / ``.poly_contact``:

- sky130 mixed contact: a poly->li1 (licon) **and** a li1->met1 (mcon)
  landing in one net, both curated, with the two nominal values unequal;
- gf180mcu poly contact: one poly->metal0 landing on a deck that curates no
  ``poly_contact`` (its ``cuts`` cover only inter-metal pairs);
- sg13g2 two level: one metal0->metal1 landing on a deck that publishes no
  ``cuts`` at all.

Every current-entry patch sits on a strip end face, so each fixture's
terminal-pair resistance is the analytic series sum of its strips' sheet
resistance terms plus the per-cut values, hand-computable from the deck
coefficients (the ``_make_two_level_series_layout`` pattern in
``tests/test_extract.py``).
"""

from __future__ import annotations

import klayout.db as kdb


def _draw(top: kdb.Cell, layout: kdb.Layout, layer: int, datatype: int, box):
    top.shapes(layout.layer(layer, datatype)).insert(box)


def _label(top: kdb.Cell, layout: kdb.Layout, layer: int, datatype: int, text, x, y):
    top.shapes(layout.layer(layer, datatype)).insert(kdb.Text(text, kdb.Trans(x, y)))


def make_sky130_mixed_contact_layout() -> kdb.Layout:
    """One sky130 net, poly -licon-> li1 -mcon-> met1 in series (issue #6917).

    Strips (dbu, all y 0..170 = the 0.17 um cut width): poly 0..4000,
    li1 300..4000, met1 300..2000. Cuts: licon 0..170 (poly only: terminal),
    licon 3830..4000 (poly<->li1 landing), mcon 300..470 (li1<->met1
    landing), via 1830..2000 (met1 only: terminal). One cut per landing, so
    the pair resistance is poly 48.2 ohm/sq * 4000/170 + licon + li1
    12.8 ohm/sq * 3700/170 + mcon + met1 0.125 ohm/sq * 1700/170."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw(top, layout, 66, 20, kdb.Box(0, 0, 4000, 170))  # poly.drawing
    _draw(top, layout, 67, 20, kdb.Box(300, 0, 4000, 170))  # li1.drawing
    _draw(top, layout, 68, 20, kdb.Box(300, 0, 2000, 170))  # met1.drawing
    _draw(top, layout, 66, 44, kdb.Box(0, 0, 170, 170))  # licon: poly terminal
    _draw(top, layout, 66, 44, kdb.Box(3830, 0, 4000, 170))  # licon landing
    _draw(top, layout, 67, 44, kdb.Box(300, 0, 470, 170))  # mcon landing
    _draw(top, layout, 68, 44, kdb.Box(1830, 0, 2000, 170))  # via: met1 terminal
    _label(top, layout, 67, 5, "VB", 1000, 85)
    return layout


def make_gf180mcu_poly_contact_layout() -> kdb.Layout:
    """One gf180mcu net: a Poly2 strip contacted up to Metal1 by a single
    `Contact` cut, terminals via Contact cuts that touch only one side
    (Poly2-only at the poly far end, Metal1-only at the Metal1 far end).
    gf180mcu curates no poly-contact resistance, so the poly->metal0
    landing stays `via_resistance_unknown`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw(top, layout, 30, 0, kdb.Box(0, 0, 4000, 300))  # Poly2.drawing
    _draw(top, layout, 34, 0, kdb.Box(3000, 0, 5000, 300))  # Metal1.drawing
    _draw(top, layout, 33, 0, kdb.Box(0, 0, 300, 300))  # Contact: Poly2 terminal
    _draw(top, layout, 33, 0, kdb.Box(3700, 0, 4000, 300))  # Contact landing
    _draw(top, layout, 33, 0, kdb.Box(4700, 0, 5000, 300))  # Contact: M1 terminal
    _label(top, layout, 34, 10, "A", 3200, 150)
    return layout


def make_sg13g2_two_level_layout() -> kdb.Layout:
    """One sg13g2 net: a Metal1 strip joined to a Metal2 strip by a single
    Via1, terminals via a Cont cut that touches only Metal1 and a Via2 cut
    that touches only Metal2. sg13g2 publishes no `cuts`, so the
    metal0->metal1 landing stays `via_resistance_unknown`."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw(top, layout, 8, 0, kdb.Box(0, 0, 4000, 300))  # Metal1.drawing
    _draw(top, layout, 10, 0, kdb.Box(2000, 0, 4000, 300))  # Metal2.drawing
    _draw(top, layout, 6, 0, kdb.Box(0, 0, 300, 300))  # Cont: Metal1 terminal
    _draw(top, layout, 19, 0, kdb.Box(3700, 0, 4000, 300))  # Via1 landing
    _draw(top, layout, 29, 0, kdb.Box(2000, 0, 2300, 300))  # Via2: Metal2 term.
    _label(top, layout, 8, 25, "N", 1000, 150)
    return layout
