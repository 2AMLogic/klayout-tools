"""``measurements[].slope_band`` for ``klt sim`` (issue #5518).

A ``measurements[]`` entry declared with ``slope_band`` reduces a ``dc`` sweep
to the largest contiguous run of sweep points, containing a named anchor, over
which the local slope ``dy/dx`` stays inside a closed band -- the shape behind
"usable range", "compliance range" and "linear region" spec rows::

    {"name": "usable_range",
     "slope_band": {"y": "v(out)", "x": "v(in)", "anchor": 0.5,
                    "min_slope": 1.0, "max_slope": 2.5, "report": "y_span"},
     "unit": "V", "limits": {"min": 0.5}}

``report`` selects which one of the run's six scalars becomes the entry's
``value``, so ``limits``, ``plausible_range``, ``spec_row``, ``worst_case`` and
Monte Carlo statistics all grade it like any other measurement.

The numerics (slope estimate, run search, error reasons) live in
:mod:`klayout_tools.sim_slope_band`; this module only validates the request,
asks ngspice to dump each unique ``x``/``y`` pair of the finished ``dc``
analysis to a small ASCII rawfile::

    let __klt_sband_<k>_x = v(in)
    let __klt_sband_<k>_y = v(out)
    write <corner_dir>/slope_band_<k>.raw __klt_sband_<k>_x __klt_sband_<k>_y

reads it back and hands the arrays to ``analyze_slope_band``. The rawfile is
independent of ``options.waveforms``.

Every failure is ``status: "error"``, ``value: null`` with a
``code: "measurement"`` diagnostic whose message starts with a ``[<reason>]``
token from :data:`ERROR_CODES` -- never a silent pass or a substituted value.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

from . import sim_raw, sim_slope_band
from .sim_errors import SimError
from .sim_forms import validate_exclusive_form

#: Reason tokens a failed ``slope_band`` entry's diagnostic message leads with.
ERROR_CODES = (
    "slope_band_vector_missing",
    "slope_band_raw_invalid",
    sim_slope_band.REASON_INVALID,
    sim_slope_band.REASON_NON_MONOTONIC,
    sim_slope_band.REASON_ANCHOR_OUTSIDE,
    sim_slope_band.REASON_ANCHOR_OUT_OF_BAND,
)

#: The selector keys, the numeric keys and the whole required set. All six are
#: required: each changes which run is found or which scalar is graded, so
#: none has a defensible default.
_SELECTOR_KEYS = ("y", "x")
_NUMBER_KEYS = ("anchor", "min_slope", "max_slope")
_KEYS = (*_SELECTOR_KEYS, *_NUMBER_KEYS, "report")
_ALLOWED_KEYS = frozenset(_KEYS)

#: Single-ended ``v(node)``: one non-empty token without whitespace, control
#: characters, commas or parentheses -- the same rule as ``snm``.
_SELECTOR_RE = re.compile(r"^[vV]\(([^\s\x00-\x1f\x7f,()]+)\)$")

_VECTOR_PREFIX = "__klt_sband_"

#: Every other measurement form an entry must not also declare. ``slope_band``
#: is the newest form, so (as for every form before it) its own tuple is the
#: complete one and its validator runs first in
#: ``sim_validate._EXCLUSIVE_FORM_VALIDATORS``.
_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "stress",
    "noise",
    "transfer",
    "tian",
    "snm",
    "search",
    "eye",
    "step_resolution",
    "settled",
)


def is_slope_band(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``slope_band``."""
    return spec.get("slope_band") is not None


def slope_band_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [spec for spec in measurements_spec if is_slope_band(spec)]


def normalize_selector(selector: Any) -> str | None:
    """``"v(node)"`` lower-cased, or ``None`` when not an accepted selector."""
    if not isinstance(selector, str):
        return None
    match = _SELECTOR_RE.match(selector)
    return None if match is None else f"v({match.group(1).lower()})"


def _is_number(value: Any) -> bool:
    """A finite real number. ``bool`` is excluded: ``True`` is not a slope."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _pair(spec: dict[str, Any]) -> tuple[str, str]:
    """The normalized ``(x, y)`` selector pair -- the rawfile's identity."""
    body = spec["slope_band"]
    return (
        normalize_selector(body["x"]) or "",
        normalize_selector(body["y"]) or "",
    )


def _params(spec: dict[str, Any]) -> tuple[str, str, float, float, float]:
    """The full computation key: the selector pair plus the anchor and band.
    Two entries sharing it differ only by ``report``, so they share one run
    search as well as one rawfile."""
    body = spec["slope_band"]
    x, y = _pair(spec)
    return (
        x,
        y,
        float(body["anchor"]),
        float(body["min_slope"]),
        float(body["max_slope"]),
    )


def unique_pairs(measurements_spec: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Distinct normalized ``(x, y)`` pairs, in first-declared order -- also
    the order their deck lines and rawfiles are numbered in."""
    pairs: list[tuple[str, str]] = []
    for spec in slope_band_measurements(measurements_spec):
        pair = _pair(spec)
        if pair not in pairs:
            pairs.append(pair)
    return pairs


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``slope_band``: refuse any other form beside it
    and validate the body. Returns ``True`` when the entry is a slope_band
    entry (so the caller has nothing more to validate)."""
    return validate_exclusive_form(
        spec,
        "slope_band",
        _FORMS,
        lambda name, spec: validate_slope_band_spec(name, spec["slope_band"]),
    )


def _validate_keys(name: Any, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'slope_band' must be an object, e.g. "
            '{"y": "v(out)", "x": "v(in)", "anchor": 0.5, "min_slope": 1.0, '
            '"max_slope": 2.5, "report": "y_span"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown slope_band key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    missing = [key for key in _KEYS if key not in body]
    if missing:
        raise SimError(
            f"measurement '{name}': slope_band requires {missing} -- every one "
            f"of {list(_KEYS)} is required (each changes which run is found or "
            "which scalar is graded, so none has a default)"
        )


def validate_slope_band_spec(name: Any, body: Any) -> None:
    _validate_keys(name, body)
    for key in _SELECTOR_KEYS:
        if normalize_selector(body.get(key)) is None:
            raise SimError(
                f"measurement '{name}': slope_band.{key} must be a "
                f'single-ended "v(node)" selector with one node token (no '
                f"whitespace, comma or parenthesis); got {body.get(key)!r}"
            )
    for key in _NUMBER_KEYS:
        if not _is_number(body.get(key)):
            raise SimError(
                f"measurement '{name}': slope_band.{key} must be a finite "
                f"number; got {body.get(key)!r}"
            )
    if float(body["min_slope"]) > float(body["max_slope"]):
        raise SimError(
            f"measurement '{name}': slope_band.min_slope "
            f"({body['min_slope']!r}) is above slope_band.max_slope "
            f"({body['max_slope']!r}): the band is the closed interval "
            "[min_slope, max_slope], which would be empty"
        )
    if body.get("report") not in sim_slope_band.REPORTS:
        raise SimError(
            f"measurement '{name}': slope_band.report must be one of "
            f"{list(sim_slope_band.REPORTS)}; got {body.get('report')!r}"
        )


def validate_slope_band_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A slope band is a DC-sweep quantity (the sweep axis is ``x``), and
    measurement names must be unique so each response entry is unambiguous."""
    declared = slope_band_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "dc":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'slope_band', which "
            f"reduces a swept transfer curve and so requires analysis.kind "
            f'"dc" (got {analysis.get("kind")!r})'
        )
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                "be unique when measurements[] declares a slope_band entry"
            )
        seen.add(key)


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    return os.path.join(corner_dir, f"slope_band_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """Per unique ``x``/``y`` pair: two ``let`` lines and one ``write`` of both
    vectors. Empty without a slope_band entry (deck byte-identical to
    before)."""
    lines: list[str] = []
    for index, (x_sel, y_sel) in enumerate(unique_pairs(measurements_spec)):
        vectors = [f"{_VECTOR_PREFIX}{index}_{key}" for key in ("x", "y")]
        lines.append(f"let {vectors[0]} = {x_sel}")
        lines.append(f"let {vectors[1]} = {y_sel}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {' '.join(vectors)}")
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

#: A pair's decoded sweep, or the pair-level failure that replaced it.
_Pair = tuple[list[float], list[float]]
#: A computation key's run search, or the ``(reason, message)`` it failed with.
_Searched = sim_slope_band.SlopeBandResult | tuple[str, str]


@dataclass
class SlopeBandOutcome:
    """One slope_band measurement's result, or why there is none."""

    value: float | None = None
    error: str | None = None
    code: str | None = None
    result: sim_slope_band.SlopeBandResult | None = None

    def report(self, spec: dict[str, Any]) -> dict[str, Any]:
        body = spec["slope_band"]
        out: dict[str, Any] = {key: body[key] for key in _KEYS}
        if self.result is not None:
            out.update(self.result.to_dict())
        return out


def _read_pair(raw_path: str) -> _Pair | SlopeBandOutcome:
    """One pair's ``(xs, ys)`` from its rawfile, or a pair-level failure
    outcome (the caller prefixes the measurement name)."""
    if not os.path.isfile(raw_path):
        return SlopeBandOutcome(
            code="slope_band_vector_missing",
            error=(
                "its x or y vector did not resolve against this DC analysis's "
                "saved vectors (no rawfile was written) -- check the node "
                "names, and that options.save_mode does not omit them"
            ),
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
    except sim_raw.RawFileError as exc:
        return SlopeBandOutcome(
            code="slope_band_raw_invalid", error=f"could not read its rawfile: {exc}"
        )
    if len(raw.names) != 3 or not raw.points:
        return SlopeBandOutcome(
            code="slope_band_raw_invalid",
            error="its rawfile must hold a sweep axis, x and y with samples",
        )
    return [z.real for z in raw.column(1)], [z.real for z in raw.column(2)]


def _search(pair: _Pair, body: dict[str, Any]) -> _Searched:
    try:
        return sim_slope_band.analyze_slope_band(
            *pair,
            anchor=float(body["anchor"]),
            min_slope=float(body["min_slope"]),
            max_slope=float(body["max_slope"]),
        )
    except sim_slope_band.SlopeBandInputError as exc:
        return (exc.reason, str(exc))


def _outcome_for(
    spec: dict[str, Any],
    pair: _Pair | SlopeBandOutcome,
    cache: dict[tuple[str, str, float, float, float], _Searched],
) -> SlopeBandOutcome:
    """One entry's outcome, reusing a cached run search for an identical
    ``(x, y, anchor, min_slope, max_slope)`` -- entries differing only by
    ``report`` share one computation as well as one rawfile."""
    name = spec["name"]
    if isinstance(pair, SlopeBandOutcome):
        return SlopeBandOutcome(
            code=pair.code, error=f"measurement '{name}': {pair.error}"
        )
    body = spec["slope_band"]
    key = _params(spec)
    if key not in cache:
        cache[key] = _search(pair, body)
    found = cache[key]
    if isinstance(found, tuple):
        reason, message = found
        return SlopeBandOutcome(
            code=reason, error=f"measurement '{name}': bad sweep: {message}"
        )
    return SlopeBandOutcome(value=found.select(body["report"]), result=found)


def extract_corner_slope_bands(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, SlopeBandOutcome]:
    """``{position in measurements_spec: outcome}`` for every slope_band entry
    of one finished corner. One rawfile read per unique ``x``/``y`` pair and
    one run search per unique anchor/band on it. Never raises: any rawfile
    problem becomes that measurement's own ``error``."""
    pairs = unique_pairs(measurements_spec)
    read = [_read_pair(rawfile_path(corner_dir, i)) for i in range(len(pairs))]
    cache: dict[tuple[str, str, float, float, float], _Searched] = {}
    return {
        position: _outcome_for(spec, read[pairs.index(_pair(spec))], cache)
        for position, spec in enumerate(measurements_spec)
        if is_slope_band(spec)
    }


# --------------------------------------------------------------------------- #
# Response entry
# --------------------------------------------------------------------------- #


def slope_band_measurement_result(
    spec: dict[str, Any],
    outcome: SlopeBandOutcome | None,
    *,
    save_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One ``slope_band`` measurement's response entry.

    ``value`` is the one scalar the entry's ``slope_band.report`` selects,
    graded like any other scalar; the request echo, all six scalars, both edge
    reasons and ``points_in_run`` ride the additive ``slope_band`` sibling. A
    failure is ``status: "error"``, ``value: null`` (so it can never pass a
    limit) with a ``code: "measurement"`` diagnostic whose message starts with
    a ``[<reason>]`` token from :data:`ERROR_CODES`."""
    from . import sim as _sim

    name = spec["name"]
    unit = spec.get("unit")
    if outcome is None or outcome.value is None:
        message = (
            outcome.error
            if outcome is not None and outcome.error is not None
            else f"measurement '{name}' produced no slope_band value"
        )
        reason = (
            outcome.code
            if outcome is not None and outcome.code is not None
            else "slope_band_vector_missing"
        )
        if save_mode == "netlist" and reason == "slope_band_vector_missing":
            message += _sim.SAVE_MODE_NETLIST_NO_VALUE_HINT
        result = {
            "name": name,
            "value": None,
            "unit": unit,
            "status": "error",
            "margin": None,
        }
        diagnostic = {
            "severity": "error",
            "code": "measurement",
            "message": f"[{reason}] {message}",
        }
        return result, [diagnostic]
    m_status, margin, grading = _sim._grade_measurement_value(spec, outcome.value)
    result = {
        "name": name,
        "value": outcome.value,
        "unit": unit,
        "status": m_status,
        "margin": margin,
        "slope_band": outcome.report(spec),
    }
    return result, grading
