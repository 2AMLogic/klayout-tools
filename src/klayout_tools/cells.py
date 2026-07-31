"""Enumerate the cell hierarchy of a GDSII/OASIS stream.

Pure library: :func:`cells_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints. Serialisation and human-readable
formatting live in the CLI command module so this function stays reusable (e.g.
by a future MCP server).

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only — no GUI, no Qt. Runnable in CI.
"""

from __future__ import annotations

from typing import Any

from ._layout import load_layout


class CellsError(Exception):
    """Raised when a layout file cannot be read or enumerated.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def cells_report(path: str, top: bool = False) -> dict[str, Any]:
    """Enumerate the cell hierarchy of a GDSII or OASIS stream.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/cells.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "dbu_um": <database unit in micrometres, float>,
            "cell_count": <total cells in the whole layout, int>,
            "top_cell_count": <total top cells in the whole layout, int>,
            "cells": [
                {
                    "name": str,
                    "index": int,
                    "is_top": bool,
                    "shapes": int,
                    "instances": int,
                    "children": [str, ...],
                    "parents": [str, ...],
                    "bbox_um": {"left": float, "bottom": float,
                                "right": float, "top": float} | None,
                },
                ...
            ],
        }

    ``cells`` is sorted by ``index`` ascending for deterministic output.
    ``cell_count``/``top_cell_count`` always describe the whole layout,
    regardless of the ``top`` filter. When ``top`` is true, ``cells`` is
    narrowed to entries with ``is_top: True`` only.

    Field semantics:

    - ``shapes`` is the count of shapes owned by this cell's own definition,
      summed across all layers — not multiplied by instantiation (same
      convention as ``klt layers``' ``shapes`` field).
    - ``instances`` is the count of child-instance *placement records*
      (``Cell.each_inst()``) — an arrayed instance counts as one placement
      record, not its expanded row-by-column total.
    - ``children``/``parents`` are deduplicated, sorted cell names one level
      of instantiation away (direct only, not transitive).
    - ``bbox_um`` is ``None`` when the cell (including its children) has no
      geometry at all, rather than a degenerate/inverted box.

    Raises :class:`CellsError` if the file is missing, unreadable, or not a
    recognisable layout stream.
    """
    layout = load_layout(path, CellsError)

    top_cell_indices = {cell.cell_index() for cell in layout.top_cells()}

    cells: list[dict[str, Any]] = []
    for cell in layout.each_cell():
        index = cell.cell_index()
        shape_count = sum(
            cell.shapes(layer_index).size() for layer_index in layout.layer_indexes()
        )
        instance_count = sum(1 for _ in cell.each_inst())
        children = sorted(
            {layout.cell(child_index).name for child_index in cell.each_child_cell()}
        )
        parents = sorted(
            {layout.cell(parent_index).name for parent_index in cell.each_parent_cell()}
        )
        bbox = cell.dbbox()
        bbox_um = (
            None
            if bbox.empty()
            else {
                "left": bbox.left,
                "bottom": bbox.bottom,
                "right": bbox.right,
                "top": bbox.top,
            }
        )

        cells.append(
            {
                "name": cell.name,
                "index": index,
                "is_top": index in top_cell_indices,
                "shapes": shape_count,
                "instances": instance_count,
                "children": children,
                "parents": parents,
                "bbox_um": bbox_um,
            }
        )

    cells.sort(key=lambda entry: entry["index"])

    cell_count = len(cells)
    top_cell_count = len(top_cell_indices)

    if top:
        cells = [entry for entry in cells if entry["is_top"]]

    return {
        "schema_version": 1,
        "file": path,
        "dbu_um": layout.dbu,
        "cell_count": cell_count,
        "top_cell_count": top_cell_count,
        "cells": cells,
    }
