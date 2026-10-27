"""``measurements[].eye`` for ``klt sim`` (issue #4292).

A ``measurements[]`` entry declared with ``eye`` folds a transient signal onto
one unit interval and reports the eye opening as scalars, replacing the
hand-built windowed ``vecmin`` / pairwise-max ``expr`` chain::

    {"name": "eye_rx",
     "eye": {"signal": "v(rx)", "ui": "100p", "uis": 64, "from": "10n"},
     "limits": {"min": 0.1}}

The reduction runs in Python inside the corner run, from a small ASCII rawfile
the generated ``.control`` block writes after the analysis (the same boundary
as :mod:`klayout_tools.sim_crossings`), so ``local`` and ``batch`` workers
return the same scalars and only the reduced numbers leave the corner::

    let __klt_eye_<k> = <signal>
    write <corner_dir>/eye_<k>.raw __klt_eye_<k>

Method
------
The window ``[from, from + uis * ui]`` holds ``uis`` unit intervals. The
decision ``level`` defaults to the midpoint of the signal's min and max inside
the window. Every (linearly interpolated) crossing of the level is folded to
its phase within a unit interval. The **width** is the widest circular gap
between folded crossing phases -- the unit interval minus the crossing spread,
i.e. the horizontal opening at the level -- and the **best phase** is the
middle of that gap. The **height** is read at the best phase: the minimum of
the samples above the level minus the maximum of those at or below it, one
sample per unit interval. The data pattern is not known, so decisions are
threshold decisions at the sampling phase; a signal that never crosses the
level, or sits on one side of it at the best phase, is an error rather than a
zero.

Response shape (additive, no ``schema_version`` bump)
-----------------------------------------------------
``value`` is the ``metric`` the entry selects (``height`` by default; also
``width`` or ``best_phase``), so ``limits``/``plausible_range``/Monte Carlo
statistics grade it like any scalar. A sibling ``eye`` object carries
``{height, width, best_phase, level, ui, uis}``. Every failure is
``status: "error"``, ``value: null`` with a ``code: "measurement"`` diagnostic
whose message starts with a ``[<reason>]`` token from :data:`ERROR_CODES`.
"""

from __future__ import annotations

import bisect
import math
import os
from dataclasses import dataclass
from typing import Any

from . import sim_crossings
from .sim_errors import SimError

#: Reason tokens a failed ``eye`` entry's diagnostic message leads with.
ERROR_CODES = (
    "eye_vector_missing",
    "eye_window_outside_transient",
    "eye_no_levels",
)

#: Values ``eye.metric`` may take: which scalar becomes ``value``.
METRICS = ("height", "width", "best_phase")

MAX_UIS = 1_000_000

_ALLOWED_KEYS = frozenset({"signal", "ui", "uis", "from", "level", "metric"})

_VECTOR_PREFIX = "__klt_eye_"

_FORMS = (
    "spice",
    "expr",
    "crossings",
    "cycle_mean",
    "energy",
    "noise",
    "transfer",
    "tian",
    "snm",
    "search",
)


def is_eye(spec: dict[str, Any]) -> bool:
    """Whether a ``measurements[]`` entry is declared with ``eye``."""
    return spec.get("eye") is not None


def eye_measurements(measurements_spec: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``eye`` entries, in declaration order -- also the order their deck
    lines and rawfiles are numbered in."""
    return [spec for spec in measurements_spec if is_eye(spec)]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def validate_form_exclusive(spec: dict[str, Any]) -> bool:
    """For an entry declaring ``eye``: refuse any other form beside it and
    validate the body. Returns ``True`` when the entry is an eye entry."""
    if not is_eye(spec):
        return False
    name = spec.get("name")
    if name is None:
        raise SimError("each request.measurements[] entry requires 'name'")
    others = [form for form in _FORMS if spec.get(form) is not None]
    if others:
        raise SimError(
            f"measurement '{name}' declares 'eye' together with "
            f"'{others[0]}': they are alternative forms of a measurement -- "
            "declare exactly one (use a separate measurements[] entry for "
            "the other)"
        )
    validate_eye_spec(name, spec["eye"])
    return True


def _seconds(name: Any, body: dict[str, Any], key: str) -> float | None:
    return sim_crossings._window_bound(name, body, key)


def validate_eye_spec(name: Any, body: Any) -> None:
    """Validate one ``measurements[].eye`` object, raising
    :class:`~klayout_tools.sim.SimError` with a corrective message."""
    if not isinstance(body, dict):
        raise SimError(
            f"measurement '{name}': 'eye' must be an object, e.g. "
            '{"signal": "v(rx)", "ui": "100p", "uis": 64}'
        )
    unknown = sorted(set(body) - _ALLOWED_KEYS)
    if unknown:
        raise SimError(
            f"measurement '{name}': unknown eye key(s) {unknown}; "
            f"allowed: {sorted(_ALLOWED_KEYS)}"
        )
    sim_crossings._validate_signal(name, body.get("signal"))
    ui = _seconds(name, body, "ui") if body.get("ui") is not None else None
    if ui is None or ui <= 0:
        raise SimError(
            f"measurement '{name}': eye.ui is required and must be a positive "
            "unit interval in seconds -- a number or a SPICE time literal "
            f'such as "100p" (got {body.get("ui")!r})'
        )
    uis = body.get("uis")
    if not isinstance(uis, int) or isinstance(uis, bool) or not 2 <= uis <= MAX_UIS:
        raise SimError(
            f"measurement '{name}': eye.uis is required and must be an "
            f"integer number of unit intervals in [2, {MAX_UIS}] -- a single "
            f"UI cannot show both rails (got {uis!r})"
        )
    if body.get("level") is not None:
        sim_crossings._validate_level(name, body["level"])
    metric = body.get("metric", "height")
    if metric not in METRICS:
        raise SimError(
            f"measurement '{name}': eye.metric must be one of "
            f"{list(METRICS)} (got {metric!r})"
        )
    if body.get("from") is not None:
        start = _seconds(name, body, "from")
        if start is not None and start < 0:
            raise SimError(
                f"measurement '{name}': eye.from must not be negative (got {start!r} s)"
            )


def validate_eye_context(
    measurements_spec: list[dict[str, Any]], analysis: dict[str, Any]
) -> None:
    """An eye is a transient-time quantity: refuse any other ``analysis.kind``
    by name, and -- when the transient's stop time is readable from
    ``analysis.args`` -- a window that runs past it, up front."""
    from .sim import _parse_tran_window

    declared = eye_measurements(measurements_spec)
    if not declared:
        return
    kind = str(analysis.get("kind", "")).lower()
    if kind != "tran":
        raise SimError(
            f"measurement '{declared[0]['name']}' declares 'eye', which folds "
            "a transient signal onto a unit interval and so requires "
            f'analysis.kind "tran" (got {analysis.get("kind")!r})'
        )
    parsed = _parse_tran_window(analysis)
    if parsed is None:
        return
    stop = parsed[3]
    for spec in declared:
        end = _window_end(spec["name"], spec["eye"])
        if end > stop * (1 + 1e-9):
            raise SimError(
                f"measurement '{spec['name']}': the eye window ends at "
                f"{end!r} s but the transient stops at {stop!r} s"
            )


def _window_end(name: Any, body: dict[str, Any]) -> float:
    start = _seconds(name, body, "from") or 0.0
    return start + body["uis"] * float(_seconds(name, body, "ui") or 0.0)


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def rawfile_path(corner_dir: str, index: int) -> str:
    """Where eye measurement number ``index`` writes its rawfile."""
    return os.path.join(corner_dir, f"eye_{index}.raw")


def control_lines(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> list[str]:
    """One ``let`` plus one ``write`` per eye entry, run after the analysis.
    Empty without an eye entry (deck byte-identical to before)."""
    lines: list[str] = []
    for index, spec in enumerate(eye_measurements(measurements_spec)):
        vector = f"{_VECTOR_PREFIX}{index}"
        lines.append(f"let {vector} = {spec['eye']['signal']}")
        lines.append(f"write {rawfile_path(corner_dir, index)} {vector}")
    return lines


# --------------------------------------------------------------------------- #
# Reduction
# --------------------------------------------------------------------------- #


@dataclass
class EyeOutcome:
    """One eye measurement's result, or why there is none."""

    height: float | None = None
    width: float | None = None
    best_phase: float | None = None
    level: float | None = None
    ui: float = 0.0
    uis: int = 0
    metric: str = "height"
    error: str | None = None
    code: str | None = None

    @property
    def value(self) -> float | None:
        if self.height is None:
            return None
        return float(getattr(self, self.metric))

    def report(self) -> dict[str, Any]:
        """The response's ``eye`` object for this measurement."""
        return {
            "height": self.height,
            "width": self.width,
            "best_phase": self.best_phase,
            "level": self.level,
            "ui": self.ui,
            "uis": self.uis,
        }


def _value_at(scale: list[float], values: list[float], t: float) -> float:
    index = bisect.bisect_left(scale, t)
    if index <= 0:
        return values[0]
    if index >= len(scale):
        return values[-1]
    t0, t1 = scale[index - 1], scale[index]
    if t1 == t0:
        return values[index]
    return values[index - 1] + (values[index] - values[index - 1]) * (
        (t - t0) / (t1 - t0)
    )


def compute_eye(
    scale: list[float],
    values: list[float],
    *,
    ui: float,
    uis: int,
    start: float = 0.0,
    level: float | None = None,
    metric: str = "height",
) -> EyeOutcome:
    """The eye opening of ``values`` over ``scale``, or an outcome carrying
    ``code``/``error``. ``scale`` must be ascending."""
    base = EyeOutcome(ui=ui, uis=uis, metric=metric)
    end = start + uis * ui
    if (
        not scale
        or scale[0] > start * (1 + 1e-12) + 1e-18
        or scale[-1] < end * (1 - 1e-9)
    ):
        base.code = "eye_window_outside_transient"
        base.error = (
            f"the eye window [{start!r}, {end!r}] s is not inside the "
            f"transient's sampled span [{scale[0] if scale else 0.0!r}, "
            f"{scale[-1] if scale else 0.0!r}] s"
        )
        return base
    inside = [v for t, v in zip(scale, values, strict=True) if start <= t <= end]
    inside += [_value_at(scale, values, start), _value_at(scale, values, end)]
    threshold = (min(inside) + max(inside)) / 2 if level is None else float(level)
    crossings = sim_crossings.find_crossings(
        scale,
        values,
        threshold,
        edge="either",
        start=start,
        stop=end,
        max_count=len(scale),
    )
    if not crossings.times:
        base.code = "eye_no_levels"
        base.error = (
            f"the signal never crosses the decision level {threshold!r} "
            "inside the window -- there is no transition to fold"
        )
        return base
    folded = sorted(((t - start) / ui) % 1.0 for t in crossings.times)
    # Gap after each folded phase; the last one wraps to the first (a lone
    # crossing leaves the whole UI open).
    gaps = [b - a for a, b in zip(folded, folded[1:], strict=False)]
    gaps.append(folded[0] + 1.0 - folded[-1])
    widest = max(range(len(gaps)), key=gaps.__getitem__)
    best_phase = (folded[widest] + gaps[widest] / 2) % 1.0
    samples = [
        _value_at(scale, values, start + (k + best_phase) * ui) for k in range(uis)
    ]
    high = [v for v in samples if v > threshold]
    low = [v for v in samples if v <= threshold]
    if not high or not low:
        base.code = "eye_no_levels"
        base.error = (
            f"at the best sampling phase every unit interval is on one side "
            f"of the decision level {threshold!r} -- no high/low pair of rails"
        )
        return base
    base.height = min(high) - max(low)
    base.width = gaps[widest] * ui
    base.best_phase = best_phase * ui
    base.level = threshold
    return base


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_corner_eyes(
    measurements_spec: list[dict[str, Any]], corner_dir: str
) -> dict[int, EyeOutcome]:
    """``{position in measurements_spec: outcome}`` for every eye entry of one
    finished corner. Never raises: any rawfile problem becomes that
    measurement's own ``error``."""
    outcomes: dict[int, EyeOutcome] = {}
    index = 0
    for position, spec in enumerate(measurements_spec):
        if not is_eye(spec):
            continue
        outcomes[position] = _extract_one(spec, rawfile_path(corner_dir, index))
        index += 1
    return outcomes


def _extract_one(spec: dict[str, Any], raw_path: str) -> EyeOutcome:
    from .sim import parse_ascii_rawfile

    body = spec["eye"]
    name = spec["name"]
    if not os.path.isfile(raw_path):
        return EyeOutcome(
            code="eye_vector_missing",
            error=(
                f"measurement '{name}': its signal ({body['signal']!r}) did "
                "not resolve against this transient's saved vectors (no "
                "rawfile was written) -- check the node/branch name"
            ),
        )
    try:
        waveform = parse_ascii_rawfile(raw_path)
    except (SimError, ValueError) as exc:
        return EyeOutcome(
            code="eye_vector_missing",
            error=f"measurement '{name}': could not read its eye rawfile: {exc}",
        )
    points = waveform["points"]
    if len(waveform["variables"]) < 2 or not points:
        return EyeOutcome(
            code="eye_vector_missing",
            error=f"measurement '{name}': its signal yielded no samples",
        )
    outcome = compute_eye(
        [point[0] for point in points],
        [point[1] for point in points],
        ui=float(_seconds(name, body, "ui") or 0.0),
        uis=body["uis"],
        start=_seconds(name, body, "from") or 0.0,
        level=body.get("level"),
        metric=body.get("metric", "height"),
    )
    if outcome.error is not None:
        outcome.error = f"measurement '{name}': {outcome.error}"
    elif outcome.value is None or not math.isfinite(outcome.value):
        return EyeOutcome(
            code="eye_vector_missing",
            error=f"measurement '{name}': the eye metric is not finite",
        )
    return outcome
