"""Expand an ngspice transient expression reduction into an ordinary .meas card."""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError
from .sim_forms import other_forms, validate_exclusive_form
from .sim_measurement_kind import require_analysis_kind
from .sim_window import window_bound

_KEYS = {"reduce", "of", "from", "to"}
_REDUCTIONS = ("min", "max", "avg", "rms", "pp")


def is_windowed(spec: dict[str, Any]) -> bool:
    return spec.get("windowed") is not None


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    return validate_exclusive_form(
        spec, "windowed", other_forms("windowed"), _validate_body
    )


def _declared(measurements: Any) -> list[dict[str, Any]]:
    if not isinstance(measurements, list):
        return []
    return [
        spec for spec in measurements if isinstance(spec, dict) and is_windowed(spec)
    ]


def _validate_body(name: Any, spec: dict[str, Any]) -> None:
    label = f"measurement '{name}': windowed"
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SimError(f"{label} requires a measurement identifier")
    body = spec["windowed"]
    if not isinstance(body, dict):
        raise SimError(f"{label} must be an object")
    unknown = body.keys() - _KEYS
    if unknown:
        raise SimError(f"{label} has unknown key '{min(unknown)}'")
    missing = _KEYS - body.keys()
    if missing:
        raise SimError(f"{label} requires '{min(missing)}'")
    reduction = body["reduce"]
    if reduction not in _REDUCTIONS:
        raise SimError(f"{label}.reduce must be one of {', '.join(_REDUCTIONS)}")
    expr = body["of"]
    if (
        not isinstance(expr, str)
        or not expr.strip()
        or re.search(r"""['"{}\r\n\x00]""", expr)
    ):
        raise SimError(
            f"{label}.of requires a single-line expression without quotes or braces"
        )
    start = window_bound(name, body, "from", form="windowed")
    end = window_bound(name, body, "to", form="windowed")
    if start is None or end is None or start >= end:
        raise SimError(f"{label} requires from < to")


def _make_card(spec: dict[str, Any]) -> str:
    validate_form_exclusive(spec)
    body = spec["windowed"]
    expression = body["of"].strip()
    return (
        f".meas tran {spec['name']} {body['reduce'].upper()} par('{expression}') "
        f"FROM={str(body['from']).strip()} TO={str(body['to']).strip()}"
    )


def _step_measurements(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    return [
        spec
        for step in steps
        if isinstance(step, dict)
        for spec in _declared(step.get("measurements"))
    ]


def expand_windowed(request: dict[str, Any]) -> dict[str, Any]:
    """Keep untouched requests by identity. Only send plain cards to workers."""
    entries = _declared(request.get("measurements"))
    steps = request.get("analysis_steps")
    if steps is not None:
        unsupported = entries or _step_measurements(steps)
        if unsupported:
            raise SimError(
                f"measurement '{unsupported[0].get('name')}': "
                "windowed is not supported with analysis_steps"
            )
    if not entries:
        return request
    if request.get("engine") == "xyce":
        raise SimError(
            f"measurement '{entries[0].get('name')}': windowed requires ngspice"
        )
    measurements = []
    for spec in request["measurements"]:
        if isinstance(spec, dict) and is_windowed(spec):
            card = _make_card(spec)
            spec = {key: value for key, value in spec.items() if key != "windowed"}
            spec["spice"] = card
        measurements.append(spec)
    analysis = request.get("analysis")
    require_analysis_kind(
        entries,
        analysis if isinstance(analysis, dict) else {},
        form="windowed",
        required="tran",
        explanation="reduces a transient expression",
    )
    return {**request, "measurements": measurements}
