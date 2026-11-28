"""Scalar measurement grading and the shared response-entry builders for
:mod:`klayout_tools.sim`.

Split out of ``sim.py`` (issue #7320, finishing what merge ``2a01ca4c7``
started with ``_vector_measurement_result``): the primitives that grade one
extracted measurement value against its spec's plausibility bound and
``limits``, the shared vector-measurement response builder they feed, and
the per-kind builders over it. Behaviour-preserving move; the JSON contract
is untouched, and every moved name is re-exported on
:mod:`klayout_tools.sim` so existing ``sim.<name>`` references (tests
included) keep working unchanged.

Stdlib-only by design -- a leaf -- so the measurement sidecar modules
(``sim_measurement_results``, ``sim_snm_meas``, ``sim_slope_band_meas``,
``sim_noise_mechanism``, ``sim_noise_contributors``, ``sim_step_grading``,
``sim_step_evidence``) import these names at module scope instead of
reaching back into the runner through a function-local
``from . import sim as _sim``, which pulled the whole runner (and its
import cascade) into every sidecar entry point called from a fresh
interpreter.
"""

from __future__ import annotations

import functools
from typing import Any

#: The measurement ``status`` of an ``optional`` measurement that produced no
#: value (issue #2925): data ("this figure does not exist at this point"),
#: not a failure -- it never makes its corner ``error``/``fail``.
_ABSENT_STATUS = "absent"

#: The additive measurement status for an unreached target (its diagnostic
#: ``code``, ``NOT_REACHED_DIAGNOSTIC_CODE``, lives in ``sim_measurement_results``).
NOT_REACHED_STATUS = "not_reached"

#: The numeric bounds that contribute coverage checks (issue #1996).
_RECOGNISED_LIMIT_KEYS = ("min", "max")

#: The marker diagnostic :func:`_grade_measurement_value` attaches to a
#: measurement whose value fell outside its declared
#: ``measurements[].plausible_range``/``options.node_voltage_bounds``
#: (issue #2493) -- the *other* reason a corner can grade
#: :data:`_INCONCLUSIVE_STATUS`, and how a consumer tells the two apart.
IMPLAUSIBLE_DIAGNOSTIC_CODE = "implausible_solution"

#: The corner/measurement/report status token issues #2492 and #2493 add
#: beside ``pass``/``fail``/``error``: the run produced numbers, but it will
#: not vouch for them. Ranked ``error > inconclusive > fail > pass`` at every
#: level (:func:`_grade_corner`, :func:`_aggregate_measurement_status`, and
#: ``run_sim``'s top-level rollup override).
#:
#: **Recorded decision -- one status, two reason codes.** The two increments
#: of split #2489 independently invented the same concept: #2492's
#: ``options.fail_on_diagnostic`` ("the solve narrated trouble I do not
#: trust") and #2493's plausibility bounds ("the solve converged somewhere
#: physically impossible"). Both assert *exactly* the same thing about the
#: report -- a number exists and must not be graded against ``limits`` as if
#: it were a result -- and both justified outranking ``fail`` with the same
#: argument, which is why neither could break the tie against the other.
#: Shipping them as two status tokens would have put two near-synonyms in
#: the JSON contract, forced a five-term sum invariant, two parallel counts,
#: two Monte Carlo exclusion fields and an arbitrary precedence between
#: them. They are therefore **one** status whose *reason* is carried by the
#: marker diagnostic's ``code`` (:data:`_GRADING_MARKER_CODES`) -- which
#: issue #2491's ``diagnostic_counts`` already rolls up per code, so the
#: per-reason breakdown a caller might want from two counts is still in the
#: report, derived rather than duplicated.
_INCONCLUSIVE_STATUS = "inconclusive"


def _within_plausible_range(value: float, plausible_range: dict[str, float]) -> bool:
    """Whether ``value`` sits inside a declared plausibility bound.

    The numeric-range pre-check for ``options.node_voltage_bounds``/
    ``measurements[].plausible_range`` (issue #2493) -- run by
    :func:`_grade_measurement_value`, *before* the ordinary ``limits``
    comparison (:func:`_evaluate_limits`). Deliberately not a
    log-text classification: an implausible-but-numerically-valid solve
    (a compact model evaluated far outside its fitted range) produces no
    distinguishing ngspice log line for ``classify_diagnostics`` to catch
    -- this is a purely numeric check the tool performs on the measured
    value itself.
    """
    min_bound = plausible_range.get("min")
    max_bound = plausible_range.get("max")
    if min_bound is not None and value < min_bound:
        return False
    if max_bound is not None and value > max_bound:
        return False
    return True


#: The trailing hint :func:`_no_value_message` (and the step-sequence
#: equivalent in :mod:`klayout_tools.sim_steps`, issue #2482) appends under
#: ``options.save_mode: "netlist"`` (issue #2732).
SAVE_MODE_NETLIST_NO_VALUE_HINT = (
    ' -- options.save_mode is "netlist", so klt emitted no `save '
    "all` and only the vectors the netlist's own .save cards retain "
    "were available: add every signal this measurement reads to the "
    "netlist's .save card, or omit options.save_mode (default "
    '"all")'
)


def _evaluate_limits(
    value: float, limits: dict[str, float] | None
) -> tuple[str, float | None]:
    """Evaluate ``value`` against ``limits`` (``min``/``max``, either optional).

    Returns ``(status, margin)``. No ``limits`` -> reported but never fails
    (``margin`` reflects the nearer bound if any bound exists, else
    ``None``). ``margin``: positive is headroom, negative is violation --
    the nearest *binding* limit when passing, the nearest *violated* limit
    when failing.
    """
    if not limits:
        return "pass", None

    min_limit = limits.get("min")
    max_limit = limits.get("max")

    violations = []
    if min_limit is not None and (
        value < min_limit
        or (value == min_limit and limits.get("exclusive_min") is True)
    ):
        violations.append(value - min_limit)
    if max_limit is not None and (
        value > max_limit
        or (value == max_limit and limits.get("exclusive_max") is True)
    ):
        violations.append(max_limit - value)

    if violations:
        # Worst (most negative) violation.
        return "fail", min(violations)

    headrooms = []
    if min_limit is not None:
        headrooms.append(value - min_limit)
    if max_limit is not None:
        headrooms.append(max_limit - value)
    margin = min(headrooms) if headrooms else None
    return "pass", margin


def _grade_measurement_value(
    spec: dict[str, Any], value: float
) -> tuple[str, float | None, list[dict[str, Any]]]:
    """Grade one extracted measurement ``value`` against its ``spec``.

    Returns ``(status, margin, diagnostics)`` -- ``diagnostics`` is empty
    except on the plausibility-violation path, where it carries the
    ``severity: "warning"``, ``code: "implausible_solution"`` entry the
    caller extends the corner's own ``diagnostics`` with. That code is the
    *reason* half of the status; the status itself is the shared
    :data:`_INCONCLUSIVE_STATUS` (see its note on why the two "produced a
    number I will not vouch for" routes are one token).

    **This function owns the grading order**, which is the whole point of
    issue #2493: a declared plausibility bound
    (``measurements[].plausible_range``, or an inherited
    ``options.node_voltage_bounds`` -- both already resolved into ``spec``
    by :func:`_resolve_plausible_ranges`) is checked **first**, and an
    out-of-range value short-circuits :func:`_evaluate_limits` entirely
    rather than being graded against ``limits`` as if it were a real
    result. ``margin`` stays ``None`` there: margin is defined relative to
    ``limits``, which never ran.

    The reason the ordering lives in one named function rather than inline
    at the call site is that the failure mode this closes is *silent*. An
    ngspice Newton solve can converge onto a numerically valid but
    physically implausible operating point (a compact model evaluated far
    outside its fitted range) without emitting any distinguishing log line
    for :func:`classify_diagnostics` to match -- so the only thing standing
    between that solve and an ordinary-looking ``pass``/``fail`` verdict is
    this check running before :func:`_evaluate_limits`. A second grading
    path that reached :func:`_evaluate_limits` without passing through here
    would silently reopen the gap.

    Never fires for a request that declares no plausibility bound: with
    ``spec["plausible_range"]`` absent, this is exactly
    :func:`_evaluate_limits` plus an empty diagnostic list.
    """
    plausible_range = spec.get("plausible_range")
    if plausible_range is not None and not _within_plausible_range(
        value, plausible_range
    ):
        return (
            _INCONCLUSIVE_STATUS,
            None,
            [
                {
                    "severity": "warning",
                    "code": IMPLAUSIBLE_DIAGNOSTIC_CODE,
                    "message": (
                        f"measurement {spec['name']!r} = {value!r} is outside its "
                        f"declared plausible range {plausible_range!r} -- graded "
                        f"{_INCONCLUSIVE_STATUS} rather than pass/fail"
                    ),
                }
            ],
        )
    status, margin = _evaluate_limits(value, spec.get("limits"))
    return status, margin, []


def _vector_measurement_result(
    spec: dict[str, Any],
    outcome: Any,
    *,
    save_mode: str,
    key: str,
    default_reason: str | None = None,
    report_with_spec: bool = False,
    hint_reason: str | None = None,
    missing_noun: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Response entry for a vector measurement with a coded diagnostic; the
    detail rides the additive ``key`` sibling. A failure is ``status:
    "error"`` with a ``[<reason>]``-prefixed ``measurement`` diagnostic.

    ``hint_reason`` names the reason token that alone triggers the
    ``save_mode: "netlist"`` hint (default: the derived
    ``{key}_vector_missing`` token); ``missing_noun`` overrides the
    failure message's "produced no <noun>" wording (default:
    ``"{key} value"``). A form whose one-off does not fit those two knobs
    keeps it in its own delegating builder."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no {missing_noun or f'{key} value'}"
        )
        missing = f"{key}_vector_missing"
        reason = (
            (outcome.code if outcome is not None else None) or default_reason or missing
        )
        if save_mode == "netlist" and reason == (hint_reason or missing):
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {"name": name, "value": None, "unit": unit}
        result.update(status="error", margin=None)
        return result, [
            {
                "severity": "error",
                "code": "measurement",
                "message": f"[{reason}] {message}",
            }
        ]
    m_status, margin, grading_diagnostics = _grade_measurement_value(
        spec, outcome.value
    )
    result = {"name": name, "value": outcome.value, "unit": unit}
    detail = outcome.report(spec) if report_with_spec else outcome.report()
    result.update(status=m_status, margin=margin, **{key: detail})
    return result, grading_diagnostics


#: Per-kind builders (#4773, #4780); ``settled`` value 1.0/0.0 = (not) settled.
_eye_measurement_result = functools.partial(_vector_measurement_result, key="eye")
_cycle_mean_measurement_result = functools.partial(
    _vector_measurement_result, key="cycle_mean"
)
_tian_measurement_result = functools.partial(
    _vector_measurement_result,
    key="tian",
    default_reason="tian_pass_missing",
    report_with_spec=True,
)
_step_resolution_measurement_result = functools.partial(
    _vector_measurement_result, key="step_resolution"
)
_settled_measurement_result = functools.partial(
    _vector_measurement_result, key="settled"
)


def _scalar_measurement_result(
    spec: dict[str, Any],
    outcome: Any,
    *,
    save_mode: str,
    key: str,
    value_attr: str,
    missing_noun: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Response entry for a scalar sidecar measurement whose failure is an
    unprefixed ``measurement`` diagnostic -- the ``energy``/``stress`` form
    (:func:`_vector_measurement_result`'s ``[<reason>]``-prefixed twin).
    The graded value is ``outcome.<value_attr>``; the breakdown rides the
    additive ``key`` sibling. The entry is missing exactly when the outcome
    carries an ``error``, and the netlist hint is unconditional there:
    no coded reason exists to gate it on."""
    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.error is not None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no {missing_noun}"
        )
        if save_mode == "netlist":
            message += SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {"name": name, "value": None, "unit": unit}
        result.update(status="error", margin=None)
        return result, [
            {"severity": "error", "code": "measurement", "message": message}
        ]
    value = getattr(outcome, value_attr)
    m_status, margin, grading_diagnostics = _grade_measurement_value(spec, value)
    result = {"name": name, "value": value, "unit": unit}
    result.update(status=m_status, margin=margin, **{key: outcome.report()})
    return result, grading_diagnostics


def _energy_measurement_result(
    spec: dict[str, Any],
    outcome: Any,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``energy`` measurement's response entry (issue #3033).

    ``value`` is the net energy in joules -- a plain number, graded through
    :func:`_grade_measurement_value` like any scalar -- and the
    net/gross/recovered/charge breakdown rides the additive ``energy``
    sibling. A source or voltage that did not resolve is an ordinary
    ``status: "error"`` entry with a ``code: "measurement"`` diagnostic and
    no ``energy`` member."""
    return _scalar_measurement_result(
        spec,
        outcome,
        save_mode=save_mode,
        key="energy",
        value_attr="net",
        missing_noun="energy",
    )


def _stress_measurement_result(
    spec: dict[str, Any],
    outcome: Any,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``stress`` measurement's response entry (issue #4592).

    ``value`` is the worst ``|v(a) - v(b)|`` in volts over the window -- a
    plain number, graded through :func:`_grade_measurement_value` like any
    scalar -- and ``{from, to, peak_time}`` rides the additive ``stress``
    sibling. A node that did not resolve is an ordinary ``status: "error"``
    entry with a ``code: "measurement"`` diagnostic and no ``stress``
    member."""
    return _scalar_measurement_result(
        spec,
        outcome,
        save_mode=save_mode,
        key="stress",
        value_attr="peak",
        missing_noun="stress",
    )
