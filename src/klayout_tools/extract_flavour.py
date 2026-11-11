"""Per-gate MOS-flavour island claiming for ``klt extract`` (issue #5301).

Split out of ``extract.py`` to keep it under its file-size ceiling. A move,
not a rewrite of the claim test; ``extract.py`` re-exports both names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .decks import MOSFlavour

if TYPE_CHECKING:
    import klayout.db as kdb


def _flavour_claims_island(
    island: kdb.Region,
    flavour: MOSFlavour,
    marker_region: kdb.Region,
    and_regions: list[kdb.Region],
    not_regions: list[kdb.Region],
    nwell: kdb.Region,
) -> bool:
    """Whether ``flavour`` claims the active ``island``: it touches the
    flavour's ``marker`` and every ``and_markers`` layer, touches no
    ``not_markers`` layer, and (``nfet_only``) lies outside the well."""
    return (
        not island.interacting(marker_region).is_empty()
        and all(not island.interacting(r).is_empty() for r in and_regions)
        and all(island.interacting(r).is_empty() for r in not_regions)
        and not (flavour.nfet_only and not island.interacting(nwell).is_empty())
    )


def _flavour_split_island(
    island: kdb.Region,
    poly: kdb.Region,
    flavour: MOSFlavour,
    marker_region: kdb.Region,
    and_regions: list[kdb.Region],
    not_regions: list[kdb.Region],
    nwell: kdb.Region,
) -> tuple[kdb.Region, kdb.Region]:
    """Split an active ``island`` into ``(claimed, remaining)`` per *gate*
    (issue #5301). A gate (``poly & island`` component) is claimed by
    ``flavour`` under the :func:`_flavour_claims_island` test applied to the
    gate area, so a marker ending inside diffusion shared with a neighbouring
    gate does not flip that neighbour. Shared source/drain diffusion stays in
    both halves. An island with no gate falls back to the island-level test;
    a wholly covered/uncovered island returns the whole island on one side.
    """
    import klayout.db as kdb

    gates = [kdb.Region(c) for c in (island & poly).merged().each()]
    if not gates:
        if _flavour_claims_island(
            island, flavour, marker_region, and_regions, not_regions, nwell
        ):
            return island, kdb.Region()
        return kdb.Region(), island
    claimed_gates = kdb.Region()
    unclaimed_gates = kdb.Region()
    for gate in gates:
        if _flavour_claims_island(
            gate, flavour, marker_region, and_regions, not_regions, nwell
        ):
            claimed_gates += gate
        else:
            unclaimed_gates += gate
    if unclaimed_gates.is_empty() and not claimed_gates.is_empty():
        return island, kdb.Region()
    if claimed_gates.is_empty():
        return kdb.Region(), island
    return island - unclaimed_gates, island - claimed_gates
