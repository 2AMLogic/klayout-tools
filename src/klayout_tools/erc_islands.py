"""Isolation-island partition shared by ``klt extract`` and ``klt erc``.

Neutral home so both engines number and classify deep-well isolation islands
with the same code (design note ``docs/design/erc-substrate-continuity.md``
section 5). Pure functions over ``klayout.db`` regions; no deck or extraction
dependency.
"""

from __future__ import annotations

import klayout.db as kdb


def sorted_islands(isolation_region: kdb.Region) -> list[kdb.Region]:
    """Connected components of ``isolation_region``, sorted by bounding box.

    The order is stable across runs (raw ``Region`` iteration order is not a
    documented guarantee), so ``vsubs_iso<n>`` numbering agrees everywhere.
    """
    return sorted(
        (kdb.Region(component) for component in isolation_region.merged().each()),
        key=lambda region: (
            region.bbox().left,
            region.bbox().bottom,
            region.bbox().right,
            region.bbox().top,
        ),
    )


def partition_region_by_islands(
    region: kdb.Region, islands: list[kdb.Region]
) -> tuple[kdb.Region, list[kdb.Region]]:
    """Split ``region``'s own connected components into an "outside every
    island" remainder plus one bucket per entry in ``islands`` (issue #1128,
    see ``ExtractionDeck.substrate_isolation``'s docstring) -- the same "any
    overlap claims the whole connected component" idiom ``mos_flavours``
    marker classification already uses in :func:`_extract_netlist` (a
    component cannot legally straddle two distinct isolation islands any
    more than a transistor can legally straddle a voltage-domain marker
    boundary, so whichever island is tested first for an overlapping
    component wins).

    Returns ``(region, [])`` -- ``region`` itself, completely unmodified,
    with no connected-component walk at all -- when ``islands`` is empty.
    This is the overwhelmingly common case (a deck that declares no
    ``substrate_isolation``, or a layout that draws no shapes on one that is
    declared) and is deliberately a fast, no-op path: skipping the walk
    means the returned region is the exact same object passed in, not a
    reassembled-from-merged-components copy that could fracture polygons
    differently, so every caller stays byte-for-byte identical to the
    pre-#1128 code path in that case.
    """
    if not islands:
        return region, []
    outside = kdb.Region()
    per_island: list[kdb.Region] = [kdb.Region() for _ in islands]
    for component in region.merged().each():
        component_region = kdb.Region(component)
        for index, island in enumerate(islands):
            if not component_region.interacting(island).is_empty():
                per_island[index] += component_region
                break
        else:
            outside += component_region
    return outside, per_island
