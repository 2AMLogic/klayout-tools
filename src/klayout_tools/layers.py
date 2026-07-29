"""Enumerate the layers of a GDSII/OASIS stream.

Pure library: :func:`layers_report` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints. Serialisation and human-readable
formatting live in the CLI command module so this function stays reusable (e.g.
by a future MCP server).

Headless invariant: uses the pip ``klayout`` package's batch database API
(``klayout.db``) only — no GUI, no Qt. Runnable in CI.
"""

from __future__ import annotations

import os
from typing import Any


class LayersError(Exception):
    """Raised when a layout file cannot be read or enumerated.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def layers_report(path: str) -> dict[str, Any]:
    """Enumerate the layers of a GDSII or OASIS stream.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/layers.md``)::

        {
            "file": <path as provided>,
            "dbu_um": <database unit in micrometres, float>,
            "layer_count": <number of layers, int>,
            "layers": [
                {"layer": int, "datatype": int, "name": str | None, "shapes": int},
                ...
            ],
        }

    ``layers`` is sorted by ``(layer, datatype)`` ascending for deterministic
    output. Unnamed layers report ``name: None``. ``shapes`` is the count summed
    across all cell *definitions* (each shape counted once where it is defined,
    not multiplied by instantiation); layers present in the layout's layer table
    but carrying no shapes are still listed with ``shapes: 0``.

    Raises :class:`LayersError` if the file is missing, unreadable, or not a
    recognisable layout stream.
    """
    if not os.path.exists(path):
        raise LayersError(f"file not found: {path}")
    if os.path.isdir(path):
        raise LayersError(f"not a file: {path}")

    # Imported lazily so that `klt --version` and argument parsing do not pay
    # the cost of loading the KLayout database module.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise LayersError(f"could not read layout '{path}': {exc}") from exc

    layers: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        shape_count = sum(
            cell.shapes(layer_index).size() for cell in layout.each_cell()
        )
        layers.append(
            {
                "layer": info.layer,
                "datatype": info.datatype,
                # LayerInfo.name is "" for unnamed layers -> normalise to None.
                "name": info.name if info.name else None,
                "shapes": shape_count,
            }
        )

    layers.sort(key=lambda entry: (entry["layer"], entry["datatype"]))

    return {
        "file": path,
        "dbu_um": layout.dbu,
        "layer_count": len(layers),
        "layers": layers,
    }
