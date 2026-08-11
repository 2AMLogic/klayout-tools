"""Shared layout-loading/writing preamble for ``klt`` library modules.

Every ``*_report``/``run_*`` entry point that reads a GDSII/OASIS stream via
``klayout.db`` (``layers.py``, ``stats.py``, ``cells.py``, ``drc.py``) starts
with the same three checks: does the path exist, is it a file (not a
directory), and does ``klayout.db`` accept it as a recognisable stream. This
module is the single place that logic lives, so a future fix (e.g. a better
read-error message) only needs to land once.

Every generator/drawing entry point that *writes* a stream (``gen.py``,
``gen_compose.py``, ``draw.py``) shares the same requirement: output must be
byte-reproducible across runs with identical inputs, so a generated GDS can
be committed as a golden artifact or used as a cache key (#320). KLayout's
default ``Layout.write(path)`` overload stamps the GDS2 ``BGNLIB``/``BGNSTR``
records with the current wall-clock time, which breaks that. :func:`write_layout`
is the single place that disables it, via ``SaveLayoutOptions.gds2_write_timestamps
= False`` (a no-op for non-GDS2 formats).

The per-verb exception classes (``LayersError``, ``StatsError``, ...) stay in
their own modules -- they are importable API and let callers write
verb-specific ``except`` clauses -- and are passed in here as ``error_cls``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import klayout.db as kdb


def load_layout(path: str, error_cls: type[Exception]) -> kdb.Layout:
    """Validate ``path`` and read it into a fresh ``klayout.db.Layout``.

    KLayout auto-detects the stream format on read, so both ``.gds`` and
    ``.oas`` inputs are handled by the same code path.

    Raises ``error_cls`` (constructed with a single message string) if the
    file is missing, is a directory, or is not a recognisable layout stream.
    """
    if not os.path.exists(path):
        raise error_cls(f"file not found: {path}")
    if os.path.isdir(path):
        raise error_cls(f"not a file: {path}")

    # Imported lazily so that `klt --version` and argument parsing do not pay
    # the cost of loading the KLayout database module.
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(path)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise error_cls(f"could not read layout '{path}': {exc}") from exc

    return layout


def select_top_cells(
    layout: kdb.Layout, top: str | None, error_cls: type[Exception]
) -> list[kdb.Cell]:
    """Return the top cells a verb should operate on, honouring an optional
    ``top`` filter (issue #554).

    With ``top`` unset, every top cell in the stream is returned -- today's
    default, whole-stream behaviour for verbs that can sensibly check/render
    more than one top cell at once (``klt layers``, ``klt drc``, ``klt
    precheck``, ``klt render``, ``klt socket-check``). With ``top`` set, only
    that named cell is returned -- and it must exist, else ``error_cls`` is
    raised with ``"top cell not found in stream: <top>"`` (a request the
    caller can fix, not a silent empty result).

    This is the "optional scope, default to every top cell" pattern
    originally implemented by ``ring_check.py``'s own ``_select_top_cells``;
    factored out here so the six verbs added in #554 share one
    implementation instead of copying it a sixth time.
    """
    if top is None:
        return list(layout.top_cells())

    cell = layout.cell(top)
    if cell is None:
        raise error_cls(f"top cell not found in stream: {top}")
    return [cell]


def cells_in_hierarchy(layout: kdb.Layout, top_cell: kdb.Cell) -> list[kdb.Cell]:
    """Every cell *definition* reachable from ``top_cell``: itself plus every
    cell it calls, directly or indirectly (``Cell.called_cells()``).

    Whole-layout shape-accumulation passes (``stats.py``'s ``_accumulate``,
    ``layers.py``'s per-layer shape counts, ...) iterate ``Layout.each_cell()``
    by default -- every cell definition in the *entire* stream, regardless of
    which top cell a caller asked about. When a caller passes ``--top`` to
    scope such a verb to one cell, restricting iteration to this function's
    return value (rather than leaving it at ``each_cell()``) is what actually
    re-scopes the count -- picking a top cell for the bounding box alone
    would leave a self-inconsistent report: a bbox scoped to one cell, but
    area/shape counts still summed across the whole library (issue #554).
    """
    return [top_cell] + [layout.cell(idx) for idx in top_cell.called_cells()]


def bbox_um_dict(box: kdb.Box, dbu: float) -> dict[str, float]:
    """Convert a ``kdb.Box`` to a JSON-serialisable ``left``/``bottom``/
    ``right``/``top``/``width``/``height`` dict, scaled from database units
    to micrometres by ``dbu``.

    An empty box (no shapes contributed to it -- e.g. a layer with zero
    flattened shapes) reports an all-zero dict rather than KLayout's raw
    empty-box sentinel coordinates, which are not meaningful bounds.

    Shared by ``stats.py`` and ``layers.py``, which both report per-scope
    bounding boxes in this exact shape (issue #691).
    """
    if box.empty():
        return {
            "left": 0.0,
            "bottom": 0.0,
            "right": 0.0,
            "top": 0.0,
            "width": 0.0,
            "height": 0.0,
        }
    return {
        "left": box.left * dbu,
        "bottom": box.bottom * dbu,
        "right": box.right * dbu,
        "top": box.top * dbu,
        "width": box.width() * dbu,
        "height": box.height() * dbu,
    }


def region(
    layout: kdb.Layout, cell: kdb.Cell, layer: tuple[int, int] | None
) -> kdb.Region:
    """A flattened ``Region`` for ``layer`` under ``cell`` (same flattening
    idiom ``drc.py`` uses via ``begin_shapes_rec``), or an empty ``Region``
    when ``layer`` is ``None``/absent from the stream.

    Shared by ``extract.py`` and ``components.py``, which both flatten a
    ``(layer, datatype)`` into a ``Region`` this exact way (issue #699).
    """
    # Imported lazily, matching load_layout()'s lazy `klayout.db` import.
    import klayout.db as kdb

    if layer is None:
        return kdb.Region()
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Region()
    return kdb.Region(cell.begin_shapes_rec(layer_index))


def texts(
    layout: kdb.Layout, cell: kdb.Cell, layer: tuple[int, int] | None
) -> kdb.Texts:
    """A flattened ``Texts`` collection for ``layer`` under ``cell``, or empty
    when ``layer`` is ``None``/absent from the stream.

    Shared by ``extract.py`` and ``components.py`` (issue #699); see
    :func:`region`.
    """
    # Imported lazily, matching load_layout()'s lazy `klayout.db` import.
    import klayout.db as kdb

    if layer is None:
        return kdb.Texts()
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Texts()
    return kdb.Texts(cell.begin_shapes_rec(layer_index))


def write_layout(layout: kdb.Layout, path: str, error_cls: type[Exception]) -> None:
    """Write ``layout`` to ``path`` with deterministic (reproducible-build) output.

    KLayout's zero-arg ``Layout.write(path)`` overload defaults to stamping
    the GDS2 ``BGNLIB``/``BGNSTR`` timestamp records with the current
    wall-clock time, so two runs of the same generator with identical inputs
    produce byte-different streams (#320). This helper always passes a
    ``SaveLayoutOptions`` with ``gds2_write_timestamps = False`` instead, so
    those fields are written as zero -- the usual convention for generated
    GDSII, and a no-op for non-GDS2 output formats (e.g. OASIS).

    Raises ``error_cls`` (constructed with a single message string) if the
    write fails (e.g. an unwritable path or unrecognised format).
    """
    # Imported lazily, matching load_layout()'s lazy `klayout.db` import.
    import klayout.db as kdb

    options = kdb.SaveLayoutOptions()
    options.gds2_write_timestamps = False
    try:
        layout.write(path, options)
    except Exception as exc:  # klayout raises RuntimeError for bad formats/paths
        raise error_cls(f"could not write output '{path}': {exc}") from exc
