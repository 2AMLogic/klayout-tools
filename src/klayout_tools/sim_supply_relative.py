"""Supply-relative voltage levels for ``klt sim`` (issue #3918).

A bench written for one supply cannot follow ``corners.supply_v`` for its
stimulus amplitude or its mid-rail measurement threshold if the level is a
literal volt. Wherever a level is already resolved in Python per corner
(``stimulus`` levels, ``measurements[].crossings.level``) it may instead be a
fraction of a named ``corners.supply_v`` key::

    {"frac_of": "vdd", "fraction": 0.5}

A plain number keeps its meaning. The fraction is resolved against the
corner's own supply value, so ``0.5`` of ``vdd`` is ``0.9 V`` at ``vdd=1.8``
and ``0.45 V`` at ``vdd=0.9``.

This module is pure: it validates the object and multiplies. It does not
rewrite ``.meas`` cards (ngspice requires a literal there).
"""

from __future__ import annotations

import math
from typing import Any

#: Keys the object form may carry.
FORM_KEYS = frozenset({"frac_of", "fraction"})


class SupplyRelativeError(ValueError):
    """A malformed or unresolvable ``{frac_of, fraction}`` level."""


def is_supply_relative(value: Any) -> bool:
    """Whether ``value`` is written in the ``{frac_of, fraction}`` form."""
    return isinstance(value, dict)


def validate(value: Any, field: str) -> None:
    """Shape-check a ``{frac_of, fraction}`` object (no supply needed)."""
    if not isinstance(value, dict):
        raise SupplyRelativeError(f"{field} must be an object (got {value!r})")
    unknown = sorted(set(value) - FORM_KEYS)
    if unknown:
        raise SupplyRelativeError(
            f"{field} has unknown key(s) {unknown}; allowed: {sorted(FORM_KEYS)}"
        )
    rail = value.get("frac_of")
    if not isinstance(rail, str) or not rail.strip():
        raise SupplyRelativeError(
            f"{field}.frac_of must name a corners.supply_v key (got {rail!r})"
        )
    fraction = value.get("fraction")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction)
        or fraction < 0
    ):
        raise SupplyRelativeError(
            f"{field}.fraction must be a finite number >= 0 (got {fraction!r})"
        )


def resolve(value: Any, field: str, supply_v: dict[str, Any] | None) -> float:
    """``fraction * supply_v[frac_of]`` volts, validated against the corner's
    supply map. Raises :class:`SupplyRelativeError` naming ``field``."""
    validate(value, field)
    rail = value["frac_of"].strip()
    supply = supply_v or {}
    if not supply:
        raise SupplyRelativeError(
            f"{field} is supply-relative (frac_of {rail!r}) but the request "
            "declares no corners.supply_v for this corner"
        )
    match = next((key for key in supply if str(key).strip() == rail), None)
    if match is None:
        match = next(
            (key for key in supply if str(key).strip().lower() == rail.lower()), None
        )
    if match is None:
        raise SupplyRelativeError(
            f"{field}.frac_of {rail!r} is not a corners.supply_v key "
            f"(keys: {sorted(str(key) for key in supply)})"
        )
    return float(value["fraction"]) * float(supply[match])
