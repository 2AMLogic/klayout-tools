"""Placement layouts and expected deviations for the pinned ASAP7 cell libraries."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import klayout.db as kdb

from klayout_tools.decks.asap7_rules import SHIPPED_CELL_DEVIATION_RECORDS


def place_between_neighbours(
    gds: Path, neighbour: str, out: Path, exclude: frozenset[str] = frozenset()
) -> list[str]:
    """Place every shipped cell not named in ``exclude`` abutted between two
    copies of ``neighbour`` (row cells such as FILLERxp5 or
    TAPCELL_WITH_FILLER carry 54 nm select/well slivers that are only legal
    abutted), one sandwich per 8 um slot. Returns the placed cell names in
    slot order. The source GDS is only read; ``out`` is the new layout."""
    src = kdb.Layout()
    src.read(str(gds))
    out_layout = kdb.Layout()
    out_layout.dbu = src.dbu
    top = out_layout.create_cell("TOP")
    names = sorted(c.name for c in src.top_cells() if c.name not in exclude)
    copies: dict[str, kdb.Cell] = {}
    boundary = src.layer(100, 0)

    def cell(name: str) -> kdb.Cell:
        if name not in copies:
            copies[name] = out_layout.create_cell(name)
            copies[name].copy_tree(src.cell(name))
        return copies[name]

    def width(name: str) -> int:
        return kdb.Region(src.cell(name).begin_shapes_rec(boundary)).bbox().width()

    pitch = int(round(8.0 / src.dbu))
    for i, name in enumerate(names):
        x, y = (i % 12) * pitch, (i // 12) * pitch
        for placed, dx in (
            (neighbour, 0),
            (name, width(neighbour)),
            (neighbour, width(neighbour) + width(name)),
        ):
            top.insert(
                kdb.CellInstArray(
                    cell(placed).cell_index(), kdb.Trans(kdb.Point(x + dx, y))
                )
            )
    out_layout.write(str(out))
    return names


def expected_deviations(suffix: str) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = defaultdict(set)
    for d in SHIPPED_CELL_DEVIATION_RECORDS:
        if suffix in d.libraries:
            expected[d.cell].add(d.rule)
    return dict(expected)
