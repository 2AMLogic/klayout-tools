"""Per-source energy over a transient window for ``klt sim`` (issue #3033).

A ``measurements[]`` entry declared with ``energy`` reduces one independent
source's transient to the energy it exchanged over a window -- the quantity an
energy or standby-power study needs -- from one declaration, instead of three
behavioural ``B`` sources and four ``.meas ... INTEG`` cards per source::

    {"name": "e_vdd",
     "energy": {"source": "vdd", "voltage": "v(a)",
                "from": "5n", "to": "34n"}}

How it is computed, and why there
---------------------------------
Like ``crossings`` (:mod:`klayout_tools.sim_crossings`, issue #2868), the
reduction happens **inside the corner run**, in Python, from a small ASCII
rawfile the generated ``.control`` block writes right after the analysis::

    let __klt_en_<k>_v = <voltage>
    let __klt_en_<k>_i = i(<source>)
    write <corner_dir>/energy_<k>.raw __klt_en_<k>_v __klt_en_<k>_i

So the reduced numbers are all that leave the corner (``keep_artifacts:
false`` and ``remote``/``batch`` workers need no waveform artifact), and **no
behavioural power node is added to the deck**: a scaled power node (for
example ``v = -v(a)*i(vs)*1e12`` to keep femtowatt hold power above
``vntol``) can make ngspice abort at the *initial* timepoint ("Timestep too
small; initial timepoint: trouble with node ..."), which this form cannot
trigger.

Sign convention and integration method
--------------------------------------
SPICE reports the current of an independent source as positive *into its
positive terminal*, so the power the source **delivers** is ``p = -v * i``
(``p > 0``: delivering, ``p < 0``: absorbing/recovering). Over the window
``[from, to]``:

- ``gross``     = integral of ``max(p, 0)``  (energy delivered),
- ``recovered`` = integral of ``max(-p, 0)`` (energy returned to the source),
- ``net``       = ``gross - recovered`` = integral of ``p``,
- ``charge``    = integral of ``i`` (so ``net = -V * charge`` for a DC source).

``p`` is sampled at the simulator's timepoints and taken **piecewise linear**
between them; the window edges are interpolated, and a segment that changes
sign is split at the zero so ``gross`` and ``recovered`` are exact for that
model (trapezoidal integration). The product of two sampled vectors is not
itself linear between samples, so the result carries an O(timestep^2) error
against the true integral -- it is not exact.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
The measurement's ``value`` stays a **number**: the net energy in joules, so
``limits``/``plausible_range``/Monte Carlo grade it like any scalar. The
breakdown rides a new sibling ``energy`` object on that
``corners[].measurements[]`` entry only, present only when it was extracted.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

#: Keys an ``energy`` object may carry. Anything else is refused by name.
_ALLOWED_KEYS = frozenset({"source", "voltage", "from", "to"})

#: Prefix of the ngspice vectors each energy measurement is bound to in the
#: generated deck -- never a caller-visible name.
_VECTOR_PREFIX = "__klt_en_"

#: An independent-source instance name as `i(<source>)` can address it.
_SOURCE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$#]*$")

#: Slack, as a fraction of the analysis window, when checking that a
#: requested edge lies inside the saved samples (float round-off in the
#: simulator's own final timepoint).
_EDGE_TOLERANCE = 1e-9


def _error(message: str) -> Exception:
    from .sim import SimError

    return SimError(message)


def is_energy(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``energy``."""
    return spec.get("energy") is not None


def energy_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``energy`` entries, in declaration order -- which is also the
    order their deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_energy(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_energy_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].energy`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise _error(
            f"measurement '{name}': 'energy' must be an object, e.g. "
            '{"source": "vdd", "voltage": "v(a)"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise _error(
            f"measurement '{name}': unknown energy key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    source = body.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise _error(
            f"measurement '{name}': energy.source is required -- the name of "
            f'an independent voltage source in the deck, e.g. "vdd" (got '
            f"{source!r})"
        )
    voltage = body.get("voltage")
    if not isinstance(voltage, str) or not voltage.strip():
        raise _error(
            f"measurement '{name}': energy.voltage is required -- the "
            "source's positive-terminal voltage vector relative to ground, "
            'e.g. "v(a)"'
        )
    if "\n" in voltage or "\r" in voltage:
        raise _error(
            f"measurement '{name}': energy.voltage must be a single line (it "
            "is spliced into one `let` command of the generated .control "
            "block)"
        )
    start = _window_bound(name, body, "from")
    stop = _window_bound(name, body, "to")
    if start is not None and stop is not None and stop <= start:
        raise _error(
            f"measurement '{name}': energy.to ({stop!r} s) must be after "
            f"energy.from ({start!r} s)"
        )


def _window_bound(name: Any, body: dict[str, Any], key: str) -> float | None:
    """``energy.from``/``.to`` in seconds: a number, or a SPICE time literal
    such as ``"5n"``. ``None`` when absent."""
    from .sim import _parse_spice_seconds

    raw = body.get(key)
    if raw is None:
        return None
    seconds: float | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        seconds = float(raw)
    elif isinstance(raw, str):
        seconds = _parse_spice_seconds(raw)
    if seconds is None or not math.isfinite(seconds):
        raise _error(
            f"measurement '{name}': energy.{key} must be a time in seconds "
            f'-- a number or a SPICE time literal such as "5n" (got {raw!r})'
        )
    return seconds


def validate_energy_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """Energy over a window is a transient quantity: refuse any other
    ``analysis.kind`` by name, and a window that is not inside the ``tran``
    analysis (``tstart``..``tstop``) when those tokens can be parsed."""
    from .sim import _parse_spice_seconds

    declared = energy_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise _error(
            f"measurement '{declared[0]['name']}' declares 'energy', which "
            "integrates power over a time window and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )
    tokens = str(analysis.get("args") or "").split()
    tstop = _parse_spice_seconds(tokens[1]) if len(tokens) > 1 else None
    tstart = _parse_spice_seconds(tokens[2]) if len(tokens) > 2 else 0.0
    if tstop is None or tstart is None:
        return
    for spec in declared:
        for key in ("from", "to"):
            bound = _window_bound(spec["name"], spec["energy"], key)
            if bound is not None and not (
                tstart - _EDGE_TOLERANCE * tstop
                <= bound
                <= tstop * (1 + _EDGE_TOLERANCE)
            ):
                raise _error(
                    f"measurement '{spec['name']}': energy.{key} ({bound!r} s)"
                    f" is outside the transient analysis window "
                    f"[{tstart!r}, {tstop!r}] s (analysis.args "
                    f"{analysis.get('args')!r})"
                )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where energy measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"energy_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump each energy measurement's voltage and
    source-current vectors to its own ASCII rawfile, run after the analysis.
    One file per measurement, so a source that does not resolve costs only its
    own measurement. Empty when no energy entry is declared (deck
    byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(energy_measurements(measurements_spec)):
        body = spec["energy"]
        v_vec = f"{_VECTOR_PREFIX}{index}_v"
        i_vec = f"{_VECTOR_PREFIX}{index}_i"
        lines.append(f"let {v_vec} = {body['voltage']}")
        lines.append(f"let {i_vec} = i({body['source']})")
        lines.append(f"write {rawfile_path(corner_dir, index)} {v_vec} {i_vec}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class EnergyOutcome:
    """One energy measurement's reduced result, or why there is none."""

    net: float = 0.0
    gross: float = 0.0
    recovered: float = 0.0
    charge: float = 0.0
    start: float = 0.0
    stop: float = 0.0
    error: str | None = None

    def report(self) -> dict[str, Any]:
        """The response's ``energy`` object for this measurement."""
        return {
            "net": self.net,
            "gross": self.gross,
            "recovered": self.recovered,
            "charge": self.charge,
            "from": self.start,
            "to": self.stop,
        }


def integrate_energy(
    scale: list[float],
    voltage: list[float],
    current: list[float],
    *,
    start: float | None = None,
    stop: float | None = None,
) -> EnergyOutcome:
    """Net/gross/recovered energy and charge of a source over ``[start,
    stop]`` (default: the whole of ``scale``), by the piecewise-linear
    trapezoidal rule described in the module docstring. Requires ``scale``
    ascending with at least two samples and a window inside it."""
    count = min(len(scale), len(voltage), len(current))
    if count < 2:
        return EnergyOutcome(error="fewer than two samples")
    lo = scale[0] if start is None else start
    hi = scale[count - 1] if stop is None else stop
    slack = _EDGE_TOLERANCE * max(abs(scale[count - 1] - scale[0]), abs(hi), 1e-30)
    if lo < scale[0] - slack or hi > scale[count - 1] + slack:
        return EnergyOutcome(
            error=(
                f"window [{lo!r}, {hi!r}] s is outside the saved samples "
                f"[{scale[0]!r}, {scale[count - 1]!r}] s"
            )
        )
    if not hi > lo:
        return EnergyOutcome(error=f"window [{lo!r}, {hi!r}] s is empty")
    outcome = EnergyOutcome(start=lo, stop=hi)
    for index in range(1, count):
        t0, t1 = scale[index - 1], scale[index]
        a, b = max(t0, lo), min(t1, hi)
        if b <= a or t1 <= t0:
            continue
        i_a = _lerp(t0, t1, current[index - 1], current[index], a)
        i_b = _lerp(t0, t1, current[index - 1], current[index], b)
        v_a = _lerp(t0, t1, voltage[index - 1], voltage[index], a)
        v_b = _lerp(t0, t1, voltage[index - 1], voltage[index], b)
        outcome.charge += 0.5 * (i_a + i_b) * (b - a)
        _add_power_segment(outcome, a, b, -v_a * i_a, -v_b * i_b)
    outcome.net = outcome.gross - outcome.recovered
    return outcome


def _lerp(t0: float, t1: float, y0: float, y1: float, t: float) -> float:
    return y0 + (y1 - y0) * (t - t0) / (t1 - t0)


def _add_power_segment(
    outcome: EnergyOutcome, a: float, b: float, p_a: float, p_b: float
) -> None:
    """Accumulate one segment of piecewise-linear power, split at a sign
    change so the positive and negative lobes land in ``gross`` and
    ``recovered`` respectively."""
    if p_a * p_b < 0:
        mid = a + (b - a) * p_a / (p_a - p_b)
        _add_power_segment(outcome, a, mid, p_a, 0.0)
        _add_power_segment(outcome, mid, b, 0.0, p_b)
        return
    area = 0.5 * (p_a + p_b) * (b - a)
    if area >= 0:
        outcome.gross += area
    else:
        outcome.recovered -= area


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_energy(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, EnergyOutcome]:
    """``{position in measurements_spec: outcome}`` for every energy entry of
    one finished corner, read from the rawfiles :func:`control_lines` asked
    ngspice to write. Never raises: an unreadable or missing rawfile becomes
    that measurement's own ``error``."""
    outcomes: dict[int, EnergyOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_energy(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> EnergyOutcome:
    from .sim import SimError, parse_ascii_rawfile

    body = spec["energy"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return EnergyOutcome(
            error=(
                f"measurement '{name}' produced no energy: source "
                f"{body['source']!r} or voltage {body['voltage']!r} did not "
                "resolve against this transient's saved vectors (no rawfile "
                "was written) -- check that the deck has an independent "
                "source of that name and that the voltage node exists"
            )
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return EnergyOutcome(
            error=f"measurement '{name}': could not read its energy rawfile: {exc}"
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 3 or not points:
        return EnergyOutcome(
            error=f"measurement '{name}' produced no energy: the source "
            "current / voltage vectors yielded no samples"
        )
    outcome = integrate_energy(
        [point[0] for point in points],
        [point[1] for point in points],
        [point[2] for point in points],
        start=_window_bound(name, body, "from"),
        stop=_window_bound(name, body, "to"),
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    return outcome
