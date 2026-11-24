"""Well-region / well-selection helpers for the ``ties[]`` checks of ``klt erc``.

Split out of :mod:`klayout_tools.erc_ties` to keep both modules under the
repo's per-file size limit. :mod:`klayout_tools.erc` imports these names back.
Pure move: no behaviour change.

Holds the region builders (``_boxes_region``, ``_cells_region``), the tie's
well/substrate region and class selection (``_tie_well_region``,
``_select_well_class``) and the well-assertion coverage report.
"""

from __future__ import annotations

from typing import Any

from ._layout import region as _region
from .coverage import work_id
from .erc_spec import ErcError

#: How much of the top cell's own extent a caller-asserted substrate region
#: (``ties[].well_boxes``, issue #2255) must leave **uncovered** to count as
#: a real claim rather than "the whole die is the substrate" -- as a
#: fraction of the top cell's bounding-box area.
#:
#: The bar is *indistinguishability*, not size: an assertion may legitimately
#: cover most of a block (a native substrate genuinely is everything outside
#: the n-wells), and nothing here requires it to be small. What it may not be
#: is the entire extent, because a single die-sized "well" polygon reduces
#: ``erc.missing_tie`` to "does any contact anywhere reach the declared
#: net?" -- satisfiable by every PMOS source on the rail, tap or not, which
#: is the same unfalsifiable pass issue #2199 rejected on the tap side. The
#: tolerance exists so a region drawn flush to the cell boundary, or one
#: rounded to the DBU grid, is not accepted on a technicality; it is
#: deliberately small, because the *evidence* an assertion carries grows
#: with how finely it partitions the block (each merged asserted polygon
#: must independently contain a tap that reaches the net).
_WELL_ASSERTION_MIN_UNCOVERED_FRACTION = 0.01


#: The four ``ties[]`` keys that *select* which merged shapes of a drawn
#: ``well_layer`` an entry is about -- the marker-layer pair (issue #2339) and
#: its literal-geometry counterpart (issue #2540). Named once, because two
#: separate places must agree on the same answer to "did this spec declare a
#: well-side selection at all?": :func:`_select_well_class` (which returns the
#: region untouched and never degenerate when none was declared) and
#: :func:`_has_well_selection`'s callers.
_WELL_SELECTION_KEYS = (
    "well_requires",
    "well_excludes",
    "well_requires_boxes",
    "well_excludes_boxes",
    "well_requires_cells",
    "well_excludes_cells",
)


def _has_well_selection(tie: dict[str, Any]) -> bool:
    """Whether this tie *declared* a well-side class selection in any of its
    four spellings (:data:`_WELL_SELECTION_KEYS`).

    The degeneracy test for a selection is presence-gated, unlike the tap
    one: a tie with no well-side selector claims every shape on its layer,
    which is the strongest claim available and exactly what every spec
    written before issue #2339 means -- see
    :data:`REASON_DEGENERATE_WELL_SELECTION`.
    """
    return any(tie[key] for key in _WELL_SELECTION_KEYS)


def _boxes_region(
    kdb: Any, layout: Any, boxes: list[tuple[float, float, float, float]]
) -> Any:
    """The merged region a ``ties[]`` micrometre-box list names -- the one
    conversion behind every literal-geometry key on that entry
    (``tap_boxes``, ``well_boxes``, ``well_requires_boxes`` /
    ``well_excludes_boxes``), so the three callers cannot drift on how a
    caller's coordinates become geometry.

    ``[left, bottom, right, top]`` micrometres go through the same
    :func:`~klayout_tools._layout.clip_box` conversion ``klt clip`` uses, and
    the boxes are merged, so adjacent or overlapping boxes become one polygon
    -- which is load-bearing for ``well_boxes`` (each merged polygon must
    independently hold a tap) and harmless for the selection keys, which only
    ever ask whether a well shape interacts with the union.
    """
    from ._layout import clip_box as _clip_box

    region = kdb.Region()
    for box_um in boxes:
        region += kdb.Region(_clip_box(kdb, box_um, layout.dbu))
    return region.merged()


def _cells_region(
    kdb: Any, layout: Any, top_cell: Any, layer: tuple[int, int], names: list[str]
) -> Any:
    """The merged region of ``layer`` geometry drawn **directly** in instances
    of the named cells, in top-cell coordinates (issue #4373).

    Walks the hierarchy once and keeps each polygon whose owning cell is one of
    ``names``, transformed by the instance's accumulated transformation -- so
    rotation, mirroring and arrays need no per-placement input. Geometry in a
    named cell's *children* belongs to the child cell and is not matched.

    A name absent from the layout is an :class:`ErcError`, not an empty
    selection: a misspelt cell would otherwise select nothing and read as a
    quiet degenerate tie.
    """
    wanted = set(names)
    known = {cell.name for cell in layout.each_cell()}
    missing = sorted(wanted - known)
    if missing:
        raise ErcError(
            f"ties[].well_requires_cells/well_excludes_cells names cell(s) "
            f"{missing} that are not in the layout"
        )
    layer_index = layout.find_layer(layer[0], layer[1])
    region = kdb.Region()
    if layer_index is None:
        return region
    iterator = top_cell.begin_shapes_rec(layer_index)
    while not iterator.at_end():
        if iterator.cell().name in wanted:
            polygon = iterator.shape().polygon
            if polygon is not None:
                region.insert(polygon.transformed(iterator.trans()))
        iterator.next()
    return region.merged()


def _well_assertion_coverage_entry(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    tie: dict[str, Any],
    asserted: Any,
) -> dict[str, Any]:
    """Issue #2427: how much of the stream's drawn tap geometry a
    ``well_boxes`` assertion leaves outside every asserted polygon.

    The degeneracy test (:func:`_tie_well_region`) only asks whether the
    assertion is indistinguishable from the whole top-cell extent. It cannot
    see an assertion going *stale* as a layout grows devices outside it --
    those fall outside every asserted polygon, so ``_tie_findings`` never
    examines them. This is the number that makes that visible across
    revisions. Reporting only: it feeds no verdict and no skip.

    ``drawn_tap_area_um2`` is the merged drawn area of the tie's declared
    ``tap_layer`` (before any ``tap_requires``/``tap_boxes`` narrowing -- the
    drawn device-contact geometry, independent of the caller's claims);
    ``uncovered_tap_area_um2`` the part of it outside the asserted region.
    ``uncovered_tap_fraction`` is ``null`` when no tap geometry is drawn.
    ``extent_uncovered_fraction`` is the same ratio the degeneracy test uses
    (top-cell extent outside the assertion), for context.
    """
    scale = layout.dbu * layout.dbu
    drawn = _region(layout, top_cell, tie["tap_layer"]).merged()
    drawn_area = drawn.area()
    uncovered_area = (drawn - asserted).area()
    extent = kdb.Region(top_cell.bbox())
    extent_area = extent.area()
    return {
        "id": work_id("erc.missing_tie", tie["name"]),
        "tap_layer": f"{tie['tap_layer'][0]}/{tie['tap_layer'][1]}",
        "drawn_tap_area_um2": round(drawn_area * scale, 6),
        "uncovered_tap_area_um2": round(uncovered_area * scale, 6),
        "uncovered_tap_fraction": (
            round(uncovered_area / drawn_area, 6) if drawn_area > 0 else None
        ),
        "extent_uncovered_fraction": (
            round((extent - asserted).area() / extent_area, 6)
            if extent_area > 0
            else None
        ),
    }


def _tie_well_region(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    tie: dict[str, Any],
) -> tuple[Any, bool]:
    """This tie's well/substrate region and whether a caller-asserted one is
    degenerate -- ``(well_region, well_degenerate)``.

    Two sources, decided by :func:`_parse_well_declaration` at spec-validation
    time, not here:

    - a drawn ``well_layer`` (every tie before issue #2255) -- the merged
      layer region, exactly as before, never degenerate (see
      :func:`_degenerate_tie_reasons`);
    - ``well_complement_of`` (issue #3750) -- the top-cell extent minus the
      named drawn layers, for a bulk substrate with no drawn well shape.
      Degenerate by the same test as ``well_boxes`` (the named layers draw
      nothing, so the "substrate" is the whole die) and also when they
      cover the whole die (nothing left to check);
    - ``well_boxes`` (issue #2255) -- the union of the caller's asserted
      micrometre boxes, for a native-substrate block that draws no well/tub
      layer at all. Degenerate when it covers the top cell's own extent to
      within :data:`_WELL_ASSERTION_MIN_UNCOVERED_FRACTION`, i.e. when the
      claim is indistinguishable from "the whole die".

    The assertion is measured against ``top_cell.bbox()`` rather than against
    the die/frame or a caller-supplied extent because that bounding box is
    the one bound this module can derive from the stream itself -- the same
    "verify against geometry, not against the caller's word" discipline
    ``tap_narrowed`` applies on the tap side.

    Split out of :func:`_extract_connectivity` to keep that function under
    the repo's C901 complexity ratchet.
    """
    if tie["well_complement_of"]:
        # Issue #3750: the substrate is everything outside the named well
        # layers. Derived from the stream, so it follows the layout.
        extent = kdb.Region(top_cell.bbox())
        wells = kdb.Region()
        for well_layer in tie["well_complement_of"]:
            wells += _region(layout, top_cell, well_layer)
        asserted = (extent - wells.merged()).merged()
        if asserted.is_empty():
            # Wells cover the whole die: no substrate left to check.
            return asserted, True
    elif tie["well_boxes"]:
        asserted = _boxes_region(kdb, layout, tie["well_boxes"])
        extent = kdb.Region(top_cell.bbox())
    else:
        return _region(layout, top_cell, tie["well_layer"]).merged(), False

    extent_area = extent.area()
    if extent_area <= 0:
        # A top cell with no geometry at all: there is no extent to be
        # distinguishable from, so no assertion about it can be falsified.
        return asserted, True
    uncovered = (extent - asserted).area()
    degenerate = uncovered <= _WELL_ASSERTION_MIN_UNCOVERED_FRACTION * extent_area
    return asserted, degenerate


def _select_well_class(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    tie: dict[str, Any],
    well_region: Any,
) -> tuple[Any, bool]:
    """This tie's well region narrowed to the *class* of well shapes it
    declares -- ``(selected_region, selection_degenerate)``, issues #2339 and
    #2540.

    A shape of the drawn ``well_layer`` is kept when it interacts with
    geometry on every ``well_requires`` layer, with none of the
    ``well_excludes`` layers, with the union of ``well_requires_boxes`` (when
    that key is non-empty) and with none of the ``well_excludes_boxes`` union.
    With none of the four declared (every spec before issue #2339) the region
    is returned untouched and never degenerate: a tie with no well-side
    selector claims every shape on the layer, which is the strongest claim
    available, not the weakest.

    **Boxes are a union, layers are a conjunction**, and both follow from what
    a caller is saying. Each ``well_requires`` *layer* is a separate condition
    a shape must satisfy (``Comp ∩ Nplus``-style reasoning, one layer at a
    time), whereas a box *list* is one region named in pieces -- the same
    union every other literal-geometry key on this entry forms
    (:func:`_boxes_region`). So three ``well_requires_boxes`` boxes select the
    shapes touching *any* of them (three wells of this class), not the shapes
    touching all three (which for disjoint boxes is none).

    ``well_requires_boxes``/``well_excludes_boxes`` (issue #2540) exist because
    the marker form is only reachable for a stream that draws a layer
    separating the classes. A single drawn tub carrying two deliberately
    differently-biased classes with no such layer has nothing to name: every
    marker-driven selection over it keeps every shape or none, i.e. is
    degenerate by the test below, and the alternatives are a false
    ``erc.missing_tie`` on every well of the undeclared class or a
    ``well_boxes`` substitution the parser rejects beside a drawn
    ``well_layer``. The box form is the same escape hatch ``tap_boxes`` (issue
    #2234) is on the tap side, and composes with the marker form exactly as
    ``tap_boxes`` composes with ``tap_requires``.

    **Selection, not intersection** -- and that is the whole design, not an
    implementation detail. ``tap_requires`` narrows the tap by ``&``-ing
    layers together, because a tap *is* the boolean its PDK draws it as.
    A well is not: the unit :func:`_tie_findings` checks is one merged shape
    of the drawn layer, and the marker that identifies a well's class (a PDK
    device marker over the bipolar it contains, say) generally covers only
    part of that shape. Intersecting would replace the tub with the marker's
    own footprint, shrinking the region the tap must fall inside
    (``tap_sites = tap_region & well_region``) and reporting the tub's real,
    correctly-wired tap ring as "no tap contact drawn inside it" -- the same
    class of false ``erc.missing_tie`` this key exists to remove. Keeping
    whole shapes also keeps each finding's ``bbox`` the well the layout
    actually draws.

    ``interacting`` (touch or overlap) rather than ``overlapping`` (area
    only) matches how :func:`_tie_findings` already tests a tap against its
    well, so "inside it" means one thing throughout this module.

    The degeneracy verdict is measured here, where both regions exist (as
    ``tap_narrowed`` and ``well_degenerate`` are): a *declared* selection
    that keeps every drawn shape, or keeps none of them, did not partition
    anything -- see :data:`REASON_DEGENERATE_WELL_SELECTION` and
    :func:`_degenerate_tie_reasons`. One test for all four keys, so a box list
    matching no shape of the drawn layer lands on the "kept none" endpoint
    rather than reading as a clean run over an empty set.
    """
    if not _has_well_selection(tie):
        return well_region, False

    selected = well_region
    for required in tie["well_requires"]:
        selected = selected.interacting(_region(layout, top_cell, required))
    for excluded in tie["well_excludes"]:
        selected = selected.not_interacting(_region(layout, top_cell, excluded))
    if tie["well_requires_boxes"]:
        selected = selected.interacting(
            _boxes_region(kdb, layout, tie["well_requires_boxes"])
        )
    if tie["well_excludes_boxes"]:
        selected = selected.not_interacting(
            _boxes_region(kdb, layout, tie["well_excludes_boxes"])
        )
    # Issue #4373: cell-name selectors, evaluated in each instance's frame.
    well_layer = tie["well_layer"]
    if tie["well_requires_cells"]:
        selected = selected.interacting(
            _cells_region(kdb, layout, top_cell, well_layer, tie["well_requires_cells"])
        )
    if tie["well_excludes_cells"]:
        selected = selected.not_interacting(
            _cells_region(kdb, layout, top_cell, well_layer, tie["well_excludes_cells"])
        )
    selected = selected.merged()
    degenerate = selected.is_empty() or (well_region - selected).is_empty()
    return selected, degenerate
