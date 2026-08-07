"""Run a DRC deck headless against a GDSII/OASIS stream.

Pure library: :func:`run_drc` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``layers.py``.

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) — specifically ``Region``'s native check primitives
(``width_check``, ``space_check``, ``separation_check``, ``enclosing_check``,
``enclosed_check``, ``notch_check``, ``overlap_check``) — no GUI, no Qt, and
no dependency on the standalone ``klayout`` application binary or its
DRC-DSL script runner. See ``docs/cli/drc.md`` for the engine-choice
rationale (native ``Region`` checks vs. running the official ``.lydrc``
deck through the full KLayout application).

Limitation: each rule is checked against the *whole layout*, flattened per
top cell (via ``Cell.begin_shapes_rec``, the same flattening idiom used
elsewhere in this repo) — there is no ``--top <cell>`` filter in this
version. The flattened check still knows *which* top cell a violation was
found under (the ``"cell"`` field), and — additively, without changing the
flattened detection — each violation is also attributed back to the
innermost placed instance whose bounding box contains it, via
``Cell.begin_instances_rec_touching`` (see :func:`_attribute_to_instance`);
that origin is reported in the ``"source_cell"`` / ``"source_path"`` fields
so macro-scale, machine-generated layout (e.g. an OpenROAD place-and-route
run with hundreds of standard-cell placements) can point at the offending
instance rather than only the top cell.
"""

from __future__ import annotations

import os
from typing import Any

from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells
from ._provenance import build_provenance
from .decks import (
    DrcRule,
    UnknownDeckError,
    deck_source_path,
    get_deck,
    get_layer_names,
    get_nominal_dbu,
    get_unmodeled_voltage_markers,
)
from .layers import layers_report

# Check kinds that operate on a single region (no other_layer).
_SINGLE_LAYER_CHECKS = {"width", "space", "notch"}
# Check kinds that compare a region against another region (other_layer required).
_TWO_LAYER_CHECKS = {"separation", "enclosing", "enclosed", "overlap"}
# Two-layer check kinds where the "enclosed" layer can lie entirely outside
# the "enclosing" layer -- a case `Region.enclosing_check`/`enclosed_check`
# never reports (they only measure *facing edges*, so zero spatial overlap
# produces zero edge pairs). See #318: this is the strictly worse violation
# an enclosure rule exists to catch, so it must not read as `status: clean`.
_OUTSIDE_CHECKS = {"enclosing", "enclosed"}


class DrcError(Exception):
    """Raised when a layout cannot be checked: bad file, unknown deck, or a
    malformed rule.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_drc(path: str, deck_name: str, top: str | None = None) -> dict[str, Any]:
    """Run ``deck_name``'s rules against the layout at ``path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/drc.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "deck": <deck name>,
            "dbu_um": <database unit in micrometres, float>,
            "status": "clean" | "violations",
            "violation_count": <int>,
            "rule_counts": {<rule id>: <int>, ...},
            "violations": [
                {
                    "rule": str, "description": str, "check": str,
                    "layer": str, "cell": str,
                    "source_cell": str | None, "source_path": [str, ...] | None,
                    "bbox": {"left": int, "bottom": int, "right": int, "top": int},
                    "polygon": [[x, y], ...] | None,
                },
                ...
            ],
            "coverage": {
                "deck_layers": ["<layer>/<datatype>", ...],
                "layers_checked": ["<layer>/<datatype>", ...],
                "layers_in_stream_without_rules": ["<layer>/<datatype>", ...],
                "rules_skipped": [<rule id>, ...],
                "voltage_domain_warnings": [
                    {"marker": "<layer>/<datatype>", "description": str}, ...
                ],
                "deck_scope": [<scope identifier>, ...],
            },
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": None,  # klt drc resolves no PDK
                "deck": {"name": <deck name>, "content_hash": "sha256:..."},
            },
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.

    ``violations`` is sorted by
    ``(rule, cell, bbox.left, bbox.bottom, bbox.right, bbox.top)`` for
    deterministic, diff-clean output. The full bbox is included in the key so
    violations that share a corner are still totally ordered, keeping output
    canonical across platforms/KLayout builds regardless of the engine's
    internal shape-enumeration order.

    ``source_cell`` / ``source_path`` attribute each violation back to the
    originating placed instance, additively to the JSON contract (both new
    fields; no ``schema_version`` bump -- see ``docs/json-contract.md``). The
    flattened check (above) reports only the top ``cell`` a violation sits
    under; for a hierarchical, multi-instance macro that is rarely the
    actionable answer. After a violation's ``bbox`` is known, it is mapped
    back to the *innermost* placed instance whose own bounding box fully
    contains that bbox (see :func:`_attribute_to_instance`): ``source_cell``
    is that instance's cell-definition name and ``source_path`` is the chain
    of cell names from the top cell's direct child down to it (inclusive).
    When no single instance contains the violation -- top-level geometry, or
    a violation whose bbox straddles an instance boundary (e.g. a spacing
    violation between two adjacent placements) -- both are ``None`` and the
    top ``cell`` remains the only attribution, which is the honest answer
    since the violation belongs to no one instance. A cell placed more than
    once is not conflated: each placement's violations fall inside only that
    placement's world bbox, so they attribute to the correct occurrence (the
    shared ``source_cell``/``source_path`` name the reused definition; the
    distinct ``bbox`` locates the specific placement).

    ``coverage`` reports what was actually checked, purely additive to the
    JSON contract (see ``docs/json-contract.md``; no ``schema_version``
    bump). A rule whose ``layer``/``other_layer`` is absent from ``path``
    is silently skipped by the engine (``layout.find_layer(...)`` returns
    ``None``) -- correct behaviour, but by itself indistinguishable from a
    genuinely-checked pass. ``coverage.layers_in_stream_without_rules`` is
    the load-bearing field: ``(layer, datatype)`` pairs present in ``path``
    that no active rule in ``deck_name`` references at all, formatted
    ``"<layer>/<datatype>"``. ``coverage.deck_layers`` is every layer the
    deck's rules reference (a static property of the deck, independent of
    ``path``); ``coverage.layers_checked`` is the subset of those layers
    actually present in this stream; ``coverage.rules_skipped`` lists the
    rule ids skipped because their layer(s) were absent. A ``"clean"``
    ``status`` with a non-empty ``layers_in_stream_without_rules`` means
    "clean, and here is exactly what was not looked at" rather than a
    fully-verified pass.

    ``coverage.voltage_domain_warnings`` (issue #552) is a second, narrower
    trust gap ``layers_in_stream_without_rules`` alone does not surface:
    some decks (today, gf180mcu's ``Dualgate`` 55/0) draw a marker layer
    that selects a second gate-oxide/voltage domain with materially
    different DRC thresholds this curated deck does not encode -- so
    geometry *inside* that marker is checked against the wrong (default)
    column and reported as an ordinary ``layers_checked`` pass, not an
    unchecked layer. Whenever such a marker (see
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`) is present
    in ``path`` *and* its geometry interacts with at least one layer this
    run actually checked (a member of ``coverage.layers_checked``, not
    merely present-but-unchecked), one entry is added --
    ``{"marker": "<layer>/<datatype>", "description": str}`` -- naming the
    marker and the concrete consequence (the deck's own registered
    description). A ``Dualgate`` shape that never overlaps any checked
    geometry produces no entry, avoiding a warning with nothing behind it.
    Always a list, empty for a deck that registers no such marker or a
    layout that draws none of it overlapping checked geometry -- purely
    additive, no existing rule threshold changes because of this field.

    ``coverage.deck_scope`` (issue #566) is a third, coarser-grained
    "what was not looked at" answer, additive alongside the layer-level
    fields above: every distinct non-empty :attr:`~klayout_tools.decks.DrcRule.scope`
    across ``deck_name``'s rules (deduplicated, sorted) -- the DRM sections
    or official rule-id prefixes this deck claims to implement, static per
    deck and independent of ``path`` (like ``deck_layers``, not filtered by
    what a given run actually found present). Where ``layers_in_stream_without_rules``
    answers "what geometry did I draw that the deck ignored entirely",
    ``deck_scope`` answers "which of the DRM's own chapters does this deck
    even attempt" -- a layer can be ``"checked"`` because one unrelated rule
    references it, even though the specific DRM section/rule a caller cares
    about was never curated at all; diffing ``deck_scope`` against the DRM's
    own table of contents surfaces that gap. A deck rule with no ``scope``
    set (the default, ``""``) contributes nothing to this list.

    Every ``DrcRule.threshold_dbu`` in ``deck_name`` is authored against that
    deck's nominal dbu (see :func:`klayout_tools.decks.get_nominal_dbu`), not
    against whatever dbu ``path`` happens to use. Before any
    ``Region.*_check()`` runs, thresholds are rescaled by
    ``nominal_dbu_um / layout.dbu`` so the same physical geometry produces
    identical violations regardless of the input stream's database unit.

    ``top`` (issue #554) restricts the cells checked -- and the
    ``coverage.layers_in_stream_without_rules``/``layers_checked``
    denominator, which is recomputed from the same scope via
    ``layers_report(path, top=top)`` -- to one named top cell, instead of
    every top cell in the stream (today's default, unchanged when omitted).
    A named cell absent from the stream is a :class:`DrcError`, matching
    ``klt ring-check --top``.

    Raises :class:`DrcError` if the file is missing/unreadable, the deck
    name is unknown, ``top`` names a cell absent from the stream, or a rule
    is malformed (e.g. a two-layer check missing ``other_layer``).
    """
    # Checked here (ahead of deck lookup) so a missing/bad path is reported
    # before an unknown deck name, matching this command's historical error
    # precedence; load_layout() repeats this cheap check before the read.
    if not os.path.exists(path):
        raise DrcError(f"file not found: {path}")
    if os.path.isdir(path):
        raise DrcError(f"not a file: {path}")

    try:
        deck = get_deck(deck_name)
        nominal_dbu_um = get_nominal_dbu(deck_name)
    except UnknownDeckError as exc:
        raise DrcError(str(exc)) from exc
    layer_names = get_layer_names(deck_name)

    layout = load_layout(path, DrcError)

    # `deck`'s threshold_dbu values are authored against nominal_dbu_um, not
    # necessarily this layout's own dbu (see DrcRule's docstring / #172) —
    # rescale so the same physical distance is checked regardless of the
    # stream's database unit.
    dbu_scale = nominal_dbu_um / layout.dbu

    # Imported lazily (after load_layout, which already paid this cost) for
    # kdb.Region() below.
    import klayout.db as kdb

    top_cells = _select_top_cells(layout, top, DrcError)

    # Deck-static: every (layer, datatype) any rule in this deck references,
    # regardless of what's actually present in `path`.
    deck_layer_tuples: set[tuple[int, int]] = set()
    for rule in deck:
        deck_layer_tuples.add(rule.layer)
        if rule.other_layer is not None:
            deck_layer_tuples.add(rule.other_layer)
        if rule.derived_layer is not None:
            # `rule.layer` is only the derived rule's *reporting* identity
            # (see DerivedLayer's docstring) -- the two layers actually read
            # to compute the checked region are these, independent of
            # whether either happens to equal `rule.layer`.
            deck_layer_tuples.add(rule.derived_layer.base)
            deck_layer_tuples.add(rule.derived_layer.intersect_with)

    # Reuse layers.py's existing per-layer enumeration (used today by
    # `klt layers`) for stream-layer enumeration, rather than a second
    # `kdb.Layout` layer walk -- see #189. `top=top` keeps this scoped to the
    # same cells `top_cells` above was just restricted to, so a scoped run's
    # `coverage` reflects only what was actually checked, not the whole
    # stream's layer usage.
    stream_layer_tuples = {
        (entry["layer"], entry["datatype"])
        for entry in layers_report(path, top=top)["layers"]
    }

    violations: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}
    rules_skipped: list[str] = []

    for rule in deck:
        # For a `derived_layer` rule (#345), the region actually checked is
        # computed from two *different* drawn layers (`derived_layer.base`/
        # `intersect_with`) rather than `rule.layer`'s own raw shapes -- see
        # `DerivedLayer`'s docstring. `rule.layer` itself is resolved only for
        # `coverage`/`layer_label` reporting below, never used to build the
        # checked region in this branch.
        base_index: int | None = None
        intersect_index: int | None = None
        layer_index: int | None = None

        if rule.derived_layer is not None:
            base_index = layout.find_layer(*rule.derived_layer.base)
            intersect_index = layout.find_layer(*rule.derived_layer.intersect_with)
            if base_index is None or intersect_index is None:
                # Either input layer of the derived region is absent from
                # this stream -> no violations possible, skip like any other
                # missing-layer rule.
                rules_skipped.append(rule.id)
                continue
        else:
            layer_index = layout.find_layer(*rule.layer)
            if layer_index is None:
                # rule's layer is absent from this stream -> no violations,
                # but record it so `coverage.rules_skipped` can surface the
                # skip.
                rules_skipped.append(rule.id)
                continue

        other_index = None
        if rule.other_layer is not None:
            other_index = layout.find_layer(*rule.other_layer)
            if other_index is None:
                rules_skipped.append(rule.id)
                continue

        layer_label = layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")

        for cell in top_cells:
            if rule.derived_layer is not None:
                # Virtual/derived region (#345): shapes of `intersect_with`
                # that already touch the *unsized* `base` region somewhere,
                # clipped to `base`'s outline oversized by `sized_by_um` --
                # see `DerivedLayer`'s docstring for the full derivation and
                # why an unscoped check against either raw input layer would
                # be wrong, not just conservative. `sized_by_um` is a real
                # micrometre distance, rescaled against this layout's own
                # `dbu` directly (unlike `rule.threshold_dbu`, which is
                # rescaled via `dbu_scale` against the deck's nominal dbu).
                base_region = kdb.Region(cell.begin_shapes_rec(base_index))
                intersect_region = kdb.Region(cell.begin_shapes_rec(intersect_index))
                size_dbu = round(rule.derived_layer.sized_by_um / layout.dbu)
                region = intersect_region.interacting(base_region) & (
                    base_region.sized(size_dbu)
                )
            else:
                region = kdb.Region(cell.begin_shapes_rec(layer_index))
            other_region = (
                kdb.Region(cell.begin_shapes_rec(other_index))
                if other_index is not None
                else None
            )

            edge_pairs, outside_region = _run_check(
                region, other_region, rule, dbu_scale
            )

            for edge_pair in edge_pairs:
                bbox = edge_pair.bbox()
                try:
                    polygon = edge_pair.polygon(0)
                    points = [[pt.x, pt.y] for pt in polygon.each_point_hull()]
                except Exception:
                    points = None

                source_cell, source_path = _attribute_to_instance(cell, bbox)
                violations.append(
                    {
                        "rule": rule.id,
                        "description": rule.description,
                        "check": rule.check,
                        "layer": layer_label,
                        "cell": cell.name,
                        "source_cell": source_cell,
                        "source_path": source_path,
                        "bbox": {
                            "left": bbox.left,
                            "bottom": bbox.bottom,
                            "right": bbox.right,
                            "top": bbox.top,
                        },
                        "polygon": points,
                    }
                )
                rule_counts[rule.id] = rule_counts.get(rule.id, 0) + 1

            # `outside_region` (only set for "enclosing"/"enclosed", see
            # _OUTSIDE_CHECKS) holds the part of the enclosed layer that
            # `enclosing_check`/`enclosed_check` structurally cannot report:
            # area with zero (or partial) spatial overlap with the enclosing
            # layer, up to and including a shape that lies entirely outside
            # it -- the worst-case enclosure violation (#318). Reported under
            # the same rule id, additive to the edge-pair violations above.
            if outside_region is not None:
                for polygon in outside_region.each_merged():
                    bbox = polygon.bbox()
                    points = [[pt.x, pt.y] for pt in polygon.each_point_hull()]

                    source_cell, source_path = _attribute_to_instance(cell, bbox)
                    violations.append(
                        {
                            "rule": rule.id,
                            "description": rule.description,
                            "check": rule.check,
                            "layer": layer_label,
                            "cell": cell.name,
                            "source_cell": source_cell,
                            "source_path": source_path,
                            "bbox": {
                                "left": bbox.left,
                                "bottom": bbox.bottom,
                                "right": bbox.right,
                                "top": bbox.top,
                            },
                            "polygon": points,
                        }
                    )
                    rule_counts[rule.id] = rule_counts.get(rule.id, 0) + 1

    violations.sort(
        key=lambda v: (
            v["rule"],
            v["cell"],
            v["bbox"]["left"],
            v["bbox"]["bottom"],
            v["bbox"]["right"],
            v["bbox"]["top"],
        )
    )

    def _fmt(layer_tuple: tuple[int, int]) -> str:
        return f"{layer_tuple[0]}/{layer_tuple[1]}"

    layers_checked = deck_layer_tuples & stream_layer_tuples
    layers_in_stream_without_rules = stream_layer_tuples - deck_layer_tuples

    # Deck-static, like `deck_layers` above: every distinct non-empty
    # DrcRule.scope this deck's rules declare, regardless of what's actually
    # present in `path` (#566).
    deck_scope = sorted({rule.scope for rule in deck if rule.scope})

    # Voltage-domain marker warnings (issue #552): a marker this deck
    # registers via `get_unmodeled_voltage_markers` (e.g. gf180mcu's
    # `Dualgate` 55/0) selects a second gate-oxide/voltage domain whose real
    # DRC thresholds this deck's rules do not encode -- geometry inside it is
    # checked against the wrong (default) column and would otherwise report
    # an unqualified `layers_checked` pass. Gated on the marker's geometry
    # actually *interacting* with at least one layer this run checked (not
    # merely present in the stream), so a marker shape that never overlaps
    # any checked geometry produces no warning with nothing behind it.
    voltage_domain_warnings: list[dict[str, str]] = []
    unmodeled_markers = get_unmodeled_voltage_markers(deck_name)
    for marker, description in sorted(unmodeled_markers.items()):
        if marker not in stream_layer_tuples:
            continue
        marker_index = layout.find_layer(*marker)
        if marker_index is None:
            continue
        interacts = False
        for cell in top_cells:
            marker_region = kdb.Region(cell.begin_shapes_rec(marker_index))
            if marker_region.is_empty():
                continue
            for checked_layer in sorted(layers_checked):
                if checked_layer == marker:
                    continue
                checked_index = layout.find_layer(*checked_layer)
                if checked_index is None:
                    continue
                checked_region = kdb.Region(cell.begin_shapes_rec(checked_index))
                if not marker_region.interacting(checked_region).is_empty():
                    interacts = True
                    break
            if interacts:
                break
        if interacts:
            voltage_domain_warnings.append(
                {"marker": _fmt(marker), "description": description}
            )

    coverage = {
        "deck_layers": [_fmt(t) for t in sorted(deck_layer_tuples)],
        "layers_checked": [_fmt(t) for t in sorted(layers_checked)],
        "layers_in_stream_without_rules": [
            _fmt(t) for t in sorted(layers_in_stream_without_rules)
        ],
        "rules_skipped": sorted(rules_skipped),
        "voltage_domain_warnings": voltage_domain_warnings,
        "deck_scope": deck_scope,
    }

    return {
        "schema_version": 1,
        "file": path,
        "deck": deck_name,
        "dbu_um": layout.dbu,
        "status": "violations" if violations else "clean",
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "violations": violations,
        "coverage": coverage,
        "provenance": build_provenance(
            deck_name=deck_name,
            deck_path=deck_source_path(deck_name),
            input_path=path,
        ),
    }


def _attribute_to_instance(
    top_cell: Any, bbox: Any
) -> tuple[str | None, list[str] | None]:
    """Map a violation ``bbox`` (in ``top_cell`` coordinates) back to the
    innermost placed instance whose own bounding box fully contains it.

    Returns a ``(source_cell, source_path)`` pair. ``source_cell`` is the
    cell-definition name of that innermost instance and ``source_path`` is
    the chain of cell names from ``top_cell``'s direct child down to it
    (inclusive). Both are ``None`` when no single placed instance contains
    ``bbox`` -- i.e. the violation is top-level geometry, or its bbox
    straddles an instance boundary (a spacing/enclosure violation *between*
    two placements belongs to neither), so the caller's top-cell attribution
    is the only honest answer.

    This never touches the flattened geometry the check actually ran on: it
    is a pure, additive lookup over the instance tree, spatially restricted
    to ``bbox`` via ``Cell.begin_instances_rec_touching`` so it stays cheap
    even for macros with hundreds of thousands of placements. The instance
    tree is walked depth-first by KLayout; among every candidate whose world
    bbox contains the violation, the *deepest* one (longest instance path)
    wins, with a deterministic tie-break (smallest world-bbox area, then
    lexicographic path) so repeated runs and different KLayout builds agree.
    """
    best_depth = -1
    best_area: int | None = None
    best_path: list[str] | None = None

    it = top_cell.begin_instances_rec_touching(bbox)
    while not it.at_end():
        inst_cell = it.inst_cell()
        world = (it.trans() * it.inst_trans()) * inst_cell.bbox()
        # Full containment (not mere touching): the violation must sit wholly
        # inside this placement for it to be the origin. `begin_..._touching`
        # over-returns edge-touching neighbours, so filter explicitly.
        if (
            world.left <= bbox.left
            and world.bottom <= bbox.bottom
            and world.right >= bbox.right
            and world.top >= bbox.top
        ):
            path = [ie.inst().cell.name for ie in it.path()]
            path.append(inst_cell.name)
            depth = len(path)
            area = world.width() * world.height()
            if (
                depth > best_depth
                or (depth == best_depth and best_area is not None and area < best_area)
                or (
                    depth == best_depth
                    and best_area is not None
                    and area == best_area
                    and best_path is not None
                    and path < best_path
                )
            ):
                best_depth = depth
                best_area = area
                best_path = path
        it.next()

    if best_path is None:
        return None, None
    return best_path[-1], best_path


def _run_check(
    region: Any, other_region: Any | None, rule: DrcRule, dbu_scale: float
) -> tuple[Any, Any | None]:
    """Dispatch to the matching ``klayout.db.Region.*_check`` primitive.

    ``dbu_scale`` (``deck's nominal_dbu_um / layout.dbu``, see
    :func:`run_drc`) rescales ``rule.threshold_dbu`` from the deck's
    authored, nominal database unit to the layout's actual one, rounding to
    the nearest whole dbu since ``Region.*_check()`` thresholds are integer
    database units.

    Returns a ``(edge_pairs, outside_region)`` pair. ``edge_pairs`` is an
    ``EdgePairs`` collection, one entry per marginal violation, as returned
    by the underlying ``Region.*_check()`` primitive. ``outside_region`` is
    ``None`` for every check kind except ``"enclosing"``/``"enclosed"``
    (see ``_OUTSIDE_CHECKS``), where it is a ``Region`` holding the part of
    an already-interacting "enclosed" shape that has zero or partial spatial
    overlap with the "enclosing" layer -- geometry ``enclosing_check``/
    ``enclosed_check`` structurally cannot report, since both only measure
    facing edges of shapes that already face each other, never a shape (or
    part of a shape) with no facing edge at all (#318). For ``"enclosing"``
    (``region`` encloses ``other_region``) this is
    ``other_region.interacting(region) - region``; for ``"enclosed"``
    (``region`` is enclosed by ``other_region``) it is the symmetric
    ``region.interacting(other_region) - other_region``.

    The ``.interacting(...)`` pre-filter (rather than a plain
    ``other_region - region``) is deliberate: it scopes the check to shapes
    of the enclosed layer that overlap the enclosing layer *somewhere* --
    catching a shape that is properly enclosed on one side but has a part
    (e.g. a protruding tab) that escapes entirely, which is this issue's
    reproducer -- while not flagging a shape of the enclosed layer that has
    *no* relationship to this rule's enclosing layer anywhere at all. That
    second case sounds identical to the first at a glance, but isn't: some
    decks reuse one physical layer as the "enclosed" side of two different
    enclosing rules for two disjoint sub-populations of that layer (e.g.
    gf180mcu's Contact layer is checked against both Poly2 -- gate contacts
    -- and Comp -- diffusion contacts; a real diffusion contact has zero
    overlap with Poly2 by design, not by defect). Our engine has no compound
    layer expressions to scope each rule to just its intended sub-population
    (see the "compound layer expression" approximation note in each deck
    module), so a plain not-inside check across the *whole* other_layer
    would flag every ordinary contact/tap against whichever of the two rules
    doesn't apply to it -- turning every realistic layout permanently
    `"violations"`. Requiring some interaction with `region` first keeps the
    fix targeted at genuine escapes of a feature this rule already covers.
    """
    check = rule.check
    d = round(rule.threshold_dbu * dbu_scale)

    if check in _SINGLE_LAYER_CHECKS:
        return getattr(region, f"{check}_check")(d), None

    if check in _TWO_LAYER_CHECKS:
        if other_region is None:
            raise DrcError(f"rule '{rule.id}': check '{check}' requires other_layer")
        edge_pairs = getattr(region, f"{check}_check")(other_region, d)

        outside_region = None
        if check in _OUTSIDE_CHECKS:
            if check == "enclosing":
                # `region` is the enclosing layer, `other_region` the
                # enclosed one -- among the `other_region` shapes that do
                # touch `region` somewhere, flag whatever part of them
                # escapes `region` entirely (a plain Boolean NOT, no
                # threshold: any escape at all is worse than a
                # marginal-distance violation).
                outside_region = other_region.interacting(region) - region
            else:  # check == "enclosed"
                # Symmetric: `region` is the enclosed layer here.
                outside_region = region.interacting(other_region) - other_region

        return edge_pairs, outside_region

    raise DrcError(f"rule '{rule.id}': unsupported check kind '{check}'")
