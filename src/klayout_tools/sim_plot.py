"""Prepare measurement annotations and render waveform SVGs for ``klt sim --plot``.

Issue #56 (waveform post-processing / threshold-crossing extraction) is the
*numeric* half of this gap; this module is the visual half, deliberately
kept separate (see #1723's "Idea"/"Non-goals" sections). When a `.meas`
misses, an agent gets the number that missed and nothing else -- for a
non-starting oscillator, a sticking comparator, or a saturating integrator,
the *shape* of the waveform is the actual diagnosis. `klt sim --plot <dir>`
calls :func:`render_waveform_svg` once
per non-sweep signal captured in a corner's waveform artifact.

Same no-dependency, string-built SVG pattern ``trajectory.render_plot_svg``
already uses for the objective-vs-turn plot (issue #56... no, issue #388/#437
-- see that module's docstring) -- no plotting library, deterministic
output, safe to embed via ``<img>`` or open directly in a browser, runnable
in CI. Deliberately a *new*, small function rather than a generalization of
``trajectory.render_plot_svg``: a waveform plot needs log-axis support (for
`ac`) and measurement-window shading (for `tran`) that renderer does not, and
the primitives it would share (axis lines, XML-escaping, number formatting)
are a handful of lines each -- see issue #1723's curator note on this
tradeoff.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from . import _spice_number as spice_number
from . import sim_derive
from .sim_slug import _slugify_corner_id, _slugify_signal

__all__ = [
    "measurement_plot_path",
    "prepare_annotations",
    "render_waveform_svg",
    "write_corner_plots",
]

_PLOT_WIDTH = 640
_PLOT_HEIGHT = 360
_MARGIN_LEFT = 72
_MARGIN_RIGHT = 24
_MARGIN_TOP = 40
_MARGIN_BOTTOM = 48


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_num(value: float) -> str:
    """Format an axis/reference value compactly: an integral value renders
    without a trailing ``.0``; anything else uses 4 significant digits so a
    tiny (``1e-9``) or huge value stays readable without a long decimal
    tail."""
    if value == 0:
        return "0"
    if value == int(value) and abs(value) < 1e6:
        return str(int(value))
    return f"{value:.4g}"


def render_waveform_svg(
    *,
    signal_name: str,
    corner_id: str,
    x_values: list[float],
    y_values: list[float],
    x_axis_label: str,
    log_x: bool = False,
    window: tuple[float, float] | None = None,
    target: float | None = None,
) -> str:
    """Render ``signal_name``'s value-vs-sweep-axis curve as a self-contained
    SVG string.

    ``x_values``/``y_values`` are the already-extracted waveform series --
    the sweep column (``variables[0]``) and one other column, from
    :func:`klayout_tools.sim.parse_ascii_rawfile`'s ``points`` shape.

    ``log_x`` draws a log10-scaled sweep axis (``ac``'s frequency sweep);
    silently falls back to linear if any ``x`` value is not strictly
    positive (log of a non-positive value is undefined) rather than raising,
    since a caller iterating many signals should not have one bad point kill
    the whole plot batch.

    ``window`` -- a ``(from, to)`` pair in the sweep axis's own units --
    shades that span with a translucent rectangle (a ``tran`` measurement's
    ``from=``/``to=`` extraction window, e.g. an ``AVG``/``PP`` measurement's
    integration window). ``target`` draws a horizontal dashed reference line
    at that value (a ``.meas ... WHEN <signal>=<value>`` measurement's search
    target) -- together these are the two pieces of "why did this miss"
    context #1723 asks for on top of the bare curve.

    Raises :class:`ValueError` if ``x_values``/``y_values`` are empty or of
    unequal length -- the caller only invokes this with an already-validated
    waveform series, so this is a defensive assertion, not a user-facing
    error path.
    """
    if not x_values or len(x_values) != len(y_values):
        raise ValueError("x_values/y_values must be non-empty and of equal length")

    if log_x and any(x <= 0 for x in x_values):
        log_x = False

    plot_w = _PLOT_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = _PLOT_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM

    x_min, x_max = min(x_values), max(x_values)
    if log_x:
        log_x_min, log_x_max = math.log10(x_min), math.log10(x_max)
        x_log_span = (log_x_max - log_x_min) or 1.0

    def px(x: float) -> float:
        if log_x:
            return _MARGIN_LEFT + (math.log10(x) - log_x_min) / x_log_span * plot_w
        span = (x_max - x_min) or 1.0
        return _MARGIN_LEFT + (x - x_min) / span * plot_w

    y_lo, y_hi = min(y_values), max(y_values)
    if target is not None:
        y_lo, y_hi = min(y_lo, target), max(y_hi, target)
    if y_lo == y_hi:
        # A perfectly flat signal (the textbook non-starting-oscillator
        # case) has y_lo == y_hi; without padding the curve collapses to a
        # zero-height range at the plot's exact vertical center, which still
        # renders, but leaves no headroom to distinguish the flat curve from
        # a target/reference line drawn at a nearby value. Pad by 10% of the
        # magnitude (or a fixed 1-unit floor for a flat zero).
        pad = max(abs(y_lo) * 0.1, 1.0)
        y_lo, y_hi = y_lo - pad, y_hi + pad
    y_span = (y_hi - y_lo) or 1.0

    def py(y: float) -> float:
        # SVG y grows downward; a higher value should sit higher on the
        # canvas, so invert.
        return _MARGIN_TOP + (y_hi - y) / y_span * plot_h

    points = list(zip(x_values, y_values, strict=True))
    polyline = " ".join(f"{px(x):.2f},{py(y):.2f}" for x, y in points)

    x_axis_y = _MARGIN_TOP + plot_h
    axis_color = "#9ca3af"
    label_color = "#374151"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_PLOT_WIDTH}" '
        f'height="{_PLOT_HEIGHT}" viewBox="0 0 {_PLOT_WIDTH} {_PLOT_HEIGHT}" '
        'font-family="sans-serif">'
    )
    parts.append(
        f'<rect width="{_PLOT_WIDTH}" height="{_PLOT_HEIGHT}" fill="#ffffff"/>'
    )
    parts.append(
        f'<text x="{_PLOT_WIDTH / 2:.0f}" y="20" text-anchor="middle" '
        f'font-size="14" fill="#111827">{_xml_escape(signal_name)} '
        f"@ {_xml_escape(corner_id)}</text>"
    )

    # Measurement window shading -- drawn before the axes/curve so it sits
    # behind everything else.
    if window is not None:
        w_from, w_to = window
        wx0 = max(_MARGIN_LEFT, min(px(w_from), px(w_to)))
        wx1 = min(_MARGIN_LEFT + plot_w, max(px(w_from), px(w_to)))
        if wx1 > wx0:
            parts.append(
                f'<rect x="{wx0:.2f}" y="{_MARGIN_TOP}" width="{wx1 - wx0:.2f}" '
                f'height="{plot_h}" fill="#fde68a" opacity="0.5"/>'
            )

    # Axes.
    parts.append(
        f'<line x1="{_MARGIN_LEFT}" y1="{_MARGIN_TOP}" x2="{_MARGIN_LEFT}" '
        f'y2="{x_axis_y:.0f}" stroke="{axis_color}" stroke-width="1"/>'
    )
    parts.append(
        f'<line x1="{_MARGIN_LEFT}" y1="{x_axis_y:.0f}" '
        f'x2="{_MARGIN_LEFT + plot_w}" y2="{x_axis_y:.0f}" '
        f'stroke="{axis_color}" stroke-width="1"/>'
    )
    # Axis end labels (min/max on each axis).
    parts.append(
        f'<text x="{_MARGIN_LEFT - 6}" y="{_MARGIN_TOP + 4:.0f}" '
        f'text-anchor="end" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(y_hi))}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT - 6}" y="{x_axis_y:.0f}" '
        f'text-anchor="end" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(y_lo))}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT}" y="{x_axis_y + 16:.0f}" '
        f'text-anchor="middle" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(x_min))}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT + plot_w}" y="{x_axis_y + 16:.0f}" '
        f'text-anchor="middle" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(x_max))}</text>"
    )
    x_axis_title = x_axis_label + (" (log)" if log_x else "")
    parts.append(
        f'<text x="{_MARGIN_LEFT + plot_w / 2:.0f}" y="{_PLOT_HEIGHT - 6}" '
        f'text-anchor="middle" font-size="11" fill="{label_color}">'
        f"{_xml_escape(x_axis_title)}</text>"
    )

    # `.meas ... WHEN <signal>=<target>` reference line.
    if target is not None:
        ty = py(target)
        parts.append(
            f'<line x1="{_MARGIN_LEFT}" y1="{ty:.2f}" '
            f'x2="{_MARGIN_LEFT + plot_w}" y2="{ty:.2f}" '
            'stroke="#dc2626" stroke-width="1.5" stroke-dasharray="5,4"/>'
        )
        parts.append(
            f'<text x="{_MARGIN_LEFT + plot_w - 4}" y="{ty - 4:.2f}" '
            f'text-anchor="end" font-size="10" fill="#dc2626">'
            f"target={_xml_escape(_fmt_num(target))}</text>"
        )

    # The signal curve itself, on top of everything.
    parts.append(
        f'<polyline points="{polyline}" fill="none" stroke="#2563eb" stroke-width="2"/>'
    )

    parts.append("</svg>")
    return "".join(parts)


#: The first `v(...)`/`i(...)`-shaped (optionally derivative-prefixed, e.g.
#: `vd(...)`/`ir(...)`) parenthesized token in a `.meas` card's `spice` text
#: -- the signal that card is actually about. ngspice itself always lowercases
#: node/signal names in its rawfile `variables[].name` (see
#: `parse_ascii_rawfile`'s docstring example), so the reconstructed name below
#: is lowercased to match, regardless of how the request's own `.meas` text
#: capitalizes it.
_MEAS_SIGNAL_TOKEN_RE = re.compile(r"\b([a-z]{1,3})\(([^)]*)\)", re.IGNORECASE)

#: `from=<value>` / `to=<value>` trailers on an `AVG`/`RMS`/`PP`/`MIN`/`MAX`/
#: `INTEG` measurement -- the "measurement window" #1723 asks to shade on a
#: `tran` plot.
_MEAS_FROM_RE = re.compile(r"\bfrom\s*=\s*(\S+)", re.IGNORECASE)
_MEAS_TO_RE = re.compile(r"\bto\s*=\s*(\S+)", re.IGNORECASE)

#: `WHEN <signal>=<value>` on a threshold-crossing measurement -- the
#: `.meas` target #1723 asks to draw as a reference line. `\S+` between
#: `when` and the final `=` is deliberately greedy-then-backtracking so it
#: still matches when the signal token itself carries no space before its
#: own `=` (e.g. `when v(out)=1.5`, not `when v(out) = 1.5`).
_MEAS_WHEN_TARGET_RE = re.compile(
    r"\bwhen\s+\S+\s*=\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?[a-zA-Z]*)", re.IGNORECASE
)


def _measurement_diagnostic_hints(spice: str) -> dict[str, Any]:
    """Best-effort extraction of the plotting context a `.meas` card's own
    ``spice`` text implies: which signal it is about, the time window it
    integrates/searches over (``from=``/``to=``), and the threshold value it
    searches for (``WHEN <signal>=<value>``).

    Never raises -- an unrecognised card shape (e.g. a ``TRIG``/``TARG``
    delay measurement, which has no single window or target this simple
    parser understands) yields ``{"signal": None, "window": None, "target":
    None}``, and the caller (:func:`write_corner_plots`) just renders that
    signal's curve without the extra context rather than failing the whole
    plot batch. This is deliberately not a general SPICE expression
    evaluator -- see :mod:`klayout_tools.sim_plot`'s module docstring on
    scope.
    """
    signal_match = _MEAS_SIGNAL_TOKEN_RE.search(spice)
    signal = (
        f"{signal_match.group(1).lower()}({signal_match.group(2).lower()})"
        if signal_match
        else None
    )

    window: tuple[float, float] | None = None
    from_match = _MEAS_FROM_RE.search(spice)
    to_match = _MEAS_TO_RE.search(spice)
    if from_match is not None and to_match is not None:
        from_s = spice_number.parse(from_match.group(1))
        to_s = spice_number.parse(to_match.group(1))
        if from_s is not None and to_s is not None:
            window = (from_s, to_s)

    target: float | None = None
    when_match = _MEAS_WHEN_TARGET_RE.search(spice)
    if when_match is not None:
        target = spice_number.parse_plain(when_match.group(1))

    return {"signal": signal, "window": window, "target": target}


def prepare_annotations(
    measurements_spec: list[dict[str, Any]],
) -> tuple[dict[str, str | None], dict[str, dict[str, Any]]]:
    """Index rollup signals by name and merge per-corner annotations by signal."""
    hints_by_signal: dict[str, dict[str, Any]] = {}
    signals_by_name: dict[str, str | None] = {}
    for spec in measurements_spec:
        hints = _measurement_diagnostic_hints(spec.get("spice", ""))
        signals_by_name[spec["name"]] = hints["signal"]
        if hints["signal"] is None or sim_derive.is_derive(spec):
            continue
        # Merge across every measurement naming this signal (e.g. a `PP`
        # window-measurement and a separate `WHEN` threshold-measurement
        # both over `v(out)`) -- the first *non-None* window/target found
        # wins for each field independently, rather than one measurement's
        # hints unconditionally shadowing another's.
        merged = hints_by_signal.setdefault(
            hints["signal"], {"window": None, "target": None}
        )
        if merged["window"] is None and hints["window"] is not None:
            merged["window"] = hints["window"]
        if merged["target"] is None and hints["target"] is not None:
            merged["target"] = hints["target"]
    return signals_by_name, hints_by_signal


def write_corner_plots(
    *,
    corner: dict[str, Any],
    plot_dir: str,
    analysis_kind: str | None,
    hints_by_signal: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Render one SVG per non-sweep signal captured in ``corner``'s persisted
    waveform artifact, writing each to ``plot_dir`` with a deterministic
    filename (``<corner_slug>__<signal_slug>.svg``).

    Any `tran` measurement whose own signal (see
    :func:`_measurement_diagnostic_hints`) matches a rendered signal gets its
    ``from=``/``to=`` window shaded and its ``WHEN``-target drawn as a
    reference line on that signal's plot -- issue #1723's "the `.meas`
    target drawn as a line" / "the measurement window shaded" acceptance
    criteria. `ac` plots use a log-scaled frequency axis; `dc`/`tran` use a
    linear sweep axis (window shading only applies to `tran`, since
    ``from=``/``to=`` on a `dc`/`ac` measurement is over the swept
    source/frequency, not simulated time, and this issue's shading is
    specifically the *time* window the Proposal describes).

    Returns ``[]`` -- writing nothing -- when this corner has no persisted
    waveform artifact (an errored corner that never produced a rawfile) or
    that artifact cannot be read/parsed; never raises, since one corner's
    bad artifact should not abort plotting every other corner.
    """
    waveform_path = corner["artifacts"].get("waveform")
    if not waveform_path or not os.path.isfile(waveform_path):
        return []
    try:
        with open(waveform_path, encoding="utf-8") as handle:
            waveform = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []

    variables = waveform.get("variables") or []
    points = waveform.get("points") or []
    if len(variables) < 2 or not points:
        return []

    sweep_name = variables[0]["name"]
    x_values = [point[0] for point in points]
    log_x = analysis_kind == "ac"

    corner_slug = _slugify_corner_id(corner["corner_id"])
    written: list[dict[str, str]] = []
    for var in variables[1:]:
        name = var["name"]
        y_values = [point[var["index"]] for point in points]
        hint = hints_by_signal.get(name.lower())
        window = hint["window"] if hint and analysis_kind == "tran" else None
        target = hint["target"] if hint else None
        svg = render_waveform_svg(
            signal_name=name,
            corner_id=corner["corner_id"],
            x_values=x_values,
            y_values=y_values,
            x_axis_label=sweep_name,
            log_x=log_x,
            window=window,
            target=target,
        )
        path = os.path.join(plot_dir, f"{corner_slug}__{_slugify_signal(name)}.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        written.append({"signal": name, "path": path})
    return written


def measurement_plot_path(
    entry: dict[str, Any],
    signal: str | None,
    plots_by_corner: dict[str, list[dict[str, str]]],
) -> str | None:
    """The SVG path (if any) belonging next to one measurement rollup
    ``entry`` -- its own ``.meas`` card's signal, rendered at its
    ``worst_case`` corner -- so a `--plot` run's miss report names the
    picture to look at next to the number that missed (issue #1723).

    Returns ``None`` when there is no worst-case corner (an empty corner
    matrix), the measurement names no recognisable signal, or that corner
    rendered no matching plot.
    """
    worst_case = entry.get("worst_case")
    if worst_case is None or signal is None:
        return None
    corner_plots = plots_by_corner.get(worst_case["corner_id"])
    if not corner_plots:
        return None
    for plot in corner_plots:
        if plot["signal"].lower() == signal:
            return plot["path"]
    return None
