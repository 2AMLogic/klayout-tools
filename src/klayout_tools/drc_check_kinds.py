"""Check-kind groupings and rule-threshold reporting for ``klt drc`` (issue #5059).

Split out of :mod:`klayout_tools.drc` to satisfy the per-file size ratchet.
"""

from __future__ import annotations

from typing import Any

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

# Check kinds that do not read ``DrcRule.threshold_dbu`` as a distance:
# ``--list-rules`` reports ``threshold_um: null`` for these rather than the
# placeholder value the field may carry.
_NON_DISTANCE_CHECKS = (
    _AREA_CHECKS
    | _DENSITY_CHECKS
    | _ANTENNA_CHECKS
    | _VERTEX_CHECKS
    | _COMP_COVERAGE_CHECKS
    | _TAP_DISTANCE_CHECKS
)


def _dbu_to_um(value_dbu: int | None, dbu_um: float) -> float | None:
    """Convert a dbu count to um, rounded to avoid float noise."""
    return None if value_dbu is None else round(value_dbu * dbu_um, 9)


def rule_threshold_fields(rule: Any, nominal_dbu_um: float) -> dict[str, float | None]:
    """``threshold_um`` / ``threshold_max_um`` entries for ``--list-rules``."""
    if rule.check in _NON_DISTANCE_CHECKS:
        return {"threshold_um": None, "threshold_max_um": None}
    return {
        "threshold_um": _dbu_to_um(rule.threshold_dbu, nominal_dbu_um),
        "threshold_max_um": _dbu_to_um(rule.threshold_max_dbu, nominal_dbu_um),
    }
