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

A ``mechanism`` key (issue #5553) narrows the value to the ``"thermal"`` or
``"flicker"`` share of the total::

    {"name": "flicker_rms", "noise": {"quantity": "onoise",
                                      "band_hz": [1, 1e5],
                                      "mechanism": "flicker"}}

That form is *not* lowered the way the total form is -- it is aggregated from
ngspice's per-device decomposition after the run by
:mod:`klayout_tools.sim_noise_mechanism`, which owns its suffix table,
completeness assertion and refusals. Everything below describes the total
form.

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

import math
from typing import Any

from . import _spice_number as spice_number
from . import sim_noise_mechanism
from .sim_errors import SimError

#: Spectrum vectors of ngspice's first noise plot, aliased by bare name
#: into the current (totals) plot for ``expr`` measurements (issue #2893).
NOISE_SPECTRUM_VECTORS = ("inoise_spectrum", "onoise_spectrum")

#: The ``noise.quantity`` values: input- or output-referred.
NOISE_QUANTITIES = ("inoise", "onoise")

_ALLOWED_KEYS = frozenset({"quantity", "at_hz", "band_hz", "mechanism"})


def is_noise(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``noise``."""
    return spec.get("noise") is not None


def swept_range(analysis: dict[str, Any]) -> tuple[str, float, float] | None:
    """``(sweep, fstart, fstop)`` parsed from a noise analysis' ``args``
    (``v(out) V1 dec 20 10 1e7 [1]``), or None when it cannot be read."""
    tokens = str(analysis.get("args", "")).split()
    for i, token in enumerate(tokens):
        if token.lower() in ("dec", "oct", "lin") and len(tokens) >= i + 4:
            start, stop = (
                spice_number.parse(tokens[i + 2]),
                spice_number.parse(tokens[i + 3]),
            )
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
        raise SimError(
            f"measurement '{name}': 'noise' must be an object, e.g. "
            '{"quantity": "inoise", "at_hz": 1000}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown noise key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    if body.get("quantity") not in NOISE_QUANTITIES:
        raise SimError(
            f"measurement '{name}': noise.quantity must be one of "
            f"{list(NOISE_QUANTITIES)} (got {body.get('quantity')!r})"
        )
    _validate_mechanism(name, body)
    has_at, has_band = "at_hz" in body, "band_hz" in body
    if has_at == has_band:
        raise SimError(
            f"measurement '{name}': noise needs exactly one of 'at_hz' (spot "
            "density) or 'band_hz' ([lo, hi], band-integrated rms)"
        )
    if has_at and not _positive_number(body["at_hz"]):
        raise SimError(
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
            raise SimError(
                f"measurement '{name}': noise.band_hz must be [lo, hi], two "
                f"positive finite numbers of Hz (got {band!r})"
            )
        if band[0] >= band[1]:
            raise SimError(
                f"measurement '{name}': noise.band_hz needs lo < hi (got {band!r})"
            )


def _validate_mechanism(name: Any, body: dict[str, Any]) -> None:
    """Issue #5553: the optional ``mechanism`` key, and the one quantity it
    cannot be asked for.

    ngspice emits per-contributor spectra for the *output*-referred quantity
    only: ``noise1`` carries ``onoise_<device>_<generator>`` vectors but no
    ``inoise_`` counterpart (``noise2``'s ``inoise_total_*`` scalars exist, but
    only integrated over the whole swept range, so ``at_hz``/``band_hz`` could
    not keep their meaning). Refused by name rather than silently returning a
    null value -- verified on ngspice-47.
    """
    if "mechanism" not in body:
        return
    mechanism = body["mechanism"]
    if mechanism not in sim_noise_mechanism.NOISE_MECHANISMS:
        raise SimError(
            f"measurement '{name}': noise.mechanism must be one of "
            f"{list(sim_noise_mechanism.NOISE_MECHANISMS)} "
            f"(got {mechanism!r})"
        )
    if body.get("quantity") != "onoise":
        raise SimError(
            f"measurement '{name}': noise.mechanism needs "
            'quantity "onoise" -- ngspice emits no per-contributor '
            "input-referred spectra, so an inoise mechanism share cannot be "
            "separated out. Measure it output-referred and divide by the "
            "gain, or use the total form for inoise"
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
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'noise', which "
            'reads the noise spectrum and so requires analysis.kind "noise" '
            f"(got {analysis.get('kind')!r})"
        )
    _refuse_missing_pts_per_summary(declared, analysis)
    swept = swept_range(analysis)
    if swept is None:
        return
    _, fmin, fmax = swept
    for spec in declared:
        body = spec["noise"]
        points = [body["at_hz"]] if "at_hz" in body else list(body["band_hz"])
        for hz in points:
            if not fmin <= hz <= fmax:
                raise SimError(
                    f"measurement '{spec['name']}': noise frequency {hz:g} Hz "
                    f"is outside the swept range [{fmin:g}, {fmax:g}] Hz "
                    "of analysis.args"
                )


def _refuse_missing_pts_per_summary(
    declared: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """Issue #5553: a ``mechanism`` measurement needs the trailing
    ``pts_per_summary`` argument on the analysis line.

    Without it ngspice's spectra plot holds exactly ``frequency``,
    ``inoise_spectrum`` and ``onoise_spectrum`` -- no per-contributor vector
    exists, so no mechanism can be separated out (measured, ngspice-47). The
    argument is *not* injected: rewriting the caller's analysis line also
    multiplies the vector count by the device count, which is the caller's
    cost to accept. Refused before any run so the request can never come back
    with a null value instead.
    """
    mechanisms = [spec for spec in declared if sim_noise_mechanism.is_mechanism(spec)]
    if not mechanisms or sim_noise_mechanism.has_pts_per_summary(analysis):
        return
    args = str(analysis.get("args", "")).strip()
    raise SimError(
        f"measurement '{mechanisms[0]['name']}' declares noise.mechanism "
        f"{mechanisms[0]['noise']['mechanism']!r}, which needs ngspice's "
        "per-device noise decomposition -- but analysis.args has no trailing "
        "pts_per_summary argument, so ngspice emits only the total spectrum. "
        f"Append it: analysis.args = {args + ' 1'!r} (the trailing 1 is "
        "pts_per_summary)"
    )


def lower_measurements(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> list[dict[str, Any]]:
    """The measurements with every ``noise`` entry lowered to an ``expr``
    entry carrying its helper ``expr_prelude`` lines (see module docstring).
    Other entries are returned untouched; the input is not mutated."""
    if not any(
        is_noise(spec) and not sim_noise_mechanism.is_mechanism(spec)
        for spec in measurements_spec
    ):
        return measurements_spec
    sweep = (swept_range(analysis) or ("lin", 0.0, 0.0))[0]
    lowered = []
    for spec in measurements_spec:
        # A `mechanism` entry (issue #5553) is reduced in Python after the run
        # from the per-contributor spectra rawfile
        # (:mod:`klayout_tools.sim_noise_mechanism`), never lowered to `let`
        # lines: the contributor names are unknown until the run and ngspice's
        # `expr` has no vector glob. It stays a bare `noise` entry here.
        if not is_noise(spec) or sim_noise_mechanism.is_mechanism(spec):
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


# --------------------------------------------------------------------------- #
# The Python twins of `_lines`
# --------------------------------------------------------------------------- #
#
# The `mechanism` form (issue #5553) cannot be lowered into ngspice at all --
# its contributor vectors are unknown until the run -- so it reduces density
# columns in Python after the run instead. These two functions are deliberate
# transcriptions of the arithmetic `_lines` emits above, interval mask and
# edge interpolation included, so a mechanism share and a total reduced over
# the same frequency or band stay directly comparable.


def spot_density(
    freqs: list[float], density: list[float], hz: float, sweep: str
) -> float | None:
    """The density interpolated at ``hz``: log-frequency for a ``dec``/``oct``
    sweep, linear for ``lin``; exact on a grid point. None when the grid
    cannot bracket ``hz``."""
    if len(freqs) < 2:
        return None
    fmax = max(freqs)
    total = 0.0
    hit = False
    for index in range(len(freqs) - 1):
        f1, f2 = freqs[index], freqs[index + 1]
        # The interval holding the point; the last one also owns fmax.
        owns_top = hz >= fmax and f2 >= fmax and f1 < fmax
        if not ((f1 <= hz < f2) or owns_top):
            continue
        s1, s2 = density[index], density[index + 1]
        if f2 == f1:
            fraction = 0.0
        elif sweep in ("dec", "oct") and f1 > 0 and hz > 0:
            fraction = math.log(hz / f1) / math.log(f2 / f1)
        else:
            fraction = (hz - f1) / (f2 - f1)
        total += s1 + (s2 - s1) * fraction
        hit = True
    return total if hit else None


def band_rms(
    freqs: list[float], density: list[float], lo: float, hi: float
) -> float | None:
    """``sqrt(integral of density**2 df)`` over ``[lo, hi]``: trapezoidal on
    the swept grid with both band edges interpolated linearly in the squared
    density. ``0.0`` when the band and the grid do not overlap."""
    if len(freqs) < 2:
        return None
    total = 0.0
    for index in range(len(freqs) - 1):
        f1, f2 = freqs[index], freqs[index + 1]
        if f2 <= f1:
            continue
        # Interval clipped to the band; an empty one contributes nothing.
        a = f1 if f1 > lo else lo
        b = f2 if f2 < hi else hi
        if b <= a:
            continue
        p1, p2 = density[index] ** 2, density[index + 1] ** 2
        pa = p1 + (p2 - p1) * (a - f1) / (f2 - f1)
        pb = p1 + (p2 - p1) * (b - f1) / (f2 - f1)
        total += (pa + pb) / 2 * (b - a)
    return math.sqrt(total) if total > 0 else 0.0


def reduce_density(
    body: dict[str, Any], sweep: str, freqs: list[float], density: list[float]
) -> float | None:
    """``body``'s own reduction (``at_hz`` spot or ``band_hz`` rms) of one
    amplitude-density column."""
    if "at_hz" in body:
        return spot_density(freqs, density, float(body["at_hz"]), sweep)
    lo, hi = (float(edge) for edge in body["band_hz"])
    return band_rms(freqs, density, lo, hi)


def emittable_exprs(expr_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of ``sim_validate._expr_measurements`` that actually has an
    ``expr`` for the deck to emit a ``let``/``print`` pair for.

    The two differ for exactly one form: a ``noise`` entry declaring a
    ``mechanism`` (issue #5553). It counts as an ``expr`` measurement
    everywhere that decides *engine support* -- it needs ngspice's control
    block just as much, so ``engine: "xyce"`` must still refuse it -- but it is
    reduced in Python after the run rather than lowered, so there is nothing to
    emit for it."""
    return [spec for spec in expr_specs if spec.get("expr") is not None]


def alias_lines(
    analysis: dict[str, Any], expr_specs: list[dict[str, Any]]
) -> list[str]:
    """Issue #2893: after ``noise``, ngspice's current plot is the integrated
    one (``noise2``: ``onoise_total``/``inoise_total``); the spectra live in
    ``noise1``. Alias them into the current plot by bare name so an ``expr``
    resolves either, leaving the totals bare. Emitted after any rawfile
    ``write``, so that artifact is unchanged. No lines without an ``expr``."""
    if not expr_specs or analysis.get("kind") != "noise":
        return []
    return [f"let {vec} = noise1.{vec}" for vec in NOISE_SPECTRUM_VECTORS]
