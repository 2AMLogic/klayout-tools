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
from ._layout import bbox_um_dict, cells_in_hierarchy, load_layout


class LayersError(Exception):
    """Raised when a layout file cannot be read or enumerated.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def _flattened_layer_stats(
    roots: list[Any], layer_index: int, dbu: float, *, include_text: bool
) -> dict[str, Any]:
    """Walk every flattened (instance-placed) shape on ``layer_index`` under
    each of ``roots``, via a raw ``RecursiveShapeIterator``
    (``Cell.begin_shapes_rec``) -- not the ``Region``/``Texts`` conversion
    shortcut used elsewhere in the repo, because per-shape cell attribution
    (``it.cell()``) is only available on the raw iterator, and that
    attribution is what makes the ``contributors`` counts instance-weighted
    and correctly named per source cell (see the issue #675 implementation
    note on why ``socket_check.py``'s ``_collect_texts`` cannot be reused
    as-is here).

    Returns the flattened fields to merge into a ``layers[]`` entry:
    ``flattened_shapes``, ``bbox_um`` (transform-aware physical extents), and
    ``contributors`` (list of ``{"cell", "shapes"}``, instance-weighted,
    sorted by cell name). When ``include_text`` is set, also returns
    ``text_count`` and ``texts`` (list of ``{"text", "count"}``, sorted by
    text) -- gated behind the flag because collecting per-string census data
    is extra work callers who only want geometry counts shouldn't pay for.
    """
    import klayout.db as kdb

    flattened_count = 0
    bbox = kdb.Box()
    contributors: dict[str, int] = {}
    text_counts: dict[str, int] = {}

    for root in roots:
        it = root.begin_shapes_rec(layer_index)
        while not it.at_end():
            shape = it.shape()
            trans = it.trans()
            flattened_count += 1
            bbox += shape.bbox().transformed(trans)
            cell_name = it.cell().name
            contributors[cell_name] = contributors.get(cell_name, 0) + 1
            if include_text and shape.is_text():
                text = shape.text.string
                text_counts[text] = text_counts.get(text, 0) + 1
            it.next()

    result: dict[str, Any] = {
        "flattened_shapes": flattened_count,
        "bbox_um": bbox_um_dict(bbox, dbu),
        "contributors": [
            {"cell": name, "shapes": count}
            for name, count in sorted(contributors.items())
        ],
    }
    if include_text:
        result["text_count"] = sum(text_counts.values())
        result["texts"] = [
            {"text": text, "count": count}
            for text, count in sorted(text_counts.items())
        ]
    return result


def layers_report(
    path: str,
    top: str | None = None,
    flattened: bool = False,
    include_text: bool = False,
) -> dict[str, Any]:
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
                    # present only when flattened=True:
                    "flattened_shapes": int,
                    "bbox_um": {"left": .., "bottom": .., "right": .., "top": ..,
                                "width": .., "height": ..},
                    "contributors": [{"cell": str, "shapes": int}, ...],
                    # present only when flattened=True and include_text=True:
                    "text_count": int,
                    "texts": [{"text": str, "count": int}, ...],
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.
    Everything below is purely additive to the schema documented before
    issue #675, so ``schema_version`` stays ``1``.

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
    raised. ``top`` also selects the flattening root when ``flattened=True``
    (see below).

    ``flattened`` (issue #675, opt-in, default ``False`` -- default output is
    byte-identical to before this flag existed) additionally reports, per
    layer, the *instantiated* layout content rather than library-definition
    counts:

    - ``flattened_shapes`` -- every physical placement of a shape on this
      layer, walked via ``Cell.begin_shapes_rec`` (KLayout's hierarchy- and
      transform-aware recursive shape iterator). Unlike ``shapes`` (each
      shape counted once where it is *defined*), this scales with
      instantiation: a cell drawn once but instanced 10 times with a shape
      on this layer contributes 10, not 1.
    - ``bbox_um`` -- the physical bounding box of every flattened shape on
      this layer, in micrometres, with each instance's transform (rotation,
      mirroring, displacement, array placement) applied before the union is
      taken. An all-zero box when the layer has no flattened shapes, same
      convention as ``klt stats``' ``bbox_um``.
    - ``contributors`` -- every cell *definition* that owns at least one
      shape reached during the flattened walk, each with an
      instance-weighted count (a shape drawn once in a cell instantiated
      ``N`` times contributes ``N`` to that cell's ``shapes`` total here) --
      sorted by cell name for deterministic output.

    Without ``top``, the flattening root is every top cell in the stream (so
    ``flattened_shapes`` sums the actual instantiated content of the whole
    library); with ``top``, it is just that one named cell's own
    sub-hierarchy.

    ``include_text`` (issue #675, requires ``flattened=True``, default
    ``False``) additionally reports, per layer:

    - ``text_count`` -- total flattened text-label occurrences on this layer
      (each placement of a text shape counted once, same instance-weighting
      as ``flattened_shapes``).
    - ``texts`` -- the distinct text strings that occur, each with its own
      flattened occurrence count, sorted by string for deterministic output.

    Raises :class:`LayersError` if the file is missing, unreadable, not a
    recognisable layout stream, ``top`` names a cell absent from the stream,
    or ``include_text=True`` is given without ``flattened=True``.
    """
    if include_text and not flattened:
        raise LayersError("--include-text requires --flattened")

    layout = load_layout(path, LayersError)

    if top is not None:
        top_cell = layout.cell(top)
        if top_cell is None:
            raise LayersError(f"top cell not found in stream: {top}")
        scope_cells: list[Any] = cells_in_hierarchy(layout, top_cell)
        flatten_roots: list[Any] = [top_cell]
    else:
        scope_cells = list(layout.each_cell())
        flatten_roots = list(layout.top_cells())

    layers: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        shape_count = sum(cell.shapes(layer_index).size() for cell in scope_cells)
        entry: dict[str, Any] = {
            "layer": info.layer,
            "datatype": info.datatype,
            # LayerInfo.name is "" for unnamed layers -> normalise to None.
            "name": info.name if info.name else None,
            "shapes": shape_count,
            "annotation": is_reserved_annotation_layer(info.layer, info.datatype),
        }
        if flattened:
            entry.update(
                _flattened_layer_stats(
                    flatten_roots,
                    layer_index,
                    layout.dbu,
                    include_text=include_text,
                )
            )
        layers.append(entry)

    layers.sort(key=lambda entry: (entry["layer"], entry["datatype"]))

    return {
        "schema_version": 1,
        "file": path,
        "dbu_um": layout.dbu,
        "layer_count": len(layers),
        "layers": layers,
    }
