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

import fnmatch
import itertools
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import env_provenance
from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells
from ._paths import _load_request_json
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape
from ._provenance import _content_hash, build_provenance
from ._report_verify import (
    build_check_result,
    build_rerun_result,
    get_path,
    hash_check,
    resolve_committed_path,
)
from ._report_verify import load_committed_report as _load_committed_report
from .coverage import (
    REASON_ALL_RULES_SKIPPED,
    REASON_DECK_HAS_NO_RULES,
    build_check_coverage,
    coverage_rollup,
    rollup_status,
)
from .decks import (
    CompCoverageSpec,
    DrcRule,
    TapDistanceSpec,
    UnknownDeckError,
    deck_source_path,
    get_deck,
    get_layer_names,
    get_marker_blind_layers,
    get_nominal_dbu,
    get_unchecked_rules,
    get_unmodeled_rules,
    get_unmodeled_voltage_markers,
)
from .layers import layers_report
from .metrics import is_registered
from .pdk import PdkNotFoundError, find_pdk

# Check kinds that operate on a single region (no other_layer).
# "isolated" (issue #1654) dispatches to `Region.isolated_check`, which
# -- unlike "space" (`Region.space_check`) -- measures spacing between
# *different* polygons of a merged region only, never a concave notch
# carved into one polygon. It exists as a first-class check kind so a rule
# transcribed from a source rule whose real semantics is `isolated` (e.g.
# sky130's `nwell.2a`, see `decks/sky130.py`'s `nwell.space.1`) can use the
# primitive that actually matches it, instead of the strictly-stricter
# `"space"` substitute. Already used internally for gf180mcu's
# `mim.space.1` peer check (see `peer_edge_pairs` below) before this issue
# exposed it as a `DrcRule.check` option.
_SINGLE_LAYER_CHECKS = {"width", "space", "notch", "isolated"}
# Check kinds `DrcRule.threshold_max_dbu` (issue #2370) is meaningful for --
# i.e. the ones whose published rule can carry an *upper* size bound
# alongside the lower one `threshold_dbu` already expresses (gf180mcu's
# `CO.1` fixed 0.22x0.22um contact, `Vn.1` fixed 0.26x0.26um via). Only
# "width" today; kept as a set (rather than an `== "width"` test) so the
# validation error message can name the admissible kinds the same way every
# other check-kind error in this module does. See
# `_validate_threshold_max`/`_run_width_max_check` below.
_MAX_BOUND_CHECKS = {"width"}
# Check kinds that compare a region against another region (other_layer required).
_TWO_LAYER_CHECKS = {
    "separation",
    "enclosing",
    "enclosed",
    "overlap",
    "enclosing_adjacent",
}
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

# Single-layer *vertex* check kinds (issue #2642): where a polygon's corners
# sit, rather than how far apart its edges are. Both return `EdgePairs` like
# every distance check above, so `run_drc()` reports them through the same
# edge-pair path -- but neither reads `threshold_dbu` (see `DrcRule.grid_um`/
# `angle_limit_deg`), so they are dispatched ahead of `_run_check` rather
# than inside it.
#
# `"ongrid"` is the manufacturing-grid check the open PDKs' own shipped
# KLayout decks declare as an on-by-default rule group (sky130's
# `OFFGRID = true`, emitting `layer.ongrid(0.005)` per layer) --
# `_run_grid_check`. `"angle"` is that group's other half
# (`layer.with_angle(0 .. limit)`), a minimum interior-corner-angle bound --
# `_run_angle_check`.
_GRID_CHECKS = {"ongrid"}
_ANGLE_CHECKS = {"angle"}
# Their union, the set `run_drc` actually branches on -- one dispatch site for
# both kinds (`_run_vertex_check`), so adding a third vertex check kind does
# not add another branch to `run_drc` itself.
_VERTEX_CHECKS = _GRID_CHECKS | _ANGLE_CHECKS
# Region rules (issues #3587, #3607): a typed `DrcRule.region_spec` descriptor
# evaluated by `_run_comp_coverage_check` / `_run_tap_distance_check`, not a
# distance primitive.
_COMP_COVERAGE_CHECKS = {"comp_coverage"}
_TAP_DISTANCE_CHECKS = {"tap_distance"}
_REGION_CHECKS = _COMP_COVERAGE_CHECKS | _TAP_DISTANCE_CHECKS
# Net-aware spacing (issue #3572): `"space"` restricted to facing edges of
# polygons on different nets, per `DrcRule.net_spec` -- `_run_net_space_check`.
# Returns `EdgePairs`, so it reuses the ordinary edge-pair reporting path.
_NET_SPACE_CHECKS = {"net_space"}


def _spec_layers(rule: DrcRule) -> set[tuple[int, int]]:
    """Layers referenced by a rule's ``region_spec`` / ``net_spec``, if any."""
    layers: set[tuple[int, int]] = set()
    for spec in (rule.region_spec, rule.net_spec):
        if spec is not None:
            layers.update(spec.layers())
    return layers


# Every `DerivedLayer.mode` `run_drc()` knows how to derive a region for --
# see that class's docstring for each one's derivation. Validated per rule
# (rather than assumed) so a deck typo fails loudly with the rule id instead
# of silently falling through to the default derivation.
_DERIVED_LAYER_MODES = {
    "sized_intersection",
    "overlapping",
    "not_interacting",
    "holes",
    "union",
    "gate_endcap",
}

# Every `DerivedLayer.context` value (issue #3484); `None` = no partition.
_DERIVED_CONTEXTS = {None, "near", "far"}

# Every `DrcRule.other_layer_selection` value `run_drc()` knows how to apply
# (issue #2688) -- validated per rule for the same "fail loudly, never
# silently check a different population" reason as `_DERIVED_LAYER_MODES`.
_OTHER_LAYER_SELECTIONS = {"squares"}

# Every `DrcRule.layer_selection` value (the primary-layer counterpart of
# `_OTHER_LAYER_SELECTIONS`, issues #2943/#3139).
_LAYER_SELECTIONS = {"squares"}

# `run_drc()`'s own field name -> its declared METRICS2.1-style name in
# `metrics.py`'s registry (issue #1847, adopting the #247 registry beyond its
# `layout-metrics` pilot). Additive: the `metrics` block this backs is a
# *parallel* object alongside the existing `violation_count`/`rule_counts`
# fields above, never a replacement for them -- see
# `docs/design/metric-namespace.md` for the additive-vs-rename decision.
_VIOLATION_COUNT_METRIC_NAME = "drc__error__count"

assert is_registered(_VIOLATION_COUNT_METRIC_NAME)


#: Bumped only on a non-additive (breaking) change to this command's own JSON
#: shape -- see docs/json-contract.md. Shared by both engines'
#: response builders (:func:`run_drc`, :func:`run_drc_klayout_engine`), which
#: have always carried the same version number and must not drift apart.
#:
#: Bumped 2 -> 3 (issue #2659): ``file`` changed from a raw (usually
#: absolute) path string to the ``{path, scope}`` shape
#: ``env_provenance.repo_relative_path`` already defines -- a committed `klt
#: drc` envelope is cited verbatim by a `klt signoff` manifest, so an
#: absolute path here pinned the record to the producing host's own
#: filesystem: ``klt signoff``'s ``input_verified`` freshness gate could
#: re-verify such a citation only on that host, and reported drift from
#: every other checkout. The same normalization `klt sim`/`klt pex` adopted
#: for their own input-path fields in issue #1261. See `_report_path` below.
SCHEMA_VERSION = 3


class DrcError(Exception):
    """Raised when a layout cannot be checked: bad file, unknown deck, or a
    malformed rule.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.

    ``code`` (issue #3076) is an optional stable machine-readable
    classification, surfaced as ``error.code`` in the JSON error envelope
    (``docs/json-contract.md``); ``None`` for every error that has none.
    """

    def __init__(self, message: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


#: Error codes for a ``--engine klayout`` second opinion that could not run
#: (issue #3076): distinguishes "unavailable" from bad input without parsing
#: ``error.message``. Exit code stays ``1``.
CODE_KLAYOUT_UNAVAILABLE = "klayout_unavailable"
CODE_KLAYOUT_VERSION_TOO_OLD = "klayout_version_too_old"
CODE_NEGATIVE_CONTROL_FAILED = "negative_control_failed"
CODE_MULTIPLE_TOP_CELLS = "multiple_top_cells"


#: The one top-level field a ``klt drc`` request document must carry (issue
#: #1867). ``deck`` is deliberately *not* required here: it is only required
#: for ``--engine curated``, and an omitted deck is already reported as an
#: application error (exit 1) by ``cli/drc_cmd.py``'s own ``_run`` rather than
#: as a request-shape error.
_REQUIRED_REQUEST_FIELDS = ("file",)

#: Contract identifier a ``klt drc`` request document may declare in its
#: optional ``schema`` field (issue #1867).
REQUEST_SCHEMA = "klt.drc.request/1"


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt drc`` request JSON file.

    Raises :class:`DrcError` if the file is missing/unreadable, not valid
    JSON, or missing the required top-level ``file`` field. Does not require
    a ``schema`` field, matching ``klt lvs``/``klt sim``'s ``load_request``
    (user-authored input, never emitted by this tool) -- when one *is*
    present, ``cli/drc_cmd.py`` checks it against :data:`REQUEST_SCHEMA`.
    """
    request = _load_request_json(request_path, DrcError)
    return _validate_request_shape(request, "request file")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared ``file`` shape check for a JSON-decoded ``klt drc`` request,
    however it was sourced (file, inline JSON, stdin). ``source`` is folded
    into the "must be a JSON object" error for context.
    """
    return _shared_validate_request_shape(
        data,
        source,
        error_cls=DrcError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve a ``klt drc`` request-document argument (issue #1867) into a
    request dict plus the directory relative paths inside it resolve against.

    ``value`` is one of the same three forms every other request-taking
    ``klt`` verb accepts (see :func:`klayout_tools._paths.load_request_arg`
    and docs/cli/drc.md): ``"-"`` for stdin, a path to an existing request
    JSON file, or an inline JSON object string. Relative paths inside the
    document resolve against the document's own directory for the file form,
    and against the current working directory for the stdin/inline forms.

    Raises :class:`DrcError` for any read/parse/shape failure -- the same
    exception type :func:`load_request` raises, so callers do not need to
    distinguish the three forms.
    """
    return _shared_load_request_arg(
        value,
        error_cls=DrcError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


#: The curated engine's ``coverage.skipped[].reason`` (issue #1996): a layer
#: this rule reads is absent from the stream, and running the rule anyway
#: could still have reported a violation from the geometry that *is* drawn.
#: Requested work that was not checked -- see :func:`_classify_skipped_rules`.
SKIP_REASON_ABSENT_INPUT_LAYER = "absent_input_layer"

#: The curated engine's ``coverage.inapplicable[].reason`` (issue #2110): a
#: layer this rule reads is absent, and with it absent the rule constrains
#: nothing in this stream -- running it is a provable no-op, not an
#: unperformed check. Outside this invocation's applicable assessment rather
#: than a skipped request; see :func:`_classify_skipped_rules`.
INAPPLICABLE_REASON_NO_APPLICABLE_GEOMETRY = "no_applicable_geometry"

#: The curated engine's second ``coverage.skipped[].reason`` (issue #2642):
#: an ``"ongrid"`` rule whose published grid cannot be expressed exactly in
#: this stream's database units -- neither a whole number of them nor a whole
#: divisor of one (e.g. a 0.005um grid against a 0.006um dbu). Real off-grid
#: coordinates exist in such a stream, so this is a *requested* check that
#: could not run (making the run partial), not inapplicable work: see
#: :func:`_grid_is_representable` for why rounding the grid to something
#: expressible would be worse than reporting the gap.
SKIP_REASON_GRID_NOT_REPRESENTABLE = "grid_not_representable"

#: The curated engine's second ``coverage.inapplicable[].reason`` (issue
#: #3587): a ``"comp_coverage"`` rule whose checked layer *is* drawn but whose
#: checked region is empty once the rule's marker exemption is applied (every
#: COMP polygon is under an exempting marker), so there was nothing for it to
#: constrain. Distinct from
#: :data:`INAPPLICABLE_REASON_NO_APPLICABLE_GEOMETRY` (checked layer absent).
INAPPLICABLE_REASON_EMPTY_CHECKED_REGION = "inapplicable_empty_checked_region"


def _grid_is_representable(rule: DrcRule, dbu: float) -> bool:
    """Whether ``rule``'s ``grid_um`` can be checked exactly against a stream
    whose database unit is ``dbu`` micrometres (issue #2642).

    ``klayout.db.Region.grid_check`` takes a grid in **whole** database units,
    which admits two cases:

    - ``grid_um`` is a whole number of database units (0.005um at a 1nm dbu is
      5 units; at a 5nm dbu, 1) -- checkable directly;
    - ``grid_um`` is finer than one database unit *and* the database unit is a
      whole multiple of the grid (a 10nm-dbu stream against a 5nm grid) --
      every representable coordinate is on-grid by construction, so the rule
      is vacuously satisfied and still counts as checked.

    Anything else is a genuine gap rather than a formality: at a 0.006um dbu,
    a coordinate of 1 unit is 0.006um, which is **not** on a 0.005um grid, so
    off-grid geometry does exist -- there is simply no integral grid for the
    primitive to measure it with. Rounding to the nearest expressible grid
    would silently check a bound the PDK never published (coarser: flags legal
    geometry; finer: passes illegal geometry), so ``run_drc`` skips the rule
    and records :data:`SKIP_REASON_GRID_NOT_REPRESENTABLE`, which makes the
    run partial rather than an unqualified ``clean``.

    A ``"tap_distance"`` rule (issue #3607) has the same problem with its
    ``max_distance_um`` and ``step_um``: both must be a whole, positive number
    of database units, or the boundary distance would be silently rounded.

    ``True`` for every other non-``"ongrid"`` rule and for an ``"ongrid"`` rule
    with no ``grid_um`` at all -- neither is this predicate's question (the
    latter is a deck-authoring error :func:`_run_grid_check` raises on).
    """
    if rule.check in _TAP_DISTANCE_CHECKS and isinstance(
        rule.region_spec, TapDistanceSpec
    ):
        spec = rule.region_spec
        return all(
            round(um / dbu) >= 1 and abs(um / dbu - round(um / dbu)) <= 1e-9
            for um in (spec.max_distance_um, spec.step_um)
        )
    if rule.check not in _GRID_CHECKS or rule.grid_um is None:
        return True
    units_per_grid = rule.grid_um / dbu
    # `>= 1` as well as integral: `grid_check(0, 0)` is KLayout's own "do not
    # check this direction", so a grid rounding to zero units would silently
    # pass everything rather than check anything.
    if (
        round(units_per_grid) >= 1
        and abs(units_per_grid - round(units_per_grid)) <= 1e-9
    ):
        return True
    grids_per_unit = dbu / rule.grid_um
    return grids_per_unit > 1.0 and abs(grids_per_unit - round(grids_per_unit)) <= 1e-9


def _rule_skipped_before_evaluation(rule: DrcRule, layout: Any) -> bool:
    """Whether ``rule`` is skipped by a gate that applies *before*
    :func:`run_drc` resolves its input layers at all.

    Two gates, both of which land the rule in ``coverage.rules_skipped``
    exactly as a missing ``layer``/``other_layer`` does (the reason code each
    reports is :func:`_classify_skipped_rules`' decision, not this
    function's):

    - ``requires_any_layer`` (issue #2634): a rule whose official DRM scope
      only ever applies in the *context* of some other layer (gf180mcu's
      ``pplus.enclosing.comp.1``, scoped by the real PDK deck to "inside
      NWELL"/"inside DNWELL" PCOMP -- see :class:`DrcRule`'s docstring) has
      nothing to check when none of those context layers were drawn anywhere
      in the stream, regardless of whether its own checked layers are
      present. These context layers contribute nothing to
      ``deck_layer_tuples``: they gate the rule, they are never read as
      checked geometry.
    - an ``"ongrid"`` rule whose grid this stream's database unit cannot
      express exactly (issue #2642) -- see :func:`_grid_is_representable`.
    """
    if rule.requires_any_layer is not None and not any(
        layout.find_layer(*context_layer) is not None
        for context_layer in rule.requires_any_layer
    ):
        return True
    return not _grid_is_representable(rule, layout.dbu)


def _vacuity_layers(rule: DrcRule) -> set[tuple[int, int]]:
    """The ``(layer, datatype)`` pairs whose absence makes ``rule`` provably
    incapable of reporting a violation (issue #2110).

    Every check kind this engine dispatches measures *drawn* geometry, and
    every one of them reports nothing when the geometry it measures is empty:

    - ``width``/``space``/``notch``/``isolated`` run a ``Region.*_check``
      over one region -- no shapes, no edge pairs.
    - ``area`` runs ``Region.with_area(..., inverse=True)``, which returns
      the violating polygons of an empty region: none (``_run_area_check``).
    - ``density`` tiles the checked region's own ``bbox()``, which is empty,
      so it tiles no window at all (``_run_density_check``).
    - ``ongrid``/``angle`` (issue #2642) measure the *vertices* of the
      checked region's polygons -- an empty region has no polygons, so no
      corner and no vertex, hence no marker (``_run_grid_check``/
      ``_run_angle_check``). Both are single-layer kinds that never set
      ``other_layer``, so the default ``{rule.layer}`` answer below is the
      whole story for them.
    - ``separation``/``enclosing``/``enclosed``/``overlap`` measure facing
      edge pairs between two regions, and issue #318's extra "escaped the
      enclosing region" term is computed with ``Region.interacting`` -- all
      of which are empty when *either* region is (``_run_check``). A curated
      enclosure rule constrains a drawn pair; with one side of the pair never
      drawn, this stream contains no instance of what it constrains.

    ``antenna`` is the documented exception, and the reason this function
    exists rather than a blanket "an absent input layer is inapplicable"
    rule: ``_run_antenna_check`` treats a nonempty antenna region whose
    protection region has **zero** area as an undefined ratio and reports it
    as a violation. So an absent ``other_layer`` there does not make the rule
    vacuous -- it hides exactly the finding the rule exists to make, which is
    a skipped request (:data:`SKIP_REASON_ABSENT_INPUT_LAYER`).

    The checked region's own source layers are ``rule.layer``, or -- for a
    ``derived_layer`` rule -- the layers the derivation actually reads:
    ``base`` always, plus ``intersect_with`` for the two modes that *scope*
    the region by intersecting it (``"sized_intersection"``,
    ``"overlapping"``). ``"not_interacting"`` and ``"holes"`` derive from
    ``base`` alone and are never skipped for an absent marker in the first
    place (see :class:`~klayout_tools.decks.DerivedLayer`). ``"union"``
    (issue #2688) contributes *neither* input here: its region is empty
    only when **both** are absent, an "all absent" condition a set of
    "any absent" layers cannot express -- :func:`_union_inputs_all_absent`
    answers it instead.

    A new check kind must decide its own entry here deliberately: the
    fallthrough treats ``other_layer`` as vacuity-forcing, matching every
    two-layer primitive above, so a kind that can fault on an *empty*
    counterpart -- as ``antenna`` does -- has to exclude itself the way
    :data:`_ANTENNA_CHECKS` is excluded below, or it will report a real gap
    as inapplicable.
    """
    if rule.require_containment and rule.other_layer is not None:
        # Required containment (#2726): only the cut layer's absence makes the
        # rule vacuous; an absent conductor is the very defect it reports.
        return {_containment_layers(rule)[0]}
    if rule.derived_layer is not None and rule.derived_layer.mode == "union":
        layers: set[tuple[int, int]] = set()
    elif rule.derived_layer is not None:
        layers = {rule.derived_layer.base}
        if rule.derived_layer.intersect_with is not None and (
            rule.derived_layer.mode
            in ("sized_intersection", "overlapping", "gate_endcap")
        ):
            layers.add(rule.derived_layer.intersect_with)
    else:
        layers = {rule.layer}
    if rule.other_layer is not None and rule.check not in _ANTENNA_CHECKS:
        layers.add(rule.other_layer)
    return layers


def _union_inputs_all_absent(rule: DrcRule, layout: Any) -> bool:
    """Whether ``rule`` is a ``"union"``-mode derived rule (issue #2688) none
    of whose union inputs is present in ``layout`` -- the only way its
    checked region can be empty, and so the only way an absent enclosing
    input makes it vacuous (see :func:`_vacuity_layers`). ``False`` for
    every other rule."""
    derived = rule.derived_layer
    if derived is None or derived.mode != "union":
        return False
    inputs = [derived.base]
    if derived.intersect_with is not None:
        inputs.append(derived.intersect_with)
    return all(layout.find_layer(*layer) is None for layer in inputs)


def _derived_inputs_absent(
    rule: DrcRule, base_index: int | None, intersect_index: int | None
) -> bool:
    """Whether a ``derived_layer`` rule's resolved inputs make its checked
    region provably empty, so :func:`run_drc` skips it like any other
    missing-layer rule. ``base_index``/``intersect_index`` are the
    ``layout.find_layer`` results for ``derived_layer.base`` /
    ``derived_layer.intersect_with`` (``None`` = absent from the stream).

    - ``"union"`` (#2688) joins its two inputs: either one alone is a
      complete enclosing region (a mask-only HBT contact window draws no
      ``base`` geometry at all), so it is skipped only when *both* are
      absent. An absent input contributes an empty region instead.
    - Every other mode: an absent ``base`` means the derived region's own
      source shapes are missing -> no violations possible, skip.
    - An absent second input empties both the ``"sized_intersection"`` and
      ``"overlapping"`` derivations, so there is nothing to check.
      ``"not_interacting"`` is the exception: "base polygons that touch no
      marker" is every base polygon when the marker layer was never drawn,
      so that rule must still run against the full base region (see
      ``DerivedLayer``'s docstring) -- the ordinary thin-oxide-only layout,
      which must stay checked against the unmarked column. ``"holes"`` is
      the other exception: it never reads a second layer at all
      (``intersect_with`` is always ``None`` for it), so ``intersect_index``
      being ``None`` is expected, not a missing-layer condition.
    """
    mode = rule.derived_layer.mode
    if mode == "union":
        return base_index is None and intersect_index is None
    if base_index is None:
        return True
    return intersect_index is None and mode not in ("not_interacting", "holes")


def _cell_region_or_empty(cell: Any, layer_index: int | None) -> Any:
    """``cell``'s recursive shapes on ``layer_index`` as a ``kdb.Region``,
    or an empty ``kdb.Region`` when the layer is absent (``None``) -- only
    reachable for a ``"union"`` input or an unused ``intersect_with``; see
    :func:`_derived_inputs_absent`."""
    import klayout.db as kdb

    if layer_index is None:
        return kdb.Region()
    return kdb.Region(cell.begin_shapes_rec(layer_index))


def _derived_region(
    rule: DrcRule,
    base_region: Any,
    intersect_region: Any,
    dbu: float,
    context_region: Any | None = None,
    context2_region: Any | None = None,
) -> Any:
    """Build the checked region for a ``derived_layer`` rule, then apply its
    optional well-context partition (issue #3484, see ``DerivedLayer``):
    ``context_region`` is the already-joined (unsized) union of
    ``derived_layer.context_layers``, oversized here by ``context_sized_by_um``
    and used to keep only the whole polygons ``interacting`` with it
    (``"near"``) or ``not_interacting`` with it (``"far"``). The optional
    second stage (issue #3682, ``context2*``) repeats that on the stage-1
    result, so the two stages AND together."""
    region = _derived_region_unscoped(rule, base_region, intersect_region, dbu)
    derived = rule.derived_layer
    if derived is None or derived.context is None or context_region is None:
        return region
    region = _apply_context(
        region, context_region, derived.context_sized_by_um, derived.context, dbu
    )
    if derived.context2 is None or context2_region is None:
        return region
    return _apply_context(
        region, context2_region, derived.context2_sized_by_um, derived.context2, dbu
    )


def _apply_context(
    region: Any, context_region: Any, sized_by_um: float, mode: str, dbu: float
) -> Any:
    """One near/far whole-polygon partition of ``region`` by ``context_region``
    oversized by ``sized_by_um``."""
    size_dbu = round(sized_by_um / dbu)
    ctx = context_region.sized(size_dbu) if size_dbu else context_region
    if mode == "near":
        return region.interacting(ctx)
    return region.not_interacting(ctx)


def _context_region(
    cell: Any, layout: Any, rule: DrcRule, stage: int = 1
) -> Any | None:
    """Join of ``rule.derived_layer.context_layers`` (``context2_layers`` for
    ``stage=2``) in ``cell`` (absent layers contribute nothing), or ``None``
    when the rule has no such context."""
    import klayout.db as kdb

    derived = rule.derived_layer
    if derived is None:
        return None
    mode, layers = (
        (derived.context, derived.context_layers)
        if stage == 1
        else (derived.context2, derived.context2_layers)
    )
    if mode is None:
        return None
    region = kdb.Region()
    for layer in layers:
        region += _cell_region_or_empty(cell, layout.find_layer(*layer))
    return region.merged()


def _derived_region_unscoped(
    rule: DrcRule, base_region: Any, intersect_region: Any, dbu: float
) -> Any:
    """Build the checked region for a ``derived_layer`` rule (#345, marker-
    scoped modes #1110, ``"holes"`` #1976, ``"union"`` #2688) from its two
    input regions -- see ``DerivedLayer``'s docstring for each mode's full
    derivation and why an unscoped check against either raw input layer
    would be wrong, not just conservative. ``sized_by_um`` is a real
    micrometre distance, rescaled against this layout's own ``dbu``
    directly (unlike ``rule.threshold_dbu``, which is rescaled via
    ``dbu_scale`` against the deck's nominal dbu). Always returns a new
    Region; neither input (nor the source layout) is modified.
    """
    derived = rule.derived_layer
    assert derived is not None, "_derived_region requires a derived_layer rule"
    size_dbu = round(derived.sized_by_um / dbu)
    if derived.mode == "holes":
        # Interior voids of the merged `base` region (#1976) --
        # `intersect_with`/`sized_by_um` are unused for this mode. Explicit
        # `.merged()` before `.holes()` documents the requirement even though
        # `Region.holes()` already applies merged semantics itself -- a hole
        # formed by several abutting drawn rectangles (the common GDS idiom
        # for a slotted plate) is only visible once merged. A `base` region
        # with no holes at all derives an empty region, which is simply
        # nothing to report -- not an error.
        return base_region.merged().holes()
    if derived.mode == "gate_endcap":
        # Endcap probe zones (#2936), e.g. sky130's `poly.8`: the part of
        # each `base` (diffusion) edge that lies under or flush with an
        # `intersect_with` (poly) shape is a gate end edge; extend it
        # outward by `sized_by_um`. Only the *outer* side is probed, so poly
        # that merely abuts the diffusion from outside never reads as a
        # gate. The rule pairs this region with `check="enclosed"`,
        # `other_layer=<poly>` and a zero threshold: whatever part of a probe
        # zone is not covered by poly is the missing endcap. A flush poly
        # edge is collinear with the diffusion edge, which `inside_part`
        # alone excludes, hence the explicit edge AND.
        base_edges = base_region.merged().edges()
        poly_region = intersect_region.merged()
        gate_end_edges = (
            base_edges.inside_part(poly_region) + (base_edges & poly_region.edges())
        ).merged()
        return gate_end_edges.extended(0, 0, size_dbu, 0, False)
    if derived.mode == "union":
        # Boolean join of both inputs (#2688), e.g. sg13g2's `Cnt.c`
        # enclosing region `activ.join(activ_mask)`. `_run_check` merges it
        # before measuring, so a contact window assembled across both layers
        # is measured against the joined outline, not either layer's seam.
        return base_region + intersect_region
    if derived.mode in ("overlapping", "not_interacting"):
        # Marker-scoped whole-polygon selection (#1110): here `sized_by_um`
        # is a guard band around the *marker* (`intersect_with`), not around
        # `base`.
        marker_region = (
            intersect_region.sized(size_dbu) if size_dbu else intersect_region
        )
        if derived.mode == "overlapping":
            # Whole `base` polygons sharing area with the marker -- the
            # marked ("_MV") half of a rule pair.
            return base_region.overlapping(marker_region)
        # Whole `base` polygons touching no marker geometry at all -- the
        # unmarked ("_LV") half of the pair.
        return base_region.not_interacting(marker_region)
    # "sized_intersection" (the default, issue #345): shapes of
    # `intersect_with` that already touch the *unsized* `base` region
    # somewhere, clipped to `base`'s outline oversized by `sized_by_um`.
    return intersect_region.interacting(base_region) & (base_region.sized(size_dbu))


def _select_other_region(rule: DrcRule, other_region: Any) -> Any:
    """Apply ``rule.other_layer_selection`` to the resolved ``other_layer``
    region; returned unchanged when no selection is set.

    ``"squares"`` (#2688) is whole-polygon square selection: the complement
    of the DRC-DSL ``non_squares`` selector, applied to the *merged* layer
    so two abutting squares forming a bar count as the bar they are.
    Non-square shapes (contact bars) are dropped from both the margin check
    and the #318 escape term.
    """
    if rule.other_layer_selection == "squares":
        return other_region.merged().squares()
    return other_region


def _select_primary_region(rule: DrcRule, region: Any) -> Any:
    """Apply ``rule.layer_selection`` to the rule's own ``layer`` region;
    returned unchanged when no selection is set.

    ``"squares"`` mirrors :func:`_select_other_region`: the *merged* layer
    filtered by ``Region.squares()``, so a contact bar -- or a row of
    abutting legal contacts that merges into one strip -- is dropped
    (``cont_sq = cont.not(cont.non_squares)`` in the IHP runset) rather than
    measured as a single oversized contact.
    """
    if rule.layer_selection == "squares":
        return region.merged().squares()
    return region


def _rule_outside_voltage_gate(
    rule: DrcRule, marker: tuple[int, int], skipped_rule_ids: set[str]
) -> bool:
    """Whether ``rule`` is excluded from ``coverage.voltage_domain_warnings``'
    unscoped-rule set for ``marker`` (issue #552, refined by #1110 and #2369).

    The warning's premise is "this rule may have applied the *wrong column*
    of a multi-column DRM table to geometry inside the marker". Three kinds
    of rule cannot have done that, and each is excluded here rather than
    inline in :func:`run_drc`'s marker loop:

    - it was **skipped** this run (``coverage.rules_skipped``) -- it applied
      no threshold at all, right or wrong;
    - its ``derived_layer`` **reads the marker itself** (gf180mcu's
      ``DF.1a``/``DF.3a`` ``_LV``/``_MV`` pairs, issue #1110) -- its checked
      region is already scoped by the marker, so it applied the right column;
    - its ``region_spec`` **reads the marker itself** (``dualgate`` / ``v5_xtor``
      in a ``"tap_distance"`` rule, issue #3607) -- the rule is scoped by the
      marker, so it applied the right column;
    - it is flagged :attr:`~klayout_tools.decks.DrcRule.voltage_independent`
      (issue #2369) -- its source DRM section publishes a single value
      column, so no second column exists for it to have misread. Opt-in per
      rule, never inferred: see that field's own docstring for the evidence
      a rule must carry before setting it.
    """
    if rule.id in skipped_rule_ids:
        return True
    if rule.voltage_independent:
        return True
    if rule.check in _TAP_DISTANCE_CHECKS and rule.region_spec is not None:
        if marker in rule.region_spec.layers():
            return True
    # A `"union"` derivation (issue #2688) *adds* its inputs to the checked
    # region rather than scoping it, so reading the marker as one of them
    # does not mean the rule applied the marker's column.
    return (
        rule.derived_layer is not None and rule.derived_layer.mode != "union"
    ) and marker in (
        rule.derived_layer.base,
        rule.derived_layer.intersect_with,
    )


def _classify_skipped_rules(
    deck: list[DrcRule],
    rules_skipped: list[str],
    layout: Any,
    empty_checked_region_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split ``rules_skipped`` into common-coverage *skipped* and
    *inapplicable* records (issue #2110).

    ``docs/coverage-contract.md``'s migration contract hands this adapter one
    open classification question: is a curated rule whose input layer is not
    drawn a *requested* check that did not run, or work this invocation never
    made applicable? Phase 1 recorded every one of them as a skipped request.
    Under the Phase 2 rollup that would make essentially every real run
    partial -- 7 of 57 sky130 rules run on
    ``examples/design-pipeline/06-layout.gds``, 15 of 57 on a `sky130_fd_sc_hd`
    standard cell -- and a verb whose successful runs are *all* partial has
    stopped distinguishing anything.

    The answer this adapter settles on is decided per rule, and decided by
    the engine's own behaviour rather than by preference:

    - Running the rule with the absent layer empty **could not have reported
      a violation** (:func:`_vacuity_layers`): the rule constrains nothing in
      this stream, so there is no check of it for the invocation to have
      asked for. ``inapplicable``, reason
      :data:`INAPPLICABLE_REASON_NO_APPLICABLE_GEOMETRY`. Per the common
      contract, inapplicable work never makes an otherwise complete run
      partial -- which is what lets ``klt drc`` still report an earned,
      unconditional ``clean`` for a small block that simply does not draw
      most of a broad PDK deck's layers.
    - Running it **could** have reported one -- today exactly
      ``check="antenna"`` with its protection layer absent, where
      ``_run_antenna_check`` faults a zero-area protection region -- so the
      skip hides a finding the deck asked for. ``skipped``, reason
      :data:`SKIP_REASON_ABSENT_INPUT_LAYER`, which makes the run
      :data:`~klayout_tools.coverage.RESULT_PARTIAL` and therefore *not* an
      unconditional success.

    One skip is not about an absent layer at all (issue #2642): an
    ``"ongrid"`` rule whose published grid this stream's database unit cannot
    express exactly (:func:`_grid_is_representable`). Its layer is present and
    off-grid geometry can exist, so it is a requested check that did not run:
    ``skipped``, reason :data:`SKIP_REASON_GRID_NOT_REPRESENTABLE`. Tested
    *after* vacuity below, so a rule whose layer is also absent stays
    inapplicable -- with no geometry at all there is nothing for either
    answer to be about, and the absent layer is the more informative reason.

    Nothing is moved between the two categories to reach a nicer rollup row:
    the line is "would this check have been able to fail?", it is auditable
    against each primitive in this module, and a check kind that can fault on
    an empty input has to be excluded from :func:`_vacuity_layers`
    explicitly.

    Absence is probed with ``Layout.find_layer`` -- the same question the
    per-rule skip decision in :func:`run_drc` itself asked -- so the two can
    never disagree about which layers this stream has.

    The verb-specific ``coverage.rules_skipped`` field keeps listing **both**
    categories, unchanged: it is the pre-contract spelling of "did not run",
    and this classification is additive to it.

    ``empty_checked_region_ids`` (issue #3587) names the ``"comp_coverage"``
    rules whose checked layer is drawn but whose checked region came out empty
    (:data:`INAPPLICABLE_REASON_EMPTY_CHECKED_REGION`): a rule constraining
    nothing in this stream, so inapplicable rather than skipped.

    Returns ``(skipped, inapplicable)``, each a list of ``{"id", "reason"}``
    records sorted by rule id.
    """
    skipped_ids = set(rules_skipped)
    skipped: list[dict[str, str]] = []
    inapplicable: list[dict[str, str]] = []
    for rule in deck:
        if rule.id not in skipped_ids:
            continue
        if rule.id in empty_checked_region_ids:
            inapplicable.append(
                {"id": rule.id, "reason": INAPPLICABLE_REASON_EMPTY_CHECKED_REGION}
            )
            continue
        vacuous = (
            any(layout.find_layer(*layer) is None for layer in _vacuity_layers(rule))
            or _union_inputs_all_absent(rule, layout)
        ) or (
            # `requires_any_layer` (issue #2634) is a second, independent way
            # a rule can be provably vacuous: its own checked geometry
            # (`_vacuity_layers`) may well be present, but the official DRM
            # scope this rule approximates only ever applies in the context
            # of one of these other layers -- with none of them drawn
            # anywhere, there is no shape in this stream the rule's
            # threshold could have correctly applied to, exactly the same
            # "nothing to check" conclusion `_vacuity_layers` reaches for its
            # own inputs. See `DrcRule.requires_any_layer`'s docstring.
            rule.requires_any_layer is not None
            and not any(
                layout.find_layer(*context_layer) is not None
                for context_layer in rule.requires_any_layer
            )
        )
        if vacuous:
            inapplicable.append(
                {"id": rule.id, "reason": INAPPLICABLE_REASON_NO_APPLICABLE_GEOMETRY}
            )
        elif not _grid_is_representable(rule, layout.dbu):
            skipped.append(
                {"id": rule.id, "reason": SKIP_REASON_GRID_NOT_REPRESENTABLE}
            )
        else:
            skipped.append({"id": rule.id, "reason": SKIP_REASON_ABSENT_INPUT_LAYER})
    return (
        sorted(skipped, key=lambda record: record["id"]),
        sorted(inapplicable, key=lambda record: record["id"]),
    )


def _curated_nothing_checked_reasons(
    deck: list[DrcRule], rules_checked: list[str]
) -> list[str]:
    """The curated engine's ``coverage.nothing_checked_reasons`` (issue #1996).

    A deck that ran not one rule produces ``"not_checked"``.
    ``rules_checked`` states what the verdict was actually
    measured over, so its emptiness -- and only its emptiness -- is the
    condition; the two reason codes then distinguish *why* there was nothing
    to run. A degenerate deck declaring no rules at all reports
    ``deck_has_no_rules``; a deck that declares rules but had every one
    skipped for an absent input layer (the empty/wrong-PDK stream case
    ``coverage.rules_skipped`` already enumerates) reports
    ``all_rules_skipped``.

    Returns ``[]`` for any run that checked at least one rule --
    ``nothing_checked`` is never a synonym for *partial* coverage (see
    :mod:`klayout_tools.coverage`).
    """
    if rules_checked:
        return []
    return [REASON_DECK_HAS_NO_RULES if not deck else REASON_ALL_RULES_SKIPPED]


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one input-path field for this module's own JSON response
    (issue #2659) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path` rather than a
    second normalizer, so `klt drc`'s reports, `klt sim`/`klt pex`'s (issue
    #1261) and `klt env-provenance`'s own emitter agree on exactly one
    ``{path, scope}`` shape.

    The reader counterpart is
    :func:`~klayout_tools._report_verify.resolve_committed_path`, which both
    ``--check`` modes here and ``klt signoff``'s ``input_verified`` gate use
    to turn the emitted entry back into a file on *their* checkout.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


def _report_repo_root(path: str) -> str | None:
    """The one repo root every input-path field a run over ``path`` echoes is
    normalised against -- resolved once from the layout's own location, the
    same "walk up from the input" default
    :func:`~klayout_tools.env_provenance.find_repo_root` uses for its own
    caller (and the same anchor `klt pex` picks for its own ``layout``)."""
    return env_provenance.find_repo_root(os.path.dirname(os.path.abspath(path)))


#: The curated engine's ``coverage.skipped[].reason`` for a deck rule the
#: caller deliberately left out of a ``--rules`` subset (issue #2768). A
#: requested-work gap by definition -- the full registered deck is what a
#: DRC verdict is about -- so any omission makes the run partial, whatever
#: the omitted rule's input layers would have done. Recorded as its own code
#: so an intentional omission stays distinguishable from a rule that was
#: naturally inapplicable (:data:`INAPPLICABLE_REASON_NO_APPLICABLE_GEOMETRY`)
#: or skipped for an absent layer (:data:`SKIP_REASON_ABSENT_INPUT_LAYER`).
SKIP_REASON_NOT_SELECTED = "not_selected"

#: Characters that make a ``--rules`` token a shell-style glob rather than an
#: exact rule id (``fnmatch`` syntax). Used only to word the "matched no
#: rule" error; matching itself always goes through :func:`fnmatch.fnmatchcase`.
_GLOB_CHARS = frozenset("*?[")


def parse_rule_selectors(raw: str | list[str] | None) -> list[str] | None:
    """Split a ``--rules`` value (or a request document's ``rules`` field)
    into its trimmed selector tokens (issue #2768).

    ``raw`` is either one comma-separated string (the argv form,
    ``--rules 'met1.*,via.width.1'``) or a list of such strings (the request
    form); every element is comma-split the same way, so the two forms
    cannot disagree. ``None`` (no selection at all) passes through as
    ``None`` -- the full-deck run every caller gets by default.

    Raises :class:`DrcError` for an empty selection or an empty token (a
    stray/trailing comma, a whitespace-only element): silently dropping one
    would quietly change which rules run.
    """
    if raw is None:
        return None
    pieces = [raw] if isinstance(raw, str) else list(raw)
    tokens: list[str] = []
    for piece in pieces:
        for token in piece.split(","):
            token = token.strip()
            if not token:
                raise DrcError(
                    f"--rules has an empty rule selector in {piece!r} -- pass "
                    "comma-separated rule ids or quoted globs, e.g. "
                    "--rules 'met1.*,via.width.1'"
                )
            tokens.append(token)
    if not tokens:
        raise DrcError("--rules selects no rules -- pass at least one rule id")
    return tokens


def resolve_rule_selection(deck: list[DrcRule], selectors: list[str]) -> dict[str, Any]:
    """Resolve ``selectors`` against the full registered ``deck`` once, before
    any geometry is read (issue #2768).

    Each token matches an exact rule id, or -- when it is not one -- every id
    it matches as a case-sensitive shell-style glob
    (:func:`fnmatch.fnmatchcase`). Overlapping tokens are deduplicated; the
    selected ids come back in deck order (the order :func:`run_drc` executes
    in), never selector order. A token that matches nothing is an error
    naming it, so a typo cannot silently shrink the run.

    Returns the report's ``rule_selection`` block: ``selectors`` (the
    trimmed tokens, as given), ``selected`` and ``omitted`` (both in deck
    order, partitioning the deck), ``deck_rule_count`` and ``subset``
    (``True`` exactly when something was omitted).
    """
    ids = [rule.id for rule in deck]
    known = set(ids)
    chosen: set[str] = set()
    unmatched: list[str] = []
    for token in selectors:
        if token in known:
            chosen.add(token)
            continue
        matches = {rule_id for rule_id in ids if fnmatch.fnmatchcase(rule_id, token)}
        if not matches:
            unmatched.append(token)
        chosen |= matches
    if unmatched:
        kinds = [
            "glob" if _GLOB_CHARS.intersection(token) else "rule id"
            for token in unmatched
        ]
        listed = ", ".join(
            f"{kind} {token!r}" for kind, token in zip(kinds, unmatched, strict=True)
        )
        raise DrcError(
            f"--rules selector(s) matched no rule in this deck: {listed} -- "
            "ids and globs are case-sensitive; see `klt drc --deck <name> "
            "--list-rules` for the registered ids"
        )
    selected = [rule_id for rule_id in ids if rule_id in chosen]
    omitted = [rule_id for rule_id in ids if rule_id not in chosen]
    return {
        "selectors": list(selectors),
        "selected": selected,
        "omitted": omitted,
        "deck_rule_count": len(ids),
        "subset": bool(omitted),
    }


def _resolve_optional_selection(
    deck: list[DrcRule], rules: list[str] | None
) -> tuple[dict[str, Any] | None, set[str]]:
    """:func:`run_drc`'s ``(rule_selection, omitted ids)`` -- ``(None,
    set())`` for a default full-deck run with no ``rules`` at all."""
    if rules is None:
        return None, set()
    selection = resolve_rule_selection(deck, list(rules))
    return selection, set(selection["omitted"])


def _dispatched_rules(deck: list[DrcRule], omitted_ids: set[str]) -> list[DrcRule]:
    """The deck rules :func:`run_drc` actually dispatches, in deck order:
    every rule except those deliberately omitted from a ``--rules`` subset
    (issue #2768)."""
    return [rule for rule in deck if rule.id not in omitted_ids]


def _fmt_layer(layer_tuple: tuple[int, int] | None) -> str | None:
    if layer_tuple is None:
        return None
    return f"{layer_tuple[0]}/{layer_tuple[1]}"


def list_deck_rules(deck_name: str) -> dict[str, Any]:
    """``klt drc --deck <name> --list-rules`` (issue #2768): enumerate a
    curated deck's registered rules without reading any layout.

    Deterministic: rules in deck (registration) order, which is also the
    order a ``--rules`` subset executes in; the two grouping maps are sorted
    by key. Deliberately carries no ``status``/``violations``/``coverage``
    field -- a listing is not a DRC result, and ``klt signoff`` refuses it as
    an unrecognised envelope rather than grading it.

    Raises :class:`DrcError` for an unknown deck name.
    """
    try:
        deck = get_deck(deck_name)
    except UnknownDeckError as exc:
        raise DrcError(str(exc)) from exc
    layer_names = get_layer_names(deck_name)

    def _name(layer_tuple: tuple[int, int] | None) -> str | None:
        return None if layer_tuple is None else layer_names.get(layer_tuple)

    rules = []
    scope_counts: dict[str, int] = {}
    check_counts: dict[str, int] = {}
    for rule in deck:
        derived = rule.derived_layer
        rules.append(
            {
                "id": rule.id,
                "description": rule.description,
                "check": rule.check,
                "layer": _fmt_layer(rule.layer),
                "layer_name": _name(rule.layer),
                "other_layer": _fmt_layer(rule.other_layer),
                "other_layer_name": _name(rule.other_layer),
                "derived_layer": None
                if derived is None
                else {
                    "mode": derived.mode,
                    "base": _fmt_layer(derived.base),
                    "intersect_with": _fmt_layer(derived.intersect_with),
                    "sized_by_um": derived.sized_by_um,
                    **(
                        {}
                        if derived.context is None
                        else {
                            "context": derived.context,
                            "context_layers": [
                                _fmt_layer(c) for c in derived.context_layers
                            ],
                            "context_sized_by_um": derived.context_sized_by_um,
                        }
                    ),
                    **(
                        {}
                        if derived.context2 is None
                        else {
                            "context2": derived.context2,
                            "context2_layers": [
                                _fmt_layer(c) for c in derived.context2_layers
                            ],
                            "context2_sized_by_um": derived.context2_sized_by_um,
                        }
                    ),
                },
                "scope": rule.scope or None,
            }
        )
        if rule.scope:
            scope_counts[rule.scope] = scope_counts.get(rule.scope, 0) + 1
        check_counts[rule.check] = check_counts.get(rule.check, 0) + 1
    return {
        "schema_version": 2,
        "mode": "list_rules",
        "deck": deck_name,
        "engine": "curated",
        "rule_count": len(rules),
        "groups": {
            "check": dict(sorted(check_counts.items())),
            "scope": dict(sorted(scope_counts.items())),
        },
        "rules": rules,
        "provenance": build_provenance(
            deck_name=deck_name,
            deck_path=deck_source_path(deck_name),
        ),
    }


_WAIVER_KEYS = ("cell", "rule", "justification")


def load_waivers(path: str) -> list[dict[str, str]]:
    """Read a ``--waivers`` file (issue #3483): a JSON object
    ``{"waivers": [{"cell": str, "rule": str, "justification": str}, ...]}``.

    ``cell`` is matched against a violation's ``source_cell``; ``rule`` is an
    exact rule id or a case-sensitive shell-style glob (the ``--rules``
    matcher). All three keys are required and non-blank -- there is
    deliberately no cell-wide blanket waiver, which would hide real
    violations in the waived cell.

    Raises :class:`DrcError` for an unreadable, non-JSON or malformed file,
    naming the offending entry -- before the layout is ever read.
    """
    label = f"waivers file (--waivers) '{path}'"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise DrcError(f"could not read {label}: {exc.strerror or exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DrcError(f"{label} is not valid JSON: {exc}") from exc
    entries = data.get("waivers") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise DrcError(f"{label} must be a JSON object with a non-empty 'waivers' list")
    waivers: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DrcError(f"{label}: waivers[{index}] must be an object")
        unknown = sorted(set(entry) - set(_WAIVER_KEYS))
        if unknown:
            raise DrcError(
                f"{label}: waivers[{index}] has unknown key(s) "
                f"{', '.join(unknown)} (allowed: {', '.join(_WAIVER_KEYS)})"
            )
        for key in _WAIVER_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise DrcError(
                    f"{label}: waivers[{index}] is missing a non-empty string '{key}'"
                )
        waivers.append({key: entry[key].strip() for key in _WAIVER_KEYS})
    return waivers


def _waiver_matches(waiver: dict[str, str], violation: dict[str, Any]) -> bool:
    source_cell = violation.get("source_cell")
    if source_cell is None or source_cell != waiver["cell"]:
        return False
    return fnmatch.fnmatchcase(violation["rule"], waiver["rule"])


def _partition_waived(
    violations: list[dict[str, Any]], waivers: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Split ``violations`` into ``(kept, waived, unused_waivers)`` (issue
    #3483). A violation is waived by the first waiver whose ``cell`` equals
    its ``source_cell`` and whose ``rule`` matches its rule id; a violation
    with no ``source_cell`` is never waived. Waived entries keep the full
    violation shape plus ``waiver_justification``."""
    kept: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    used: set[int] = set()
    for violation in violations:
        for index, waiver in enumerate(waivers):
            if _waiver_matches(waiver, violation):
                used.add(index)
                waived.append(
                    {**violation, "waiver_justification": waiver["justification"]}
                )
                break
        else:
            kept.append(violation)
    unused = [waiver for index, waiver in enumerate(waivers) if index not in used]
    return kept, waived, unused


def _apply_waivers(
    violations: list[dict[str, Any]], waivers: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """Apply ``waivers`` to ``violations`` (issue #3483). Returns the kept
    violations, their recomputed per-rule counts, and the report fields the
    waiver run adds (``waived_count``, ``waived_rule_counts``, ``waived``,
    ``unused_waivers``)."""
    kept, waived, unused = _partition_waived(violations, waivers)
    rule_counts: dict[str, int] = {}
    for violation in kept:
        rule_counts[violation["rule"]] = rule_counts.get(violation["rule"], 0) + 1
    waived_rule_counts: dict[str, int] = {}
    for entry in waived:
        waived_rule_counts[entry["rule"]] = waived_rule_counts.get(entry["rule"], 0) + 1
    fields = {
        "waived_count": len(waived),
        "waived_rule_counts": dict(sorted(waived_rule_counts.items())),
        "waived": waived,
        "unused_waivers": unused,
    }
    return kept, rule_counts, fields


@dataclass
class _DrcSetup:
    """Run-wide inputs :func:`run_drc` resolves once before any rule runs."""

    deck_name: str
    deck: list[DrcRule]
    layer_names: dict[tuple[int, int], str]
    rule_selection: dict[str, Any] | None
    omitted_ids: set[str]
    waivers: Any
    pdk_info: dict[str, Any] | None
    layout: Any
    dbu_scale: float
    top_cells: list[Any]
    deck_layer_tuples: set[tuple[int, int]]
    stream_layer_tuples: set[tuple[int, int]]
    # Issue #4226 (`--tile`): the parsed ``(columns, rows)``, and for each
    # wrapper cell the checked top cells were swapped for -- its name mapped
    # to ``(original cell name, centre-instance window)``. Both are empty/None
    # for a default run.
    tile: tuple[int, int] | None = None
    tile_windows: dict[str, tuple[str, Any]] = field(default_factory=dict)


@dataclass
class _DrcState:
    """Accumulators the per-rule loop fills and the report assembly reads."""

    violations: list[dict[str, Any]] = field(default_factory=list)
    rule_counts: dict[str, int] = field(default_factory=dict)
    rules_skipped: list[str] = field(default_factory=list)
    rules_checked: list[str] = field(default_factory=list)
    empty_checked_region_ids: set[str] = field(default_factory=set)


def _layer_str(layer_tuple: tuple[int, int]) -> str:
    return f"{layer_tuple[0]}/{layer_tuple[1]}"


#: Reserved annotation layer carrying a cell's abutment boundary (see
#: "Reserved annotation layer" in docs/cli/drc.md). Preferred over the
#: geometry bbox as the tile pitch when a cell draws one.
_PR_BOUNDARY_LAYER = (235, 4)


def parse_tile(raw: str | None) -> tuple[int, int] | None:
    """Parse a ``--tile`` value (issue #4226): ``<cols>x<rows>``, each a
    positive odd integer so one instance sits exactly at the array centre.
    ``None`` stays ``None``; anything malformed is a :class:`DrcError`."""
    if raw is None:
        return None
    match = re.fullmatch(r"(\d+)[xX](\d+)", raw.strip())
    if match is None:
        raise DrcError(f"--tile must look like <cols>x<rows>, e.g. 3x1 (got {raw!r})")
    cols, rows = int(match.group(1)), int(match.group(2))
    if cols < 1 or rows < 1 or cols % 2 == 0 or rows % 2 == 0:
        raise DrcError(
            f"--tile counts must be positive odd integers so the checked cell "
            f"sits at the array centre (got {raw!r})"
        )
    return cols, rows


def _tile_pitch_box(layout: Any, cell: Any) -> Any:
    """The abutment box of ``cell``: its ``prBoundary`` shapes' bbox when it
    draws any, else the cell's own bbox. Empty for an empty cell."""
    index = layout.find_layer(*_PR_BOUNDARY_LAYER)
    if index is not None:
        box = cell.bbox_per_layer(index)
        if not box.empty():
            return box
    return cell.bbox()


def _tile_top_cells(
    layout: Any, top_cells: list[Any], tile: tuple[int, int]
) -> tuple[list[Any], dict[str, tuple[str, Any]]]:
    """Swap each top cell for an in-memory wrapper holding a
    ``cols x rows`` array of it at its abutment pitch (issue #4226). The
    array is offset so the centre instance sits at the original coordinates;
    the returned windows map each wrapper's name to ``(original name, that
    instance's pitch box)``. An empty cell, or a 1x1 tile, is left as is."""
    import klayout.db as kdb

    cols, rows = tile
    if (cols, rows) == (1, 1):
        return top_cells, {}
    checked: list[Any] = []
    windows: dict[str, tuple[str, Any]] = {}
    for cell in top_cells:
        box = _tile_pitch_box(layout, cell)
        if box.empty() or box.width() == 0 or box.height() == 0:
            checked.append(cell)
            continue
        wrapper = layout.create_cell(f"{cell.name}__klt_tile")
        pitch_x, pitch_y = box.width(), box.height()
        wrapper.insert(
            kdb.CellInstArray(
                cell.cell_index(),
                kdb.Trans(-(cols // 2) * pitch_x, -(rows // 2) * pitch_y),
                kdb.Vector(pitch_x, 0),
                kdb.Vector(0, pitch_y),
                cols,
                rows,
            )
        )
        checked.append(wrapper)
        windows[wrapper.name] = (cell.name, box)
    return checked, windows


def _overlaps_window(low: int, high: int, start: int, stop: int) -> bool:
    """Whether the marker span ``[low, high]`` falls in the half-open window
    ``[start, stop)``: by interior overlap, or -- for a zero-extent span -- by
    containing point. Merely touching the window edge does not count, so a
    marker lying wholly in a neighbour instance is excluded."""
    if low == high:
        return start <= low < stop
    return low < stop and high > start


def _restrict_to_tile_centre(state: _DrcState, setup: _DrcSetup) -> None:
    """Keep only the markers overlapping the centre instance's window,
    relabelled to the original cell (issue #4226): neighbour-only
    markers are dropped so the array's edge instances raise no findings."""
    kept: list[dict[str, Any]] = []
    for violation in state.violations:
        entry = setup.tile_windows.get(violation["cell"])
        if entry is None:
            kept.append(violation)
            continue
        original, window = entry
        bbox = violation["bbox"]
        # Overlap, not centre-in-window: a violation on a conductor merged
        # across instances spans several of them and must still be reported.
        if not (
            _overlaps_window(bbox["left"], bbox["right"], window.left, window.right)
            and _overlaps_window(bbox["bottom"], bbox["top"], window.bottom, window.top)
        ):
            continue
        path = violation["source_path"]
        # The array instance of the cell under check is not a real child of
        # it: drop that first hop so attribution matches an untiled run.
        path = path[1:] if path else None
        violation["cell"] = original
        violation["source_path"] = path or None
        violation["source_cell"] = path[-1] if path else None
        kept.append(violation)
    state.violations = kept
    counts: dict[str, int] = {}
    for violation in kept:
        counts[violation["rule"]] = counts.get(violation["rule"], 0) + 1
    state.rule_counts = counts


def _deck_layer_tuples(deck: Any) -> set[tuple[int, int]]:
    """Every (layer, datatype) any rule in ``deck`` references."""
    # Deck-static: every (layer, datatype) any rule in this deck references,
    # regardless of what's actually present in `path`.
    deck_layer_tuples: set[tuple[int, int]] = set()
    for rule in deck:
        deck_layer_tuples.add(rule.layer)
        if rule.other_layer is not None:
            deck_layer_tuples.add(rule.other_layer)
        deck_layer_tuples.update(_spec_layers(rule))
        if rule.derived_layer is not None:
            # `rule.layer` is only the derived rule's *reporting* identity
            # (see DerivedLayer's docstring) -- the layer(s) actually read
            # to compute the checked region are these, independent of
            # whether either happens to equal `rule.layer`. `"holes"` mode
            # (#1976) reads only `base` -- `intersect_with` is `None` for it.
            deck_layer_tuples.add(rule.derived_layer.base)
            if rule.derived_layer.intersect_with is not None:
                deck_layer_tuples.add(rule.derived_layer.intersect_with)
    return deck_layer_tuples


def _prepare_drc(
    path: str,
    deck_name: str,
    top: str | None,
    pdk_variant: str | None,
    pdk_root: str | None,
    rules: list[str] | None,
    waivers_file: str | None,
    tile: tuple[int, int] | None = None,
) -> _DrcSetup:
    """The setup phase of :func:`run_drc`: PDK/path/deck resolution, rule
    selection, the layout load and the deck-static layer inventory."""
    # Resolved only when `--pdk`/`--pdk-root` was given -- this engine's
    # rules come entirely from `deck_name`, so the resolved PDK is used for
    # nothing but `provenance.pdk` (issue #1901). Mirrors extract.py's own
    # `pdk_variant is not None or pdk_root is not None` guard so an omitted
    # `--pdk` still yields `provenance.pdk: null`, not a spuriously-resolved
    # default PDK.
    pdk_info: dict[str, Any] | None = None
    if pdk_variant is not None or pdk_root is not None:
        try:
            pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
        except PdkNotFoundError as exc:
            raise DrcError(str(exc)) from exc

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

    # Issue #2768: resolved against the full deck before the layout is read,
    # so a bad selector never costs a stream load. `omitted_ids` is empty for
    # a full run (no selection, or a selection naming every rule).
    rule_selection, omitted_ids = _resolve_optional_selection(deck, rules)
    waivers = None if waivers_file is None else load_waivers(waivers_file)

    layout = load_layout(path, DrcError)

    # `deck`'s threshold_dbu values are authored against nominal_dbu_um, not
    # necessarily this layout's own dbu (see DrcRule's docstring / #172) —
    # rescale so the same physical distance is checked regardless of the
    # stream's database unit.
    dbu_scale = nominal_dbu_um / layout.dbu

    top_cells = _select_top_cells(layout, top, DrcError)
    tile_windows: dict[str, tuple[str, Any]] = {}
    if tile is not None:
        top_cells, tile_windows = _tile_top_cells(layout, top_cells, tile)

    deck_layer_tuples = _deck_layer_tuples(deck)

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
    return _DrcSetup(
        deck_name=deck_name,
        deck=deck,
        layer_names=layer_names,
        rule_selection=rule_selection,
        omitted_ids=omitted_ids,
        waivers=waivers,
        pdk_info=pdk_info,
        layout=layout,
        dbu_scale=dbu_scale,
        top_cells=top_cells,
        deck_layer_tuples=deck_layer_tuples,
        stream_layer_tuples=stream_layer_tuples,
        tile=tile,
        tile_windows=tile_windows,
    )


def _record_violation(
    state: _DrcState,
    rule: DrcRule,
    layer_label: str,
    cell: Any,
    bbox: Any,
    points: list[list[int]] | None,
) -> None:
    """Append one ``violations[]`` entry for ``rule`` and bump its count."""
    source_cell, source_path = _attribute_to_instance(cell, bbox)
    state.violations.append(
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
    state.rule_counts[rule.id] = state.rule_counts.get(rule.id, 0) + 1


def _separate_peer_plates(
    rule: DrcRule, other_region: Any, base_region: Any, dbu_scale: float
) -> tuple[Any, Any]:
    """Split ``other_region`` for a ``derived_layer`` ``"separation"`` rule
    whose ``other_layer`` is the same drawn layer ``derived_layer.intersect_with``
    reads (issue #1033); returns ``(other_region, peer_edge_pairs)``."""
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
    peer_edge_pairs = peer_region.isolated_check(round(rule.threshold_dbu * dbu_scale))
    return other_region, peer_edge_pairs


def _cell_check_regions(
    rule: DrcRule,
    cell: Any,
    setup: _DrcSetup,
    layer_index: int | None,
    base_index: int | None,
    intersect_index: int | None,
    other_index: int | None,
) -> tuple[Any, Any, Any, Any | None]:
    """Build ``(region, other_region, base_region, peer_edge_pairs)`` for
    ``rule`` on one top ``cell``."""
    import klayout.db as kdb

    layout = setup.layout
    dbu_scale = setup.dbu_scale

    base_region = None
    if rule.derived_layer is not None:
        # Virtual/derived region (#345, marker-scoped modes #1110) --
        # see `DerivedLayer`'s docstring for each mode's full
        # derivation and why an unscoped check against either raw
        # input layer would be wrong, not just conservative.
        # `sized_by_um` is a real micrometre distance, rescaled
        # against this layout's own `dbu` directly (unlike
        # `rule.threshold_dbu`, which is rescaled via `dbu_scale`
        # against the deck's nominal dbu).
        base_region = _cell_region_or_empty(cell, base_index)
        region = _derived_region(
            rule,
            base_region,
            _cell_region_or_empty(cell, intersect_index),
            layout.dbu,
            _context_region(cell, layout, rule),
            _context_region(cell, layout, rule, stage=2),
        )
    else:
        region = _select_primary_region(
            rule,
            kdb.Region(cell.begin_shapes_rec(layer_index))
            if layer_index is not None
            else kdb.Region(),  # absent conductor (#2726)
        )
    other_region = (
        _select_other_region(rule, kdb.Region(cell.begin_shapes_rec(other_index)))
        if other_index is not None
        else (kdb.Region() if rule.require_containment else None)
    )

    # Supplementary edge pairs reported under the same rule id,
    # alongside whatever `_run_check` returns -- the same additive
    # pattern `_run_check`'s own `outside_region` escape term already
    # uses (#318). Only ever set for the derived+`"separation"`
    # same-drawn-layer case (issue #1033).
    peer_edge_pairs: Any | None = None

    if (
        rule.derived_layer is not None
        and rule.check == "separation"
        and other_region is not None
        and rule.other_layer == rule.derived_layer.intersect_with
    ):
        other_region, peer_edge_pairs = _separate_peer_plates(
            rule, other_region, base_region, dbu_scale
        )

    return region, other_region, base_region, peer_edge_pairs


def _record_polygons(
    state: _DrcState,
    rule: DrcRule,
    layer_label: str,
    cell: Any,
    polygons: Any,
) -> None:
    """Record one violation per merged polygon in ``polygons``."""
    for polygon in polygons.each_merged():
        bbox = polygon.bbox()
        points = [[pt.x, pt.y] for pt in polygon.each_point_hull()]
        _record_violation(state, rule, layer_label, cell, bbox, points)


def _check_non_edge_pair_rule(
    rule: DrcRule,
    cell: Any,
    setup: _DrcSetup,
    state: _DrcState,
    layer_label: str,
    region: Any,
    other_region: Any,
) -> bool:
    """Run the area / density / antenna checks, which report something other
    than edge pairs. Returns whether ``rule`` was one of those kinds (and so
    is fully handled)."""
    if rule.check in _AREA_CHECKS:
        _record_polygons(
            state,
            rule,
            layer_label,
            cell,
            _run_area_check(region, rule, setup.dbu_scale),
        )
        return True

    if rule.check in _DENSITY_CHECKS:
        for window_box in _run_density_check(region, rule, setup.layout.dbu):
            points = [
                [window_box.left, window_box.bottom],
                [window_box.right, window_box.bottom],
                [window_box.right, window_box.top],
                [window_box.left, window_box.top],
            ]
            _record_violation(state, rule, layer_label, cell, window_box, points)
        return True

    if rule.check in _ANTENNA_CHECKS:
        antenna_bbox = _run_antenna_check(region, other_region, rule)
        if antenna_bbox is not None:
            # Flat, whole-cell aggregate -- no single real polygon
            # corresponds to "the violation" the way an "area" or
            # edge-pair check's does, see `_run_antenna_check`'s docstring.
            _record_violation(state, rule, layer_label, cell, antenna_bbox, None)
        return True

    return False


def _edge_pairs_for_rule(
    rule: DrcRule,
    cell: Any,
    setup: _DrcSetup,
    region: Any,
    other_region: Any,
) -> tuple[Any, Any]:
    """``(edge_pairs, outside_region)`` for an edge-pair-reporting ``rule``."""
    if rule.check in _NET_SPACE_CHECKS:
        return _run_net_space_check(cell, setup.layout, rule, setup.dbu_scale), None
    if rule.check in _VERTEX_CHECKS:
        # Vertex checks (issue #2642). Dispatched here rather than
        # inside `_run_check` because neither reads `threshold_dbu`
        # (and `"ongrid"` needs the layout's own `dbu`, which
        # `_run_check`'s `dbu_scale` has already divided out) -- but
        # both return the same `EdgePairs` shape, so everything
        # downstream (bbox/polygon extraction, `rule_counts`,
        # `violations[]`) is reused verbatim. Neither kind has an
        # `outside_region` term.
        return _run_vertex_check(region, rule, setup.layout.dbu), None
    return _run_check(region, other_region, rule, setup.dbu_scale)


def _check_rule_on_cell(
    rule: DrcRule,
    cell: Any,
    setup: _DrcSetup,
    state: _DrcState,
    layer_label: str,
    layer_index: int | None,
    base_index: int | None,
    intersect_index: int | None,
    other_index: int | None,
) -> None:
    """Evaluate one resolved ``rule`` against one top ``cell``, recording
    every violation in ``state``."""
    region, other_region, _, peer_edge_pairs = _cell_check_regions(
        rule, cell, setup, layer_index, base_index, intersect_index, other_index
    )

    if _check_non_edge_pair_rule(
        rule, cell, setup, state, layer_label, region, other_region
    ):
        return

    edge_pairs, outside_region = _edge_pairs_for_rule(
        rule, cell, setup, region, other_region
    )

    checked_edge_pairs = (
        edge_pairs
        if peer_edge_pairs is None
        else itertools.chain(edge_pairs, peer_edge_pairs)
    )

    for edge_pair in checked_edge_pairs:
        _record_violation(
            state,
            rule,
            layer_label,
            cell,
            edge_pair.bbox(),
            _edge_pair_hull_points(edge_pair),
        )

    # Polygon-shaped violation terms reported under the same rule id,
    # additive to the edge-pair violations above (each `None` for
    # every rule that doesn't opt into it):
    #
    # - `outside_region` (only set for "enclosing"/"enclosed", see
    #   _OUTSIDE_CHECKS) holds the part of the enclosed layer that
    #   `enclosing_check`/`enclosed_check` structurally cannot
    #   report: area with zero (or partial) spatial overlap with the
    #   enclosing layer, up to and including a shape that lies
    #   entirely outside it -- the worst-case enclosure violation
    #   (#318).
    # - the max-size term (only set for a `"width"` rule that
    #   declares `threshold_max_dbu`, see `_run_width_max_check`)
    #   holds the merged cuts whose bounding box exceeds the *upper*
    #   bound of a fixed-size rule like gf180mcu's `CO.1`/`Vn.1`
    #   (#2370) -- the half no `Region` check primitive reports.
    polygon_terms = [
        term
        for term in (
            outside_region,
            _run_width_max_check(region, rule, setup.dbu_scale),
        )
        if term is not None
    ]
    for polygon_term in polygon_terms:
        _record_polygons(state, rule, layer_label, cell, polygon_term)


def _evaluate_rule(rule: DrcRule, setup: _DrcSetup, state: _DrcState) -> None:
    """Validate ``rule``, resolve its input layers, and run it on every top
    cell -- or record it in ``state.rules_skipped`` when an input is absent."""
    layout = setup.layout

    # A `threshold_max_dbu` (#2370) attached to a check kind with no
    # upper-bound branch is a deck-authoring mistake that must fail
    # loudly, including on a stream where this rule's layer is absent
    # (below) and the check itself never runs -- see
    # `_validate_threshold_max`.
    _validate_threshold_max(rule)
    _validate_require_containment(rule)
    _validate_other_layer_selection(rule)
    _validate_layer_selection(rule)

    # Gates that skip a rule before any layer resolution below -- see
    # `_rule_skipped_before_evaluation` for each one and why. Like a
    # missing `layer`/`other_layer`, they land the rule in
    # `coverage.rules_skipped`; `_classify_skipped_rules` decides which
    # reason code each one reports.
    if _rule_skipped_before_evaluation(rule, layout):
        state.rules_skipped.append(rule.id)
        return

    # For a `derived_layer` rule (#345), the region actually checked is
    # computed from two *different* drawn layers (`derived_layer.base`/
    # `intersect_with`) rather than `rule.layer`'s own raw shapes -- see
    # `DerivedLayer`'s docstring. `rule.layer` itself is resolved only for
    # `coverage`/`layer_label` reporting below, never used to build the
    # checked region in this branch.
    if _dispatch_region_rule(
        rule,
        layout,
        setup.top_cells,
        setup.layer_names,
        state.violations,
        state.rule_counts,
        state.rules_checked,
        state.rules_skipped,
        state.empty_checked_region_ids,
    ):
        return

    base_index: int | None = None
    intersect_index: int | None = None
    layer_index: int | None = None

    if rule.derived_layer is not None:
        _validate_derived_layer(rule)
        base_index = layout.find_layer(*rule.derived_layer.base)
        intersect_index = (
            layout.find_layer(*rule.derived_layer.intersect_with)
            if rule.derived_layer.intersect_with is not None
            else None
        )
        if _derived_inputs_absent(rule, base_index, intersect_index):
            # See `_derived_inputs_absent` for each mode's skip condition.
            state.rules_skipped.append(rule.id)
            return
    else:
        layer_index = layout.find_layer(*rule.layer)
        if layer_index is None and not (
            rule.require_containment
            and _containment_layers(rule)[1] == rule.layer
            and layout.find_layer(*_containment_layers(rule)[0]) is not None
        ):
            # rule's layer is absent from this stream -> no violations,
            # but record it so `coverage.rules_skipped` can surface the
            # skip.
            state.rules_skipped.append(rule.id)
            return

    other_index = None
    if rule.other_layer is not None:
        other_index = layout.find_layer(*rule.other_layer)
        if other_index is None and not (
            rule.require_containment
            and _containment_layers(rule)[1] == rule.other_layer
            and layout.find_layer(*_containment_layers(rule)[0]) is not None
        ):
            state.rules_skipped.append(rule.id)
            return

    # Every return above is a skip; reaching here means this rule's
    # input layer(s) all resolved, so it is about to be evaluated against
    # real geometry -- record it so `coverage.rules_checked` (and the
    # `coverage.nothing_checked` roll-up derived from it) can state what
    # this verdict was actually measured over (issue #1996).
    state.rules_checked.append(rule.id)

    layer_label = setup.layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")

    for cell in setup.top_cells:
        _check_rule_on_cell(
            rule,
            cell,
            setup,
            state,
            layer_label,
            layer_index,
            base_index,
            intersect_index,
            other_index,
        )


def _marker_interacts_in_cell(
    layout: Any,
    cell: Any,
    marker_index: int,
    unscoped_layers: set[tuple[int, int]],
) -> bool:
    """Whether the marker layer's geometry in ``cell`` interacts with any of
    ``unscoped_layers``."""
    import klayout.db as kdb

    marker_region = kdb.Region(cell.begin_shapes_rec(marker_index))
    if marker_region.is_empty():
        return False
    for checked_layer in sorted(unscoped_layers):
        checked_index = layout.find_layer(*checked_layer)
        if checked_index is None:
            continue
        checked_region = kdb.Region(cell.begin_shapes_rec(checked_index))
        if not marker_region.interacting(checked_region).is_empty():
            return True
    return False


def _voltage_domain_warnings(
    setup: _DrcSetup, skipped_rule_ids: set[str]
) -> list[dict[str, str]]:
    """``coverage.voltage_domain_warnings`` (issue #552).

    A marker this deck registers via `get_unmodeled_voltage_markers` (e.g.
    gf180mcu's `Dualgate` 55/0) selects a second gate-oxide/voltage domain
    whose real DRC thresholds this deck's rules do not encode -- geometry
    inside it is checked against the wrong (default) column and would
    otherwise report an unqualified `layers_checked` pass. Gated on the
    marker's geometry actually *interacting* with at least one layer an
    **unscoped** rule checked on this run (not merely present in the stream),
    so a marker shape that never overlaps any such geometry produces no
    warning with nothing behind it.

    "Unscoped" is per rule, not per deck (issue #1110): a marker can be
    partly modelled. gf180mcu's `DF.1a`/`DF.3a` now ship as `_LV`/`_MV`
    rule pairs that read `Dualgate` themselves via `DerivedLayer`'s
    `"overlapping"`/`"not_interacting"` modes, so geometry those rules
    checked was *not* checked against the wrong column and must not
    produce a warning -- while every rule that ran without reading the
    marker still can. Rules skipped this run (their layers absent from
    this stream) are excluded too: a rule that never ran applied no
    threshold, right or wrong.
    """
    layout = setup.layout
    stream_layer_tuples = setup.stream_layer_tuples
    warnings: list[dict[str, str]] = []
    unmodeled_markers = get_unmodeled_voltage_markers(setup.deck_name)
    for marker, description in sorted(unmodeled_markers.items()):
        if marker not in stream_layer_tuples:
            continue
        marker_index = layout.find_layer(*marker)
        if marker_index is None:
            continue
        unscoped_layers: set[tuple[int, int]] = set()
        for rule in setup.deck:
            if _rule_outside_voltage_gate(rule, marker, skipped_rule_ids):
                continue
            unscoped_layers |= _rule_input_layers(rule)
        unscoped_layers &= stream_layer_tuples
        unscoped_layers.discard(marker)
        if any(
            _marker_interacts_in_cell(layout, cell, marker_index, unscoped_layers)
            for cell in setup.top_cells
        ):
            warnings.append({"marker": _layer_str(marker), "description": description})
    return warnings


def _build_coverage(setup: _DrcSetup, state: _DrcState) -> dict[str, Any]:
    """The report's ``coverage`` block."""
    deck = setup.deck
    deck_name = setup.deck_name
    stream_layer_tuples = setup.stream_layer_tuples
    deck_layer_tuples = setup.deck_layer_tuples
    omitted_ids = setup.omitted_ids
    rules_checked = state.rules_checked
    rules_skipped = state.rules_skipped

    layers_checked = deck_layer_tuples & stream_layer_tuples
    layers_in_stream_without_rules = stream_layer_tuples - deck_layer_tuples

    # Deck-static, like `deck_layers` above: every distinct non-empty
    # DrcRule.scope this deck's rules declare, regardless of what's actually
    # present in `path` (#566).
    deck_scope = sorted({rule.scope for rule in deck if rule.scope})

    # A rule omitted from a `--rules` subset (issue #2768) applied no
    # threshold either, exactly like a skipped one.
    skipped_rule_ids = set(rules_skipped) | omitted_ids
    voltage_domain_warnings = _voltage_domain_warnings(setup, skipped_rule_ids)

    # Issue #3012: foundry rules the deck knows but cannot evaluate, named so
    # a `clean` cannot be read as covering them. Only when the rule's layer
    # is actually in the stream.
    unchecked_rules = [
        {"rule": rule_id, "layer": _layer_str(layer), "description": description}
        for rule_id, (layer, description) in sorted(
            get_unchecked_rules(deck_name).items()
        )
        if layer in stream_layer_tuples
    ]

    # Issue #3815: area-marker layers the deck accepts but does not model, so
    # a result identical with and without the marker is not read as
    # marker-aware. Informational only, like `unchecked_rules`.
    marker_blind_layers_in_stream = sorted(
        _layer_str(layer)
        for layer in get_marker_blind_layers(deck_name)
        if layer in stream_layer_tuples
    )

    # Issue #2110: the two common-coverage categories this engine's single
    # "a layer it reads is absent" skip splits into -- see
    # `_classify_skipped_rules` for why an empty assessment domain is
    # inapplicable work rather than a skipped request, and why anything else
    # stays a disclosed skip.
    skipped_records, inapplicable_records = _classify_skipped_rules(
        deck, rules_skipped, setup.layout, frozenset(state.empty_checked_region_ids)
    )
    # Issue #2768: every deliberately omitted rule is requested work that did
    # not run -- a skip, never inapplicable, even when its layers are absent
    # (the full registered deck is what a DRC verdict is about).
    skipped_records += [
        {"id": rule_id, "reason": SKIP_REASON_NOT_SELECTED}
        for rule_id in sorted(omitted_ids)
    ]
    return {
        "deck_layers": [_layer_str(t) for t in sorted(deck_layer_tuples)],
        "layers_checked": [_layer_str(t) for t in sorted(layers_checked)],
        "layers_in_stream_without_rules": [
            _layer_str(t) for t in sorted(layers_in_stream_without_rules)
        ],
        "rules_checked": sorted(rules_checked),
        "rules_skipped": sorted(rules_skipped),
        "voltage_domain_warnings": voltage_domain_warnings,
        "unchecked_rules": unchecked_rules,
        "marker_blind_layers_in_stream": marker_blind_layers_in_stream,
        "deck_scope": deck_scope,
        # Issue #3031: deck-static, like `deck_scope` -- official rule ids the
        # deck knowingly does not transcribe.
        "unmodeled_rules": get_unmodeled_rules(deck_name),
        # Issue #1996: the shared roll-up, so a reader (`klt signoff`) can
        # refuse a "clean" verdict measured over nothing without re-deriving
        # that emptiness from `rules_skipped` vs. the deck's own rule count.
        **build_check_coverage(
            checked=sorted(rules_checked),
            skipped=skipped_records,
            inapplicable=inapplicable_records,
            nothing_checked_reasons=_curated_nothing_checked_reasons(
                deck, rules_checked
            ),
        ),
    }


def _assemble_report(
    path: str, setup: _DrcSetup, state: _DrcState, waivers_file: str | None
) -> dict[str, Any]:
    """The report-assembly phase of :func:`run_drc`: sort, coverage, waivers,
    verdict and provenance."""
    if setup.tile_windows:
        _restrict_to_tile_centre(state, setup)
    violations = state.violations
    rule_counts = state.rule_counts
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

    coverage = _build_coverage(setup, state)

    # Issue #2110: the verdict is the common decision table's, never a local
    # re-derivation of it -- violations are `failed` (exit 3, decided before
    # coverage is consulted at all), known zero checked work is `not_checked`
    # (exit 4), and successful checks alongside a nonempty skipped-request
    # list are `clean_partial` (exit 0, a real result that is *not* this
    # verb's unconditional success). See `docs/coverage-contract.md`.
    # Issue #3483: waived markers leave every count and the verdict, and are
    # reported in full under `waived` -- never dropped.
    waiver_fields: dict[str, Any] = {}
    if setup.waivers is not None:
        violations, rule_counts, waiver_fields = _apply_waivers(
            violations, setup.waivers
        )
    rollup = coverage_rollup({"coverage": coverage}, failed=bool(violations))
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        # Issue #2659: the committable `{path, scope}` shape, never the
        # producing host's absolute path -- see `_report_path`.
        "file": _report_path(path, repo_root=_report_repo_root(path)),
        "deck": setup.deck_name,
        "dbu_um": setup.layout.dbu,
        "status": rollup_status(rollup, success="clean", failure="violations"),
        "violation_count": len(violations),
        "rule_counts": dict(sorted(rule_counts.items())),
        "metrics": {_VIOLATION_COUNT_METRIC_NAME: len(violations)},
        "violations": violations,
        "coverage": coverage,
        # Issue #2768: present only for a `--rules` run, so a default run's
        # report (and every committed one) is byte-identical to before.
        **(
            {}
            if setup.rule_selection is None
            else {"rule_selection": setup.rule_selection}
        ),
        **waiver_fields,
        # Issue #4226: present only for a `--tile` run, so a default run's
        # report is byte-identical to before.
        **(
            {}
            if setup.tile is None
            else {"tile": {"columns": setup.tile[0], "rows": setup.tile[1]}}
        ),
    }
    report["provenance"] = build_provenance(
        deck_name=setup.deck_name,
        deck_path=deck_source_path(setup.deck_name),
        pdk=setup.pdk_info,
        input_path=path,
        include_klayout_version_mismatch=True,
    )
    if waivers_file is not None:
        report["provenance"]["waivers"] = {
            "file": waivers_file,
            "content_hash": _content_hash(waivers_file),
        }
    return report


def run_drc(
    path: str,
    deck_name: str,
    top: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    rules: list[str] | None = None,
    waivers_file: str | None = None,
    tile: str | tuple[int, int] | None = None,
    negative_control: str | None = None,
    negative_control_categories: list[str] | None = None,
) -> dict[str, Any]:
    """Run ``deck_name``'s rules against the layout at ``path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/drc.md``)::

        {
            "schema_version": 3,
            "file": {"path": <repo-relative path>, "scope": "repo"},
            "deck": <deck name>,
            "dbu_um": <database unit in micrometres, float>,
            "status": "clean" | "clean_partial" | "violations" | "not_checked",
            "violation_count": <int>,
            "rule_counts": {<rule id>: <int>, ...},
            "metrics": {"drc__error__count": <int>},
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
                "rules_checked": [<rule id>, ...],
                "rules_skipped": [<rule id>, ...],
                "voltage_domain_warnings": [
                    {"marker": "<layer>/<datatype>", "description": str}, ...
                ],
                "unchecked_rules": [
                    {"rule": str, "layer": "<layer>/<datatype>",
                     "description": str}, ...
                ],
                "marker_blind_layers_in_stream": ["<layer>/<datatype>", ...],
                "deck_scope": [<scope identifier>, ...],
                "unmodeled_rules": [<official rule id>, ...],
                "nothing_checked": bool,
                "nothing_checked_reasons": [<reason code>, ...],
            },
            "rule_selection": {  # only when `rules` is given (issue #2768)
                "selectors": [str, ...], "selected": [<rule id>, ...],
                "omitted": [<rule id>, ...], "deck_rule_count": int,
                "subset": bool,
            },
            "provenance": {  # shared reproducibility block, see _provenance.py
                "klt_version": <str | None>,
                "klayout_version": <str | None>,
                "pdk": {"name": ..., "source": ..., "version": ...} | None,
                "deck": {"name": <deck name>, "content_hash": "sha256:..."},
            },
        }

    ``schema_version`` is versioned independently per command (see
    ``docs/json-contract.md``); it starts at ``1`` and only increments when
    this command's JSON shape changes in a way that isn't purely additive.

    ``metrics`` (issue #1847, adopting the declared metric namespace
    registry from #247 beyond its ``klt layout-metrics`` pilot) is a
    **parallel, additive** object re-keying ``violation_count`` under its
    declared METRICS2.1-style name from :mod:`klayout_tools.metrics`'s
    registry -- ``violation_count`` -> ``drc__error__count``. It never
    replaces or changes ``violation_count``/``rule_counts`` above, which stay
    exactly as documented; ``metrics`` is always present (even in the clean,
    zero-violation case, where ``drc__error__count`` is ``0``), purely
    additive to the JSON contract (no ``schema_version`` bump).

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
    rule ids skipped because their layer(s) were absent, and
    ``coverage.rules_checked`` (issue #1996) its complement -- the rule ids
    that actually ran against real geometry. A ``"clean"`` ``status`` with a
    non-empty ``layers_in_stream_without_rules`` means "clean, and here is
    exactly what was not looked at" rather than a fully-verified pass.

    ``coverage.nothing_checked``/``nothing_checked_reasons`` (issue #1996)
    are the shared convention declared in :mod:`klayout_tools.coverage`,
    emitted by every verb that can reach a vacuous verdict. Here they are
    ``True`` / ``["all_rules_skipped"]`` exactly when ``rules_checked`` is
    empty while the deck declares rules (the degenerate empty-stream case:
    every rule's layer is absent, so a verdict would have been measured over
    nothing at all), or ``["deck_has_no_rules"]`` for a deck that declares
    none. That case reports ``status: "not_checked"`` (exit 4, issue #2108):
    it reaches no verdict rather than a vacuous ``"clean"``, which is what
    lets `klt signoff` refuse to count such a report as evidence (see
    ``signoff.py``'s "Vacuous-verdict refusal" docstring section).

    ``status`` itself comes from the common rollup table (issue #2110,
    :func:`~klayout_tools.coverage.coverage_rollup` /
    :func:`~klayout_tools.coverage.rollup_status`), not from a mapping
    private to this module, so this path cannot disagree with any other
    audited verb about when a result may be called an unconditional success:
    violations win first (``"violations"``, exit 3), then known zero checked
    work (``"not_checked"``, exit 4), then a nonempty list of skipped
    *requested* rules (``"clean_partial"``, exit 0 -- a real result that is
    not this verb's unconditional success), and otherwise ``"clean"``.
    :func:`drc_exit_code` derives the matching exit status from the same
    table.

    The ``skipped``/``inapplicable`` split behind that ``"clean_partial"``
    row is :func:`_classify_skipped_rules`'s, and it is the reason an
    ordinary run over a small block still earns an unconditional ``"clean"``:
    a rule skipped for an absent input layer is *inapplicable* work when
    running it could not have reported a violation anyway, and a skipped
    *request* only when it could have. ``coverage.rules_skipped`` above keeps
    listing both, unchanged.

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
    (it applied no threshold at all) and any rule flagged
    :attr:`~klayout_tools.decks.DrcRule.voltage_independent` (issue #2369 --
    a rule whose source DRM table publishes a single column, so no second
    column exists for it to have read by mistake; gf180mcu's Nplus/Pplus
    implant rules are the first). The warning therefore keeps firing for
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

    ``pdk_variant``/``pdk_root`` (the ``--pdk``/``--pdk-root`` flags, issue
    #1901) resolve no rule content -- this curated engine's rules come
    entirely from ``deck_name`` -- but, when either is given, are resolved
    via :func:`klayout_tools.pdk.find_pdk` purely so ``provenance.pdk`` can
    record which PDK revision the run was evidence for (the same shape
    ``klt extract``/``klt sta``/``klt place-and-route`` already populate).
    Omitting both (the default) leaves ``provenance.pdk`` ``None``, matching
    every prior release's behaviour.

    ``rules`` (issue #2768, the ``--rules`` flag) is a list of selector
    tokens -- exact rule ids or case-sensitive shell-style globs, already
    split by :func:`parse_rule_selectors` -- restricting which deck rules are
    *executed*. It is resolved once against the full registered deck by
    :func:`resolve_rule_selection` before the layout is even read, so an
    unmatched token fails fast. Unselected rules are never dispatched (not
    filtered out afterwards). The full deck still drives every deck-static
    field (``coverage.deck_layers``/``layers_checked``/
    ``layers_in_stream_without_rules``/``deck_scope``) and ``provenance``;
    each omitted rule is recorded as a common-coverage ``skipped`` record
    with reason :data:`SKIP_REASON_NOT_SELECTED` -- never as inapplicable,
    whatever its layers -- so a proper subset rolls up ``"clean_partial"``
    (or ``"violations"``/``"not_checked"`` by the usual precedence) and can
    never read as an unconditional ``"clean"``. A top-level
    ``rule_selection`` block (:func:`resolve_rule_selection`) records the
    selectors, selected and omitted ids; it is absent when ``rules`` is
    ``None``, so a default run's report is unchanged. Selecting every rule is
    equivalent to a full run (``omitted: []``).

    ``waivers_file`` (issue #3483, ``--waivers``) names a reviewed JSON file
    of rule-scoped cell waivers (:func:`load_waivers`). A violation whose
    ``source_cell`` and rule both match a waiver moves out of ``violations``/
    ``violation_count``/``rule_counts``/``metrics`` (and so out of
    ``status``) into a top-level ``waived`` array carrying
    ``waiver_justification``, with ``waived_count``, ``waived_rule_counts``
    and ``unused_waivers`` alongside; ``provenance.waivers`` pins the file by
    content hash. All of these are absent when ``waivers_file`` is ``None``,
    so a default report is unchanged. A marker straddling two instances
    carries the container's ``source_cell`` (see
    :func:`_attribute_to_instance`), so waive it under that cell.

    ``negative_control`` (issue #4320, ``--negative-control``) names a
    known-bad layout the same deck is run over *first*, with the same
    ``top``/``rules``/pdk options but no waivers (a waiver must not hide the
    control's violations). A fixture with no violation, or one missing a
    ``negative_control_categories`` rule id, raises :class:`DrcError` (code
    ``negative_control_failed``) and no verdict is reached for ``path``. On
    success the report gains the native engine's ``negative_control`` block;
    it is absent without the flag, so a default report is unchanged.

    Raises :class:`DrcError` if the file is missing/unreadable, the deck
    name is unknown, ``top`` names a cell absent from the stream, a rule is
    malformed (e.g. a two-layer check missing ``other_layer``),
    ``pdk_variant``/``pdk_root`` is given but no matching PDK can be found,
    a ``rules`` selector matches no rule, or the negative control fails.
    """
    # Issue #4226 (``--tile``): ``tile`` is ``"<cols>x<rows>"`` (odd counts)
    # or an already-parsed pair. Each checked top cell is placed as that
    # array at its abutment pitch (``prBoundary`` when drawn, else its bbox)
    # and only markers overlapping the middle instance are reported, so a
    # cell cut at its boundary is judged as placed among identical
    # neighbours. The report gains ``tile``; absent otherwise.
    tile_pair = parse_tile(tile) if isinstance(tile, str) else tile
    control_result = None
    if negative_control is not None:
        control_result = _run_curated_negative_control(
            negative_control,
            negative_control_categories,
            deck_name=deck_name,
            top=top,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            rules=rules,
        )
    setup = _prepare_drc(
        path, deck_name, top, pdk_variant, pdk_root, rules, waivers_file, tile_pair
    )
    state = _DrcState()

    # Issue #2768: a rule left out of a `--rules` subset is never dispatched
    # -- not run-and-filtered (see `_dispatched_rules`). It is recorded
    # separately from `rules_skipped` (absent-layer skips) so the omission
    # cannot be reclassified as inapplicable work.
    for rule in _dispatched_rules(setup.deck, setup.omitted_ids):
        _evaluate_rule(rule, setup, state)

    report = _assemble_report(path, setup, state, waivers_file)
    if control_result is not None:
        report["negative_control"] = control_result
    return report


def _run_curated_negative_control(
    control_path: str,
    categories: list[str] | None,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Run the same curated deck over a known-bad fixture and require
    findings (issue #4320), mirroring the native engine's control: proof the
    deck can fail on this stream's own layers, so a clean verdict on the real
    layout is not vacuous. Raises :class:`DrcError` (code
    ``negative_control_failed``) on no violations or a missing category."""
    control = run_drc(control_path, **run_kwargs)
    found = sorted({v["rule"] for v in control["violations"]})
    missing = sorted(set(categories or []) - set(found))
    if control["violation_count"] < 1 or missing:
        detail = (
            f"missing expected categories {missing}"
            if control["violation_count"] and missing
            else "no violations reported"
        )
        raise DrcError(
            f"negative control {control_path!r} failed on the same deck: "
            f"{detail} -- the deck may not have executed its rules, so a "
            "clean verdict on the real layout cannot be trusted",
            code=CODE_NEGATIVE_CONTROL_FAILED,
        )
    return {
        "file": control_path,
        "status": "passed",
        "violation_count": control["violation_count"],
        "rules_found": found,
        "expected_categories": sorted(categories or []),
    }


def drc_exit_code(report: dict[str, Any]) -> int:
    """The exit code the common rollup rule assigns ``report`` (issue #2110).

    One derivation for both engines and for every consumer that mirrors
    ``klt drc``'s own exit status (`klt eval`'s gate, today), so the
    ``violations`` -> ``3`` / ``not_checked``|``coverage_unknown`` -> ``4`` /
    ``clean``|``clean_partial`` -> ``0`` split cannot drift between them:
    the producer's own failure determination (``status == "violations"``,
    which :func:`~klayout_tools.coverage.coverage_rollup` decides *before*
    coverage) is fed back in, and the table supplies the rest.

    ``report`` must be a finished DRC report, not a ``--check``/``--rerun``
    verification result -- those keep their own ``match``/``drifted`` 0/3
    contract (see :func:`check_drc_report`).
    """
    return coverage_rollup(
        report, failed=report.get("status") == "violations"
    ).exit_code


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


def check_drc_report(
    report_path: str, *, input_path: str | None = None
) -> dict[str, Any]:
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
            _content_hash(
                resolve_committed_path(
                    committed.get("file"),
                    report_path=report_path,
                    override=input_path,
                )
            ),
        ),
        hash_check(
            "provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            _content_hash(_deck_path_for_report(committed)),
        ),
    ]
    # Issue #3483: a waived report also pins its waivers file.
    waivers_file = get_path(committed, ("provenance", "waivers", "file"))
    if waivers_file is not None:
        checks.append(
            hash_check(
                "provenance.waivers.content_hash",
                get_path(committed, ("provenance", "waivers", "content_hash")),
                _content_hash(waivers_file),
            )
        )
    recorded_tree = get_path(committed, ("provenance", "deck", "tree", "content_hash"))
    if recorded_tree is not None:
        deck_path = _deck_path_for_report(committed)
        from .drc_klayout_engine import _deck_tree_identity

        current_tree = _deck_tree_identity(deck_path) if deck_path else None
        checks.append(
            hash_check(
                "provenance.deck.tree.content_hash",
                recorded_tree,
                current_tree["content_hash"] if current_tree else None,
            )
        )
    return build_check_result(report_path=report_path, checks=checks)


def _recorded_rule_selectors(committed: dict[str, Any]) -> list[str] | None:
    """The ``--rules`` selectors a committed curated report was run with
    (issue #2768), or ``None`` for a full-deck report (no ``rule_selection``
    key -- including every report predating the flag).

    Raises :class:`DrcError` for a present but malformed block rather than
    silently replaying it as a full run, which would turn a recorded subset
    into a full-deck claim.
    """
    selection = committed.get("rule_selection")
    if selection is None:
        return None
    selectors = selection.get("selectors") if isinstance(selection, dict) else None
    if (
        not isinstance(selectors, list)
        or not selectors
        or not all(isinstance(token, str) and token.strip() for token in selectors)
    ):
        raise DrcError(
            "committed report's 'rule_selection.selectors' must be a non-empty "
            "list of rule selector strings to replay the selection"
        )
    return parse_rule_selectors(selectors)


def _recorded_tile(committed: dict[str, Any]) -> tuple[int, int] | None:
    """The ``(columns, rows)`` a committed report's ``tile`` echo records
    (issue #4226), or ``None`` for a report that carries none."""
    recorded = committed.get("tile")
    if not isinstance(recorded, dict):
        return None
    cols, rows = recorded.get("columns"), recorded.get("rows")
    if isinstance(cols, int) and isinstance(rows, int):
        return cols, rows
    return None


def rerun_drc_report(
    report_path: str, *, input_path: str | None = None
) -> dict[str, Any]:
    """``klt drc --check <report> --rerun`` (full mode, issue #1106):
    verify a previously committed ``klt drc --format json`` report at
    ``report_path`` by actually re-running the DRC deck it names and
    diffing the fresh report against the committed one.

    Re-runs against exactly the ``file``/``deck``/``engine`` the committed
    report itself names -- :func:`run_drc_klayout_engine` when
    ``committed["engine"] == "klayout"``, :func:`run_drc` (the curated
    engine) otherwise -- replaying a curated ``--rules`` subset's recorded
    ``rule_selection.selectors`` (issue #2768, :func:`_recorded_rule_selectors`).
    **Known limitation**: neither the curated nor the
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
    file_path = resolve_committed_path(
        committed.get("file"), report_path=report_path, override=input_path
    )
    if not file_path:
        raise DrcError(f"committed report has no 'file' field to rerun: {report_path}")
    deck = committed.get("deck")
    if not deck:
        raise DrcError(f"committed report has no 'deck' field to rerun: {report_path}")

    if committed.get("engine") == "klayout":
        from .drc_klayout_engine import _rerun_assertion_kwargs, run_drc_klayout_engine

        # A committed report carrying `engine_deck_errors` was produced with
        # `--allow-deck-errors` (issue #1941); re-running it strictly would
        # fail the verification outright rather than diffing it, so the
        # rerun reproduces the original invocation's own error tolerance.
        fresh = run_drc_klayout_engine(
            file_path,
            deck,
            top=None,
            allow_deck_errors=bool(committed.get("engine_deck_errors")),
            # Issue #2697: reproduce the original coverage assertion too,
            # or a committed `clean` would always drift to `coverage_unknown`.
            # Issue #2806: dispatched on its `kind`; a set assertion replays
            # its embedded names, never the original manifest file.
            **_rerun_assertion_kwargs(committed),
        )
    else:
        # Issue #2768: a committed `--rules` subset replays its own recorded
        # selectors (so a deck change that alters what a glob matches shows
        # up as drift); a report without `rule_selection` -- every report
        # written before the flag existed -- keeps its full-deck meaning.
        fresh = run_drc(
            file_path,
            deck,
            top=None,
            rules=_recorded_rule_selectors(committed),
            # Issue #3483: replay the waivers the committed report applied.
            waivers_file=get_path(committed, ("provenance", "waivers", "file")),
            # Issue #4226: replay the abutment context the report was run in.
            tile=_recorded_tile(committed),
        )

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
        if check == "enclosing_adjacent":
            return _run_enclosing_adjacent_check(region, other_region, d)
        edge_pairs = getattr(region, f"{check}_check")(other_region, d)

        outside_region = None
        if check in _OUTSIDE_CHECKS and rule.report_escapes:
            if check == "enclosing":
                # `region` is the enclosing layer, `other_region` the
                # enclosed one -- among the `other_region` shapes that do
                # touch `region` somewhere, flag whatever part of them
                # escapes `region` entirely (a plain Boolean NOT, no
                # threshold: any escape at all is worse than a
                # marginal-distance violation).
                outside_region = (
                    other_region - region
                    if rule.require_containment
                    else other_region.interacting(region) - region
                )
            else:  # check == "enclosed"
                # Symmetric: `region` is the enclosed layer here.
                outside_region = (
                    region - other_region
                    if rule.require_containment
                    else region.interacting(other_region) - other_region
                )

        return edge_pairs, outside_region

    raise DrcError(f"rule '{rule.id}': unsupported check kind '{check}'")


def _run_net_space_check(
    cell: Any, layout: Any, rule: DrcRule, dbu_scale: float
) -> Any:
    """``check="net_space"`` (issue #3572): spacing between polygons of
    ``region`` that sit on *different* nets.

    Nets come from KLayout's own ``LayoutToNetlist`` connectivity over the
    chain declared in ``rule.net_spec`` (:class:`NetSpaceSpec`): the checked
    layer joins the tap (``tap_and`` layers intersected with it), the tap
    joins ``chain[0]``, and so on up the chain. Each polygon of ``region``
    is tagged with its net id as a user property, and
    ``Region.space_check(..., DifferentPropertiesConstraint)`` -- the
    primitive behind the DRC-DSL ``space(d, props_ne)`` the ASAP7 deck uses
    -- then measures only polygon pairs on different nets. A polygon with no
    tap is its own net; a connection outside the chain is not modelled.
    """
    import klayout.db as kdb

    spec = rule.net_spec
    if spec is None:
        raise DrcError(f"rule '{rule.id}': check '{rule.check}' requires net_spec")
    d = round(rule.threshold_dbu * dbu_scale)

    # Extract on a flattened copy: `LayoutToNetlist` is hierarchical, so a
    # well whose net lives wholly inside a subcell circuit would never be
    # visited through the top circuit's nets (a silent miss -- the opposite
    # of this check's over-flag policy). Flat, every shape is a top-circuit
    # shape in top coordinates and nets are unique by construction.
    flat_layout = kdb.Layout()
    flat_layout.assign(layout)
    cell = flat_layout.cell(cell.name)
    cell.flatten(True)
    layout = flat_layout
    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, cell, []))

    def layer_region(pair: tuple[int, int]) -> Any:
        index = layout.find_layer(*pair)
        if index is None:
            return l2n.make_layer(f"{pair[0]}_{pair[1]}")
        return l2n.make_layer(index, f"{pair[0]}_{pair[1]}")

    checked = layer_region(rule.layer)
    tap = checked
    for pair in spec.tap_and:
        tap = tap & layer_region(pair)
    l2n.register(tap, "tap")
    chain = [layer_region(pair) for pair in spec.chain]
    for conductor in (checked, tap, *chain):
        l2n.connect(conductor)
    l2n.connect(checked, tap)
    previous = tap
    for conductor in chain:
        l2n.connect(previous, conductor)
        previous = conductor
    l2n.extract_netlist()

    tagged = kdb.Region()
    circuit = l2n.netlist().circuit_by_name(cell.name)
    if circuit is None:
        return kdb.EdgePairs()
    for net in circuit.each_net():
        for polygon in l2n.shapes_of_net(net, checked, True).each():
            tagged.insert(kdb.PolygonWithProperties(polygon, {1: net.cluster_id}))
    return tagged.space_check(
        d,
        False,
        kdb.Region.Euclidian,
        90,
        None,
        None,
        False,
        kdb.Region.NoOppositeFilter,
        kdb.Region.NoRectFilter,
        False,
        kdb.Region.DifferentPropertiesConstraint,
    )


def _run_enclosing_adjacent_check(
    region: Any, other_region: Any, d: int
) -> tuple[Any, Any]:
    """``check="enclosing_adjacent"``: sky130's two-adjacent-edges enclosure
    rules (``via.5a``, ``m2.5``, ``via2.5``, ``via3.5``, #3316).

    Mirrors ``sky130A_mr.drc``: the cut edges whose enclosure is below ``d``
    (``enclosing_check(...).second_edges``) are tested for a *corner* --
    two short edges meeting at an angle under 100 degrees -- and every cut
    touching such a corner is a violation. So a cut passes when its short
    sides (< ``d``) are at most one side or two opposite sides; it fails
    when two adjacent sides are both short. A cut with no facing edge at
    all yields no edge pair and is not reported here (the sibling
    ``enclosing`` rule already reports an escape).

    Returns ``(empty EdgePairs, cuts)``: one polygon per offending cut
    (reported through ``run_drc``'s polygon-term path), not one entry per
    corner, which is the official deck's per-cut counting.
    """
    import klayout.db as kdb

    short_edges = region.enclosing_check(
        other_region, d, metrics=kdb.Metrics.Projection
    ).second_edges()
    corners = short_edges.width_check(
        1, metrics=kdb.Metrics.Euclidian, ignore_angle=100
    )
    bad_cuts = other_region.interacting(corners.polygons(1))
    return kdb.EdgePairs(), bad_cuts


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
        if rule.derived_layer.intersect_with is not None:
            layers.add(rule.derived_layer.intersect_with)
    else:
        layers.add(rule.layer)
    if rule.other_layer is not None:
        layers.add(rule.other_layer)
    return layers


def _validate_require_containment(rule: DrcRule) -> None:
    """Reject a malformed ``require_containment`` declaration (issue #2726).

    Runs for every rule before any layer-presence shortcut, so a bad
    declaration fails loudly even on a stream that lacks its layers.
    """
    if not rule.require_containment:
        return
    if rule.check not in _OUTSIDE_CHECKS:
        raise DrcError(
            f"rule '{rule.id}': require_containment is only valid on "
            f"'enclosing'/'enclosed' checks, not '{rule.check}'"
        )
    if rule.other_layer is None:
        raise DrcError(f"rule '{rule.id}': require_containment requires other_layer")
    if rule.derived_layer is not None:
        raise DrcError(
            f"rule '{rule.id}': require_containment cannot be combined with "
            "derived_layer"
        )


def _containment_layers(rule: DrcRule) -> tuple[tuple[int, int], tuple[int, int]]:
    """``(cut_layer, conductor_layer)`` of a ``require_containment`` rule."""
    assert rule.other_layer is not None
    if rule.check == "enclosing":
        return rule.other_layer, rule.layer
    return rule.layer, rule.other_layer


def _validate_derived_layer(rule: DrcRule) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose
    ``derived_layer.mode`` names a derivation this engine has no branch for
    (issue #1110).

    Mirrors ``_run_area_check``'s own "a deck-authoring mistake fails loudly
    with the rule id, never silently returns nothing" contract: an unknown
    mode would otherwise fall through to the default ``"sized_intersection"``
    derivation and quietly check a completely different region than the deck
    author asked for.

    Also rejects a ``"union"`` derivation with no ``intersect_with``
    (issue #2688), which would silently degrade to a plain ``base`` check.
    """
    derived = rule.derived_layer
    if derived is not None and derived.mode not in _DERIVED_LAYER_MODES:
        raise DrcError(
            f"rule '{rule.id}': unknown derived_layer mode "
            f"'{derived.mode}' (known: {', '.join(sorted(_DERIVED_LAYER_MODES))})"
        )
    if (
        derived is not None
        and derived.mode == "union"
        and derived.intersect_with is None
    ):
        raise DrcError(
            f"rule '{rule.id}': derived_layer mode 'union' requires intersect_with"
        )
    if derived is not None and derived.context not in _DERIVED_CONTEXTS:
        raise DrcError(
            f"rule '{rule.id}': unknown derived_layer context "
            f"'{derived.context}' (known: near, far, or unset)"
        )
    if derived is not None and (derived.context is None) == bool(
        derived.context_layers
    ):
        raise DrcError(
            f"rule '{rule.id}': derived_layer context and context_layers "
            "must be set together"
        )
    if derived is not None and derived.context2 not in _DERIVED_CONTEXTS:
        raise DrcError(
            f"rule '{rule.id}': unknown derived_layer context2 "
            f"'{derived.context2}' (known: near, far, or unset)"
        )
    if derived is not None and (derived.context2 is None) == bool(
        derived.context2_layers
    ):
        raise DrcError(
            f"rule '{rule.id}': derived_layer context2 and context2_layers "
            "must be set together"
        )
    if derived is not None and derived.context2 is not None and derived.context is None:
        raise DrcError(
            f"rule '{rule.id}': derived_layer context2 requires context "
            "(stage 2 applies after stage 1)"
        )


def _validate_other_layer_selection(rule: DrcRule) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose
    ``other_layer_selection`` this engine cannot apply (issue #2688): an
    unknown value, or one set on a rule with no ``other_layer`` to select
    from. Same "fail loudly with the rule id" contract as
    :func:`_validate_derived_layer` (which owns the ``"union"``-needs-
    ``intersect_with`` check alongside the other ``derived_layer`` mode
    validation).
    """
    selection = rule.other_layer_selection
    if selection is not None:
        if selection not in _OTHER_LAYER_SELECTIONS:
            raise DrcError(
                f"rule '{rule.id}': unknown other_layer_selection "
                f"'{selection}' (known: {', '.join(sorted(_OTHER_LAYER_SELECTIONS))})"
            )
        if rule.other_layer is None:
            raise DrcError(
                f"rule '{rule.id}': other_layer_selection requires other_layer"
            )


def _validate_layer_selection(rule: DrcRule) -> None:
    """Reject a ``layer_selection`` this engine cannot apply (issues
    #2943/#3139): an unknown value, or one on a ``derived_layer`` rule (whose
    checked region is not the rule's own drawn ``layer``). Same "fail loudly
    with the rule id" contract as :func:`_validate_other_layer_selection`.
    """
    selection = rule.layer_selection
    if selection is None:
        return
    if selection not in _LAYER_SELECTIONS:
        raise DrcError(
            f"rule '{rule.id}': unknown layer_selection "
            f"'{selection}' (known: {', '.join(sorted(_LAYER_SELECTIONS))})"
        )
    if rule.derived_layer is not None:
        raise DrcError(
            f"rule '{rule.id}': layer_selection cannot be combined with derived_layer"
        )


def _validate_threshold_max(rule: DrcRule) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` that sets
    ``threshold_max_dbu`` on a check kind with no upper-bound branch, or
    whose upper bound sits below its own lower bound (issue #2370).

    Mirrors :func:`_validate_derived_layer`'s "a deck-authoring mistake
    fails loudly with the rule id, never silently checks less than the
    author asked for" contract: an upper bound attached to (say) a
    ``"space"`` rule would otherwise be silently ignored, leaving the deck
    author believing a half of their rule is enforced when nothing reads the
    field at all. Validated per rule at the top of ``run_drc``'s rule loop
    rather than inside :func:`_run_width_max_check`, so the mistake surfaces
    even on a stream whose layer for that rule is absent (where the check
    itself never runs).
    """
    if rule.threshold_max_dbu is None:
        return
    if rule.check not in _MAX_BOUND_CHECKS:
        raise DrcError(
            f"rule '{rule.id}': threshold_max_dbu is only supported for check "
            f"kinds {', '.join(sorted(_MAX_BOUND_CHECKS))} (got '{rule.check}')"
        )
    if rule.threshold_max_dbu < rule.threshold_dbu:
        raise DrcError(
            f"rule '{rule.id}': threshold_max_dbu ({rule.threshold_max_dbu}) is "
            f"below threshold_dbu ({rule.threshold_dbu}) -- no geometry can "
            "satisfy both bounds"
        )


def _run_width_max_check(region: Any, rule: DrcRule, dbu_scale: float) -> Any | None:
    """The *upper*-bound half of a ``check="width"`` rule that sets
    ``threshold_max_dbu`` (issue #2370): the merged polygons of ``region``
    whose bounding box exceeds the maximum in either dimension.

    Returns ``None`` for any rule that does not set ``threshold_max_dbu``
    (every rule predating this field), so a caller can treat it as an
    optional, purely additive term -- exactly like ``_run_check``'s
    ``outside_region`` escape term (#318).

    **Why a bounding-box bound and not an inverted** ``width_check``. A
    published fixed-size rule -- gf180mcu's ``CO.1`` ("min/max contact size
    -> 0.22um": every contact is a *fixed* 0.22 x 0.22 um square) and
    ``Vn.1`` (a fixed 0.26 x 0.26 um via) -- bounds the cut from both sides,
    but ``Region.width_check``, like every other ``Region`` check primitive,
    only ever reports the lower-bound half. There is no native upper-bound
    primitive to invert, and the obvious geometric substitute (shrink the
    region by half the maximum and flag whatever survives) measures the
    *facing-edge* width only: an over-long contact bar drawn 0.22 x 2 um has
    a perfectly legal 0.22 um facing-edge width, collapses to nothing when
    shrunk, and would go unreported -- yet it is precisely the geometry
    ``CO.1`` forbids.

    ``klayout.db.Region.with_bbox_max(0, max + 1, inverse=True)`` bounds the
    *larger* bounding-box dimension of each polygon instead (KLayout's
    ``with_*`` range selectors use a half-open ``[min, max)`` interval, hence
    the ``+ 1``: a cut whose bbox is exactly the threshold is legal and must
    stay clean). That flags an oversized square, an elongated bar, and an
    L-shaped cut alike. The bound is measured on the axis-aligned envelope,
    so a non-axis-aligned cut (e.g. a 45-degree-rotated square) is measured
    conservatively by its envelope rather than its true size -- acceptable
    for the fixed-size cut/via rules this exists for, whose official
    geometry is an axis-aligned square.

    ``region`` is merged first, for the same reason ``_run_check`` merges
    (#995): a cut drawn as several abutting shapes is one physical cut, and
    its size must be measured on the shape the fab sees. This cuts both ways
    here and deliberately so -- two abutting oversized cuts merge into one
    over-wide polygon that this term now reports, which is the geometry
    issue #546's own reproducer draws.

    Like ``_run_area_check``, this returns violating *polygons* (a
    ``Region``), not an ``EdgePairs`` collection, so ``run_drc`` reports each
    under the same rule ``id`` and the same ``check: "width"`` string as the
    minimum-width edge pairs it is additive to.
    """
    if rule.threshold_max_dbu is None:
        return None
    max_d = round(rule.threshold_max_dbu * dbu_scale)
    return region.merged().with_bbox_max(0, max_d + 1, True)


def _edge_pair_hull_points(edge_pair: Any) -> list[list[int]] | None:
    """``violations[].polygon`` for one ``EdgePair`` violation marker: the
    hull points of the quadrilateral spanning its two edges, or ``None`` when
    the marker has no renderable outline.

    Two ways a marker has none, both reported as ``null`` per the contract in
    ``docs/cli/drc.md``:

    - ``EdgePair.polygon(0)`` raises (a degenerate pair KLayout declines to
      turn into a polygon at all) -- the long-standing case;
    - it returns a polygon whose hull is *empty*, which is what a fully
      degenerate single-point marker yields -- ``"ongrid"``'s per-vertex
      markers are exactly that (issue #2642). Normalized to ``None`` here
      rather than emitted as an empty array, which would be a third,
      undocumented shape for this field.
    """
    try:
        points = [[pt.x, pt.y] for pt in edge_pair.polygon(0).each_point_hull()]
    except Exception:
        return None
    return points or None


def _run_vertex_check(region: Any, rule: DrcRule, dbu: float) -> Any:
    """Dispatch a :data:`_VERTEX_CHECKS` rule to its primitive (issue #2642).

    The single-call analogue of :func:`_run_check` for the vertex check
    kinds: ``"ongrid"`` -> :func:`_run_grid_check`, ``"angle"`` ->
    :func:`_run_angle_check`. Both return an ``EdgePairs`` collection, and
    neither has an ``outside_region``-style supplementary term, so this
    returns the collection directly rather than ``_run_check``'s tuple.

    ``dbu`` is the *layout's own* database unit in micrometres, which
    ``"ongrid"`` needs (its grid is a physical distance, not a count of
    deck-nominal units -- see :func:`_run_grid_check`); ``"angle"`` is
    dimensionless and ignores it.
    """
    if rule.check in _GRID_CHECKS:
        return _run_grid_check(region, rule, dbu)
    return _run_angle_check(region, rule)


def _run_grid_check(region: Any, rule: DrcRule, dbu: float) -> Any:
    """``check="ongrid"`` (issue #2642): the off-grid vertices of ``region``,
    as an ``EdgePairs`` collection of degenerate (single-point) edge pairs --
    driven by ``klayout.db.Region.grid_check(gx, gy)``, the exact primitive
    the open PDKs' own DRC-DSL ``layer.ongrid(g)`` compiles to (verified
    against KLayout's ``_drc_layer.rb``, whose ``ongrid`` is a straight
    ``grid_check(g, g)`` call).

    ``rule.grid_um`` is a **real physical** micrometre distance, converted
    here against the *layout's own* ``dbu`` -- not via ``run_drc``'s
    ``dbu_scale`` the way ``threshold_dbu`` is. A manufacturing grid is a
    property of the process, so 0.005um is 0.005um whether the stream was
    written at 1nm or 10nm per unit; routing it through ``dbu_scale`` would
    instead hold the grid at a fixed *number of database units* and silently
    change the physical grid with the stream's dbu. (Same reasoning, and the
    same conversion, as ``density_window_um`` and
    ``DerivedLayer.sized_by_um``.)

    The region is merged first, for the same reason every other check here
    merges (#995) and because the DRC-DSL ``ongrid`` applies merged semantics
    itself: a layer drawn as several abutting shapes is one physical polygon.
    Merging is safe for a grid check specifically -- a union's vertices are a
    subset of its inputs' vertices plus intersection points of their edges,
    and axis-parallel on-grid edges can only intersect on-grid -- so it can
    only drop the duplicate seam vertices, never invent an off-grid one.

    ``Region.grid_check`` takes a grid in whole database units, so the
    conversion must come out integral. Two outcomes reach here, because
    ``run_drc`` has already skipped (not run) every rule whose grid this
    stream cannot express -- see :func:`_grid_is_representable` and
    :data:`SKIP_REASON_GRID_NOT_REPRESENTABLE`:

    - ``grid_um`` is a whole number of database units (the ordinary case: a
      0.005um grid is 5 units at sky130's 1nm dbu, 1 unit at a 5nm one) --
      check it.
    - ``grid_um`` is *finer* than one database unit, but the database unit is
      itself a whole multiple of the grid (a 10nm-dbu stream against a 5nm
      grid): every representable coordinate is on-grid by construction, so
      the check is vacuously satisfied and this returns an empty
      ``EdgePairs``. Reporting nothing here is the true answer, not a
      silenced one.

    The final :class:`DrcError` is therefore a deck-authoring/programming
    guard rather than a stream condition: an ``"ongrid"`` rule with no
    ``grid_um`` at all, or a direct call that bypassed the skip gate. It
    mirrors ``_run_area_check``/``_run_antenna_check``'s own fail-loud
    validation.
    """
    if rule.grid_um is None:
        raise DrcError(f"rule '{rule.id}': check 'ongrid' requires grid_um")
    if not _grid_is_representable(rule, dbu):
        raise DrcError(
            f"rule '{rule.id}': grid_um {rule.grid_um} is not a whole number of "
            f"this layout's database units ({dbu} um), and the database unit is "
            f"not a whole multiple of the grid either, so no exact grid check "
            f"is possible (run_drc skips such a rule rather than reaching here)"
        )
    units_per_grid = rule.grid_um / dbu
    grid_dbu = round(units_per_grid)
    if grid_dbu >= 1:
        return region.merged().grid_check(grid_dbu, grid_dbu)
    # Grid finer than one database unit, with the unit a whole multiple of it:
    # every representable coordinate is on-grid, so there is nothing to report.
    # Lazily imported for the same reason `run_drc` imports it lazily.
    import klayout.db as kdb

    return kdb.EdgePairs()


def _run_angle_check(region: Any, rule: DrcRule) -> Any:
    """``check="angle"`` (issue #2642): the corners of ``region`` whose
    *interior* angle is strictly below ``rule.angle_limit_deg``, as an
    ``EdgePairs`` collection holding the two edges forming each offending
    corner -- driven by ``klayout.db.Region.with_angle(0.0, limit, False)``,
    which is what the open PDKs' own DRC-DSL ``layer.with_angle(0 .. limit)``
    evaluates on a polygon layer.

    The interval is half-open (``[0, limit)``), so a corner exactly at the
    limit is legal: ``angle_limit_deg=45.0`` passes a 45-degree chamfer and
    flags anything sharper; ``90.0`` passes a right angle and flags every
    acute corner. Verified empirically against KLayout 0.30: a 30-60-90
    triangle reports its 30-degree corner under a 45 limit and both its
    30- and 60-degree corners under a 90 limit, while a rectangle and a
    45-degree-chamfered octagon report nothing under either.

    **This is a minimum-angle bound, not an off-axis-edge detector** -- see
    :attr:`~klayout_tools.decks.rules.DrcRule.angle_limit_deg` for why a
    curated rule transcribed from an upstream "non 45 degree angle" rule
    description still only enforces the bound the upstream primitive
    actually enforces.

    Merged for the same reasons as every other check (#995; the DRC-DSL
    ``with_angle`` applies merged semantics too) -- the corners of a layer
    drawn as several abutting shapes are the corners of their union, not of
    whichever tiling the GDS author happened to write.

    An unset or out-of-range ``angle_limit_deg`` raises :class:`DrcError`
    naming the rule: ``0`` (or less) can never report anything, and nothing
    above 180 is a meaningful interior angle, so either is a deck-authoring
    mistake rather than a stricter check.
    """
    if rule.angle_limit_deg is None:
        raise DrcError(f"rule '{rule.id}': check 'angle' requires angle_limit_deg")
    if not 0.0 < rule.angle_limit_deg <= 180.0:
        raise DrcError(
            f"rule '{rule.id}': angle_limit_deg must be in (0, 180], "
            f"got {rule.angle_limit_deg}"
        )
    return region.merged().with_angle(0.0, rule.angle_limit_deg, False)


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


def _dispatch_region_rule(
    rule: DrcRule,
    layout: Any,
    top_cells: list[Any],
    layer_names: dict[tuple[int, int], str],
    violations: list[dict[str, Any]],
    rule_counts: dict[str, int],
    rules_checked: list[str],
    rules_skipped: list[str],
    empty_checked_region_ids: set[str],
) -> bool:
    """Run a :data:`_REGION_CHECKS` rule (issues #3587, #3607), recording its
    outcome in the caller's accumulators; ``False`` when ``rule`` is not one.

    A region rule resolves its own layers (named layers other than the
    checked one may be absent) and decides checked-vs-inapplicable itself --
    see :func:`_run_comp_coverage_check`. Only an absent *checked* layer or
    an empty checked region lands the rule in ``rules_skipped`` (the latter
    also in ``empty_checked_region_ids``).
    """
    if rule.check not in _REGION_CHECKS:
        return False
    if layout.find_layer(*rule.layer) is None:
        rules_skipped.append(rule.id)  # absent checked layer
        return True
    runner = (
        _run_tap_distance_check
        if rule.check in _TAP_DISTANCE_CHECKS
        else _run_comp_coverage_check
    )
    found = runner(rule, layout, top_cells, layer_names)
    if found is None:
        rules_skipped.append(rule.id)
        empty_checked_region_ids.add(rule.id)
        return True
    rules_checked.append(rule.id)
    violations.extend(found)
    if found:
        rule_counts[rule.id] = rule_counts.get(rule.id, 0) + len(found)
    return True


def _run_comp_coverage_check(
    rule: DrcRule,
    layout: Any,
    top_cells: list[Any],
    layer_names: dict[tuple[int, int], str],
) -> list[dict[str, Any]] | None:
    """``check="comp_coverage"`` (issue #3587): the rule's checked layer minus
    every ``region_spec.covered_by_any`` layer, with whole checked polygons
    exempted when they interact with an ``exempt_markers`` polygon -- gf180mcu
    ``DF.12`` (``comp.not_interacting(schottky_diode).not(nplus).not(pplus)``).

    Evaluated flat per top cell on merged regions. Every named layer other
    than the checked one is read as an *empty* region when absent from the
    stream and is never a reason to skip: an unimplanted COMP is the
    violation. One violation per residual polygon.

    Returns ``None`` when the checked region (the checked layer after the
    marker exemption) is empty in every top cell -- the rule constrains
    nothing here, so the caller records it as inapplicable
    (:data:`INAPPLICABLE_REASON_EMPTY_CHECKED_REGION`), not as checked.
    """
    spec = rule.region_spec
    if not isinstance(spec, CompCoverageSpec):
        raise DrcError(f"rule '{rule.id}': check '{rule.check}' requires region_spec")
    layer_label = layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")

    def merged(cell: Any, layer: tuple[int, int]) -> Any:
        return _cell_region_or_empty(cell, layout.find_layer(*layer)).merged()

    any_checked = False
    found: list[dict[str, Any]] = []
    for cell in top_cells:
        region = merged(cell, rule.layer)
        for marker in spec.exempt_markers:
            region = region.not_interacting(merged(cell, marker))
        if region.is_empty():
            continue
        any_checked = True
        for covering in spec.covered_by_any:
            region = region - merged(cell, covering)
        found.extend(_polygon_violations(rule, cell, region.merged(), layer_label))
    return found if any_checked else None


def _polygon_violations(
    rule: DrcRule, cell: Any, region: Any, layer_label: str
) -> list[dict[str, Any]]:
    """One violation record per polygon of ``region`` (bbox + polygon), the
    reporting shape shared by every :data:`_REGION_CHECKS` rule."""
    records: list[dict[str, Any]] = []
    for polygon in region.each():
        bbox = polygon.bbox()
        source_cell, source_path = _attribute_to_instance(cell, bbox)
        records.append(
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
                "polygon": [[pt.x, pt.y] for pt in polygon.each_point_hull()],
            }
        )
    return records


def _run_tap_distance_check(
    rule: DrcRule,
    layout: Any,
    top_cells: list[Any],
    layer_names: dict[tuple[int, int], str],
) -> list[dict[str, Any]] | None:
    """``check="tap_distance"`` (issue #3607): gf180mcu ``DF.13`` / ``DF.14``,
    a ``klayout.db.Region`` translation of the upstream operation sequence
    (``docs/design/gf180mcu-comp-checks-spike.md`` sections 3 and 7; ported
    from ``tests/helpers/gf180mcu_comp_spike.py``'s
    ``evaluate_port_upstream_clip``).

    Evaluated flat per top cell on merged regions. Every layer other than the
    checked ``comp`` is read as an *empty* region when absent, so zero taps
    with active present is a violation, never a skip. One violation per
    selected (whole) active polygon.

    Returns ``None`` when the voltage-selected active region is empty in every
    top cell -- the rule constrains nothing here (inapplicable,
    :data:`INAPPLICABLE_REASON_EMPTY_CHECKED_REGION`).
    """
    spec = rule.region_spec
    if not isinstance(spec, TapDistanceSpec) or rule.check not in _TAP_DISTANCE_CHECKS:
        raise DrcError(f"rule '{rule.id}': check '{rule.check}' requires region_spec")
    layer_label = layer_names.get(rule.layer, f"{rule.layer[0]}/{rule.layer[1]}")
    dbu = layout.dbu
    distance = round(spec.max_distance_um / dbu)
    step = round(spec.step_um / dbu)
    steps = round(spec.max_distance_um / spec.step_um)

    def merged(cell: Any, layer: tuple[int, int]) -> Any:
        return _cell_region_or_empty(cell, layout.find_layer(*layer)).merged()

    any_checked = False
    found: list[dict[str, Any]] = []
    for cell in top_cells:
        comp = merged(cell, rule.layer)
        nwell = merged(cell, spec.nwell)
        all_nwell = (merged(cell, spec.dnwell) - merged(cell, spec.lvpwell)) + nwell
        all_nwell = all_nwell.merged()
        ncomp = comp & merged(cell, spec.nplus)
        pcomp = comp & merged(cell, spec.pplus)
        res_mk = merged(cell, spec.res_mk)
        dualgate = merged(cell, spec.dualgate)
        if spec.well_domain == "inside":
            active = pcomp & all_nwell
            tap = (ncomp & all_nwell) - res_mk
        else:
            active = ncomp - all_nwell
            tap = (pcomp - all_nwell) - res_mk
        if spec.voltage == "lv":
            active = active.not_interacting(merged(cell, spec.v5_xtor))
            active = active.not_interacting(dualgate)
        else:
            active = active.overlapping(dualgate)
        if active.is_empty():
            continue
        any_checked = True
        if spec.well_domain == "inside":
            # Upstream clips every growth step with the *raw* `nwell`, not
            # `all_nwell` (spike discrepancy D2); reproduced faithfully.
            reach = tap
            for _ in range(steps):
                reach = reach.sized(step, 1).merged() & nwell
            violating = active.not_interacting(reach)
        else:
            candidates = active.not_interacting(tap.sized(distance, 0))
            good = candidates.separation_check(tap, distance).polygons()
            violating = candidates.not_interacting(good)
        found.extend(_polygon_violations(rule, cell, violating, layer_label))
    return found if any_checked else None


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


#: Names that moved to :mod:`klayout_tools.drc_klayout_engine` (issue #3704).
#: Re-exported lazily because that module imports this one at top level.
_ENGINE_REEXPORTS = (
    "KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S",
    "load_rule_category_manifest",
    "run_drc_klayout_engine",
)


def __getattr__(name: str) -> Any:
    if name in _ENGINE_REEXPORTS:
        from . import drc_klayout_engine

        return getattr(drc_klayout_engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
