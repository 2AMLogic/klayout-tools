"""Marker-scoped MOS capacitor recognition shared by extraction and diagnostics."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from ._layout import region as _region

if TYPE_CHECKING:
    from collections.abc import Callable

    import klayout.db as kdb

    from .decks import ExtractionDeck, MosCapacitorDevice


def _claim_mos_capacitor_islands(
    islands: kdb.Region,
    deck: ExtractionDeck,
    poly: kdb.Region,
    nwell: kdb.Region,
    region_of: Callable[[tuple[int, int]], kdb.Region],
) -> tuple[dict[int, kdb.Region], kdb.Region]:
    """Claim marker-bearing islands in deck order with one region query per layer."""
    import klayout.db as kdb

    if not any(not region_of(entry.marker).is_empty() for entry in deck.mos_capacitors):
        return {}, kdb.Region()
    gated = islands.interacting(poly & islands)
    in_well = islands.interacting(nwell)
    outside_well = islands - in_well

    @functools.cache
    def touching(layer: tuple[int, int]) -> kdb.Region:
        # Memoized per layer (#6527): the entries share their marker/implant/
        # requires/excludes layers. An empty layer claims nothing (#5807).
        region = region_of(layer)
        return kdb.Region() if region.is_empty() else islands.interacting(region)

    claimed: dict[int, kdb.Region] = {}
    taken = kdb.Region()
    for index, entry in enumerate(deck.mos_capacitors):
        if region_of(entry.marker).is_empty():
            continue  # no marker shapes: this entry can claim no island
        candidates = gated & touching(entry.marker) & touching(entry.implant)
        for layer in entry.requires:
            candidates &= touching(layer)
        for layer in entry.excludes:
            candidates -= touching(layer)
        candidates &= (in_well if entry.in_well else outside_well) - taken
        if not candidates.is_empty():
            claimed[index] = candidates
            taken += candidates
    return claimed, taken


def _resolve_mos_capacitors(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    active: kdb.Region,
    poly: kdb.Region,
    nwell: kdb.Region,
    dummy: kdb.Region,
) -> tuple[
    list[tuple[MosCapacitorDevice, kdb.Region, kdb.Region]],
    kdb.Region,
    kdb.Region,
    int,
]:
    """Recognise the deck's marker-scoped MOS capacitors (issue #2919).

    Each connected component ("island") of ``active`` that carries a poly gate
    is offered to ``deck.mos_capacitors`` in declaration order; the first entry
    whose marker/implant/well/``requires``/``excludes`` tests all hold (see
    :class:`~klayout_tools.decks.MosCapacitorDevice`) claims it. A claimed
    island is removed from the returned ``active`` so neither the MOS
    source/drain/gate split nor the PDK's own ``mos_cap_mk``-excluding
    derivations see it; one with no claiming entry stays an ordinary MOS
    device (the unsupported-marker residue still reports it).

    Returns ``(capacitors, active_without_capacitors, recognised, dropped)``:
    ``capacitors`` is one ``(entry, gate, source_drain)`` triple per claiming
    entry (``gate`` = ``island & poly`` minus ``dummy``, ``source_drain`` =
    ``island - poly``), ``recognised`` the union of the claimed islands, and
    ``dropped`` the count of gate components ``dummy`` fully covers. A deck
    declaring no ``mos_capacitors`` returns empty/``0`` results.

    Claim sets are resolved with bulk region algebra, not a per-island loop
    (issue #5807): one ``interacting()`` per layer over the merged island
    set, memoized per layer (issue #6527), first-claim-wins via the ordered sweep.
    """
    import klayout.db as kdb

    if not deck.mos_capacitors or active.is_empty():
        return [], active, kdb.Region(), 0
    layers: dict[tuple[int, int], kdb.Region] = {}

    def region_of(layer: tuple[int, int]) -> kdb.Region:
        if layer not in layers:
            layers[layer] = _region(layout, top_cell, layer)
        return layers[layer]

    islands = active.merged()
    claimed, taken = _claim_mos_capacitor_islands(islands, deck, poly, nwell, region_of)

    capacitors: list[tuple[MosCapacitorDevice, kdb.Region, kdb.Region]] = []
    recognised = kdb.Region()
    dropped = 0
    for index, island_region in sorted(claimed.items()):
        recognised += island_region
        gate = island_region & poly
        if not dummy.is_empty():
            for gate_component in gate.merged().each():
                if (kdb.Region(gate_component) - dummy).is_empty():
                    dropped += 1
            gate = gate - dummy
        capacitors.append((deck.mos_capacitors[index], gate, island_region - poly))
    return capacitors, islands - taken, recognised, dropped
