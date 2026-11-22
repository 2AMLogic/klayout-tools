"""Pitch-override helpers shared by the gen layer (issues #3299, #5764).

``_clamp_pitch_override`` resolves a user pitch override against its legal
minimum; ``_pitch_hints_and_warnings`` reports the effective pitch (and any
clamp) in ``drc_hints.pitch``, and only once an override was requested, so
default reports are unchanged. Leaf module: imports nothing from the gen
package, so ``gen.py``/``gen_describe.py`` may import it at module level
without a cycle.
"""

from __future__ import annotations

from typing import Any

# Per axis: (override param, effective pitch, legal minimum, clamp flag).
_PITCH_AXIS_FIELDS: dict[str, tuple[str, str, str, str]] = {
    "x": ("pitch_x_um", "col_pitch_um", "min_col_pitch_um", "pitch_x_clamped"),
    "y": ("pitch_y_um", "row_pitch_um", "min_row_pitch_um", "pitch_y_clamped"),
}

# Report order: effective pitches, then legal minimums, then clamp flags --
# identical to the pre-#5764 mos_array layout.
_PITCH_HINT_ORDER = (
    "col_pitch_um",
    "row_pitch_um",
    "min_col_pitch_um",
    "min_row_pitch_um",
    "pitch_x_clamped",
    "pitch_y_clamped",
)


def _clamp_pitch_override(override_um: float, minimum_um: float) -> tuple[float, bool]:
    """Resolve a pitch override against its legal minimum (issue #3299).

    Returns ``(effective_pitch_um, clamped)``: ``0.0`` means "no override"
    (the minimum, not clamped); a positive value below ``minimum_um`` is
    clamped up to it and reported as clamped."""
    if override_um <= 0.0:
        return minimum_um, False
    return max(override_um, minimum_um), override_um < minimum_um


def _pitch_hints_and_warnings(
    params: dict[str, Any],
    info: dict[str, Any],
    axes: tuple[str, ...] = ("x", "y"),
) -> tuple[dict[str, Any], list[str]]:
    """Only when a pitch override was requested, report the effective pitch
    and whether it was clamped up to the legal minimum (empty otherwise, so
    default reports are unchanged). ``axes`` selects the reported axes --
    diff_pair (issue #5764) passes ``("x",)``: its row spacing is the
    pre-existing ``row_spacing_um`` param, so it has no ``pitch_y_um``."""
    active = [a for a in axes if params[_PITCH_AXIS_FIELDS[a][0]] > 0]
    if not active:
        return {}, []
    reported = {key for a in active for key in _PITCH_AXIS_FIELDS[a][1:]}
    hints = {"pitch": {key: info[key] for key in _PITCH_HINT_ORDER if key in reported}}
    warnings = []
    for axis in active:
        if info[_PITCH_AXIS_FIELDS[axis][3]]:
            name = _PITCH_AXIS_FIELDS[axis][0]
            minimum = info[_PITCH_AXIS_FIELDS[axis][2]]
            warnings.append(
                f"params.{name}={params[name]} is below the legal minimum "
                f"pitch {minimum}um; clamped up to {minimum}um"
            )
    return hints, warnings
