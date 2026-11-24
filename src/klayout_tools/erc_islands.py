"""Isolation-island partition shared by ``klt extract`` and ``klt erc``.

Neutral home so both engines number and classify deep-well isolation islands
with the same code (design note ``docs/design/erc-substrate-continuity.md``
section 5). Pure functions over ``klayout.db`` regions; no deck or extraction
dependency.
"""

from __future__ import annotations

import klayout.db as kdb


def is_ring_closed(island: kdb.Region, ring_region: kdb.Region) -> bool:
    """Whether ``ring_region`` (an n-well) closes ``island``'s whole outline.

    A deep-n-well only isolates the p-well above it when an n-well ring
    joins it all the way round. The island's outer outline (holes ignored)
    must be covered by ``ring_region`` on its outside band (a ring abutting
    or enclosing the island) or on its inside band (a ring drawn overlapping
    the island edge); each band is one database unit wide.
    """
    hull = kdb.Region()
    for polygon in island.each():
        hull.insert(kdb.Polygon(list(polygon.each_point_hull())))
    hull = hull.merged()
    if hull.is_empty():
        return False
    outside_band = hull.sized(1) - hull
    inside_band = hull - hull.sized(-1)
    return (outside_band - ring_region).is_empty() or (
        inside_band - ring_region
    ).is_empty()


def sorted_islands(
    isolation_region: kdb.Region, ring_region: kdb.Region | None = None
) -> list[kdb.Region]:
    """Connected components of ``isolation_region``, sorted by bounding box.

    The order is stable across runs (raw ``Region`` iteration order is not a
    documented guarantee), so ``vsubs_iso<n>`` numbering agrees everywhere.

    When ``ring_region`` is given, only islands closed by it (see
    :func:`is_ring_closed`) are returned: an unclosed deep-n-well does not
    isolate its p-well, so that geometry stays on the shared substrate
    rather than hiding an ``erc.supply_short``. ``None`` keeps every island.
    """
    components = (
        kdb.Region(component) for component in isolation_region.merged().each()
    )
    if ring_region is not None:
        components = (c for c in components if is_ring_closed(c, ring_region))
    return sorted(
        components,
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
