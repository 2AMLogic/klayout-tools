"""Enumerate the layers of a GDSII/OASIS stream.

Pure library: :func:`layers_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints. Serialisation and human-readable
formatting live in the CLI command module so this function stays reusable (e.g.
by a future MCP server).

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only — no GUI, no Qt. Runnable in CI.
"""

from __future__ import annotations

from typing import Any

from ._annotation import is_reserved_annotation_layer
from ._layout import cells_in_hierarchy, load_layout


class LayersError(Exception):
    """Raised when a layout file cannot be read or enumerated.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def layers_report(path: str, top: str | None = None) -> dict[str, Any]:
    """Enumerate the layers of a GDSII or OASIS stream.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/layers.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "dbu_um": <database unit in micrometres, float>,
            "layer_count": <number of layers, int>,
            "layers": [
                {
                    "layer": int,
                    "datatype": int,
                    "name": str | None,
                    "shapes": int,
                    "annotation": bool,
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.

    ``layers`` is sorted by ``(layer, datatype)`` ascending for deterministic
    output. Unnamed layers report ``name: None``. ``shapes`` is the count summed
    across all cell *definitions* (each shape counted once where it is defined,
    not multiplied by instantiation); layers present in the layout's layer table
    but carrying no shapes are still listed with ``shapes: 0``. ``annotation``
    is ``True`` when the layer number falls in the reserved annotation-layer
    range (GDS layers 990-999, any datatype -- see
    ``docs/cli/drc.md`` -> "Reserved annotation layer"), so a reader can tell
    "this is a floorplan reservation" from "this is unrecognised real
    geometry" without opening the source that generated the GDS.

    ``top`` (issue #554) restricts the per-layer ``shapes`` counts to one
    named top cell's own hierarchy -- itself plus every cell it calls,
    directly or indirectly (see
    :func:`klayout_tools._layout.cells_in_hierarchy`) -- instead of every
    cell definition in the stream. Optional: when omitted, today's
    whole-stream union over every top cell is unchanged; when given, it must
    name a cell present in the stream (like ``klt extract --top``, not
    validated to actually be parent-less), else :class:`LayersError` is
    raised.

    Raises :class:`LayersError` if the file is missing, unreadable, not a
    recognisable layout stream, or ``top`` names a cell absent from the
    stream.
    """
    layout = load_layout(path, LayersError)

    if top is not None:
        top_cell = layout.cell(top)
        if top_cell is None:
            raise LayersError(f"top cell not found in stream: {top}")
        scope_cells: list[Any] = cells_in_hierarchy(layout, top_cell)
    else:
        scope_cells = list(layout.each_cell())

    layers: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        shape_count = sum(cell.shapes(layer_index).size() for cell in scope_cells)
        layers.append(
            {
                "layer": info.layer,
                "datatype": info.datatype,
                # LayerInfo.name is "" for unnamed layers -> normalise to None.
                "name": info.name if info.name else None,
                "shapes": shape_count,
                "annotation": is_reserved_annotation_layer(info.layer, info.datatype),
            }
        )

    layers.sort(key=lambda entry: (entry["layer"], entry["datatype"]))

    return {
        "schema_version": 1,
        "file": path,
        "dbu_um": layout.dbu,
        "layer_count": len(layers),
        "layers": layers,
    }
