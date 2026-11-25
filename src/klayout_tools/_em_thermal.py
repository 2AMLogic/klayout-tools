"""Thermal requests and reports for Geode's supplied-mesh heat solvers."""

import json
from typing import Any

from ._geode import EmError
from ._jsonnum import is_finite_number

THERMAL_MODES = ("thermal", "self_heating")


def validate_thermal_request(request: dict[str, Any], mode: str) -> None:
    """Keep Geode's mode-specific settings together and delegate physics to Geode."""
    other = "self_heating" if mode == "thermal" else "thermal"
    conflicts = {
        "layout",
        "region",
        "stackup",
        "materials",
        "terminals",
        "ground",
        other,
    }
    unexpected = sorted(conflicts.intersection(request))
    if unexpected:
        raise EmError(
            f"{mode} expects a supplied mesh and a {mode} section, got {unexpected}"
        )
    if not isinstance(request.get(mode), dict) or not request[mode]:
        raise EmError(f"request field {mode!r} must be a non-empty settings object")
    if request.get("outputs", ["temperature"]) != ["temperature"]:
        raise EmError(f"unsupported outputs for {mode}: expected ['temperature']")
    if request.get("checks", []) != []:
        raise EmError(f"capacitance checks cannot be evaluated for {mode}")


def parse_thermal_report(text: str, mode: str) -> dict[str, Any]:
    """Expose temperatures only from a complete, converged Geode solution."""
    try:
        report = json.loads(text)
    except ValueError as exc:
        raise EmError(f"geode {mode} returned an invalid JSON report") from exc
    if (
        not isinstance(report, dict)
        or report.get("kind") != mode
        or report.get("status") != "ok"
    ):
        raise EmError(f"expected a successful geode {mode} report")
    result: dict[str, Any] = {}
    if mode == "self_heating":
        termination = report.get("termination")
        if termination != "converged":
            raise EmError(f"geode self-heating Picard termination: {termination!r}")
        if report.get("has_thermal_solution") is not True:
            raise EmError(
                "geode self-heating reported convergence without a thermal solution"
            )
        result["termination"] = termination
    mesh = report.get("mesh")
    count = mesh.get("n_nodes") if isinstance(mesh, dict) else None
    values = report.get("temperature_k")
    if (
        type(count) is not int
        or not isinstance(values, list)
        or not values
        or len(values) != count
    ):
        raise EmError("geode temperature_k must contain one temperature per mesh node")
    if not all(is_finite_number(value) for value in values):
        raise EmError("geode temperature_k contains a non-finite or non-numeric value")
    result.update(temperature_k=values, peak_temperature_k=max(values))
    for field in ("source_power_w", "boundary_power_w", "energy_rel_mismatch"):
        if not is_finite_number(report.get(field)):
            raise EmError(f"geode {mode} report needs a finite {field}")
        result[field] = report[field]
    return result
