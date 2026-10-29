"""``measurements[].snm`` for ``klt sim`` (issue #3895).

A ``measurements[]`` entry declared with ``snm`` grades the butterfly static
noise margin of a bistable pair as one scalar::

    {"name": "snm_hold",
     "snm": {"x": "v(in)", "y1": "v(out_a)", "y2": "v(out_b)"},
     "limits": {"min": 0.1}}

The numerics (rotation, per-eye extreme, error reasons) live in
:mod:`klayout_tools.sim_snm`; this module only validates the request, asks
ngspice to dump each unique ``x``/``y1``/``y2`` triple of the finished ``dc``
analysis to a small ASCII rawfile, reads it back and hands the arrays to
``analyze_snm``::

    let __klt_snm_<k>_x = v(in)
    let __klt_snm_<k>_y1 = v(out_a)
    let __klt_snm_<k>_y2 = v(out_b)
    write <corner_dir>/snm_<k>.raw __klt_snm_<k>_x __klt_snm_<k>_y1 __klt_snm_<k>_y2

The rawfile is independent of ``options.waveforms``. ``value`` is the SNM in
volts (graded like any scalar); an additive ``snm`` sibling carries the request
echo and the two per-eye sides. Every failure is ``status: "error"``,
``value: null`` with a ``code: "measurement"`` diagnostic whose message starts
with a ``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import sim_raw, sim_snm
from .sim_errors import SimError

#: Reason tokens a failed ``snm`` entry's diagnostic message leads with.
ERROR_CODES = (
    "snm_vector_missing",
    "snm_raw_invalid",
    sim_snm.REASON_INVALID,
    sim_snm.REASON_NON_MONOTONIC,
    sim_snm.REASON_NO_SECOND_EYE,
)

_KEYS = ("x", "y1", "y2")
_ALLOWED_KEYS = frozenset(_KEYS)

#: Single-ended ``v(node)``: one non-empty token without whitespace, control
#: characters, commas or parentheses.
_SELECTOR_RE = re.compile(r"^[vV]\(([^\s\x00-\x1f\x7f,()]+)\)$")

_VECTOR_PREFIX = "__klt_snm_"

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
)


def is_snm(spec: dict[str, Any]) -> bool:
    return spec.get("snm") is not None


def snm_measurements(measurements_spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [spec for spec in measurements_spec if is_snm(spec)]


def normalize_selector(selector: Any) -> str | None:
    """``"v(node)"`` lower-cased, or ``None`` when not an accepted selector."""
    if not isinstance(selector, str):
        return None
    match = _SELECTOR_RE.match(selector)
    return None if match is None else f"v({match.group(1).lower()})"


def _triple(spec: dict[str, Any]) -> tuple[str, str, str]:
    body = spec["snm"]
    return tuple(normalize_selector(body[key]) or "" for key in _KEYS)  # type: ignore[return-value]


def unique_triples(
    measurements_spec: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """Distinct normalized ``(x, y1, y2)`` triples, in first-declared order --
    also the order their deck lines and rawfiles are numbered in."""
    triples: list[tuple[str, str, str]] = []
    for spec in snm_measurements(measurements_spec):
        triple = _triple(spec)
        if triple not in triples:
            triples.append(triple)
    return triples


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``snm``: refuse any other form beside it and
    validate the body. Returns ``True`` when the entry is an snm entry (so the
    caller has nothing more to validate)."""
    if not is_snm(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'snm' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_snm_spec(name, spec["snm"])
    return True


def validate_snm_spec(name: Any, body: Any) -> None:
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'snm' must be an object, e.g. "
            '{"x": "v(in)", "y1": "v(out_a)", "y2": "v(out_b)"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown snm key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    for key in _KEYS:
        if normalize_selector(body.get(key)) is None:
            raise SimError(
                f"measurement '{name}': snm.{key} must be a single-ended "
                f'"v(node)" selector with one node token (no whitespace, '
                f"comma or parenthesis); got {body.get(key)!r}"
            )


def validate_snm_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """An SNM is a DC-sweep quantity (the sweep axis is ``x``), and
    measurement names must be unique so each response entry is unambiguous."""
    declared = snm_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "dc":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'snm', which needs "
            f'a DC sweep and so requires analysis.kind "dc" (got '
            f"{analysis.get('kind')!r})"
        )
    seen: set[str] = set()
    for spec in measurements_spec:
        key = str(spec.get("name")).lower()
        if key in seen:
            raise SimError(
                f"duplicate measurement name '{spec.get('name')}': names must "
                "be unique when measurements[] declares an snm entry"
            )
        seen.add(key)


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    return os.path.join(corner_dir, f"snm_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """Per unique triple: three ``let`` lines and one ``write`` of the three
    vectors. Empty without an snm entry (deck byte-identical to before)."""
    lines: list[str] = []
    for index, triple in enumerate(unique_triples(measurements_spec)):
        vectors = [f"{_VECTOR_PREFIX}{index}_{key}" for key in _KEYS]
        lines.extend(
            f"let {vec} = {sel}" for vec, sel in zip(vectors, triple, strict=True)
        )
        lines.append(f"write {rawfile_path(corner_dir, index)} {' '.join(vectors)}")
    return lines


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


@dataclass
class SnmOutcome:
    """One snm measurement's result, or why there is none."""

    value: float | None = None
    error: str | None = None
    code: str | None = None
    result: sim_snm.SnmResult | None = None

    def report(self, spec: dict[str, Any]) -> dict[str, Any]:
        body = spec["snm"]
        out: dict[str, Any] = {key: body[key] for key in _KEYS}
        if self.result is not None:
            out.update(self.result.to_dict())
        return out


def _analyze_triple(raw_path: str) -> sim_snm.SnmResult | SnmOutcome:
    """The SNM for one triple's rawfile, or a triple-level failure outcome
    (the caller prefixes the measurement name)."""
    if not os.path.isfile(raw_path):
        return SnmOutcome(
            code="snm_vector_missing",
            error=(
                "its x, y1 or y2 vector did not resolve against this DC "
                "analysis's saved vectors (no rawfile was written) -- check "
                "the node names, and that options.save_mode does not omit them"
            ),
        )
    try:
        raw = sim_raw.read_complex_ascii_rawfile(raw_path)
    except sim_raw.RawFileError as exc:
        return SnmOutcome(
            code="snm_raw_invalid", error=f"could not read its rawfile: {exc}"
        )
    if len(raw.names) != 4 or not raw.points:
        return SnmOutcome(
            code="snm_raw_invalid",
            error="its rawfile must hold a sweep axis, x, y1 and y2 with samples",
        )
    columns = [[z.real for z in raw.column(i)] for i in (1, 2, 3)]
    try:
        return sim_snm.analyze_snm(*columns)
    except sim_snm.SnmInputError as exc:
        return SnmOutcome(code=exc.reason, error=f"bad sweep: {exc}")


def _outcome_for(
    spec: dict[str, Any], analysed: sim_snm.SnmResult | SnmOutcome
) -> SnmOutcome:
    if isinstance(analysed, SnmOutcome):
        return SnmOutcome(
            code=analysed.code,
            error=f"measurement '{spec['name']}': {analysed.error}",
        )
    return SnmOutcome(value=float(analysed.snm), result=analysed)


def extract_corner_snms(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, SnmOutcome]:
    """``{position in measurements_spec: outcome}`` for every snm entry of one
    finished corner. One computation per unique triple. Never raises: any
    rawfile problem becomes that measurement's own ``error``."""
    triples = unique_triples(measurements_spec)
    analysed = [
        _analyze_triple(rawfile_path(corner_dir, i)) for i in range(len(triples))
    ]
    return {
        position: _outcome_for(spec, analysed[triples.index(_triple(spec))])
        for position, spec in enumerate(measurements_spec)
        if is_snm(spec)
    }
