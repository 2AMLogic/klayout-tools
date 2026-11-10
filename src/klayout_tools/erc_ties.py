"""The ``ties[]`` / well-selection subsystem of ``klt erc``.

Split out of :mod:`klayout_tools.erc` (which imports these names back, so
``from klayout_tools.erc import REASON_DEGENERATE_*`` keeps working), following
the precedent of :mod:`klayout_tools.erc_spec` and
:mod:`klayout_tools.erc_disclosure`. Pure move: no behaviour change.

Holds ``_tie_findings`` (the ``erc.missing_tie`` finding computation), the
degenerate-declaration grading (``_degenerate_tie_reasons`` and the
``REASON_DEGENERATE_*`` / ``REASON_EMPTY_WELL_REGION`` tokens). The
well-region / well-selection helpers they share live in
:mod:`klayout_tools.erc_well_regions`.
"""

from __future__ import annotations

from typing import Any

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
