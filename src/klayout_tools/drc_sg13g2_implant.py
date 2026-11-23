"""Coverage of the drawn source/drain implant used by SG13G2 well rules."""

from __future__ import annotations

from typing import Any


def implant_coverage(setup: Any) -> dict[str, list[dict[str, str]]]:
    """Identify selected cells with unimplanted gate-crossed active inside NWell."""
    if setup.deck_name != "sg13g2":
        return {}

    import klayout.db as kdb

    def region(cell: Any, layer: int) -> Any:
        index = setup.layout.find_layer(layer, 0)
        return (
            kdb.Region() if index is None else kdb.Region(cell.begin_shapes_rec(index))
        )

    warnings = []
    for cell in setup.top_cells:
        # Tiling replaces the selected cell with a synthetic array. Report the
        # original cell, whose device mask is independent of the tile neighbours.
        if cell.name in setup.tile_windows:
            cell = setup.layout.cell(setup.tile_windows[cell.name][0])
        active = region(cell, 1) & region(cell, 31)
        gates = region(cell, 5)
        diffusion = active.overlapping(gates) - gates
        if diffusion.is_empty():
            continue
        if not (diffusion - region(cell, 14) - region(cell, 7)).is_empty():
            warnings.append(
                {
                    "cell": cell.name,
                    "description": "Gate-crossed Activ inside NWell has source/drain "
                    "area without pSD (14/0) or nSD (7/0). Check the PMOS implant. "
                    "Curated well enclosure checks use the drawn implants.",
                }
            )
    return (
        {"implant_warnings": sorted(warnings, key=lambda item: item["cell"])}
        if warnings
        else {}
    )
