"""Write a bounding-box region or a named cell's full subtree out of an
existing GDSII/OASIS stream into a fresh top-cell stream (issue #1608).

Pure library: :func:`run_clip` returns plain Python data (a JSON-serialisable
``dict``) and never prints, mirroring ``ring_check.py``/``components.py``.
Serialisation and human-readable formatting live in the CLI command module
(``cli/clip_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- no GUI, no Qt, and no dependency on the standalone
``klayout`` application binary.

Why this exists (issue #1603, item 2): a workflow that needs to hand a single
device or region to an external tool (EM extraction, third-party meshing, or
just isolating a device for review) has no ``klt`` path today for writing
that subset back out as its own top-cell stream -- ``--region`` on ``klt
ring-check``/``klt components`` only *restricts analysis*, it never writes
the clipped subset back out.

Two mutually exclusive modes:

- **Region-clip** (``region_um`` given): every layer of a single top cell
  (``top``, or the stream's sole top cell) is flattened (:func:`_layout.region`
  / :func:`_layout.texts`, the same per-layer flattening idiom ``components.py``
  uses), intersected with the clip window (:func:`_layout.clip_box`), and
  inserted into a fresh output cell -- one flat cell, since flattening is
  inherent to how ``region()``/``texts()`` walk the source hierarchy.
- **Cell-extraction** (``cell`` given): the named cell's own subtree is
  copied verbatim (:meth:`kdb.Cell.copy_tree`, cross-layout -- the same
  mechanism ``gen_compose.py``'s block-placement step and
  ``extract_abstract.py``'s "shadow cell" duplication already use) into a
  fresh top cell of the same name -- hierarchy (nested instances, arrays) is
  preserved exactly, unlike region-clip.

Either way, the result is written via the shared :func:`_layout.write_layout`
-- deterministic (no embedded GDS2 timestamp), so repeated runs on the same
input produce byte-identical output.
"""

from __future__ import annotations

from typing import Any

from ._layout import clip_box as _clip_box
from ._layout import load_layout, resolve_top_cell, write_layout
from ._layout import region as _region
from ._layout import texts as _texts

#: Output top-cell name used for region-clip mode. Region-clip flattens
#: shapes from (potentially) many cells in the source hierarchy into one
#: cell -- there is no single source cell name that describes the result --
#: so a fixed, descriptive name is used instead of borrowing the source top
#: cell's own name (which cell-extraction mode does, since it copies that
#: cell's subtree verbatim and keeps its identity).
REGION_CLIP_CELL_NAME = "CLIP"


class ClipError(Exception):
    """Raised when a clip cannot be produced: a bad input/output path, a
    named ``cell`` not present in the stream, a ``region_um`` that is
    degenerate at the layout's own database unit, or a region that matches
    no geometry.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_clip(
    layout_path: str,
    output_path: str,
    region_um: tuple[float, float, float, float] | None = None,
    cell: str | None = None,
    top: str | None = None,
) -> dict[str, Any]:
    """Write a bbox region or a named cell's subtree from ``layout_path`` out
    as its own top-cell stream at ``output_path``.

    Exactly one of ``region_um``/``cell`` must be given -- the CLI enforces
    this with a required, mutually exclusive ``--region``/``--cell`` argument
    group (an argparse usage error, exit 2); this function re-checks it for
    every direct (non-CLI) caller too, as an application error (:class:`ClipError`).

    ``region_um``, when given, is a ``(left, bottom, right, top)`` window in
    **micrometres** -- the same shape ``load_region()`` already parses for
    ``klt ring-check``/``klt components``' own ``--region``. It is clipped
    against ``top`` (or the stream's sole top cell, when ``top`` is omitted --
    see :func:`_layout.resolve_top_cell`). Zero/negative area in micrometres
    is already rejected by ``load_region()`` before this function runs; this
    function additionally rejects a region that survives that check but still
    rounds to zero width or height at the layout's own database unit (e.g. a
    sub-dbu-grid window on a coarse-dbu stream) -- writing an intentionally
    empty output would be indistinguishable from a caller's mistake, so it is
    treated as an error rather than a silently empty result. A region with
    positive area at the layout's dbu that nonetheless contains no shapes on
    any layer also raises :class:`ClipError`, distinctly worded, so a caller
    can tell "your window is degenerate" apart from "your window is fine but
    there is nothing there."

    ``cell``, when given, names a cell anywhere in ``layout_path`` (top-level
    or nested -- not restricted to top cells); its full subtree is copied
    into the output's fresh top cell of the same name via
    :meth:`kdb.Cell.copy_tree`. A ``cell`` with no matching name in the stream
    raises :class:`ClipError`; an existing cell with no shapes anywhere in its
    subtree is not an error (there is nothing ambiguous about "this cell is
    empty," unlike a region matching no geometry).

    ``top`` is ignored (and should be left unset) in cell-extraction mode --
    it only disambiguates which top cell a region-clip's window is measured
    against.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/clip.md``)::

        {
            "schema_version": 1,
            "file": <layout_path as provided>,
            "output": <output_path as provided>,
            "mode": "region" | "cell",
            "cell": <str> | None,
            "region_um": [left, bottom, right, top] | None,
            "top": <str> | None,
            "dbu_um": <database unit in micrometres, float>,
            "cell_count": <int>,
            "shape_count": <int>,
        }

    ``top`` in the response is the resolved top-cell name region-clip
    actually clipped against (``None`` for cell-extraction mode). ``cell_count``
    is the number of cells written to the output stream (cell-extraction mode
    only, when the copied subtree pulls in nested cell definitions;
    region-clip mode always writes exactly one flat cell). ``shape_count`` is
    the total shape count written to the output's layers (both modes).

    Raises :class:`ClipError` for a missing/unreadable input layout, an
    invalid mode selection, a nonexistent ``cell``, a degenerate/no-match
    ``region_um``, or a write failure.
    """
    if (region_um is None) == (cell is None):
        raise ClipError("exactly one of region_um or cell must be given")

    import klayout.db as kdb

    layout = load_layout(layout_path, ClipError)
    dbu = layout.dbu

    out = kdb.Layout()
    out.dbu = dbu

    if cell is not None:
        src_cell = layout.cell(cell)
        if src_cell is None:
            raise ClipError(f"cell '{cell}' not found in '{layout_path}'")

        out_top = out.create_cell(src_cell.name)
        out_top.copy_tree(src_cell)

        mode = "cell"
        resolved_top: str | None = None
        cell_count = out.cells()
        shape_count = sum(
            out.cell(index).shapes(layer_index).size()
            for index in range(out.cells())
            for layer_index in out.layer_indexes()
        )
    else:
        assert region_um is not None  # guaranteed by the exactly-one check above
        src_cell = resolve_top_cell(layout, top, ClipError, path=layout_path)
        clip_kbox = _clip_box(kdb, region_um, dbu)
        if clip_kbox.width() <= 0 or clip_kbox.height() <= 0:
            left, bottom, right, top_um = region_um
            raise ClipError(
                f"--region [{left}, {bottom}, {right}, {top_um}] rounds to "
                f"zero width or height at this layout's dbu={dbu} -- widen "
                "the window or use a finer-dbu input"
            )
        clip_region = kdb.Region(clip_kbox)

        out_top = out.create_cell(REGION_CLIP_CELL_NAME)
        shape_count = 0
        for layer_index in layout.layer_indexes():
            info = layout.get_info(layer_index)
            clipped_region = _region(layout, src_cell, (info.layer, info.datatype))
            clipped_region &= clip_region
            clipped_texts = _texts(layout, src_cell, (info.layer, info.datatype))
            clipped_texts = clipped_texts.interacting(clip_region)
            if clipped_region.is_empty() and clipped_texts.is_empty():
                continue

            out_layer_index = out.layer(info)
            out_top.shapes(out_layer_index).insert(clipped_region)
            out_top.shapes(out_layer_index).insert(clipped_texts)
            shape_count += clipped_region.count() + clipped_texts.count()

        if shape_count == 0:
            raise ClipError(
                f"--region [{region_um[0]}, {region_um[1]}, {region_um[2]}, "
                f"{region_um[3]}] matches no geometry in '{layout_path}' "
                f"(top cell '{src_cell.name}')"
            )

        mode = "region"
        resolved_top = src_cell.name
        cell_count = 1

    write_layout(out, output_path, ClipError)

    return {
        "schema_version": 1,
        "file": layout_path,
        "output": output_path,
        "mode": mode,
        "cell": cell,
        "region_um": list(region_um) if region_um is not None else None,
        "top": resolved_top,
        "dbu_um": dbu,
        "cell_count": cell_count,
        "shape_count": shape_count,
    }
