"""Native noise measurements for ``klt sim`` (issue #2938).

ngspice's ``.meas`` has no ``noise`` analysis type, so before this form the
only way to read a number out of an ``analysis.kind: "noise"`` run was
``measurements[].expr`` with a hard-coded ``noise1.`` plot prefix and
hand-rolled index/trapezoid arithmetic. A ``measurements[]`` entry declared
with ``noise`` states the quantity instead::

    {"name": "n_1k",   "noise": {"quantity": "inoise", "at_hz": 1e3}}
    {"name": "n_band", "noise": {"quantity": "onoise", "band_hz": [100, 1e5]}}

- ``at_hz`` -- the spot density (V/sqrt(Hz) or A/sqrt(Hz)) at that frequency,
  interpolated between the two bracketing spectrum points (log-frequency for
  a ``dec``/``oct`` sweep, linear for ``lin``); exact on a grid point.
- ``band_hz`` -- the total rms value over ``[lo, hi]``:
  ``sqrt(integral of density^2 df)``, trapezoidal on the swept grid with the
  band edges interpolated linearly in the squared density. Agrees with
  ngspice's own ``inoise_total`` on a band-limited re-run to well under 1e-3
  relative on a reasonably dense grid.

How it is computed
------------------
Lowered, before the deck is written, into ngspice ``let`` lines in the
generated ``.control`` block (the seam ``expr`` uses): the entry becomes an
``expr`` measurement plus an ``expr_prelude`` of hidden helper vectors, so
the harvest, grading, name-uniqueness and engine-refusal paths are the ones
``expr`` already has. The spectra live in ngspice's *first* noise plot
(``noise1``; the current plot after ``noise`` is the integrated one, issue
#2893) -- that prefix is an implementation detail callers never see.
"""

from __future__ import annotations

import re
from typing import Any

#: The ``noise.quantity`` values: input- or output-referred.
NOISE_QUANTITIES = ("inoise", "onoise")

_ALLOWED_KEYS = frozenset({"quantity", "at_hz", "band_hz"})

_SPICE_NUMBER_RE = re.compile(
    r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(meg|mil|[tgkmunpf])?[a-z]*$",
    re.IGNORECASE,
)
_SUFFIX = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "mil": 25.4e-6,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}


def _error(message: str) -> Exception:
    from .sim import SimError

    return SimError(message)


def is_noise(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``noise``."""
    return spec.get("noise") is not None


def _spice_number(token: str) -> float | None:
    match = _SPICE_NUMBER_RE.match(token.strip())
    if match is None:
        return None
    return float(match.group(1)) * _SUFFIX.get((match.group(2) or "").lower(), 1.0)


def swept_range(analysis: dict[str, Any]) -> tuple[str, float, float] | None:
    """``(sweep, fstart, fstop)`` parsed from a noise analysis' ``args``
    (``v(out) V1 dec 20 10 1e7 [1]``), or None when it cannot be read."""
    tokens = str(analysis.get("args", "")).split()
    for i, token in enumerate(tokens):
        if token.lower() in ("dec", "oct", "lin") and len(tokens) >= i + 4:
            start, stop = _spice_number(tokens[i + 2]), _spice_number(tokens[i + 3])
            if start is not None and stop is not None and 0 < start <= stop:
                return token.lower(), start, stop
            return None
    return None


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and 0 < value < float("inf")
    )


def validate_noise_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].noise`` object's shape."""
    if not isinstance(body, dict):
        raise _error(
            f"measurement '{name}': 'noise' must be an object, e.g. "
            '{"quantity": "inoise", "at_hz": 1000}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise _error(
            f"measurement '{name}': unknown noise key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    if body.get("quantity") not in NOISE_QUANTITIES:
        raise _error(
            f"measurement '{name}': noise.quantity must be one of "
            f"{list(NOISE_QUANTITIES)} (got {body.get('quantity')!r})"
        )
    has_at, has_band = "at_hz" in body, "band_hz" in body
    if has_at == has_band:
        raise _error(
            f"measurement '{name}': noise needs exactly one of 'at_hz' (spot "
            "density) or 'band_hz' ([lo, hi], band-integrated rms)"
        )
    if has_at and not _positive_number(body["at_hz"]):
        raise _error(
            f"measurement '{name}': noise.at_hz must be a positive finite "
            f"number of Hz (got {body['at_hz']!r})"
        )
    if has_band:
        band = body["band_hz"]
        if not (
            isinstance(band, list)
            and len(band) == 2
            and all(_positive_number(edge) for edge in band)
        ):
            raise _error(
                f"measurement '{name}': noise.band_hz must be [lo, hi], two "
                f"positive finite numbers of Hz (got {band!r})"
            )
        if band[0] >= band[1]:
            raise _error(
                f"measurement '{name}': noise.band_hz needs lo < hi (got {band!r})"
            )


def validate_noise_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A noise measurement reads the noise spectrum: refuse it under any
    other ``analysis.kind``, and a frequency outside the swept range."""
    declared = [s for s in measurements_spec if is_noise(s)]
    if not declared:
        return
    if str(analysis.get("kind", "")).lower() != "noise":
        raise _error(
            f"measurement '{declared[0]['name']}' declares 'noise', which "
            'reads the noise spectrum and so requires analysis.kind "noise" '
            f"(got {analysis.get('kind')!r})"
        )
    swept = swept_range(analysis)
    if swept is None:
        return
    _, fmin, fmax = swept
    for spec in declared:
        body = spec["noise"]
        points = [body["at_hz"]] if "at_hz" in body else list(body["band_hz"])
        for hz in points:
            if not fmin <= hz <= fmax:
                raise _error(
                    f"measurement '{spec['name']}': noise frequency {hz:g} Hz "
                    f"is outside the swept range [{fmin:g}, {fmax:g}] Hz "
                    "of analysis.args"
                )


def lower_measurements(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> list[dict[str, Any]]:
    """The measurements with every ``noise`` entry lowered to an ``expr``
    entry carrying its helper ``expr_prelude`` lines (see module docstring).
    Other entries are returned untouched; the input is not mutated."""
    if not any(is_noise(spec) for spec in measurements_spec):
        return measurements_spec
    sweep = (swept_range(analysis) or ("lin", 0.0, 0.0))[0]
    lowered = []
    for spec in measurements_spec:
        if not is_noise(spec):
            lowered.append(spec)
            continue
        prelude, expr = _lines(spec["name"], spec["noise"], sweep)
        lowered.append({**spec, "expr": expr, "expr_prelude": prelude})
    return lowered


def _lines(name: str, body: dict[str, Any], sweep: str) -> tuple[list[str], str]:
    p = f"__klt_nz_{name}_"
    spectrum = f"noise1.{body['quantity']}_spectrum"
    prelude = [
        f"let {p}n = length(noise1.frequency)",
        f"let {p}f1 = noise1.frequency[0,{p}n-2]",
        f"let {p}f2 = noise1.frequency[1,{p}n-1]",
    ]
    if "at_hz" in body:
        hz = repr(float(body["at_hz"]))
        prelude += [
            f"let {p}s1 = {spectrum}[0,{p}n-2]",
            f"let {p}s2 = {spectrum}[1,{p}n-1]",
            f"let {p}fm = vecmax(noise1.frequency)",
            # The interval holding the point; the last one also owns fmax.
            f"let {p}m = ({p}f1 le {hz})*({hz} lt {p}f2)"
            f" + ({hz} ge {p}fm)*({p}f2 ge {p}fm)*({p}f1 lt {p}fm)",
        ]
        if sweep in ("dec", "oct"):
            frac = f"ln({hz}/{p}f1)/ln({p}f2/{p}f1)"
        else:
            frac = f"({hz}-{p}f1)/({p}f2-{p}f1)"
        prelude.append(f"let {p}v = {p}m*({p}s1+({p}s2-{p}s1)*{frac})")
        return prelude, f"mean({p}v)*length({p}v)"
    lo, hi = (repr(float(edge)) for edge in body["band_hz"])
    prelude += [
        f"let {p}p = {spectrum}*{spectrum}",
        f"let {p}p1 = {p}p[0,{p}n-2]",
        f"let {p}p2 = {p}p[1,{p}n-1]",
        # Interval clipped to the band; the weight zeroes empty ones.
        f"let {p}a = ({p}f1 gt {lo})*{p}f1 + ({p}f1 le {lo})*{lo}",
        f"let {p}b = ({p}f2 lt {hi})*{p}f2 + ({p}f2 ge {hi})*{hi}",
        f"let {p}w = ({p}b gt {p}a)",
        f"let {p}pa = {p}p1+({p}p2-{p}p1)*({p}a-{p}f1)/({p}f2-{p}f1)",
        f"let {p}pb = {p}p1+({p}p2-{p}p1)*({p}b-{p}f1)/({p}f2-{p}f1)",
        f"let {p}t = {p}w*({p}pa+{p}pb)/2*({p}b-{p}a)",
    ]
    return prelude, f"sqrt(mean({p}t)*length({p}t))"
