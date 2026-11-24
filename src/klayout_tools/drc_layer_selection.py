"""Layer-selection narrowing for ``klt drc``, and the coverage grading that
depends on it (issues #2688, #2943/#3139, #5435).

Split out of :mod:`klayout_tools.drc` to satisfy the per-file size ratchet,
the same way :mod:`klayout_tools.drc_check_kinds` was.

A ``DrcRule``'s ``layer_selection`` / ``other_layer_selection`` narrows the
rule to a whole-polygon sub-population of its own layer (sg13g2's ``cont_sq``:
square contacts only, never contact bars). Because such a narrowing can select
away *everything*, the same module decides applicability from it:
:func:`selection_empties_every_top_cell` grades a rule whose selected region is
empty in every top cell as inapplicable rather than checked, so a ``clean``
verdict never names a rule that measured zero polygons (#5435).
"""

from __future__ import annotations

from typing import Any

from .decks import DrcRule
from .drc_check_kinds import (
    _ANTENNA_CHECKS,
    _AREA_CHECKS,
    _DENSITY_CHECKS,
    _SINGLE_LAYER_CHECKS,
    _TWO_LAYER_CHECKS,
    _VERTEX_CHECKS,
)

# Check kinds for which an *empty primary region* -- the rule's own `layer`
# region after `rule.layer_selection` is applied (`select_primary_region`)
# -- provably reports no violation, so a rule whose selection selects away
# every polygon in every top cell measured nothing and is graded
# `inapplicable` rather than `checked` (issue #5435, see
# `selection_empties_every_top_cell`).
#
# Confirmed per kind against the primitive each one dispatches to, rather
# than assumed from "no geometry, no findings":
#
# - `_SINGLE_LAYER_CHECKS`: `Region.{width,space,notch,isolated}_check` on an
#   empty region returns empty `EdgePairs`. The additive upper-bound term a
#   `"width"` rule may carry (`_run_width_max_check`) is
#   `empty.merged().with_bbox_max(...)` -- also empty.
# - `_TWO_LAYER_CHECKS`: `empty.{separation,enclosing,enclosed,overlap}_check
#   (other, d)` measures facing edges of the primary region, of which there
#   are none; `enclosing_adjacent` derives its cuts from that same empty
#   edge-pair set. The `_OUTSIDE_CHECKS` escape term (#318) is
#   `other.interacting(empty) - empty` / `empty.interacting(other) - other`,
#   both empty -- *except* under `require_containment` on an `"enclosing"`
#   rule, where it drops the `.interacting()` pre-filter and becomes the
#   whole of `other_region` (the #2726 absent-conductor violation). That one
#   shape is excluded in `selection_empties_every_top_cell`, not here,
#   since it turns on a rule flag rather than the check kind.
# - `_AREA_CHECKS`: `empty.with_area(..., inverse=True)` selects from no
#   polygons, so a minimum-area rule reports nothing (it faults undersized
#   drawn shapes, never the absence of one).
# - `_DENSITY_CHECKS`: `_run_density_check` tiles `region.bbox()` and returns
#   no violations for an empty bbox -- a minimum-density rule included.
# - `_VERTEX_CHECKS`: `empty.merged().grid_check(...)` / `.with_angle(...)`
#   have no vertices and no edges to fault.
# - `_ANTENNA_CHECKS`: `_run_antenna_check` returns `None` as soon as the
#   primary region's area is zero (its absent-*protection*-layer fault needs
#   a non-empty primary region).
#
# Deliberately excluded: `_NET_SPACE_CHECKS`, whose `_run_net_space_check`
# rebuilds its own regions from the layout and never reads the selected
# `region` at all, so an empty selection says nothing about what it measured;
# and `_REGION_CHECKS`, which `_dispatch_region_rule` has already graded
# (with this same reason token) before the grading below is reached.
SELECTION_VACUOUS_CHECKS = (
    _SINGLE_LAYER_CHECKS
    | _TWO_LAYER_CHECKS
    | _AREA_CHECKS
    | _DENSITY_CHECKS
    | _VERTEX_CHECKS
    | _ANTENNA_CHECKS
)


def select_other_region(rule: DrcRule, other_region: Any) -> Any:
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


def select_primary_region(rule: DrcRule, region: Any) -> Any:
    """Apply ``rule.layer_selection`` to the rule's own ``layer`` region;
    returned unchanged when no selection is set.

    ``"squares"`` mirrors :func:`select_other_region`: the *merged* layer
    filtered by ``Region.squares()``, so a contact bar -- or a row of
    abutting legal contacts that merges into one strip -- is dropped
    (``cont_sq = cont.not(cont.non_squares)`` in the IHP runset) rather than
    measured as a single oversized contact.

    Because this can select away *everything*, it also decides coverage:
    :func:`selection_empties_every_top_cell` grades a rule whose selected
    region is empty in every top cell as ``inapplicable`` rather than
    ``checked`` (issue #5435), so a ``clean`` verdict never names a rule that
    measured zero polygons.
    """
    if rule.layer_selection == "squares":
        return region.merged().squares()
    return region


def cell_primary_region(rule: DrcRule, cell: Any, layer_index: int | None) -> Any:
    """``rule``'s own ``layer`` region in one top ``cell``, after
    :func:`select_primary_region`.

    The one place this region is built, shared by ``drc._cell_check_regions``
    (which measures it) and :func:`selection_empties_every_top_cell` (which
    grades coverage from it, issue #5435) so the two can never disagree about
    what a rule's selected input actually is. Only for a non-``derived_layer``
    rule; a derived region is built by ``drc._derived_region`` instead, and
    ``layer_selection`` cannot be combined with ``derived_layer``
    (``drc._validate_layer_selection``).

    ``layer_index`` is ``None`` for an absent conductor (#2726), which reads
    as an empty region rather than a skip.
    """
    import klayout.db as kdb

    return select_primary_region(
        rule,
        kdb.Region(cell.begin_shapes_rec(layer_index))
        if layer_index is not None
        else kdb.Region(),  # absent conductor (#2726)
    )


def selection_empties_every_top_cell(
    rule: DrcRule, layout: Any, top_cells: list[Any]
) -> bool:
    """Whether ``rule``'s ``layer_selection`` selects away *all* of its
    layer's geometry in *every* top cell, making the rule inapplicable rather
    than checked (issue #5435).

    Applicability was previously decided from layer *presence* alone, before
    a rule's own ``layer_selection`` narrowing was applied, so a rule
    selecting ``"squares"`` from a layer holding only contact bars graded
    ``checked`` -- a ``clean`` verdict naming a rule that measured zero
    polygons, with no coverage field a consumer could consult to notice. The
    selected region is graded with the same reason token #3587 already uses
    for a marker-exempted ``"comp_coverage"`` region
    (``drc.INAPPLICABLE_REASON_EMPTY_CHECKED_REGION``): the checked layer *is*
    drawn, but this rule's checked region is empty.

    Three guards keep this from widening into a verdict change:

    - no ``layer_selection``, no re-grading -- every other rule is graded
      exactly as before, from layer presence;
    - the check kind must be one where an empty primary region provably
      cannot report a violation (:data:`SELECTION_VACUOUS_CHECKS`), and an
      ``"enclosing"`` rule with ``require_containment`` is excluded on top of
      that: there, an empty primary region *is* the violation (every cut
      unlanded on its conductor, #2726);
    - a layout with no top cell at all is left to the existing grading, since
      "empty in every top cell" is vacuously true of no cells.

    The region is rebuilt here rather than threaded out of
    ``drc._check_rule_on_cell``; for the (currently two) rules carrying a
    ``layer_selection`` that is one extra pass over one layer's shapes, and it
    keeps the grading decision ahead of the first violation being recorded.
    """
    if rule.layer_selection is None:
        return False
    if rule.check not in SELECTION_VACUOUS_CHECKS:
        return False
    if rule.require_containment and rule.check == "enclosing":
        return False
    if not top_cells:
        return False
    layer_index = layout.find_layer(*rule.layer)
    return all(
        cell_primary_region(rule, cell, layer_index).is_empty() for cell in top_cells
    )
