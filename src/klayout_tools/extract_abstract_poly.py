"""Physical poly terminal connections across abstracted cell boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import klayout.db as kdb

if TYPE_CHECKING:
    from .decks.extraction import ExtractionDeck


def terminal_poly(
    layout: kdb.Layout,
    deck: ExtractionDeck,
    instances: list[tuple[int, kdb.ICplxTrans]],
) -> kdb.Region:
    """Capture each placed poly component identified by a leaf's own pin text."""
    cover = kdb.Region()
    if deck.poly_label is None:
        return cover
    label_layer = layout.find_layer(*deck.poly_label)
    poly_layer = layout.find_layer(*deck.poly)
    if label_layer is None or poly_layer is None:
        return cover
    by_cell: dict[int, kdb.Region] = {}
    for index, transform in instances:
        if index not in by_cell:
            cell = layout.cell(index)
            polys = kdb.Region(cell.begin_shapes_rec(poly_layer)).merged()
            by_cell[index] = polys.interacting(kdb.Texts(cell.shapes(label_layer)))
        cover += by_cell[index].transformed(transform)
    # Keep individual leaf polygons for the count of contributors at each seam.
    cover.merged_semantics = False
    return cover


def connect_terminal_poly(
    l2n: kdb.LayoutToNetlist,
    cover: kdb.Region | None,
    poly: kdb.Region,
    contact: kdb.Region,
) -> list[tuple[str, kdb.Region]]:
    """Connect actual terminal polygons that reach a conductor or another terminal."""
    if cover is None or cover.is_empty():
        return []
    islands = cover.merged()
    retained = (
        islands.interacting(cover, 2) + islands.interacting(poly + contact)
    ).merged()
    if not retained.is_empty():
        # A separate conductor keeps this material out of device recognition.
        l2n.register(retained, "abstract_terminal_poly")
        l2n.connect(retained)
        l2n.connect(retained, poly)
        l2n.connect(retained, contact)
        return [("poly", retained)]
    return []
