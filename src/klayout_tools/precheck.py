"""Run a named battery of layout-hygiene checks against a GDSII/OASIS stream.

Pure library: :func:`run_precheck` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``drc.py``.

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- no GUI, no Qt, and no dependency on the standalone
``klayout`` application binary.

Scope: this is a battery of *layout hygiene* checks -- shapes off the
manufacturing grid, zero-area (degenerate) polygons, cell names carrying
characters that break downstream tools, pin labels that don't land on any
drawn geometry, and (optionally) layers outside a caller-supplied whitelist
-- distinct from ``klt drc``'s width/space/enclosure *design-rule* deck.
These checks are fast relative to a full DRC deck (no
``Region.*_check()`` polygon-processing pass) and catch a class of problem a
DRC deck does not model at all: malformed geometry and naming/labelling
mistakes rather than spacing/width violations. See ``docs/cli/precheck.md``
and issue #245.

Each check reports its own named ``pass``/``fail``/``skipped`` result (rather
than one monolithic verdict) so an agent can route a specific failure (e.g.
``cell_names`` -> rename; ``offgrid`` -> snap-to-grid) instead of parsing a
flat violations list to figure out what kind of problem it is looking at.
"""

from __future__ import annotations

from typing import Any

from ._layout import load_layout
from ._provenance import build_provenance
from .decks import (
    UnknownExtractionDeckError,
    deck_source_path,
    get_extraction_deck,
)

#: Characters that break downstream tooling if they appear in a cell name:
#: filesystem-path-reserved characters (POSIX ``/`` and Windows'
#: ``\ : * ? " < > |``), whitespace (breaks whitespace-delimited SPICE/CLI
#: tokenisation), and ``#`` (the SPICE netlist comment marker -- a cell name
#: containing it silently truncates every line that references it once
#: written to a `.spice` netlist). Not a full GDSII cell-name legality check
#: (the GDSII spec itself is far more permissive) -- a deliberately narrow,
#: documented deny-list of characters known to break *this repo's* downstream
#: consumers (`klt extract`'s SPICE writer, shell/file-path handling).
_CELL_NAME_FORBIDDEN_CHARS = frozenset(' \t\n\r/\\#:*?"<>|')

#: Every check name this module runs, in report order. Kept as a module
#: constant so the CLI/tests can assert the full battery ran without
#: hardcoding the list twice.
CHECK_NAMES: tuple[str, ...] = (
    "offgrid",
    "zero_area",
    "cell_names",
    "layer_whitelist",
    "pin_labels_over_drawing",
)


class PrecheckError(Exception):
    """Raised when a layout cannot be checked: bad file, unknown deck, or a
    bad ``--grid-um``/``--allowed-layers`` value.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_precheck(
    path: str,
    grid_um: float | None = None,
    allowed_layers: list[tuple[int, int]] | None = None,
    deck: str | None = None,
) -> dict[str, Any]:
    """Run the layout-hygiene check battery against the layout at ``path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/precheck.md``)::

        {
            "schema_version": 2,
            "file": <path as provided>,
            "dbu_um": <database unit in micrometres, float>,
            "status": "pass" | "fail",
            "check_count": <int>,
            "checks": [
                {
                    "name": str,
                    "status": "pass" | "fail" | "skipped",
                    "violation_count": <int>,
                    "violations": [...],
                    "skip_reason": str | None,
                },
                ...
            ],
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it started at ``1`` and is now ``2`` --
    bumped for the breaking value-semantics change to ``layer_whitelist``
    violations' ``shapes`` field (instance-weighted, not per-cell-definition;
    see issue #452 and ``CHANGELOG.md``). No other field changed shape or
    meaning.

    ``status`` is ``"fail"`` iff any check's own ``status`` is ``"fail"`` --
    a ``"skipped"`` check (one whose optional input, e.g. ``grid_um``, was
    not supplied) never causes an overall failure, only a documented gap in
    coverage (mirroring ``klt drc``'s ``coverage.layers_in_stream_without_rules``
    -- a skip is reported, not silently treated as a pass).

    ``checks`` always has one entry per name in :data:`CHECK_NAMES`, in that
    order:

    - ``offgrid`` -- every polygon vertex, across every layer and top cell
      (instance placement transforms included), lands on a multiple of
      ``grid_um`` in both x and y. Skipped when ``grid_um`` is ``None``: this
      repo curates no per-PDK manufacturing-grid table (distinct from a
      layout's own ``dbu_um``, which GDSII/OASIS coordinates are always
      integer multiples of by construction -- the manufacturing grid a real
      foundry flow enforces is typically coarser), so the grid is a required,
      caller-supplied physical value rather than a guessed default.
    - ``zero_area`` -- every polygon/box/path shape (text labels are exempt
      -- see ``pin_labels_over_drawing`` below) has nonzero area, across
      every layer and top cell. Always runs (needs no extra input).
    - ``cell_names`` -- no cell in the layout (whole hierarchy, not just top
      cells) has a name containing a character in
      :data:`_CELL_NAME_FORBIDDEN_CHARS`. Always runs.
    - ``layer_whitelist`` -- every ``(layer, datatype)`` pair present in the
      stream is a member of ``allowed_layers``. Each violation's ``shapes``
      count is **instance-weighted**: a cell definition's own shape count on
      that layer is multiplied by the cell's total placement multiplicity
      across the full hierarchy (composed multiplicatively through nested
      placements), then summed across all cell definitions -- so it reflects
      true placed-shape prevalence on a hierarchical, multi-instance macro,
      not just how many shapes are drawn once per cell *definition* (schema
      version 2; see issue #452 -- version 1 undercounted by the placement
      multiplicity, e.g. reporting ``10`` for 800 shapes actually placed
      across 320 instances of a repeated cell). Skipped when
      ``allowed_layers`` is ``None``: this repo's per-PDK deck layer tables
      (``decks/sky130.py``, ``decks/gf180mcu.py``) are documented **curated
      starter subsets**, not full valid-layer enumerations (see
      ``docs/cli/drc.md``'s "Coverage" section) -- reusing one as an implied
      whitelist would misflag every legitimate PDK layer the curated deck
      simply hasn't modelled yet as "invalid." The caller supplies the real
      whitelist explicitly instead.
    - ``pin_labels_over_drawing`` -- every text label on a deck's known label
      layer (``ExtractionDeck.well_label``/``poly_label``/``metal_labels``,
      see ``decks/__init__.py``) overlaps a drawn shape on that label's
      paired drawing layer (``nwell``/``poly``/the matching ``metals[]``
      entry). Skipped when ``deck`` is ``None`` (no label/drawing layer
      pairing to check against) or when the resolved deck declares no label
      layers at all.

    Raises :class:`PrecheckError` if the file is missing/unreadable,
    ``deck`` names an unregistered extraction deck, or ``grid_um`` is given
    but not representable as a positive integer number of database units.
    """
    layout = load_layout(path, PrecheckError)
    top_cells = list(layout.top_cells())

    checks = [
        _check_offgrid(layout, top_cells, grid_um),
        _check_zero_area(layout, top_cells),
        _check_cell_names(layout),
        _check_layer_whitelist(layout, allowed_layers),
        _check_pin_labels_over_drawing(layout, top_cells, deck),
    ]

    overall_status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"

    return {
        "schema_version": 2,
        "file": path,
        "dbu_um": layout.dbu,
        "status": overall_status,
        "check_count": len(checks),
        "checks": checks,
        "provenance": build_provenance(
            deck_name=deck,
            deck_path=(deck_source_path(deck) if deck else None),
        ),
    }


def _fmt_layer(layer_tuple: tuple[int, int]) -> str:
    return f"{layer_tuple[0]}/{layer_tuple[1]}"


def _bbox_dict(bbox: Any) -> dict[str, int]:
    return {
        "left": bbox.left,
        "bottom": bbox.bottom,
        "right": bbox.right,
        "top": bbox.top,
    }


def _finish(name: str, violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "fail" if violations else "pass",
        "violation_count": len(violations),
        "violations": violations,
        "skip_reason": None,
    }


def _skipped(name: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "skipped",
        "violation_count": 0,
        "violations": [],
        "skip_reason": reason,
    }


# --------------------------------------------------------------------------- #
# offgrid
# --------------------------------------------------------------------------- #


def _check_offgrid(
    layout: Any, top_cells: list[Any], grid_um: float | None
) -> dict[str, Any]:
    if grid_um is None:
        return _skipped("offgrid", "no --grid-um given")

    grid_dbu = round(grid_um / layout.dbu)
    if grid_dbu <= 0:
        raise PrecheckError(
            f"--grid-um {grid_um} is not representable as a positive number "
            f"of database units at this layout's dbu ({layout.dbu} um)"
        )

    violations: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_label = _fmt_layer((info.layer, info.datatype))
        for cell in top_cells:
            iterator = cell.begin_shapes_rec(layer_index)
            while not iterator.at_end():
                shape = iterator.shape()
                polygon = shape.polygon
                if polygon is not None:
                    polygon = polygon.transformed(iterator.trans())
                    if not _polygon_on_grid(polygon, grid_dbu):
                        violations.append(
                            {
                                "cell": iterator.cell().name,
                                "layer": layer_label,
                                "bbox": _bbox_dict(polygon.bbox()),
                                "polygon": [
                                    [pt.x, pt.y] for pt in polygon.each_point_hull()
                                ],
                            }
                        )
                iterator.next()

    violations.sort(
        key=lambda v: (v["cell"], v["layer"], v["bbox"]["left"], v["bbox"]["bottom"])
    )
    return _finish("offgrid", violations)


def _polygon_on_grid(polygon: Any, grid_dbu: int) -> bool:
    return all(
        pt.x % grid_dbu == 0 and pt.y % grid_dbu == 0
        for pt in polygon.each_point_hull()
    )


# --------------------------------------------------------------------------- #
# zero_area
# --------------------------------------------------------------------------- #


def _check_zero_area(layout: Any, top_cells: list[Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_label = _fmt_layer((info.layer, info.datatype))
        for cell in top_cells:
            iterator = cell.begin_shapes_rec(layer_index)
            while not iterator.at_end():
                shape = iterator.shape()
                # `.polygon` is None for text shapes (labels) -- excluded
                # here on purpose, see `pin_labels_over_drawing` for the
                # text-specific check.
                if shape.polygon is not None and shape.area() == 0:
                    polygon = shape.polygon.transformed(iterator.trans())
                    violations.append(
                        {
                            "cell": iterator.cell().name,
                            "layer": layer_label,
                            "bbox": _bbox_dict(polygon.bbox()),
                            "polygon": [
                                [pt.x, pt.y] for pt in polygon.each_point_hull()
                            ],
                        }
                    )
                iterator.next()

    violations.sort(
        key=lambda v: (v["cell"], v["layer"], v["bbox"]["left"], v["bbox"]["bottom"])
    )
    return _finish("zero_area", violations)


# --------------------------------------------------------------------------- #
# cell_names
# --------------------------------------------------------------------------- #


def _check_cell_names(layout: Any) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    for cell in layout.each_cell():
        name = cell.name
        bad_chars = sorted({c for c in name if c in _CELL_NAME_FORBIDDEN_CHARS})
        if bad_chars:
            violations.append(
                {
                    "cell": name,
                    "reason": (
                        "cell name contains forbidden character(s): "
                        + ", ".join(repr(c) for c in bad_chars)
                    ),
                }
            )

    violations.sort(key=lambda v: v["cell"])
    return _finish("cell_names", violations)


# --------------------------------------------------------------------------- #
# layer_whitelist
# --------------------------------------------------------------------------- #


def _cell_placement_multiplicities(layout: Any) -> dict[int, int]:
    """Every cell's total placement count across the full hierarchy.

    Multiplicities compose **multiplicatively** along nested placements: a
    cell placed 5 times (e.g. as an array) inside a cell that is itself
    placed 3 times elsewhere has an effective multiplicity of 15, not 8 or
    5. A cell with no parent instances anywhere is, by KLayout's own
    definition, a top cell -- it exists exactly once, as itself, so its
    multiplicity is 1.

    Computed via :meth:`Layout.each_cell_top_down` (KLayout's own
    topologically-sorted cell order -- every cell is delivered before it is
    used as a child anywhere, so each cell's parents have already had their
    own multiplicity computed by the time it's this cell's turn) and each
    cell's :meth:`Cell.each_parent_inst` (parent-instance *array*
    multiplicity via each parent link's ``inst().size()``, i.e. ``na * nb``
    for a regular array or ``1`` for a single placement). Deliberately
    **iterative**, not recursive -- a real macro's hierarchy depth is
    typically shallow (2-3 levels: top -> subcells -> standard cells), but
    nothing here bounds it, and Python's recursive-call depth is a much
    lower ceiling than a legitimately deep hierarchy could hit. Also
    deliberately *not* a full recursive shape flatten (``begin_shapes_rec()``
    materializing every placed shape), which would be far more expensive on
    a real macro (see issue #452). O(hierarchy edges), not O(placed shapes)
    or O(hierarchy depth).
    """
    multiplicities: dict[int, int] = {}
    for cell_index in layout.each_cell_top_down():
        total = sum(
            multiplicities[parent.parent_cell_index()] * parent.inst().size()
            for parent in layout.cell(cell_index).each_parent_inst()
        )
        # No parent instances anywhere -- a top cell -- gets multiplicity 1.
        multiplicities[cell_index] = total if total > 0 else 1
    return multiplicities


def _check_layer_whitelist(
    layout: Any, allowed_layers: list[tuple[int, int]] | None
) -> dict[str, Any]:
    if allowed_layers is None:
        return _skipped("layer_whitelist", "no --allowed-layers given")

    allowed = {tuple(entry) for entry in allowed_layers}

    violations: list[dict[str, Any]] = []
    multiplicities: dict[int, int] | None = None
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        layer_tuple = (info.layer, info.datatype)
        if layer_tuple in allowed:
            continue
        # Computed lazily -- and only once -- so a layout with no
        # off-whitelist layers never pays for the hierarchy walk.
        if multiplicities is None:
            multiplicities = _cell_placement_multiplicities(layout)
        shape_count = sum(
            cell.shapes(layer_index).size() * multiplicities[cell.cell_index()]
            for cell in layout.each_cell()
        )
        violations.append(
            {
                "layer": _fmt_layer(layer_tuple),
                "name": info.name if info.name else None,
                "shapes": shape_count,
            }
        )

    violations.sort(key=lambda v: v["layer"])
    return _finish("layer_whitelist", violations)


# --------------------------------------------------------------------------- #
# pin_labels_over_drawing
# --------------------------------------------------------------------------- #


def _check_pin_labels_over_drawing(
    layout: Any, top_cells: list[Any], deck_name: str | None
) -> dict[str, Any]:
    if deck_name is None:
        return _skipped("pin_labels_over_drawing", "no --deck given")

    try:
        deck = get_extraction_deck(deck_name)
    except UnknownExtractionDeckError as exc:
        raise PrecheckError(str(exc)) from exc

    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    if deck.well_label is not None:
        pairs.append((deck.well_label, deck.nwell))
    if deck.poly_label is not None:
        pairs.append((deck.poly_label, deck.poly))
    for label, metal in zip(deck.metal_labels, deck.metals, strict=False):
        if label is not None:
            pairs.append((label, metal))

    if not pairs:
        return _skipped(
            "pin_labels_over_drawing",
            f"deck '{deck_name}' declares no label layers",
        )

    # Imported lazily, matching drc.py's own lazy `klayout.db` import
    # convention (paid only once load_layout() already read the file).
    import klayout.db as kdb

    violations: list[dict[str, Any]] = []
    for label_layer, drawing_layer in pairs:
        label_index = layout.find_layer(*label_layer)
        if label_index is None:
            continue  # no labels of this kind drawn in this stream
        label_str = _fmt_layer(label_layer)
        drawing_index = layout.find_layer(*drawing_layer)

        for cell in top_cells:
            texts = kdb.Texts(cell.begin_shapes_rec(label_index))
            if texts.is_empty():
                continue
            if drawing_index is None:
                stray = texts
            else:
                drawing_region = kdb.Region(cell.begin_shapes_rec(drawing_index))
                stray = texts.not_interacting(drawing_region)
            for text in stray.each():
                bbox = text.bbox()
                violations.append(
                    {
                        "cell": cell.name,
                        "layer": label_str,
                        "text": text.string,
                        "position": {"x": bbox.left, "y": bbox.bottom},
                    }
                )

    violations.sort(
        key=lambda v: (
            v["cell"],
            v["layer"],
            v["text"],
            v["position"]["x"],
            v["position"]["y"],
        )
    )
    return _finish("pin_labels_over_drawing", violations)
