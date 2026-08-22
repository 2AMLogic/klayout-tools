"""Run a DRC deck headless against a GDSII/OASIS stream.

Pure library: :func:`run_drc` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``layers.py``.

Headless invariant (default engine, :func:`run_drc`): uses only the pip
``klayout`` package's batch database API (``klayout.db``) — specifically
``Region``'s native check primitives (``width_check``, ``space_check``,
``separation_check``, ``enclosing_check``, ``enclosed_check``,
``notch_check``, ``overlap_check``) — no GUI, no Qt, and by default no
dependency on the standalone ``klayout`` application binary or its DRC-DSL
script runner. See ``docs/cli/drc.md`` for the engine-choice rationale
(native ``Region`` checks vs. running the official ``.lydrc`` deck through
the full KLayout application).

An opt-in second engine, :func:`run_drc_klayout_engine` (issue #565), does
depend on that standalone binary — it shells out to it as a subprocess to
run a PDK-native DRC-DSL script directly, still headless (``klayout -b``)
but a distinct dependency posture from the default. See that function's own
docstring and ``docs/cli/drc.md`` → "Engine" → ``"klayout"``.

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

import itertools
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Any

from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells
from ._provenance import _content_hash, build_provenance
from ._report_verify import build_check_result, build_rerun_result, get_path, hash_check
from ._report_verify import load_committed_report as _load_committed_report
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

# Single-layer check kind (issue #812) whose native primitive
# (`Region.with_area`) returns violating *polygons* directly (a `Region`),
# not an `EdgePairs` collection like every check kind above -- dispatched via
# `_run_area_check` rather than `_run_check`. See `DrcRule.area_min_dbu2`/
# `area_max_dbu2`.
_AREA_CHECKS = {"area"}
# Single-layer check kind (issue #812) with no native `Region` primitive at
# all -- computed via windowed area accounting, `_run_density_check`. See
# `DrcRule.density_window_um`/`density_min`/`density_max`.
_DENSITY_CHECKS = {"density"}
# Two-layer check kind (issue #812) approximating an antenna/PAAR check as a
# flat, connectivity-free area-ratio comparison -- `_run_antenna_check`. See
# `DrcRule.antenna_ratio_max`'s docstring for why this is *not* a net-aware
# antenna check.
_ANTENNA_CHECKS = {"antenna"}

# Every `DerivedLayer.mode` `run_drc()` knows how to derive a region for --
# see that class's docstring for each one's derivation. Validated per rule
# (rather than assumed) so a deck typo fails loudly with the rule id instead
# of silently falling through to the default derivation.
_DERIVED_LAYER_MODES = {"sized_intersection", "overlapping", "not_interacting"}


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
    in ``path`` *and* its geometry interacts with at least one layer an
    **unscoped rule actually checked on this run**, one entry is added --
    ``{"marker": "<layer>/<datatype>", "description": str}`` -- naming the
    marker and the concrete consequence (the deck's own registered
    description). A ``Dualgate`` shape that never overlaps any such geometry
    produces no entry, avoiding a warning with nothing behind it.

    "Unscoped" is evaluated per rule (issue #1110), because a marker can be
    *partially* modelled: a rule whose ``derived_layer`` reads the marker
    itself -- gf180mcu's ``DF.1a``/``DF.3a`` ``_LV``/``_MV`` pairs, which
    split ``Comp`` on ``Dualgate`` via
    :attr:`~klayout_tools.decks.DerivedLayer.mode` -- applied the *right*
    column and is excluded from the gate, as is any rule skipped this run
    (it applied no threshold at all). The warning therefore keeps firing for
    the parts of a deck that still ignore the marker, and stops firing once
    every rule that touched the marked geometry reads it. Always a list,
    empty for a deck that registers no such marker or a layout that draws
    none of it overlapping unscoped checked geometry.

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
            _validate_derived_layer(rule)
            base_index = layout.find_layer(*rule.derived_layer.base)
            intersect_index = layout.find_layer(*rule.derived_layer.intersect_with)
            if base_index is None:
                # The derived region's own source shapes are absent from this
                # stream -> no violations possible in any mode, skip like any
                # other missing-layer rule.
                rules_skipped.append(rule.id)
                continue
            if intersect_index is None and rule.derived_layer.mode != "not_interacting":
                # The second input layer is absent -> both the
                # "sized_intersection" and "overlapping" derivations yield an
                # empty region, so there is nothing to check. "not_interacting"
                # is the exception: "base polygons that touch no marker" is
                # every base polygon when the marker layer was never drawn, so
                # that rule must still run against the full base region (see
                # `DerivedLayer`'s docstring) -- the ordinary thin-oxide-only
                # layout, which must stay checked against the unmarked column.
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
                # Virtual/derived region (#345, marker-scoped modes #1110) --
                # see `DerivedLayer`'s docstring for each mode's full
                # derivation and why an unscoped check against either raw
                # input layer would be wrong, not just conservative.
                # `sized_by_um` is a real micrometre distance, rescaled
                # against this layout's own `dbu` directly (unlike
                # `rule.threshold_dbu`, which is rescaled via `dbu_scale`
                # against the deck's nominal dbu).
                base_region = kdb.Region(cell.begin_shapes_rec(base_index))
                intersect_region = (
                    kdb.Region(cell.begin_shapes_rec(intersect_index))
                    if intersect_index is not None
                    else kdb.Region()
                )
                size_dbu = round(rule.derived_layer.sized_by_um / layout.dbu)
                if rule.derived_layer.mode in ("overlapping", "not_interacting"):
                    # Marker-scoped whole-polygon selection (#1110): here
                    # `sized_by_um` is a guard band around the *marker*
                    # (`intersect_with`), not around `base`.
                    marker_region = (
                        intersect_region.sized(size_dbu)
                        if size_dbu
                        else intersect_region
                    )
                    if rule.derived_layer.mode == "overlapping":
                        # Whole `base` polygons sharing area with the marker
                        # -- the marked ("_MV") half of a rule pair.
                        region = base_region.overlapping(marker_region)
                    else:
                        # Whole `base` polygons touching no marker geometry
                        # at all -- the unmarked ("_LV") half of the pair.
                        region = base_region.not_interacting(marker_region)
                else:  # "sized_intersection" (the default, issue #345)
                    # Shapes of `intersect_with` that already touch the
                    # *unsized* `base` region somewhere, clipped to `base`'s
                    # outline oversized by `sized_by_um`.
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

            # Supplementary edge pairs reported under the same rule id,
            # alongside whatever `_run_check` returns -- the same additive
            # pattern `_run_check`'s own `outside_region` escape term already
            # uses (#318). Only ever set for the derived+`"separation"`
            # same-drawn-layer case immediately below (issue #1033).
            peer_edge_pairs: Any | None = None

            if (
                rule.derived_layer is not None
                and rule.check == "separation"
                and other_region is not None
                and rule.other_layer == rule.derived_layer.intersect_with
            ):
                # A `derived_layer` "separation" rule whose `other_layer` is
                # the *same* drawn layer `derived_layer.intersect_with` reads
                # (issue #1033, e.g. gf180mcu's `mim.space.1`: `region` is the
                # virtual bottom plate carved out of `Metal4`, `other_layer`
                # is `Metal4` itself) needs `other_region` scoped to exclude
                # the plate's own footprint -- otherwise `other_region`
                # trivially contains every shape `region` was built from
                # (they're the same drawn layer), which is not a meaningful
                # "spacing to other metal" comparison.
                #
                # A plain `other_region - region` is not safe here: `region`
                # is only the *clipped* fragment of an `other_layer` polygon
                # that falls inside the oversized `base` window (see
                # `DerivedLayer`'s docstring) -- any part of that same
                # physical polygon *outside* the window would be left behind
                # in `other_region`, touching `region` at the sizing cutoff
                # with zero gap: a `separation_check` between them reports a
                # synthetic violation at that seam, an artefact of one
                # continuous shape being cut in two, not a real spacing gap.
                #
                # Instead, exclude the *whole* `other_region` polygon for any
                # polygon that already overlaps the raw (unsized) `base`
                # region anywhere. Every polygon that can contribute a
                # fragment to `region` only does so because it already passed
                # this same `.interacting(base_region)` pre-filter (see
                # `region`'s own construction above), so this removes exactly
                # the candidates `region` was built from, wholesale --
                # leaving only genuinely distinct `other_layer` geometry (e.g.
                # ordinary routing/PDN metal with no relationship to any MiM
                # structure) as "other".
                other_merged = other_region.merged()
                peer_region = other_merged.interacting(base_region)
                other_region = other_merged - peer_region

                # That wholesale exclusion leaves one half of the official
                # rule unmeasured: gf180mcu's `MIMTM.1` is spacing to the
                # bottom plate metal "whether adjacent MiM or routing
                # metal", and every *adjacent MiM* plate has just been
                # excluded from `other_region` along with this rule's own
                # plate (both overlap `base`). Measure that half here, as a
                # peer-to-peer check among the excluded, plate-bearing
                # polygons themselves, and report it under the same rule id.
                #
                # `isolated_check` (spacing between *different* polygons of
                # one region), not `space_check` (which also measures
                # intra-polygon notches): a single MiM's own bottom plate
                # metal is one polygon after `.merged()`, and a legitimate
                # slot in wide plate metal (a routine density/stress-relief
                # feature, typically ~1um wide) is not "spacing to another
                # bottom plate" -- `space_check` would flag it, a fresh
                # false positive of exactly the kind this issue is about.
                #
                # Measured on the *whole* plate-bearing polygons rather than
                # the sizing-clipped virtual plates, which is deliberate:
                # clipped plates split a shared bottom plate carrying two
                # top plates more than `2 * sized_by_um` apart into two
                # region polygons with no real gap between them (the metal
                # is continuous), which `isolated_check` would flag. The
                # residual conservatism this trades for -- two plate-bearing
                # polygons whose *facing* metal is routing tail rather than
                # virtual plate on both sides -- is documented in the deck's
                # own `mim.space.1` note.
                peer_edge_pairs = peer_region.isolated_check(
                    round(rule.threshold_dbu * dbu_scale)
                )

            if rule.check in _AREA_CHECKS:
                for polygon in _run_area_check(region, rule, dbu_scale).each_merged():
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
                continue

            if rule.check in _DENSITY_CHECKS:
                for window_box in _run_density_check(region, rule, layout.dbu):
                    points = [
                        [window_box.left, window_box.bottom],
                        [window_box.right, window_box.bottom],
                        [window_box.right, window_box.top],
                        [window_box.left, window_box.top],
                    ]
                    source_cell, source_path = _attribute_to_instance(cell, window_box)
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
                                "left": window_box.left,
                                "bottom": window_box.bottom,
                                "right": window_box.right,
                                "top": window_box.top,
                            },
                            "polygon": points,
                        }
                    )
                    rule_counts[rule.id] = rule_counts.get(rule.id, 0) + 1
                continue

            if rule.check in _ANTENNA_CHECKS:
                antenna_bbox = _run_antenna_check(region, other_region, rule)
                if antenna_bbox is not None:
                    source_cell, source_path = _attribute_to_instance(
                        cell, antenna_bbox
                    )
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
                                "left": antenna_bbox.left,
                                "bottom": antenna_bbox.bottom,
                                "right": antenna_bbox.right,
                                "top": antenna_bbox.top,
                            },
                            # Flat, whole-cell aggregate -- no single real
                            # polygon corresponds to "the violation" the way
                            # an "area" or edge-pair check's does, see
                            # `_run_antenna_check`'s docstring.
                            "polygon": None,
                        }
                    )
                    rule_counts[rule.id] = rule_counts.get(rule.id, 0) + 1
                continue

            edge_pairs, outside_region = _run_check(
                region, other_region, rule, dbu_scale
            )

            checked_edge_pairs = (
                edge_pairs
                if peer_edge_pairs is None
                else itertools.chain(edge_pairs, peer_edge_pairs)
            )

            for edge_pair in checked_edge_pairs:
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
    # actually *interacting* with at least one layer an **unscoped** rule
    # checked on this run (not merely present in the stream), so a marker
    # shape that never overlaps any such geometry produces no warning with
    # nothing behind it.
    #
    # "Unscoped" is per rule, not per deck (issue #1110): a marker can be
    # partly modelled. gf180mcu's `DF.1a`/`DF.3a` now ship as `_LV`/`_MV`
    # rule pairs that read `Dualgate` themselves via `DerivedLayer`'s
    # `"overlapping"`/`"not_interacting"` modes, so geometry those rules
    # checked was *not* checked against the wrong column and must not
    # produce a warning -- while every rule that ran without reading the
    # marker still can. Rules skipped this run (their layers absent from
    # this stream) are excluded too: a rule that never ran applied no
    # threshold, right or wrong.
    voltage_domain_warnings: list[dict[str, str]] = []
    unmodeled_markers = get_unmodeled_voltage_markers(deck_name)
    skipped_rule_ids = set(rules_skipped)
    for marker, description in sorted(unmodeled_markers.items()):
        if marker not in stream_layer_tuples:
            continue
        marker_index = layout.find_layer(*marker)
        if marker_index is None:
            continue
        unscoped_layers: set[tuple[int, int]] = set()
        for rule in deck:
            if rule.id in skipped_rule_ids:
                continue
            if rule.derived_layer is not None and marker in (
                rule.derived_layer.base,
                rule.derived_layer.intersect_with,
            ):
                # This rule reads the marker itself -- its checked region is
                # already scoped by the marker, so it is not part of the gap
                # this warning is about.
                continue
            unscoped_layers |= _rule_input_layers(rule)
        unscoped_layers &= stream_layer_tuples
        unscoped_layers.discard(marker)
        interacts = False
        for cell in top_cells:
            marker_region = kdb.Region(cell.begin_shapes_rec(marker_index))
            if marker_region.is_empty():
                continue
            for checked_layer in sorted(unscoped_layers):
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


def _deck_path_for_report(committed: dict[str, Any]) -> str | None:
    """The on-disk deck path a committed ``klt drc`` report's own ``deck``
    field names, for re-hashing (issue #1106).

    ``--engine curated`` reports store the deck *name* (``"sky130"``, ...)
    under ``deck`` -- resolved to a source-module path via
    :func:`deck_source_path`, exactly as :func:`run_drc` itself does when it
    first builds ``provenance.deck.content_hash``. ``--engine klayout``
    reports (``committed["engine"] == "klayout"``) instead store the deck
    *script's own path* directly under ``deck`` (see
    :func:`run_drc_klayout_engine`'s docstring -- there is no separate short
    name for an arbitrary PDK-native script), so it is used as-is.
    """
    deck = committed.get("deck")
    if deck is None:
        return None
    if committed.get("engine") == "klayout":
        return deck
    return deck_source_path(deck)


def check_drc_report(report_path: str) -> dict[str, Any]:
    """``klt drc --check`` (cheap mode, issue #1106): verify a previously
    committed ``klt drc --format json`` report at ``report_path`` still
    reproduces, without re-running the DRC engine at all.

    Re-hashes the input layout stream (``committed["file"]``) and the deck
    (:func:`_deck_path_for_report`) named in the committed report and
    compares each against the ``sha256:``-prefixed digest already recorded
    in its own ``provenance.input.content_hash``/``provenance.deck
    .content_hash`` -- reusing :func:`klayout_tools._provenance
    .sha256_file`/its internal ``sha256:``-prefixing convention, never
    reimplementing hashing (see ``_provenance.py``). Returns the shared
    ``--check`` payload built by
    :func:`klayout_tools._report_verify.build_check_result`: ``status:
    "match"`` when both hashes agree, ``"drifted"`` (naming which one moved)
    otherwise. A recorded hash that is itself ``None`` (a report predating
    issue #331, before ``provenance.input``/``deck.content_hash`` existed)
    never counts as a match -- see
    :func:`klayout_tools._report_verify.hash_check`'s docstring.

    Raises :class:`DrcError` for a missing/unparseable committed report
    (:func:`klayout_tools._report_verify.load_committed_report`) -- never a
    traceback.
    """
    committed = _load_committed_report(report_path, DrcError)
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            _content_hash(committed.get("file")),
        ),
        hash_check(
            "provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            _content_hash(_deck_path_for_report(committed)),
        ),
    ]
    return build_check_result(report_path=report_path, checks=checks)


def rerun_drc_report(report_path: str) -> dict[str, Any]:
    """``klt drc --check <report> --rerun`` (full mode, issue #1106):
    verify a previously committed ``klt drc --format json`` report at
    ``report_path`` by actually re-running the DRC deck it names and
    diffing the fresh report against the committed one.

    Re-runs against exactly the ``file``/``deck``/``engine`` the committed
    report itself names -- :func:`run_drc_klayout_engine` when
    ``committed["engine"] == "klayout"``, :func:`run_drc` (the curated
    engine) otherwise. **Known limitation**: neither the curated nor the
    ``klayout`` engine's report records which ``--top`` (if any) the
    original run used, so ``--rerun`` always re-checks every top cell
    (``top=None``) -- a report originally scoped to one top cell via
    ``--top`` will legitimately drift under ``--rerun`` if the stream has
    others. Use ``--check`` (cheap mode) to avoid this if that matters.

    Diffs the fresh report against the committed one via
    :func:`klayout_tools._report_verify.diff_verdict_fields`, excluding
    :data:`klayout_tools._report_verify.VOLATILE_PROVENANCE_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` --
    fields that legitimately vary between runs of identical inputs).
    ``status: "drifted"`` names every other field that changed, including a
    changed ``violation_count``/``violations`` (the DRC-outcome-changed
    case) as well as a changed ``provenance.input.content_hash`` (the
    input-moved case ``--check`` also catches, redundantly but harmlessly
    here since this mode always re-hashes as a side effect of re-running).

    Raises :class:`DrcError` for a missing/unparseable committed report, a
    report missing ``file``/``deck`` to rerun, or any error the rerun itself
    raises (bad file, unknown deck, engine error) -- never a traceback.
    """
    committed = _load_committed_report(report_path, DrcError)
    file_path = committed.get("file")
    if not file_path:
        raise DrcError(f"committed report has no 'file' field to rerun: {report_path}")
    deck = committed.get("deck")
    if not deck:
        raise DrcError(f"committed report has no 'deck' field to rerun: {report_path}")

    if committed.get("engine") == "klayout":
        fresh = run_drc_klayout_engine(file_path, deck, top=None)
    else:
        fresh = run_drc(file_path, deck, top=None)

    return build_rerun_result(report_path=report_path, committed=committed, fresh=fresh)


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

    Both regions are merged first (#995) -- see the comment on the
    ``.merged()`` calls below for why KLayout's own merged semantics is not
    enough for ``enclosing_check``/``enclosed_check``.

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

    # Merge each checked region before measuring anything (#995). A layer
    # drawn as several abutting (touching, non-overlapping) shapes rather
    # than one polygon is *one* physical region -- an ordinary GDS
    # authoring/tiling choice, not a drawn defect -- but the raw shape
    # iterator `run_drc` builds these regions from preserves the seams.
    # KLayout's own "merged semantics" only papers over this for some of the
    # primitives dispatched below (empirically: the single-layer checks and
    # the `other` argument of a two-layer check); `enclosing_check`/
    # `enclosed_check` still measure the *primary* region's raw polygon
    # edges, so a cut sitting close to an internal seam is measured against
    # that seam instead of the merged region's real outer edge -- a
    # false-positive enclosure violation on geometry with hundreds of dbu of
    # true margin. Merging is monotonic for these checks: the merged region
    # covers exactly the same area with weakly fewer edges, so this only
    # removes false positives, never masks a real shortfall (the
    # `..._real_shortfall` regression tests pin that half).
    region = region.merged()
    if other_region is not None:
        other_region = other_region.merged()

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


def _rule_input_layers(rule: DrcRule) -> set[tuple[int, int]]:
    """The ``(layer, datatype)`` pairs whose *shapes* ``rule`` actually reads.

    Distinct from ``run_drc``'s own ``deck_layer_tuples`` (which also carries
    every rule's ``layer`` unconditionally, since that is the rule's
    reporting identity in ``violations[].layer``/``coverage`` even when the
    checked region is derived from two other drawn layers): a
    ``derived_layer`` rule reads ``derived_layer.base``/``intersect_with``,
    *not* ``layer``. Used by the ``coverage.voltage_domain_warnings`` gate
    (issue #1110), which must reason about which geometry a rule really
    applied a threshold to.
    """
    layers = set()
    if rule.derived_layer is not None:
        layers.add(rule.derived_layer.base)
        layers.add(rule.derived_layer.intersect_with)
    else:
        layers.add(rule.layer)
    if rule.other_layer is not None:
        layers.add(rule.other_layer)
    return layers


def _validate_derived_layer(rule: DrcRule) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose
    ``derived_layer.mode`` names a derivation this engine has no branch for
    (issue #1110).

    Mirrors ``_run_area_check``'s own "a deck-authoring mistake fails loudly
    with the rule id, never silently returns nothing" contract: an unknown
    mode would otherwise fall through to the default ``"sized_intersection"``
    derivation and quietly check a completely different region than the deck
    author asked for.
    """
    derived = rule.derived_layer
    if derived is not None and derived.mode not in _DERIVED_LAYER_MODES:
        raise DrcError(
            f"rule '{rule.id}': unknown derived_layer mode "
            f"'{derived.mode}' (known: {', '.join(sorted(_DERIVED_LAYER_MODES))})"
        )


def _run_area_check(region: Any, rule: DrcRule, dbu_scale: float) -> Any:
    """``check="area"`` (issue #812): the violating polygons of ``region``
    (a ``Region``), driven by ``klayout.db.Region.with_area(min_area,
    max_area, inverse=True)``.

    Unlike every check in ``_SINGLE_LAYER_CHECKS``/``_TWO_LAYER_CHECKS``,
    ``with_area`` already returns exactly the shapes that *violate* the
    bound (area below ``min_area``, or at/above ``max_area``) when
    ``inverse=True`` -- there is no separate edge-pair-shaped "violation"
    concept for an area check, so callers iterate the returned ``Region``'s
    polygons directly (see ``run_drc``'s ``_AREA_CHECKS`` branch), the same
    way ``_run_check``'s ``outside_region`` escape term is already reported.

    ``rule.area_min_dbu2``/``area_max_dbu2`` are expressed in **square**
    database units of the deck's own nominal dbu -- rescaled here by
    ``dbu_scale ** 2`` (not the plain ``dbu_scale`` a linear-distance
    threshold uses), since an area scales with the square of a linear
    rescaling; reusing ``dbu_scale`` unscaled would silently miscompute the
    threshold by orders of magnitude for any layout whose dbu differs from
    the deck's nominal one. At least one of ``area_min_dbu2``/
    ``area_max_dbu2`` must be set -- both absent means this rule can never
    detect anything, almost certainly a deck-authoring mistake, so this
    raises :class:`DrcError` rather than silently returning an empty
    result.
    """
    if rule.area_min_dbu2 is None and rule.area_max_dbu2 is None:
        raise DrcError(
            f"rule '{rule.id}': check 'area' requires area_min_dbu2 and/or "
            "area_max_dbu2"
        )
    min_area = (
        None
        if rule.area_min_dbu2 is None
        else round(rule.area_min_dbu2 * dbu_scale * dbu_scale)
    )
    max_area = (
        None
        if rule.area_max_dbu2 is None
        else round(rule.area_max_dbu2 * dbu_scale * dbu_scale)
    )
    return region.with_area(min_area, max_area, True)


def _run_density_check(region: Any, rule: DrcRule, dbu: float) -> list[Any]:
    """``check="density"`` (issue #812): windowed area-density accounting,
    since no native ``klayout.db.Region`` primitive computes this.

    Tiles ``region.bbox()`` -- the checked layer's own drawn extent in
    *this* cell, not a chip-boundary layer this engine has no concept of --
    into non-overlapping ``rule.density_window_um`` squares (rescaled
    against the layout's own ``dbu`` directly, like
    ``DerivedLayer.sized_by_um``, since a window size has no natural
    "nominal dbu" the way a rule threshold does). For each window, the
    covered-area fraction is ``(region & window).area() / window.area()``;
    a window outside ``[rule.density_min, rule.density_max]`` (either bound
    may be ``None`` for "no floor"/"no ceiling") is returned as one
    violation, reported by the caller as a single rectangular polygon
    matching the window itself.

    Only whole windows entirely inside ``region.bbox()`` are tiled -- a
    remainder narrower than one window at the right/top edge is left
    unchecked, a documented approximation of this first cut (see
    ``docs/cli/drc.md``), not a defect. An empty ``region`` (nothing drawn
    on this layer in this cell) tiles no windows and so reports no
    violations, the same "nothing to check" behaviour every other check
    kind has for an empty input region.

    Raises :class:`DrcError` if neither ``density_min`` nor ``density_max``
    is set (an unbounded rule can never detect anything, almost certainly a
    deck-authoring mistake), or if ``density_window_um`` is missing or does
    not round to at least one whole database unit at this layout's ``dbu``.
    """
    if rule.density_min is None and rule.density_max is None:
        raise DrcError(
            f"rule '{rule.id}': check 'density' requires density_min and/or density_max"
        )
    if rule.density_window_um is None:
        raise DrcError(f"rule '{rule.id}': check 'density' requires density_window_um")

    import klayout.db as kdb

    window_dbu = round(rule.density_window_um / dbu)
    if window_dbu <= 0:
        raise DrcError(
            f"rule '{rule.id}': density_window_um={rule.density_window_um} is "
            f"too small to represent at dbu={dbu}"
        )

    bbox = region.bbox()
    violations: list[Any] = []
    if bbox.empty():
        return violations

    window_area = window_dbu * window_dbu
    y = bbox.bottom
    while y + window_dbu <= bbox.top:
        x = bbox.left
        while x + window_dbu <= bbox.right:
            window_box = kdb.Box(x, y, x + window_dbu, y + window_dbu)
            covered = region & kdb.Region(window_box)
            covered_area = sum(p.area() for p in covered.each_merged())
            density = covered_area / window_area
            below_min = rule.density_min is not None and density < rule.density_min
            above_max = rule.density_max is not None and density > rule.density_max
            if below_min or above_max:
                violations.append(window_box)
            x += window_dbu
        y += window_dbu

    return violations


def _run_antenna_check(region: Any, other_region: Any | None, rule: DrcRule) -> Any:
    """``check="antenna"`` (issue #812): a flat, connectivity-free
    approximation of an antenna/process-antenna-area-ratio (PAAR) check.

    **This is not a net-aware antenna check.** A real antenna/PAAR rule
    accumulates conductor area *per net*, reset at each via level -- a
    question this purely-geometric engine cannot answer without net
    extraction (``extract.py``'s ``LayoutToNetlist`` machinery, a different
    code path entirely, not wired into this module). Instead, this sums
    ``region``'s and ``other_region``'s total merged area across the whole
    checked cell (no per-net split) and reports at most one flat violation
    per ``(rule, cell)`` when ``area(region) / area(other_region)`` exceeds
    ``rule.antenna_ratio_max`` -- or when ``region`` has nonzero area but
    ``other_region`` has none at all (an undefined/infinite ratio, treated
    as a violation whenever ``region`` is present). Mirrors how
    ``"enclosing"``/``"enclosed"`` already document their own approximation
    of the official rule they check (see ``DrcRule``'s docstring); a future
    golden-pair author must not assume more precision than this primitive
    actually has.

    Returns ``region``'s own bounding box (``kdb.Box``) as the violation's
    reported location when the ratio is exceeded, or ``None`` when it is
    not -- there is no single real polygon that "is" this flat, whole-cell
    violation the way there is for ``"area"``, so the caller reports
    ``polygon: null`` for it (see ``run_drc``'s ``_ANTENNA_CHECKS`` branch).

    Raises :class:`DrcError` if ``rule.antenna_ratio_max`` is unset or
    ``other_region`` is ``None`` (a two-layer check kind missing its second
    layer, the same requirement ``_run_check`` enforces for
    ``_TWO_LAYER_CHECKS``).
    """
    if other_region is None:
        raise DrcError(f"rule '{rule.id}': check 'antenna' requires other_layer")
    if rule.antenna_ratio_max is None:
        raise DrcError(f"rule '{rule.id}': check 'antenna' requires antenna_ratio_max")

    layer_area = sum(p.area() for p in region.each_merged())
    if layer_area == 0:
        return None

    other_area = sum(p.area() for p in other_region.each_merged())
    if other_area == 0:
        # Nonzero antenna-layer area with zero protection-layer area
        # anywhere in this cell -- an undefined ratio, always worse than any
        # finite `antenna_ratio_max`.
        return region.bbox()

    ratio = layer_area / other_area
    if ratio > rule.antenna_ratio_max:
        return region.bbox()
    return None


# --------------------------------------------------------------------------- #
# "klayout" engine (issue #565): opt-in subprocess wrapper around the
# standalone `klayout` application binary, running a PDK-native DRC-DSL
# script (`.lydrc`/`.drc`) instead of this module's own curated `DrcRule`
# tables. Mirrors `lvs.py`'s `"netgen"` engine (issue #343) closely --
# binary-resolution-without-`shutil.which`-precheck, timeout handling, and
# "never trust the exit code, parse the tool's own report" discipline are all
# copied from `_run_netgen_lvs`/`_parse_netgen_report`/
# `_cleanup_netgen_work_dir` (see those functions' docstrings in `lvs.py`).
# See `docs/cli/drc.md`, "Engine" -> "klayout" (opt-in, issue #565) for the
# caller-facing writeup.
# --------------------------------------------------------------------------- #

#: Default `klayout -b -r ...` wall-clock budget -- the same idiom as
#: `lvs.py`'s `_NETGEN_DEFAULT_TIMEOUT_S`, but scoped to a single DRC deck
#: run rather than a netlist compare. Overridable via `klt drc`'s
#: `--timeout-s` flag.
KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S = 300.0

#: Matches a coordinate pair inside a KLayout report-database (`.lyrdb`) item
#: value string, e.g. the `(0.2,1;0.2,0)` in
#: `"edge-pair: (0,0;0,1)|(0.2,1;0.2,0)"` or the `(0,0;0,1;0.2,1;0.2,0)` in
#: `"polygon: (0,0;0,1;0.2,1;0.2,0)"` (both forms verified empirically against
#: a real `klayout -b -r ... -rd report=...lyrdb` run for this issue --
#: KLayout's RDB writer always reports coordinates in the layout's *user*
#: units, i.e. micrometres, regardless of the check kind). Deliberately
#: generic across every value kind (`edge-pair:`, `polygon:`, `box:`,
#: `edge:`, ...) rather than one regex per kind, since an arbitrary
#: PDK-native deck can emit `.output()` calls this module has no static list
#: of.
_RDB_COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def _strip_rdb_quotes(text: str) -> str:
    """Strip a matching pair of leading/trailing single quotes KLayout's RDB
    writer sometimes wraps a category name in (empirically, when the name
    contains a period -- `'.'` is the RDB category-path hierarchy separator,
    so a literal rule id like ``"poly.width.1"`` is quoted to disambiguate it
    from a three-level category path; a name with no period, e.g. ``"L1"``,
    is written bare). Both forms were verified against real
    ``klayout -b -r ...`` output for this issue -- this normalises them back
    to the same rule id either way."""
    text = text.strip()
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return text


def _rdb_value_points_um(value_text: str) -> list[tuple[float, float]]:
    """Every coordinate pair in one RDB item ``<value>`` string, in the
    layout's user units (micrometres) -- see :data:`_RDB_COORD_RE`."""
    return [(float(x), float(y)) for x, y in _RDB_COORD_RE.findall(value_text)]


def _parse_klayout_rdb_report(
    report_path: str, dbu: float
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Classify a KLayout report-database (``.lyrdb``, RDB XML) file into
    ``(violations, rule_counts)`` matching ``klt drc``'s existing
    ``violations[]``/``rule_counts`` shape (see :func:`run_drc`'s docstring).

    Unlike the curated engine (:func:`run_drc`), an arbitrary PDK-native
    ``.lydrc``/``.drc`` script is not built from this module's own
    :class:`~klayout_tools.decks.DrcRule` table -- there is no declarative
    ``check``/``layer`` identity to report per violation, only whatever the
    deck's own ``report(...)``/``.output(...)`` calls wrote to the RDB. This
    parser is therefore necessarily generic: each RDB ``<category>`` becomes
    one ``violations[].rule`` id (with its ``<description>`` carried
    through), ``violations[].check`` is always the literal string
    ``"external"`` (this module cannot recover a `width`/`space`/... kind
    from RDB XML alone), and ``violations[].layer`` echoes the same rule id
    (an external deck's own category naming is the only per-violation layer
    identity available). ``source_cell``/``source_path`` are always ``None``
    -- per-instance attribution (see :func:`_attribute_to_instance`) needs
    this module's own instance-tree walk against the *input* layout, which an
    RDB report does not carry.

    ``bbox``/``polygon`` are derived from every coordinate pair embedded in
    an item's ``<values><value>`` text (see :data:`_RDB_COORD_RE`), converted
    from the RDB's reported micrometre user units back to the input layout's
    own database units via ``dbu`` (``round(value_um / dbu)``) -- the same
    integer-dbu convention :func:`run_drc`'s own ``violations[].bbox`` uses.
    ``polygon`` is populated only for a value whose text starts with
    ``"polygon:"`` (a plain closed-polygon vertex list echoes back
    faithfully); every other value kind (``edge-pair:``, ``box:``, ``edge:``,
    ...) still contributes its points to the ``bbox`` bound but leaves
    ``polygon`` ``None``, matching :func:`run_drc`'s own "``None`` if the
    check produced a degenerate edge pair" convention for the analogous case.
    An item with no recognisable coordinate payload at all gets a degenerate
    zero ``bbox`` rather than being silently dropped -- mirroring
    ``_parse_netgen_report``'s "never silently drop a declared defect"
    discipline (`lvs.py`).

    Raises :class:`DrcError` if ``report_path`` is not well-formed XML at
    all -- a malformed/truncated report must never silently parse as "no
    violations found."
    """
    try:
        tree = ET.parse(report_path)
    except ET.ParseError as exc:
        raise DrcError(
            f"could not parse klayout DRC report '{report_path}': {exc}"
        ) from exc

    root = tree.getroot()

    descriptions: dict[str, str] = {}
    categories_el = root.find("categories")
    if categories_el is not None:
        for category_el in categories_el.findall("category"):
            name_el = category_el.find("name")
            if name_el is None or not name_el.text:
                continue
            rule_id = _strip_rdb_quotes(name_el.text)
            desc_el = category_el.find("description")
            descriptions[rule_id] = (
                desc_el.text if desc_el is not None and desc_el.text else ""
            )

    violations: list[dict[str, Any]] = []
    rule_counts: dict[str, int] = {}

    items_el = root.find("items")
    if items_el is None:
        return violations, rule_counts

    for item_el in items_el.findall("item"):
        category_el = item_el.find("category")
        cell_el = item_el.find("cell")
        if (
            category_el is None
            or not category_el.text
            or cell_el is None
            or not cell_el.text
        ):
            continue
        rule_id = _strip_rdb_quotes(category_el.text)
        cell_name = cell_el.text

        points_um: list[tuple[float, float]] = []
        polygon_points_um: list[tuple[float, float]] | None = None
        values_el = item_el.find("values")
        if values_el is not None:
            for value_el in values_el.findall("value"):
                if not value_el.text:
                    continue
                value_text = value_el.text
                value_points = _rdb_value_points_um(value_text)
                points_um.extend(value_points)
                if value_text.lstrip().startswith("polygon:") and value_points:
                    polygon_points_um = value_points

        if points_um:
            xs = [round(x / dbu) for x, _ in points_um]
            ys = [round(y / dbu) for _, y in points_um]
            bbox = {
                "left": min(xs),
                "bottom": min(ys),
                "right": max(xs),
                "top": max(ys),
            }
            polygon = (
                [[round(x / dbu), round(y / dbu)] for x, y in polygon_points_um]
                if polygon_points_um
                else None
            )
        else:
            bbox = {"left": 0, "bottom": 0, "right": 0, "top": 0}
            polygon = None

        violations.append(
            {
                "rule": rule_id,
                "description": descriptions.get(rule_id, ""),
                "check": "external",
                "layer": rule_id,
                "cell": cell_name,
                "source_cell": None,
                "source_path": None,
                "bbox": bbox,
                "polygon": polygon,
            }
        )
        rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1

    return violations, rule_counts


def _cleanup_klayout_drc_work_dir(work_dir: str) -> None:
    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)


def run_drc_klayout_engine(
    path: str,
    deck_file: str,
    top: str | None = None,
    timeout_s: float = KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    deck_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a PDK-native KLayout DRC-DSL rule-deck script (``deck_file``,
    typically resolved via :func:`klayout_tools.pdk.drc_deck_file` or an
    explicit ``--deck-file`` override) against the layout at ``path``, via
    the standalone ``klayout`` application binary -- opt-in alongside
    :func:`run_drc`'s curated, pip-only engine (see ``docs/cli/drc.md``,
    "Engine").

    Invokes::

        klayout -b -r <deck_file> -rd input=<path> -rd report=<tmp>.lyrdb

    -- the standard batch-mode DRC-DSL invocation KLayout's own tooling
    documents and open_pdks' sky130 deck itself embeds as a usage comment
    (verified for this issue against a real ``volare``-fetched sky130A
    install's ``libs.tech/klayout/drc/sky130A.lydrc``). The report path is
    always a ``.lyrdb`` (RDB XML) file, regardless of what ``deck_file``
    itself would otherwise default to, so the result is always the
    structured format :func:`_parse_klayout_rdb_report` reads -- never
    KLayout's plain-text list alternative.

    ``deck_vars`` (issue #1302, CLI: repeatable ``--deck-var name=value``)
    appends additional ``-rd name=value`` pairs to the invocation, after the
    ``input``/``report`` pair, in insertion order. Some PDK-native decks are
    assembled from feature-toggle globals a PDK's own run wrapper would
    normally set (FEOL/BEOL enable flags, a metal-stack selector, etc.) and
    gate every rule behind them -- without a way to set those extra globals,
    every rule's enable condition evaluates false and the deck silently
    checks nothing while still producing a well-formed, empty report (a
    false "clean" verdict indistinguishable from a real one). ``deck_vars``
    is empty/``None`` by default, so the existing zero-config contract for a
    self-contained deck (e.g. sky130A's ``sky130A.lydrc``, which needs only
    ``input``/``report``) is unchanged.

    Mirrors ``lvs.py``'s ``_run_netgen_lvs`` in every structural way that
    module's docstring calls out:

    - **Binary resolution**: no ``shutil.which`` precheck -- ``subprocess.run``
      is attempted directly and a ``FileNotFoundError`` is caught and
      re-raised as an actionable :class:`DrcError` naming the missing binary
      and how to get one, or how to fall back to the curated engine.
    - **Timeout**: ``subprocess.TimeoutExpired`` -> :class:`DrcError`.
    - **Never trusts the exit code.** ``klayout -b -r`` can exit ``0`` even
      when the deck script itself errored out before reaching
      ``report(...)`` (e.g. a deck expecting a variable this invocation does
      not set) -- the *report file's own presence* is the only trustworthy
      completion signal, mirroring netgen's "no log file at all" check.
      Missing report file -> :class:`DrcError` carrying klayout's own
      stdout/stderr, never a silent ``status: "clean"``.
    - **Workdir**: ``tempfile.mkdtemp``, cleaned up via
      :func:`_cleanup_klayout_drc_work_dir` (``shutil.rmtree(...,
      ignore_errors=True)``) in a ``finally``.

    ``top`` is not yet supported for this engine (unlike :func:`run_drc`'s
    curated engine, issue #554): an arbitrary PDK-native DRC-DSL script has
    no standard ``-rd``-settable "restrict to this one top cell" variable
    this module can rely on generically (`sky130A.lydrc`, e.g., always reads
    the whole ``$input`` stream). Passing ``top`` raises :class:`DrcError`
    rather than silently ignoring the caller's stated scope request --
    mirroring how the ``"netgen"`` LVS engine rejects request fields it has
    no equivalent hook for (``lvs.py``'s ``hints``/``reference.device_bulk``/
    ``options.parameter_tolerance`` checks).

    The returned dict matches :func:`run_drc`'s documented top-level shape
    (``schema_version``, ``file``, ``deck``, ``dbu_um``, ``status``,
    ``violation_count``, ``rule_counts``, ``violations``, ``coverage``,
    ``provenance``) plus one additive field, ``"engine": "klayout"`` (the
    curated engine's own output carries no ``engine`` key at all, unchanged,
    since it has always been the sole engine until this issue). ``deck`` is
    ``deck_file`` itself (there is no separate short deck *name* the way the
    curated engine's ``sky130``/``gf180mcu`` deck identifiers are -- an
    arbitrary PDK-native script is identified by its own path).

    Unlike the curated engine, ``coverage`` cannot be meaningfully populated
    for an externally-run script this module has no declarative rule table
    for -- every ``coverage`` sub-field is an empty list (never fabricated),
    a known, documented limitation (see ``docs/cli/drc.md``, "Engine" ->
    "klayout").

    Raises :class:`DrcError` for every failure mode above, plus a missing/
    unreadable ``path`` or ``deck_file`` (checked before the subprocess is
    launched, the same fail-fast order :func:`run_drc` uses for ``path``).
    """
    if top is not None:
        raise DrcError(
            "the klayout engine does not support --top yet -- omit --top, "
            "or use the curated deck engine (the default) instead"
        )
    if not os.path.isfile(deck_file):
        raise DrcError(f"deck file not found: {deck_file}")

    # Validates `path` (missing/directory/unreadable) before the subprocess
    # is launched, the same fail-fast order `run_drc` uses -- and gives this
    # engine the input layout's own `dbu`, needed to convert the RDB report's
    # micrometre coordinates back to database units (see
    # `_parse_klayout_rdb_report`).
    layout = load_layout(path, DrcError)
    dbu = layout.dbu

    work_dir = tempfile.mkdtemp(prefix="klt-drc-klayout-")
    try:
        report_path = os.path.join(work_dir, "report.lyrdb")
        cmd = [
            "klayout",
            "-b",
            "-r",
            deck_file,
            "-rd",
            f"input={path}",
            "-rd",
            f"report={report_path}",
        ]
        for name, value in (deck_vars or {}).items():
            cmd.extend(["-rd", f"{name}={value}"])
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            raise DrcError(
                "could not launch klayout: binary not found on PATH. "
                "Install KLayout (https://www.klayout.de/build.html) or "
                "omit --engine klayout to use the curated deck instead. "
                f"({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DrcError(
                f"klayout did not complete within {timeout_s}s (raise "
                "--timeout-s to allow more time)"
            ) from exc

        if not os.path.isfile(report_path):
            # klayout -b -r can exit 0 even when the deck script errored out
            # before reaching report(...) -- never trust the exit code alone
            # (see this function's docstring). No report file at all means no
            # trustworthy verdict is possible; surface klayout's own
            # stdout/stderr rather than a bare "no report" message.
            raise DrcError(
                "klayout did not produce a report file -- the deck script "
                "likely failed before completing. klayout's own output:\n"
                + (completed.stdout or completed.stderr or "").strip()
            )

        violations, rule_counts = _parse_klayout_rdb_report(report_path, dbu)
    finally:
        _cleanup_klayout_drc_work_dir(work_dir)

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

    return {
        "schema_version": 1,
        "file": path,
        "deck": deck_file,
        "engine": "klayout",
        "dbu_um": dbu,
        "status": "violations" if violations else "clean",
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "violations": violations,
        "coverage": {
            "deck_layers": [],
            "layers_checked": [],
            "layers_in_stream_without_rules": [],
            "rules_skipped": [],
            "voltage_domain_warnings": [],
            "deck_scope": [],
        },
        "provenance": build_provenance(
            deck_name=os.path.basename(deck_file),
            deck_path=deck_file,
            input_path=path,
            deck_options=deck_vars,
        ),
    }
