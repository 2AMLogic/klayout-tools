"""Per-corner measurement-result assembly for :mod:`klayout_tools.sim`.

Split out of ``sim.py`` (issue #4936, continuing #3689 / #3714): the
result-dict assembly that turns one corner's harvested values and sidecar
outcomes into ``measurement_results`` entries. Behaviour-preserving move; the
JSON contract is untouched.

The grading and log-parsing helpers it shares with the rest of the runner still
live in :mod:`klayout_tools.sim`, which imports this module at load time. To
avoid a circular import they are reached through a function-local
``from . import sim as _sim`` rather than a module-level import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import (
    sim_crossings,
    sim_cycle_mean,
    sim_energy,
    sim_eye,
    sim_search,
    sim_settled,
    sim_slope_band_meas,
    sim_snm_meas,
    sim_step_resolution,
    sim_steps,
    sim_stress,
    sim_tian,
    sim_transfer_meas,
)

#: The ``code`` of the info diagnostic an ``on_unreached: "null"`` measurement
#: whose target was never reached carries (its status is
#: ``sim.NOT_REACHED_STATUS``).
NOT_REACHED_DIAGNOSTIC_CODE = "measurement_not_reached"

#: The marker diagnostic :func:`_margin_measurement_result` attaches to a
#: ``transfer`` margin measurement (issue #3088) whose limit was met but whose
#: AC band cannot support a pass -- so the corner grades
#: ``sim._INCONCLUSIVE_STATUS`` and the message lists the
#: ``MarginReport.ambiguity`` reasons.
MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE = "margin_ambiguous"


def _corner_measurement_results(
    measurement_values: dict[str, float],
    measurements_spec: list[dict[str, Any]],
    *,
    analysis: dict[str, Any],
    log_text: str,
    is_xyce: bool,
    save_mode: str,
    crossings: dict[int, sim_crossings.CrossingsOutcome] | None = None,
    energy: dict[int, sim_energy.EnergyOutcome] | None = None,
    stress: dict[int, sim_stress.StressOutcome] | None = None,
    cycle_means: dict[int, sim_cycle_mean.CycleMeanOutcome] | None = None,
    transfers: dict[int, sim_transfer_meas.TransferOutcome] | None = None,
    snms: dict[int, sim_snm_meas.SnmOutcome] | None = None,
    slope_bands: dict[int, sim_slope_band_meas.SlopeBandOutcome] | None = None,
    eyes: dict[int, sim_eye.EyeOutcome] | None = None,
    step_resolutions: (
        dict[int, sim_step_resolution.StepResolutionOutcome] | None
    ) = None,
    settleds: dict[int, sim_settled.SettledOutcome] | None = None,
    searches: dict[int, sim_search.SearchOutcome] | None = None,
    tians: dict[int, sim_tian.TianOutcome] | None = None,
    engine_diagnostics: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """One corner's ``(measurement_results, diagnostics, extra_corner_fields)``
    from its harvested log values.

    ``crossings`` (issue #2868) maps a ``measurements_spec`` position to that
    ``crossings`` entry's extracted series (see
    :func:`klayout_tools.sim_crossings.extract_corner_crossings`); such an
    entry is graded by :func:`_crossings_measurement_result` instead of being
    looked up in the log. ``energy`` (issue #3033) does the same for ``energy``
    entries, graded by :func:`_energy_measurement_result`.

    An ``analysis_steps[]`` corner (issue #2482) is graded by
    :func:`klayout_tools.sim_steps.grade_step_measurements`, which adds the
    per-step outcome list and the step/derived failure propagation. Every
    other corner takes the original single-analysis path below, unchanged:
    one result per spec in declared order, a ``measurement`` diagnostic for
    each one that produced no value, and ``extra_corner_fields`` empty.

    ``engine_diagnostics`` (issue #3108) are the corner's diagnostics so far
    (solver/timeout classification); a ``spice`` measurement declaring
    ``on_unreached: "null"`` is graded :data:`NOT_REACHED_STATUS` instead of
    ``error`` only when none of them is error-severity. Not applied to an
    ``analysis_steps[]`` corner.
    """
    from . import sim as _sim

    if sim_steps.is_steps_analysis(analysis):
        return sim_steps.grade_step_measurements(
            measurement_values,
            measurements_spec,
            analysis=analysis,
            log_text=log_text,
            save_mode=save_mode,
        )
    measurement_results: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    meas_errors = {} if is_xyce else _sim._parse_meas_errors(log_text)
    ac_too_few_points = (
        not is_xyce
        and analysis.get("kind") == "ac"
        and _sim._ac_sweep_too_few_points(log_text)
    )
    unreached_names = set() if is_xyce else _sim._parse_meas_unreached_names(log_text)
    attributed: set[str] = set()
    # Forms whose value comes from a rawfile reduction rather than the log:
    # (spec predicate, response-entry builder, position -> outcome map).
    sidecar_forms: tuple[
        tuple[
            Callable[[dict[str, Any]], bool],
            Callable[..., tuple[dict[str, Any], list[dict[str, Any]]]],
            dict[int, Any] | None,
        ],
        ...,
    ] = (
        (sim_crossings.is_crossings, _sim._crossings_measurement_result, crossings),
        (sim_energy.is_energy, _sim._energy_measurement_result, energy),
        (sim_stress.is_stress, _sim._stress_measurement_result, stress),
        (
            sim_cycle_mean.is_cycle_mean,
            _sim._cycle_mean_measurement_result,
            cycle_means,
        ),
        (sim_transfer_meas.is_transfer, _transfer_measurement_result, transfers),
        (sim_tian.is_tian, _sim._tian_measurement_result, tians),
        (sim_snm_meas.is_snm, sim_snm_meas.snm_measurement_result, snms),
        (
            sim_slope_band_meas.is_slope_band,
            sim_slope_band_meas.slope_band_measurement_result,
            slope_bands,
        ),
        (sim_eye.is_eye, _sim._eye_measurement_result, eyes),
        (
            sim_step_resolution.is_step_resolution,
            _sim._step_resolution_measurement_result,
            step_resolutions,
        ),
        (sim_settled.is_settled, _sim._settled_measurement_result, settleds),
        (sim_search.is_search, _search_measurement_result, searches),
    )
    for position, spec in enumerate(measurements_spec):
        sidecar = next(
            (
                (render, outcomes)
                for is_form, render, outcomes in sidecar_forms
                if is_form(spec)
            ),
            None,
        )
        if sidecar is not None:
            render, outcomes = sidecar
            result, extra = render(
                spec, (outcomes or {}).get(position), save_mode=save_mode
            )
            measurement_results.append(result)
            diagnostics.extend(extra)
            continue
        name = spec["name"]
        value = _sim._harvest_measurement_value(
            measurement_values, spec, is_xyce=is_xyce
        )
        unit = spec.get("unit")
        if value is None and _sim._is_unreached(
            spec,
            unreached_names=unreached_names,
            diagnostics=engine_diagnostics or [],
        ):
            attributed.add(str(name).lower())
            measurement_results.append(
                {
                    "name": name,
                    "value": None,
                    "unit": unit,
                    "status": _sim.NOT_REACHED_STATUS,
                    "margin": None,
                }
            )
            diagnostics.append(
                {
                    "severity": "info",
                    "code": NOT_REACHED_DIAGNOSTIC_CODE,
                    "message": f"measurement '{name}' target not reached: the "
                    "analysis completed but ngspice's .meas never satisfied "
                    f"its condition ({spec['spice'].strip()})",
                }
            )
            continue
        if value is None:
            result, diagnostic, was_attributed = _sim._no_value_measurement(
                spec,
                meas_errors,
                save_mode=save_mode,
                too_few_points=ac_too_few_points,
            )
            measurement_results.append(result)
            diagnostics.append(diagnostic)
            if was_attributed:
                attributed.add(str(name).lower())
            continue
        # `_grade_measurement_value` owns the plausibility-pre-check ->
        # `limits` ordering (issue #2493) in one place, so there is no
        # second grading path where the pre-check could be skipped.
        m_status, margin, grading_diagnostics = _sim._grade_measurement_value(
            spec, value
        )
        measurement_results.append(
            {
                "name": name,
                "value": value,
                "unit": unit,
                "status": m_status,
                "margin": margin,
            }
        )
        diagnostics.extend(grading_diagnostics)
    # Corner-level fallback (issue #2826): engine `.meas` errors that could
    # not be tied to a missing result by name are still surfaced, as a
    # warning so grading is unchanged, but only when something is missing.
    if diagnostics and any(d["code"] == "measurement" for d in diagnostics):
        unattributed = [
            f"{n}: {t}" for n, t in meas_errors.items() if n not in attributed
        ]
        if unattributed:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "measurement",
                    "message": "ngspice reported .meas errors not matched to a "
                    "missing measurement: "
                    + "; ".join(unattributed[: _sim._MEAS_ERROR_MAX_ENTRIES]),
                }
            )
    return measurement_results, diagnostics, {}


def _transfer_measurement_result(
    spec: dict[str, Any],
    outcome: sim_transfer_meas.TransferOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``transfer`` measurement's response entry (issue #2923).

    ``value`` is the metric, graded through :func:`_grade_measurement_value`
    like any scalar; the request echo rides the additive ``transfer`` sibling.
    A failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from
    :data:`klayout_tools.sim_transfer_meas.ERROR_CODES`.

    The all-crossing margin tokens (issue #3088) are graded by
    :func:`_margin_measurement_result`."""
    from . import sim as _sim

    name = spec["name"]
    unit = spec.get("unit")
    if outcome is not None and outcome.margins is not None:
        return _margin_measurement_result(spec, outcome)
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no transfer value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "transfer_vector_missing"
        )
        if save_mode == "netlist" and reason == "transfer_vector_missing":
            message += _sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _sim._grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "transfer": outcome.report(spec),
    }
    return result, grading_diagnostics


def _search_measurement_result(
    spec: dict[str, Any],
    outcome: sim_search.SearchOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``search`` measurement's response entry (issue #4121).

    ``value`` is the midpoint of the converged bracket, graded through
    :func:`_grade_measurement_value` like any scalar; the additive ``search``
    sibling carries ``{source, direction, iterations, lo, hi, converged}``. A
    failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from
    :data:`klayout_tools.sim_search.ERROR_CODES`."""
    from . import sim as _sim

    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no search value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "search_no_result"
        )
        if save_mode == "netlist" and reason == "search_probe_failed":
            message += _sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
        result: dict[str, Any] = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        if outcome is not None and outcome.iterations is not None:
            result["search"] = outcome.report(spec)
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _sim._grade_measurement_value(
        spec, outcome.value
    )
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "search": outcome.report(spec),
    }
    return result, grading_diagnostics


def _margin_measurement_result(
    spec: dict[str, Any], outcome: sim_transfer_meas.TransferOutcome
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A ``transfer`` margin-metric entry (issue #3088).

    ``value`` is graded through :func:`_grade_measurement_value` like any
    scalar, then the AC band's verdict is applied: a limit that is *met* on a
    band that cannot support a pass is :data:`_INCONCLUSIVE_STATUS` with a
    :data:`MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE` warning -- never ``pass`` -- and a
    violation is a sound ``fail`` only when ``grade_margins`` found it among
    the crossings actually seen (a hidden crossing can only lower a minimum).
    A metric with no crossing at all is vacuously ``pass`` on a clean band.
    Without ``limits`` the entry is report-only and the band never downgrades
    it; a valueless metric is then an ordinary ``transfer_metric_unavailable``
    error."""
    from . import sim as _sim

    assert outcome.margins is not None
    name = spec["name"]
    metric = spec["transfer"]["metric"]
    limits = spec.get("limits") or {}
    has_limits = limits.get("min") is not None or limits.get("max") is not None
    ambiguity = outcome.margins.ambiguity
    diagnostics: list[dict[str, Any]] = []
    status: str
    margin: float | None = None
    downgrade = False
    if outcome.value is None:
        if has_limits and sim_transfer_meas.is_vacuous(outcome, spec):
            status = "pass"
        elif has_limits and ambiguity:
            status, downgrade = _sim._INCONCLUSIVE_STATUS, True
        else:
            reason = sim_transfer_meas.unavailable_reason(outcome, metric)
            return (
                {
                    "name": name,
                    "value": None,
                    "unit": spec.get("unit"),
                    "status": "error",
                    "margin": None,
                    "transfer": outcome.report(spec),
                },
                [
                    {
                        "severity": "error",
                        "code": "measurement",
                        "message": (
                            f"[transfer_metric_unavailable] measurement "
                            f"'{name}': {metric} is unavailable ({reason})"
                        ),
                    }
                ],
            )
    else:
        status, margin, diagnostics = _sim._grade_measurement_value(spec, outcome.value)
        if (
            has_limits
            and ambiguity
            and (status == "pass" or (status == "fail" and outcome.verdict != "fail"))
        ):
            status, margin, downgrade = _sim._INCONCLUSIVE_STATUS, None, True
    if downgrade:
        diagnostics.append(
            {
                "severity": "warning",
                "code": MARGIN_AMBIGUOUS_DIAGNOSTIC_CODE,
                "message": (
                    f"measurement {name!r}: {metric} cannot be graded -- the AC "
                    f"band is ambiguous ({', '.join(ambiguity)}) -- graded "
                    f"{_sim._INCONCLUSIVE_STATUS} rather than pass/fail"
                ),
            }
        )
    return (
        {
            "name": name,
            "value": outcome.value,
            "unit": spec.get("unit"),
            "status": status,
            "margin": margin,
            "transfer": outcome.report(spec),
        },
        diagnostics,
    )
