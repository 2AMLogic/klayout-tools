"""Grade ordered ngspice steps and their measurement results."""

from __future__ import annotations

from typing import Any

from . import sim_step_evidence
from .sim_eye import EyeOutcome, is_eye
from .sim_step_evidence import step_outcomes


def measurement_provenance(spec: dict[str, Any]) -> dict[str, Any]:
    """The additive provenance fields a measurement result/rollup entry
    carries: ``step`` for a step-scoped measurement, ``derived_from`` for one
    computed from other results, ``case`` for one expanded from a ``{case}``
    template (issue #5731). Empty for every plain scalar-``analysis``
    measurement, so those report entries keep their exact shape."""
    fields: dict[str, Any] = {k: spec[k] for k in ("step", "case") if k in spec}
    if "derived_from" in spec:
        fields["derived_from"] = list(spec["derived_from"])
    return fields


# --------------------------------------------------------------------------- #
# Request resolution and validation
# --------------------------------------------------------------------------- #


def grade_step_measurements(
    measurement_values: dict[str, float],
    measurements_spec: list[dict[str, Any]],
    *,
    analysis: dict[str, Any],
    log_text: str,
    save_mode: str,
    eyes: dict[int, EyeOutcome] | None = None,
    engine_diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """One corner's step-sequence results: ``(measurement_results,
    diagnostics, extra_corner_fields)``.

    ``extra_corner_fields`` is the additive ``{"steps": [...]}`` per-step
    outcome list. Results are graded in flattened order (steps, then derived
    measurements), so every input a result depends on is decided first.
    ``engine_diagnostics`` (issue #7072) is the corner's diagnostics so far;
    an opted-in step ``.meas`` card grades ``not_reached`` only when none of
    them is error-severity, exactly like the scalar path (#3108)."""
    outcomes = step_outcomes(analysis, log_text)
    opted_in = any(spec.get("on_unreached") for spec in measurements_spec)
    unreached = (
        sim_step_evidence.step_unreached_locals(analysis, log_text) if opted_in else {}
    )
    unmet = _apply_arg_dependencies(
        analysis, measurements_spec, measurement_values, outcomes
    )
    diagnostics: list[dict[str, str]] = []
    for step in analysis["steps"]:
        if step["name"] in unmet:
            diagnostics.append(_input_unavailable_diagnostic(step, unmet[step["name"]]))
        elif not outcomes[step["name"]]:
            diagnostics.append(_step_failed_diagnostic(step, measurements_spec))
    results: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for position, spec in enumerate(measurements_spec):
        result, spec_diagnostics = _grade_one(
            spec,
            measurement_values,
            outcomes,
            by_name,
            save_mode=save_mode,
            eye=(eyes or {}).get(position),
            unreached_locals=unreached.get(spec.get("step") or "", set()),
            engine_diagnostics=engine_diagnostics,
        )
        by_name[spec["name"]] = result
        results.append(result)
        diagnostics.extend(spec_diagnostics)
    steps_field = [
        {
            "name": step["name"],
            "kind": step["kind"],
            "status": "ok" if outcomes[step["name"]] else "error",
        }
        for step in analysis["steps"]
    ]
    return results, diagnostics, {"steps": steps_field}


def _apply_arg_dependencies(
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    measurement_values: dict[str, float],
    outcomes: dict[str, bool],
) -> dict[str, list[str]]:
    """Issue #3055: a step whose sweep window reads an earlier result that
    produced no value is marked failed (in place in ``outcomes``) whatever
    ngspice made of the unresolved window, so it never reports a solve over
    a stale or garbage range. Returns ``{step: [unavailable inputs]}``."""
    specs = {spec["name"]: spec for spec in measurements_spec}
    unmet: dict[str, list[str]] = {}
    for step in analysis["steps"]:
        missing = [
            name
            for name in step.get("arg_inputs", [])
            if not outcomes.get(specs[name]["step"], False)
            or measurement_values.get(specs[name]["log_key"]) is None
        ]
        if missing:
            outcomes[step["name"]] = False
            unmet[step["name"]] = missing
    return unmet


def _input_unavailable_diagnostic(
    step: dict[str, Any], missing: list[str]
) -> dict[str, str]:
    return {
        "severity": "error",
        "code": "derived_input_unavailable",
        "message": (
            f"analysis step {step['name']!r} was not run against a derived "
            f"sweep window ({step['args']!r}): its input(s) "
            f"{', '.join(missing)} produced no value in this corner"
        ),
    }


def _step_failed_diagnostic(
    step: dict[str, Any], measurements_spec: list[dict[str, Any]]
) -> dict[str, str]:
    owned = [s["name"] for s in measurements_spec if s.get("step") == step["name"]]
    suffix = (
        f"; its measurements ({', '.join(owned)}) are reported without a value"
        if owned
        else ""
    )
    return {
        "severity": "error",
        "code": "step_failed",
        "message": (
            f"analysis step {step['name']!r} ({step['kind']} {step['args']}".rstrip()
            + ") produced no analysis plot -- the solve failed or never ran "
            "(see this corner's log)" + suffix
        ),
    }


def _grade_one(
    spec: dict[str, Any],
    measurement_values: dict[str, float],
    outcomes: dict[str, bool],
    by_name: dict[str, dict[str, Any]],
    *,
    save_mode: str,
    eye: EyeOutcome | None,
    unreached_locals: set[str],
    engine_diagnostics: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    from .sim import _eye_measurement_result, _grade_measurement_value

    base = {"name": spec["name"], "value": None, "unit": spec.get("unit")}
    errored = {
        **base,
        "status": "error",
        "margin": None,
        **measurement_provenance(spec),
    }
    step = spec.get("step")
    if step is not None and not outcomes[step]:
        # Reported by the step's own `step_failed` diagnostic; never read a
        # value from a step that produced no plot of its own.
        return errored, []
    if is_eye(spec):
        result, diagnostics = _eye_measurement_result(spec, eye, save_mode=save_mode)
        return {**result, **measurement_provenance(spec)}, diagnostics
    missing = [
        name
        for name in spec.get("derived_from", [])
        if by_name.get(name, {}).get("value") is None
    ]
    missing += [
        f"{name}.spectrum"
        for name in spec.get("spectrum_steps", [])
        if not outcomes[name]
    ]
    if missing:
        return errored, [
            {
                "severity": "error",
                "code": "derived_input_unavailable",
                "message": (
                    f"measurement {spec['name']!r} was not graded: its "
                    f"input(s) {', '.join(missing)} produced no value in this "
                    "corner"
                ),
            }
        ]
    value = measurement_values.get(spec["log_key"])
    if value is None:
        unreached = sim_step_evidence.not_reached_result(
            spec,
            unreached_locals=unreached_locals,
            engine_diagnostics=engine_diagnostics,
        )
        if unreached is not None:
            return unreached
        return errored, [
            {
                "severity": "error",
                "code": "measurement",
                "message": sim_step_evidence.no_value_message(spec, save_mode),
            }
        ]
    status, margin, grading_diagnostics = _grade_measurement_value(spec, value)
    result = {
        **base,
        "value": value,
        "status": status,
        "margin": margin,
        **measurement_provenance(spec),
    }
    return result, grading_diagnostics
