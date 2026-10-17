"""``measurements[].tian`` for ``klt sim`` (issue #3160).

A ``measurements[]`` entry declared with ``tian`` grades one scalar stability
metric of the dual-injection (Tian) return ratio of a bench acquired with
``analysis.passes`` (issue #3159)::

    {"name": "pm",
     "tian": {"voltage_pass": "inj_v", "current_pass": "inj_i",
              "tv": "v(fv)", "ti": "i(vfi)", "metric": "phase_margin_deg"},
     "limits": {"min": 45}}

``tv`` is read from the ``voltage_pass`` result and ``ti`` from the
``current_pass`` result, **as is**: each selected complex vector must already
be the return ratio of its injection (unit-magnitude stimulus, bench sign
arranged so the loop is negative-feedback, e.g. through a ``-1`` VCVS), so the
request itself carries no sign or scale knob. Selectors are single-ended
``v(node)`` or ``i(source)`` -- the vectors ngspice names that way in the
pass rawfile; nothing else is accepted.

Per frequency sample::

    T = (Tv * Ti - 1) / (Tv + Ti + 2)

The array ``T`` is then reduced with :func:`klayout_tools.sim_transfer.
analyze_transfer` (denominator ``1``), so the sign, interpolation and
first-crossing conventions are exactly those of ``measurements[].transfer``
(#2922) -- see that module's docstring. ``max_gain_above_crossover_db`` is the
one extra token (:func:`klayout_tools.sim_transfer.max_gain_above_crossover_db`).

Strictness: both passes must carry the identical, finite, positive, strictly
increasing frequency grid (compared exactly -- never resampled); a non-finite
``Tv``/``Ti`` sample or a (relatively) singular ``Tv + Ti + 2`` is an error
for the whole measurement, never an interpolated gap. The ``T`` vector is
computed once per ``(voltage_pass, current_pass, tv, ti)`` tuple of a corner and
shared by every entry using it. Every failure is ``status: "error"``,
``value: null`` with a ``code: "measurement"`` diagnostic whose message starts
with a ``[<reason>]`` token from :data:`ERROR_CODES`, so it can never pass a
limit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from . import sim_transfer
from .sim_errors import SimError
from .sim_passes import PassResult
from .sim_transfer_meas import normalize_selector as _normalize_voltage

#: Every accepted ``tian.metric`` token.
METRICS = (
    "unity_crossing_hz",
    "phase_at_unity_deg",
    "phase_margin_deg",
    "gain_margin_db",
    "max_gain_above_crossover_db",
)

#: Reason tokens a failed ``tian`` entry's diagnostic message leads with.
ERROR_CODES = (
    "tian_pass_missing",
    "tian_vector_missing",
    "tian_grid_invalid",
    "tian_grid_mismatch",
    "tian_invalid_samples",
    "tian_singular_denominator",
    "tian_metric_unavailable",
)

#: ``|Tv + Ti + 2|`` at or below this fraction of ``|Tv| + |Ti| + 2`` is a
#: singular sample (the quotient would be numerical noise).
SINGULAR_RELATIVE_TOLERANCE = 1e-9

_ALLOWED_KEYS = frozenset({"voltage_pass", "current_pass", "tv", "ti", "metric"})

_CURRENT_RE = re.compile(r"^[iI]\(([^\s\x00-\x1f\x7f,()]+)\)$")

#: Every other measurement form an entry must not also declare.
_OTHER_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "noise",
    "transfer",
)


def is_tian(spec: dict[str, Any]) -> bool:
    return spec.get("tian") is not None


def tian_measurements(measurements_spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [spec for spec in measurements_spec if is_tian(spec)]


def normalize_selector(selector: Any) -> str | None:
    """``"v(node)"`` / ``"i(source)"`` lower-cased (the rawfile's own vector
    name), or ``None`` when not an accepted selector."""
    if not isinstance(selector, str):
        return None
    voltage = _normalize_voltage(selector)
    if voltage is not None:
        return voltage
    match = _CURRENT_RE.match(selector)
    return None if match is None else f"i({match.group(1).lower()})"


def _tuple_key(spec: dict[str, Any]) -> tuple[str, str, str, str]:
    body = spec["tian"]
    return (
        body["voltage_pass"].lower(),
        body["current_pass"].lower(),
        normalize_selector(body["tv"]) or "",
        normalize_selector(body["ti"]) or "",
    )


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``tian``: refuse any other form beside it and
    validate the body. Returns ``True`` when the entry is a tian entry."""
    if not is_tian(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _OTHER_FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'tian' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_tian_spec(name, spec["tian"])
    return True


def validate_tian_spec(name: Any, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'tian' must be an object, e.g. "
            '{"voltage_pass": "inj_v", "current_pass": "inj_i", '
            '"tv": "v(fv)", "ti": "i(vfi)", "metric": "phase_margin_deg"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown tian key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    for key in ("voltage_pass", "current_pass"):
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise SimError(
                f"measurement '{name}': tian.{key} must be the name of an "
                f"analysis.passes entry (got {value!r})"
            )
    for key in ("tv", "ti"):
        if normalize_selector(body.get(key)) is None:
            raise SimError(
                f"measurement '{name}': tian.{key} must be a single-ended "
                '"v(node)" or "i(source)" selector with one token (no '
                f"whitespace, comma or parenthesis); got {body.get(key)!r}"
            )
    if body.get("metric") not in METRICS:
        raise SimError(
            f"measurement '{name}': tian.metric must be one of "
            f"{list(METRICS)} (got {body.get('metric')!r})"
        )


def validate_tian_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A tian metric needs the named passes of a resolved ``analysis.passes``
    request, and measurement names must be unique so each response entry is
    unambiguous."""
    declared = tian_measurements(measurements_spec)
    if not declared:
        return
    passes = analysis.get("passes")
    if passes is None:
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'tian', which "
            "combines the complex results of named AC passes and so requires "
            "analysis.passes"
        )
    known = {p["name"].lower() for p in passes}
    for spec in declared:
        for key in ("voltage_pass", "current_pass"):
            pass_name = spec["tian"][key]
            if pass_name.lower() not in known:
                raise SimError(
                    f"measurement '{spec['name']}': tian.{key} {pass_name!r} "
                    f"is not an analysis.passes name (declared: "
                    f"{sorted(p['name'] for p in passes)})"
                )
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                "be unique when measurements[] declares a tian entry"
            )
        seen.add(key)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class TianOutcome:
    """One tian measurement's result, or why there is none."""

    value: float | None = None
    error: str | None = None
    code: str | None = None

    def report(self, spec: dict[str, Any]) -> dict[str, Any]:
        body = spec["tian"]
        return {key: body[key] for key in sorted(_ALLOWED_KEYS)}


@dataclass
class _Failure:
    code: str
    message: str


@dataclass
class _Reduced:
    freqs: list[float]
    metrics: sim_transfer.TransferMetrics


def _pass_vector(
    results: dict[str, PassResult], pass_name: str, selector: str
) -> tuple[list[float], list[complex]] | _Failure:
    result = results.get(pass_name.lower())
    if result is None or result.data is None:
        why = "was not acquired" if result is None else str(result.error)
        return _Failure("tian_pass_missing", f"pass {pass_name!r} {why}")
    data = result.data
    if selector not in data.names or not data.names or not data.points:
        return _Failure(
            "tian_vector_missing",
            f"{selector} is not in pass {pass_name!r}'s saved vectors -- check "
            "the node/source name and that options.save_mode does not omit it",
        )
    freqs = [z.real for z in data.column(0)]
    return freqs, data.column(data.names.index(selector))


def _check_grid(pass_name: str, freqs: list[float]) -> _Failure | None:
    try:
        sim_transfer._check_arrays(freqs, [1] * len(freqs), [1] * len(freqs))
    except sim_transfer.TransferInputError as exc:
        return _Failure(
            "tian_grid_invalid", f"pass {pass_name!r} has a bad frequency grid: {exc}"
        )
    return None


def _tian_ratio(tv: list[complex], ti: list[complex]) -> list[complex] | _Failure:
    """``(Tv*Ti - 1) / (Tv + Ti + 2)`` per sample, or the first failure."""
    bad = [
        i
        for i, (a, b) in enumerate(zip(tv, ti, strict=True))
        if not (math.isfinite(abs(a)) and math.isfinite(abs(b)))
    ]
    if bad:
        return _Failure(
            "tian_invalid_samples",
            f"Tv/Ti has {len(bad)} non-finite sample(s), first at index {bad[0]}",
        )
    ratio: list[complex] = []
    singular: list[int] = []
    for i, (a, b) in enumerate(zip(tv, ti, strict=True)):
        denominator = a + b + 2
        scale = abs(a) + abs(b) + 2
        if abs(denominator) <= SINGULAR_RELATIVE_TOLERANCE * scale:
            singular.append(i)
            continue
        value = (a * b - 1) / denominator
        if not math.isfinite(abs(value)):
            return _Failure(
                "tian_invalid_samples", f"T is non-finite at sample index {i}"
            )
        ratio.append(value)
    if singular:
        return _Failure(
            "tian_singular_denominator",
            f"Tv + Ti + 2 is singular at {len(singular)} sample(s), first at "
            f"index {singular[0]}",
        )
    return ratio


def tian_vector(
    results: dict[str, PassResult], key: tuple[str, str, str, str]
) -> tuple[list[float], list[complex]] | _Failure:
    """The frequency axis and ``T`` for one ``(voltage_pass, current_pass,
    tv, ti)`` tuple, or why it cannot be formed."""
    voltage_pass, current_pass, tv_sel, ti_sel = key
    v = _pass_vector(results, voltage_pass, tv_sel)
    if isinstance(v, _Failure):
        return v
    i = _pass_vector(results, current_pass, ti_sel)
    if isinstance(i, _Failure):
        return i
    for name, freqs in ((voltage_pass, v[0]), (current_pass, i[0])):
        grid_error = _check_grid(name, freqs)
        if grid_error is not None:
            return grid_error
    if v[0] != i[0]:
        return _Failure(
            "tian_grid_mismatch",
            f"passes {voltage_pass!r} ({len(v[0])} points) and "
            f"{current_pass!r} ({len(i[0])} points) do not share an identical "
            "frequency grid (never resampled)",
        )
    ratio = _tian_ratio(v[1], i[1])
    if isinstance(ratio, _Failure):
        return ratio
    return v[0], ratio


def _reduce(vector: tuple[list[float], list[complex]]) -> _Reduced:
    freqs, ratio = vector
    return _Reduced(
        freqs, sim_transfer.analyze_transfer(freqs, ratio, [1] * len(ratio))
    )


def _outcome_for(spec: dict[str, Any], reduced: _Reduced | _Failure) -> TianOutcome:
    name = spec["name"]
    if isinstance(reduced, _Failure):
        return TianOutcome(
            code=reduced.code, error=f"measurement '{name}': {reduced.message}"
        )
    metric = spec["tian"]["metric"]
    reason: str | None
    if metric == "max_gain_above_crossover_db":
        value, reason = sim_transfer.max_gain_above_crossover_db(
            reduced.freqs, reduced.metrics
        )
    else:
        value = getattr(reduced.metrics, metric)
        reason = reduced.metrics.reasons.get(metric, "unavailable")
    if value is None:
        return TianOutcome(
            code="tian_metric_unavailable",
            error=f"measurement '{name}': {metric} is unavailable ({reason})",
        )
    return TianOutcome(value=float(value))


def extract_corner_tians(
    measurements_spec: list[dict[str, Any]], pass_results: list[PassResult]
) -> dict[int, TianOutcome]:
    """``{position in measurements_spec: outcome}`` for every tian entry of
    one finished corner. ``T`` is formed and reduced once per unique
    ``(voltage_pass, current_pass, tv, ti)`` tuple. Never raises."""
    by_name = {result.name.lower(): result for result in pass_results}
    cache: dict[tuple[str, str, str, str], _Reduced | _Failure] = {}
    outcomes: dict[int, TianOutcome] = {}
    for position, spec in enumerate(measurements_spec):
        if not is_tian(spec):
            continue
        key = _tuple_key(spec)
        if key not in cache:
            vector = tian_vector(by_name, key)
            cache[key] = vector if isinstance(vector, _Failure) else _reduce(vector)
        outcomes[position] = _outcome_for(spec, cache[key])
    return outcomes
