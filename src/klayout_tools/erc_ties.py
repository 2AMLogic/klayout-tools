"""The ``ties[]`` / well-selection subsystem of ``klt erc``.

Split out of :mod:`klayout_tools.erc` (which imports these names back, so
``from klayout_tools.erc import REASON_DEGENERATE_*`` keeps working), following
the precedent of :mod:`klayout_tools.erc_spec` and
:mod:`klayout_tools.erc_disclosure`. Pure move: no behaviour change.

Holds ``_tie_findings`` (the ``erc.missing_tie`` finding computation), the
degenerate-declaration grading (``_degenerate_tie_reasons`` and the
``REASON_DEGENERATE_*`` / ``REASON_EMPTY_WELL_REGION`` tokens), and the
well-region / well-selection helpers they share.
"""

from __future__ import annotations

from typing import Any

from ._layout import region as _region
from .coverage import work_id
from .erc_spec import ErcError

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: declared tap region is indistinguishable from ordinary source/drain
#: contacts (issue #2199). See :func:`_degenerate_tie_reasons` for exactly
#: when it applies and `docs/cli/erc.md` for how a spec clears it.
REASON_DEGENERATE_TAP_DECLARATION = "degenerate_tap_declaration"

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: caller-asserted substrate region (``well_layer: null`` + ``well_boxes``,
#: issue #2255) is geometrically indistinguishable from the whole top-cell
#: extent. The tap-side counterpart of
#: :data:`REASON_DEGENERATE_TAP_DECLARATION`, and kept distinct from it
#: because the two name different things to go fix: that one says "narrow
#: the tap", this one says "name the substrate region you are actually
#: claiming, not the die". See :func:`_degenerate_tie_reasons`.
REASON_DEGENERATE_WELL_ASSERTION = "degenerate_well_assertion"

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: declared well-side selection (``well_requires``/``well_excludes``, issue
#: #2339, or their literal-geometry counterparts
#: ``well_requires_boxes``/``well_excludes_boxes``, issue #2540 -- the same
#: test, applied to whichever of the four the spec declared) did not
#: actually select a *proper, non-empty* subset of the drawn
#: ``well_layer``: it either kept every merged shape on that layer (the
#: selection distinguishes nothing, so the entry silently grades the other
#: bias class against its own ``net`` exactly as an unselected tie would) or
#: kept none of them (the entry's per-well loop runs zero times, so zero
#: ``erc.missing_tie`` findings mean "nothing was examined", not "every well
#: is tied").
#:
#: Kept distinct from :data:`REASON_DEGENERATE_WELL_ASSERTION` because the
#: two say different things to go fix -- that one says "name the substrate
#: region you are actually claiming, not the die"; this one says "the marker
#: layer (or box list) you named does not partition this well layer" -- and
#: from
#: :data:`REASON_DEGENERATE_TAP_DECLARATION`, which is about the tap. Unlike
#: the tap test, this one fires only when a selection was *declared*: a tie
#: with no well-side selector claims every shape on the layer, which is the
#: strongest claim available and exactly what every spec written before this
#: key existed means. See :func:`_select_well_class`.
REASON_DEGENERATE_WELL_SELECTION = "degenerate_well_selection"

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry that names a
#: drawn ``well_layer`` (never ``well_boxes`` -- see below) with **no
#: geometry at all** in the stream: a typo'd layer/datatype, a PDK whose tub
#: layer number changed, or a GDS written without the tub layer, issue #2377.
#:
#: An empty well region collapses :func:`_tie_findings`'s per-well loop to
#: zero iterations -- the same "nothing was examined" state
#: :data:`REASON_DEGENERATE_WELL_SELECTION` already rejects for a *declared*
#: selection that keeps none of the drawn layer's shapes, one step earlier:
#: here the layer itself contributes nothing, before any selection is even
#: applied. Without this reason, zero ``erc.missing_tie`` findings from a
#: well nobody drew read exactly like zero findings from a well that was
#: examined and found clean -- the absence-of-evidence pass every other
#: member of this family exists to reject.
#:
#: Drawn-layer only: a caller-asserted region (``well_layer: null`` +
#: ``well_boxes``) cannot be empty after spec parsing (boxes are validated
#: non-degenerate before this module ever sees them), so this reason never
#: applies to an asserted tie -- see :func:`_tie_well_region`. Kept distinct
#: from :data:`REASON_DEGENERATE_WELL_SELECTION` because the two say
#: different things to go fix -- that one says "the marker layer you named
#: does not partition this well layer"; this one says "the well layer itself
#: draws nothing here" -- and it is not distinguished from "the layer is
#: present but every shape on it happens to be empty", since
#: :func:`_region` collapses both to the same empty ``Region``. See
#: :func:`_degenerate_tie_reasons`.
REASON_EMPTY_WELL_REGION = "empty_well_region"

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


def _tie_findings(
    l2n: Any, circuit: Any, tie_layers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``erc.missing_tie`` findings (issue #861), driven by the optional
    ``ties`` spec section.

    For every physically distinct well/tub shape (each merged polygon of a
    tie's ``well_layer`` -- narrowed to the shapes this tie's class
    selection keeps, issue #2339 -- or of its caller-asserted
    ``well_boxes``, issue #2255; this function cannot and need not tell the
    forms apart, since the region arrives already resolved from
    :func:`_extract_connectivity`), a tap (the
    derived ``tap_layer`` ∩ ``tap_requires`` region, clipped to the well
    itself) must be drawn
    inside it *and* at least one such tap must be electrically connected --
    via the tie connectivity graph, where the tap is wired to
    ``connect_to``'s ``stackup`` region during registration, see
    :func:`_extract_connectivity` -- to the declared ``net``. Both failure
    modes are reported under the same rule id: no tap drawn in the well at
    all, or taps present but none of them wired to the declared net.

    Which taps reach the declared net is answered once per tie, by asking
    the graph for the declared net's *own* polygons on the tap layer
    (``LayoutToNetlist.polygons_of_net``) and then intersecting that set
    against each well shape (issue #2169). This replaces the previous
    single ``probe_net`` of whichever tap happened to come first: with the
    well no longer acting as a blanket conductor merging every tap in it
    into one net (see :func:`_extract_connectivity`), taps in the same well
    are genuinely independent, so a well with one untied and one correctly
    tied tap must still pass -- and it stays a single region operation per
    well rather than a probe per tap.
    """
    if not tie_layers:
        return []

    # Imported lazily, matching this module's/the codebase's convention (see
    # `load_layout`'s docstring) -- needed here for `kdb.Region(polygon)`.
    import klayout.db as kdb

    from .erc import _bbox_dict, _finding, _match_net_clusters

    findings: list[dict[str, Any]] = []
    for tie in tie_layers:
        tied_taps = kdb.Region()
        for net in _match_net_clusters(circuit, tie["net"]):
            tied_taps += l2n.polygons_of_net(net, tie["tap_index"])
        tied_taps = tied_taps.merged()

        for well_poly in tie["well_region"].merged().each():
            poly_region = kdb.Region(well_poly)
            tap_here = tie["tap_region"].interacting(poly_region)
            if tap_here.is_empty():
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            f"well/tub region has no {tie['name']!r} tap "
                            "contact drawn inside it"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
                continue

            if tied_taps.interacting(poly_region).is_empty():
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            "well/tub tap is not connected to declared net "
                            f"{tie['net']!r}"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
    return findings


def _degenerate_tie_reasons(tie_layers: list[dict[str, Any]]) -> dict[str, str]:
    """The declared ties whose ``erc.missing_tie`` verdict is unfalsifiable
    (issues #2199, #2255, #2339 and #2377), each mapped to the stable
    ``erc_coverage.skipped[].reason`` naming *why* -- reported as skipped
    work by :func:`_connectivity_coverage` rather than as a check that
    passed.

    There is no device recognition here: a tap and an ordinary source/drain
    contact drawn on the same diffusion/contact layer are the *same
    geometry* to this module. So a tie whose derived tap region is just
    "whatever that layer happens to draw inside the well" reports the well
    as tied as soon as **any** contact in it reaches the declared net --
    which, for a PMOS row whose sources sit on VDD, is every well, every
    time, regardless of whether a tap was ever drawn. The verdict is not
    wrong so much as it is not about taps; the reproduction in issue #2199
    is the same spec and the same GDS answering "tied" for ``vdd`` and
    "untied" for ``vss``.

    A tie is **tap-degenerate** (:data:`REASON_DEGENERATE_TAP_DECLARATION`)
    when all three hold:

    - the spec did not affirm ``tap_is_dedicated`` -- a tap-only layer
      (sky130's ``tap``) needs no narrowing and is a real tap by
      construction;
    - the declared narrowing removed **nothing** from the drawn
      ``tap_layer`` inside the well (``tap_narrowed``, measured in
      :func:`_extract_connectivity`). Omitting ``tap_requires`` is the
      usual way to land here, but a ``tap_requires`` that happens to
      intersect the whole drawn layer in *this* stream (an implant drawn
      over every contact, not only the taps) is exactly as unfalsifiable,
      so the test is geometric rather than a check for the key's presence;
    - the resulting tap region is non-empty. An empty one asserts nothing
      about taps either -- but it cannot pass: every well in it is reported
      as having no tap drawn, which is an honest finding, not a silent
      clean.

    A tie is **well-degenerate** (:data:`REASON_DEGENERATE_WELL_ASSERTION`,
    issue #2255) when it *asserted* its substrate region (``well_layer:
    null`` + ``well_boxes``) instead of naming drawn geometry, and that
    region covers the whole top-cell extent to within
    :data:`_WELL_ASSERTION_MIN_UNCOVERED_FRACTION` (measured in
    :func:`_extract_connectivity`, where both regions exist). "The entire
    die is the substrate" collapses :func:`_tie_findings`'s per-well loop to
    one polygon spanning everything, so the check degrades to "does any
    contact anywhere reach the declared net?" -- satisfied by every PMOS
    source sitting on the rail, tap or not. The same unfalsifiable pass as
    the tap case, one level up. A tie with a *drawn* ``well_layer`` is never
    well-degenerate however much of the die that layer covers: a blanket
    drawn well is a fact about the stream, not an unverifiable claim, and
    rejecting it would break every spec written before this test existed.

    A tie is **selection-degenerate**
    (:data:`REASON_DEGENERATE_WELL_SELECTION`, issue #2339) when it
    *declared* a well-side class selection -- ``well_requires`` /
    ``well_excludes`` (marker layers) or ``well_requires_boxes`` /
    ``well_excludes_boxes`` (literal geometry, issue #2540), one test for all
    four -- that did not actually partition the drawn
    ``well_layer``: it kept every merged shape on it (the selection
    distinguishes nothing, so the entry grades the other bias class against
    its own ``net`` exactly as an unselected tie would -- the false
    ``erc.missing_tie`` the key exists to remove is still reported, just
    unexplained), or it kept none of them (the per-well loop runs zero
    times, so a zero-finding report means "nothing was examined", which is
    the absence-of-evidence pass every test in this function rejects).
    Unlike the tap test, this one is gated on the key having been *declared*:
    a tie with no well-side selector claims every shape on the layer, which
    is the strongest claim available and the meaning of every spec written
    before this key existed. The measurement itself is geometric, in
    :func:`_select_well_class`, so a selector that happens to keep
    everything in *this* stream is as degenerate as one that names the well
    layer itself -- and a box list that matches no shape of the drawn layer
    at all lands on the "kept none" endpoint, not on a silent pass.

    A tie's well region is **empty** (:data:`REASON_EMPTY_WELL_REGION`,
    issue #2377) when it names a *drawn* ``well_layer`` (never ``well_boxes``
    -- an asserted region cannot be empty, see :func:`_tie_well_region`) and
    that layer draws **no geometry at all** in the stream: a typo'd
    layer/datatype, a PDK whose tub layer number changed, or a GDS written
    without the tub layer. Measured before :func:`_select_well_class` even
    runs, so it is distinct from -- and takes priority over --
    :data:`REASON_DEGENERATE_WELL_SELECTION`: that reason means "the layer
    is there but the declared class selector kept none of it"; this one
    means "the layer itself contributed nothing, before any selection was
    applied". Both collapse :func:`_tie_findings`'s per-well loop to zero
    iterations, which is exactly the absence-of-evidence pass every test in
    this function exists to reject.

    **The well tests are applied first**, and win when more than one holds,
    because the tap measurement is taken *inside* the well region:
    ``tap_narrowed`` is ``(drawn_tap ∩ well) - tap_sites``, so an
    unfalsifiable well makes the tap-side answer a statement about the die
    rather than about a tap. Reporting "narrow your tap" for a declaration
    whose actual defect is "you claimed the whole die" (or "your well layer
    draws nothing") would send a reader to fix the wrong half.

    Deliberately *not* a change to ``erc_findings``: the same findings are
    emitted for the same geometry as before. What changes is that the
    envelope now says the check could not be performed, instead of letting
    ``erc_status: "clean"`` stand for it.
    """
    reasons: dict[str, str] = {}
    for tie in tie_layers:
        if tie["well_degenerate"]:
            reasons[tie["name"]] = REASON_DEGENERATE_WELL_ASSERTION
        elif tie["well_region_empty"]:
            reasons[tie["name"]] = REASON_EMPTY_WELL_REGION
        elif tie["well_selection_degenerate"]:
            reasons[tie["name"]] = REASON_DEGENERATE_WELL_SELECTION
        elif (
            not tie["tap_is_dedicated"]
            and not tie["tap_narrowed"]
            and not tie["tap_region"].is_empty()
        ):
            reasons[tie["name"]] = REASON_DEGENERATE_TAP_DECLARATION
    return reasons


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
