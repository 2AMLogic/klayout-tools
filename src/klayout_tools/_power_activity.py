"""Shared ``constraints.power_activity`` validation / Tcl / response helpers.

Issue #3744 (``klt sta``) and issue #4914 (``klt place-and-route``) share one
implementation so both verbs validate and report power activity identically.
"""

from __future__ import annotations

import math
from typing import Any

POWER_ACTIVITY_KEYS = ("activity", "duty")


def validate_power_activity(
    constraints: dict[str, Any], error: type[Exception] = ValueError
) -> dict[str, float] | None:
    """Validate ``constraints.power_activity`` (issue #3744), returning the
    validated ``{"activity"?: float, "duty"?: float}`` mapping, or ``None``
    when omitted or ``null`` (no ``set_power_activity`` is emitted and the
    generated script is unchanged).

    The value must be a non-empty JSON object with only the keys
    ``activity`` (a finite number ``>= 0``) and ``duty`` (a finite number in
    ``[0, 1]``). Booleans, non-finite numbers, unknown keys and a non-object
    are request errors that name the offending field.
    """
    value = constraints.get("power_activity")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise error(
            "constraints.power_activity must be an object with 'activity' and/or 'duty'"
        )
    unknown = sorted(set(value) - set(POWER_ACTIVITY_KEYS))
    if unknown:
        raise error(
            "constraints.power_activity has unknown key(s): "
            + ", ".join(unknown)
            + " (allowed: activity, duty)"
        )
    if not value:
        raise error(
            "constraints.power_activity must give at least one of 'activity' and 'duty'"
        )
    out: dict[str, float] = {}
    for key in POWER_ACTIVITY_KEYS:
        if key not in value:
            continue
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
        ):
            raise error(f"constraints.power_activity.{key} must be a finite number")
        if number < 0 or (key == "duty" and number > 1):
            bound = ">= 0" if key == "activity" else "in [0, 1]"
            raise error(
                f"constraints.power_activity.{key} must be {bound}, got {number}"
            )
        out[key] = float(number)
    return out


def power_activity_lines(power_activity: dict[str, float] | None) -> list[str]:
    """The ``set_power_activity -global`` command (issue #3744) for a
    validated ``constraints.power_activity``; empty when none was requested,
    leaving OpenSTA's built-in default activity in force."""
    if not power_activity:
        return []
    flags = "".join(
        f" -{key} {power_activity[key]!r}"
        for key in POWER_ACTIVITY_KEYS
        if key in power_activity
    )
    return [f"set_power_activity -global{flags}"]


def power_basis_block(
    power_activity: dict[str, float] | None, has_spef: bool
) -> dict[str, Any]:
    """The ``power_basis`` response block (issue #3744): what
    ``estimated_power_mw`` was computed under. ``engine_default`` carries
    ``null`` activity/duty rather than a guessed numeric default; a
    ``power_activity`` with only one key leaves the other ``null``."""
    given = power_activity or {}
    return {
        "activity_source": "global" if power_activity else "engine_default",
        "activity": given.get("activity"),
        "duty": given.get("duty"),
        "parasitics": "spef_annotated" if has_spef else "estimate_only",
    }
