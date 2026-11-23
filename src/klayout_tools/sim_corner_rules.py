"""Corner selectors shared by nodesets, initial conditions, and analysis overrides."""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_finite_number
from .sim_corner_axes import _matches_exclude
from .sim_corner_point import CornerPoint, _validate_param_name, _validate_param_scalar
from .sim_errors import SimError

_NODESET_MATCH_KEYS = ("process", "temperature_c", "supply_v", "params")


def _nodeset_match_process(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise SimError(f"{where} must be a string")
    return value


def _nodeset_match_number(value: Any, where: str) -> Any:
    if not is_finite_number(value):
        raise SimError(f"{where} must be a finite number")
    return value


def _nodeset_match_supply(supply: Any, where: str) -> dict[str, Any]:
    if not isinstance(supply, dict):
        raise SimError(f"{where} must be an object of rail: number")
    return {
        rail: _nodeset_match_number(v, f"{where}.{rail}") for rail, v in supply.items()
    }


def _nodeset_match_params(params: Any, where: str) -> dict[str, int | float]:
    if not isinstance(params, dict):
        raise SimError(f"{where} must be an object of name: number")
    row: dict[str, int | float] = {}
    for name, value in params.items():
        lowered = _validate_param_name(name, where)
        if lowered in row:
            raise SimError(
                f"{where} names {lowered!r} more than once (names are case-insensitive)"
            )
        row[lowered] = _validate_param_scalar(value, f"{where}.{name}")
    return row


_NODESET_MATCH_VALIDATORS = {
    "process": _nodeset_match_process,
    "temperature_c": _nodeset_match_number,
    "supply_v": _nodeset_match_supply,
    "params": _nodeset_match_params,
}


def _validate_nodeset_match(raw: Any, where: str) -> dict[str, Any]:
    """One rule's ``match`` selector, normalized to the shape
    :func:`_matches_exclude` consumes (``params`` names lowercased)."""
    if not isinstance(raw, dict):
        raise SimError(f"{where} must be an object")
    unknown = sorted(str(key) for key in set(raw) - set(_NODESET_MATCH_KEYS))
    if unknown:
        raise SimError(
            f"{where} has unknown key(s) {', '.join(unknown)} "
            f"(supported: {', '.join(_NODESET_MATCH_KEYS)})"
        )
    return {
        key: _NODESET_MATCH_VALIDATORS[key](value, f"{where}.{key}")
        for key, value in raw.items()
    }


def _check_nodeset_selector_names(
    points: list[CornerPoint],
    rules: list[dict[str, Any]],
    label: str = "options.nodeset",
) -> None:
    """A selector naming a parameter or supply rail no point carries could
    never match, so it is refused by name rather than as an unused rule."""
    declared_params = {name for point in points for name in point.params}
    declared_rails = {str(rail) for point in points for rail in point.supply_v}
    for index, rule in enumerate(rules):
        match = rule["match"]
        for name in match.get("params", {}):
            if name not in declared_params:
                raise SimError(
                    f"{label}[{index}].match.params names {name!r}, "
                    "which is not a corners.params parameter of this request"
                )
        for rail in match.get("supply_v", {}):
            if str(rail) not in declared_rails:
                raise SimError(
                    f"{label}[{index}].match.supply_v names rail "
                    f"{rail!r}, which no corner of this request sets"
                )


def _apply_nodeset_rules(
    points: list[CornerPoint],
    rules: list[dict[str, Any]],
    *,
    label: str = "options.nodeset",
    attr: str = "nodeset",
) -> None:
    """Resolve ``options.nodeset`` rules onto ``points`` in place (issue
    #3036). Runs once, after corner expansion and exclusions and before Monte
    Carlo cloning, so a sample inherits its base point's map.

    Zero matching rules at a point mean no hint there; exactly one selects its
    whole map; two is an error whatever their node maps are (no implicit merge
    or priority). A rule that matches no retained point is an error, and so is
    a selector naming a parameter or supply rail no point carries.
    """
    if not rules:
        return
    _check_nodeset_selector_names(points, rules, label)
    used = [False] * len(rules)
    for point in points:
        hits = [
            i for i, rule in enumerate(rules) if _matches_exclude(point, rule["match"])
        ]
        if len(hits) > 1:
            raise SimError(
                f"{label} rules {hits[0]} and {hits[1]} both match "
                f"corner {point.corner_id!r}; overlapping rules are never "
                "merged -- make the selectors disjoint"
            )
        if hits:
            used[hits[0]] = True
            chosen = rules[hits[0]]["values"]
            setattr(point, attr, dict(chosen) if isinstance(chosen, dict) else chosen)
    if not all(used):
        raise SimError(
            f"{label}[{used.index(False)}] matches no retained corner "
            "of this request (every declared rule must apply somewhere)"
        )
