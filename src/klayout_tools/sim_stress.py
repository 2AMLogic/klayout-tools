"""Worst-case voltage stress over a transient window for ``klt sim`` (issue #4592).

A ``measurements[]`` entry declared with ``stress`` reduces the voltage
across a node pair to ``max |v(a) - v(b)|`` over a settled window -- the
quantity a device-rating check needs, with the edge-charging transient left
out -- from one declaration instead of a hand-written ``expr``/``FIND ... AT=``
per pair::

    {"name": "vstress_md1",
     "stress": {"a": "d1", "b": "s1", "from": "20n", "to": "90n"},
     "unit": "V", "limits": {"max": 5.5}}

Like ``energy`` (:mod:`klayout_tools.sim_energy`, issue #3033) the reduction
happens inside the corner run, in Python, from a small ASCII rawfile the
generated ``.control`` block writes right after the analysis::

    let __klt_vs_<k> = v(<a>) - v(<b>)
    write <corner_dir>/stress_<k>.raw __klt_vs_<k>

The difference is sampled at the simulator's timepoints and taken piecewise
linear between them; ``|.|`` of a linear segment peaks at an end point, so the
maximum over the window is the larger of the interior samples and the
interpolated window edges.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
``value`` stays a **number** (the worst |V|), so ``limits``/``plausible_range``
/Monte Carlo grade it like any scalar. A sibling ``stress`` object
``{from, to, peak_time}`` rides that ``corners[].measurements[]`` entry only.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

from ._jsonnum import is_number
from .sim_errors import SimError

#: Keys a ``stress`` object may carry. Anything else is refused by name.
_ALLOWED_KEYS = frozenset({"a", "b", "from", "to"})

#: Prefix of the ngspice vector each stress measurement is bound to in the
#: generated deck -- never a caller-visible name.
_VECTOR_PREFIX = "__klt_vs_"

#: A node name as `v(<node>)` can address it: no whitespace, parentheses or
#: commas that would break out of the `let` command.
_NODE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$#:/\[\]<>!+-]*$")

#: Slack, as a fraction of the analysis window, when checking that a
#: requested edge lies inside the saved samples.
_EDGE_TOLERANCE = 1e-9


def is_stress(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``stress``."""
    return spec.get("stress") is not None


def stress_measurements(
    measurements_spec: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``stress`` entries, in declaration order -- which is also the
    order their deck lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_stress(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_stress_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].stress`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'stress' must be an object, e.g. "
            '{"a": "d1", "b": "s1"}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown stress key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    a = body.get("a")
    if not isinstance(a, str) or not _NODE_RE.match(a):
        raise SimError(
            f"measurement '{name}': stress.a is required -- the name of a "
            f'circuit node, e.g. "d1" (got {a!r})'
        )
    b = body.get("b")
    if b is not None and (not isinstance(b, str) or not _NODE_RE.match(b)):
        raise SimError(
            f"measurement '{name}': stress.b must be a circuit node name "
            f"(omit it to measure against ground; got {b!r})"
        )
    start = _window_bound(name, body, "from")
    stop = _window_bound(name, body, "to")
    if start is not None and stop is not None and stop <= start:
        raise SimError(
            f"measurement '{name}': stress.to ({stop!r} s) must be after "
            f"stress.from ({start!r} s)"
        )


def _window_bound(name: Any, body: dict[str, Any], key: str) -> float | None:
    """``stress.from``/``.to`` in seconds: a number, or a SPICE time literal
    such as ``"20n"``. ``None`` when absent."""
    from .sim import _parse_spice_seconds

    raw = body.get(key)
    if raw is None:
        return None
    seconds: float | None = None
    if is_number(raw):
        seconds = float(raw)
    elif isinstance(raw, str):
        seconds = _parse_spice_seconds(raw)
    if seconds is None or not math.isfinite(seconds):
        raise SimError(
            f"measurement '{name}': stress.{key} must be a time in seconds "
            f'-- a number or a SPICE time literal such as "20n" (got {raw!r})'
        )
    return seconds


def validate_stress_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """A stress window is a transient quantity: refuse any other
    ``analysis.kind`` by name, and a window that is not inside the ``tran``
    analysis (``tstart``..``tstop``) when those tokens can be parsed."""
    from .sim import _parse_spice_seconds

    declared = stress_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'stress', which "
            "reduces a node-pair voltage over a time window and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )
    tokens = str(analysis.get("args") or "").split()
    tstop = _parse_spice_seconds(tokens[1]) if len(tokens) > 1 else None
    tstart = _parse_spice_seconds(tokens[2]) if len(tokens) > 2 else 0.0
    if tstop is None or tstart is None:
        return
    for spec in declared:
        for key in ("from", "to"):
            bound = _window_bound(spec["name"], spec["stress"], key)
            if bound is not None and not (
                tstart - _EDGE_TOLERANCE * tstop
                <= bound
                <= tstop * (1 + _EDGE_TOLERANCE)
            ):
                raise SimError(
                    f"measurement '{spec['name']}': stress.{key} ({bound!r} s)"
                    f" is outside the transient analysis window "
                    f"[{tstart!r}, {tstop!r}] s (analysis.args "
                    f"{analysis.get('args')!r})"
                )


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where stress measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"stress_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """The ``.control`` lines that dump each stress measurement's voltage
    difference to its own ASCII rawfile, run after the analysis. One file per
    measurement, so a node that does not resolve costs only its own
    measurement. Empty when no stress entry is declared (deck
    byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(stress_measurements(measurements_spec)):
        body = spec["stress"]
        vec = f"{_VECTOR_PREFIX}{index}"
        expr = f"v({body['a']})"
        if body.get("b") is not None:
            expr += f" - v({body['b']})"
        lines.append(f"let {vec} = {expr}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {vec}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class StressOutcome:
    """One stress measurement's reduced result, or why there is none."""

    peak: float = 0.0
    peak_time: float = 0.0
    start: float = 0.0
    stop: float = 0.0
    error: str | None = None

    def report(self) -> dict[str, Any]:
        """The response's ``stress`` object for this measurement."""
        return {"from": self.start, "to": self.stop, "peak_time": self.peak_time}


def reduce_stress(
    scale: list[float],
    diff: list[float],
    *,
    start: float | None = None,
    stop: float | None = None,
) -> StressOutcome:
    """``max |diff|`` over ``[start, stop]`` (default: the whole of
    ``scale``), counting the interior samples and the interpolated window
    edges. Requires ``scale`` ascending with at least two samples and a
    window inside it."""
    count = min(len(scale), len(diff))
    if count < 2:
        return StressOutcome(error="fewer than two samples")
    lo = scale[0] if start is None else start
    hi = scale[count - 1] if stop is None else stop
    slack = _EDGE_TOLERANCE * max(abs(scale[count - 1] - scale[0]), abs(hi), 1e-30)
    if lo < scale[0] - slack or hi > scale[count - 1] + slack:
        return StressOutcome(
            error=(
                f"window [{lo!r}, {hi!r}] s is outside the saved samples "
                f"[{scale[0]!r}, {scale[count - 1]!r}] s"
            )
        )
    if not hi > lo:
        return StressOutcome(error=f"window [{lo!r}, {hi!r}] s is empty")
    candidates: list[tuple[float, float]] = []
    for index in range(1, count):
        t0, t1 = scale[index - 1], scale[index]
        a, b = max(t0, lo), min(t1, hi)
        if b < a or t1 <= t0:
            continue
        for t in (a, b):
            value = diff[index - 1] + (diff[index] - diff[index - 1]) * (t - t0) / (
                t1 - t0
            )
            candidates.append((abs(value), t))
    if not candidates:
        return StressOutcome(error=f"window [{lo!r}, {hi!r}] s holds no samples")
    peak, peak_time = max(candidates, key=lambda c: c[0])
    return StressOutcome(peak=peak, peak_time=peak_time, start=lo, stop=hi)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_stress(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, StressOutcome]:
    """``{position in measurements_spec: outcome}`` for every stress entry of
    one finished corner, read from the rawfiles :func:`control_lines` asked
    ngspice to write. Never raises: an unreadable or missing rawfile becomes
    that measurement's own ``error``."""
    outcomes: dict[int, StressOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_stress(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> StressOutcome:
    from .sim import SimError, parse_ascii_rawfile

    body = spec["stress"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return StressOutcome(
            error=(
                f"measurement '{name}' produced no stress: node "
                f"{body['a']!r} or {body.get('b')!r} did not resolve against "
                "this transient's saved vectors (no rawfile was written) -- "
                "check that the nodes exist in the deck"
            )
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return StressOutcome(
            error=f"measurement '{name}': could not read its stress rawfile: {exc}"
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 2 or not points:
        return StressOutcome(
            error=f"measurement '{name}' produced no stress: the voltage "
            "vector yielded no samples"
        )
    outcome = reduce_stress(
        [point[0] for point in points],
        [point[1] for point in points],
        start=_window_bound(name, body, "from"),
        stop=_window_bound(name, body, "to"),
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    return outcome
