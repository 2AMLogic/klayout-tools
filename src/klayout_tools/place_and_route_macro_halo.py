"""Per-side margin validation for ``request.macros[].halo`` (issue #2635) and
``request.macros[].keepout`` (issue #5313).

Both fields share one shape -- a bare number (uniform, um) or an object naming
any subset of :data:`MACRO_HALO_SIDES` -- so one validator serves both; it
lives here, error-class-injected like :mod:`.macros`, to keep
:mod:`.place_and_route_request` from growing.
"""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_number

MACRO_HALO_SIDES = ("left", "bottom", "right", "top")


def validate_macro_margin(
    value: Any, *, label: str, error_cls: type[Exception], field: str = "halo"
) -> dict[str, float] | None:
    """Validate one ``request.macros[].<field>`` value, returning
    ``{"left": f, "bottom": f, "right": f, "top": f}`` or ``None`` when the
    field is omitted.

    Accepts a bare number (uniform on all four sides -- the overwhelmingly
    common case, and the shape every ORFS platform config uses) or an object
    naming any subset of :data:`MACRO_HALO_SIDES`, each side defaulting to
    ``0``. An empty object is rejected rather than silently treated as
    "omitted": a caller who wrote ``"halo": {}`` meant something, and
    guessing which is worse than asking.

    Negative values are rejected -- a "negative halo" would mean letting
    standard cells *overlap* the macro, which no caller means and which
    ``create_blockage`` has no way to express anyway.
    """
    if value is None:
        return None
    sides = ", ".join(MACRO_HALO_SIDES)
    name = f"{label}.{field}"
    if isinstance(value, bool):
        raise error_cls(f"{name} must be a number or an object naming any of: {sides}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise error_cls(f"{name} must not be negative")
        return dict.fromkeys(MACRO_HALO_SIDES, float(value))
    if not isinstance(value, dict):
        raise error_cls(
            f"{name} must be a number (uniform, in um) or an object naming any of: "
            f"{sides}"
        )
    return _validate_per_side(value, name=name, error_cls=error_cls)


def _validate_per_side(
    value: dict[str, Any], *, name: str, error_cls: type[Exception]
) -> dict[str, float]:
    """The object half of :func:`validate_macro_margin`: any subset of
    :data:`MACRO_HALO_SIDES`, each unnamed side defaulting to ``0``."""
    sides = ", ".join(MACRO_HALO_SIDES)
    if not value:
        raise error_cls(f"{name} must name at least one of: {sides}")
    unknown = sorted(set(value) - set(MACRO_HALO_SIDES))
    if unknown:
        raise error_cls(
            f"{name} has unknown side(s) {', '.join(unknown)} -- "
            f"valid sides are: {sides}"
        )
    resolved: dict[str, float] = {}
    for side in MACRO_HALO_SIDES:
        side_value = value.get(side, 0)
        if not is_number(side_value):
            raise error_cls(f"{name}.{side} must be a number")
        if side_value < 0:
            raise error_cls(f"{name}.{side} must not be negative")
        resolved[side] = float(side_value)
    return resolved


def merge_macro_margins(
    halo: dict[str, float] | None, keepout: dict[str, float] | None
) -> dict[str, float] | None:
    """The ``create_blockage`` margin: per-side ``max(halo, keepout)``, or
    ``None`` when neither is declared (issue #5313). The PDN grid keeps
    ``halo`` alone."""
    if halo is None and keepout is None:
        return None
    return {
        side: max((halo or {}).get(side, 0.0), (keepout or {}).get(side, 0.0))
        for side in MACRO_HALO_SIDES
    }
