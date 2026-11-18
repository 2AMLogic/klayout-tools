"""Marker-scoped MOS-capacitor recognition for ``klt extract`` (issues #2919,
#5893).

Split out of ``extract.py`` to keep it under its file-size ceiling. A move of
``_mos_capacitor_entry_index`` and ``_resolve_mos_capacitors`` -- the claim
test and the recognition walk are unchanged -- except that the issue #5893
candidate-restricted claim scan is its own helper
(``_claim_mos_capacitor_islands``) so ``_resolve_mos_capacitors`` stays under
the complexity ceiling. It lives in its own module (not ``extract_flavour``)
because both ``extract.py`` and ``extract_detect_markers.py`` call it, and the
latter imports ``extract_flavour`` at module level already.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._layout import region as _region

if TYPE_CHECKING:
    from collections.abc import Callable

    import klayout.db as kdb

    from .decks import ExtractionDeck, MosCapacitorDevice


def _mos_capacitor_entry_index(
    island: kdb.Region,
    deck: ExtractionDeck,
    poly: kdb.Region,
    nwell: kdb.Region,
    region_of: Callable[[tuple[int, int]], kdb.Region],
) -> int | None:
    """Index into ``deck.mos_capacitors`` of the first entry that claims
    ``island`` (see :class:`~klayout_tools.decks.MosCapacitorDevice` for the
    tests), or ``None`` when none does."""
    in_well = not island.interacting(nwell).is_empty()

    def touches(layer: tuple[int, int]) -> bool:
        return not island.interacting(region_of(layer)).is_empty()

    for index, entry in enumerate(deck.mos_capacitors):
        if (
            entry.in_well == in_well
            and touches(entry.marker)
            and touches(entry.implant)
            and all(touches(layer) for layer in entry.requires)
            and not any(touches(layer) for layer in entry.excludes)
        ):
            return index
    return None


def _claim_mos_capacitor_islands(
    merged_islands: kdb.Region,
    deck: ExtractionDeck,
    poly: kdb.Region,
    nwell: kdb.Region,
    region_of: Callable[[tuple[int, int]], kdb.Region],
) -> dict[int, kdb.Region]:
    """Claim-scan over *candidate* islands only (issue #5893).

    A claim requires the island to touch the entry's marker layer
    (``_mos_capacitor_entry_index``'s ``touches(entry.marker)``), so only the
    merged islands that interact with the union of the cap markers can ever
    be claimed. On a typical digital block (gf180mcu, no MOS caps drawn) the
    marker union is empty and the scan collapses to nothing, where the
    pre-#5893 full-island walk cost ~12 of the ~12.3 wall minutes on a 166 KB
    ttgf0p2 block. Returns ``{entry index: union of the islands it claimed}``.
    """
    import klayout.db as kdb

    claimed: dict[int, kdb.Region] = {}
    cap_markers = kdb.Region()
    for entry in deck.mos_capacitors:
        cap_markers += region_of(entry.marker)
    if cap_markers.is_empty():
        return claimed
    for component in merged_islands.interacting(cap_markers).each():
        island = kdb.Region(component)
        if (island & poly).is_empty():
            continue
        chosen = _mos_capacitor_entry_index(island, deck, poly, nwell, region_of)
        if chosen is not None:
            claimed[chosen] = claimed.get(chosen, kdb.Region()) + island
    return claimed


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

    Each connected component ("island") of ``active`` that carries a poly
    gate is offered to ``deck.mos_capacitors`` in declaration order; the
    first entry whose marker/implant/well/``requires``/``excludes`` tests all
    hold (see :class:`~klayout_tools.decks.MosCapacitorDevice`) claims it. A
    claimed island is removed from the returned ``active`` so the MOS
    source/drain/gate split never sees it -- the PDK likewise excludes
    ``mos_cap_mk`` from its MOS derivations -- and a capacitor with no
    claiming entry stays an ordinary MOS device (the unsupported-marker
    residue still reports it).

    The walk visits only the islands that interact with the union of the
    entries' marker layers: a claim requires ``touches(entry.marker)``, so
    islands outside them can only return ``None`` (issue #5893 -- the
    unrestricted walk was ~95% of wall time on marker-free digital blocks).
    ``active_without_capacitors`` is computed as the full island set minus
    the recognised union, so it is exactly what the unfiltered walk built.

    Returns ``(capacitors, active_without_capacitors, recognised, dropped)``:
    ``capacitors`` is one ``(entry, gate, source_drain)`` triple per entry
    that claimed at least one island (``gate`` = ``island & poly``,
    ``source_drain`` = ``island - poly``, both minus the ``dummy`` marker for
    the gate), ``recognised`` is the union of the claimed islands, and
    ``dropped`` counts gate components fully covered by ``dummy``. Everything
    is empty/``0`` for a deck declaring no ``mos_capacitors``.
    """
    import klayout.db as kdb

    if not deck.mos_capacitors or active.is_empty():
        return [], active, kdb.Region(), 0
    layers: dict[tuple[int, int], kdb.Region] = {}

    def region_of(layer: tuple[int, int]) -> kdb.Region:
        if layer not in layers:
            layers[layer] = _region(layout, top_cell, layer)
        return layers[layer]

    merged_islands = active.merged()
    claimed = _claim_mos_capacitor_islands(merged_islands, deck, poly, nwell, region_of)

    capacitors: list[tuple[MosCapacitorDevice, kdb.Region, kdb.Region]] = []
    recognised = kdb.Region()
    dropped = 0
    for index in sorted(claimed):
        island_region = claimed[index]
        recognised += island_region
        gate = island_region & poly
        if not dummy.is_empty():
            for gate_component in gate.merged().each():
                if (kdb.Region(gate_component) - dummy).is_empty():
                    dropped += 1
            gate = gate - dummy
        capacitors.append((deck.mos_capacitors[index], gate, island_region - poly))
    # Islands that no entry claimed stay ordinary MOS devices; computing them
    # as the set difference (instead of accumulating during the scan) is what
    # lets the scan itself be candidate-restricted. The components are already
    # maximal merges, so nothing re-merges across the boundary.
    return capacitors, merged_islands - recognised, recognised, dropped
