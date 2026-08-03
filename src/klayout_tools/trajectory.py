"""Read, analyze, and render an optimization trajectory log.

An optimization run (a multi-candidate loop, or a human proposing candidates
by hand) is evidence in two halves. The *artifact* half -- the final layout
and its DRC/LVS/sim envelopes -- proves what the winning design measures.
The *trajectory* half -- how many candidates were evaluated, what was tried
and abandoned, and where the real wins came from -- proves the run did not
just plateau after the low-hanging fruit. This module owns the second half:
a small, append-only JSONL log of per-evaluation records, plus a renderer
that turns it into the milestone table + objective-vs-turn plot a block
repo's README embeds. See issue #388.

Pure library: the public functions return plain Python data (dicts / lists
of JSON-serialisable primitives, or a rendered string) and never print.
Serialisation and console printing live in the CLI command module
(``cli/trajectory_cmd.py``), mirroring ``layers.py`` / ``report.py``.

Design invariants:

- **The log references artifacts by path, never inlines them.** A 500-turn
  run with full gate output per turn is not small; each record carries a
  ``candidate_ref`` (a path or identifier) and a *summary* of that turn's
  gate results, not the layout snapshot or the full DRC report. Downstream
  reads the referenced file if it wants the detail (the original issue's
  storage concern).
- **The record schema mirrors the ``klt eval`` envelope shape (#387).** A
  record's ``objective`` is ``{name, value, polarity}`` and its
  ``gate_results`` is a ``[{"check": ..., "status": ...}, ...]`` list -- the
  same vocabulary a single scored gate emits -- so once ``klt eval`` lands a
  turn's record is just that turn's eval envelope's objective/gate_results,
  not a re-derived parallel summary. The record stays *candidate-shape
  agnostic*: ``candidate_ref`` is whatever the driving loop points at (a
  GDS/OASIS path today, a synthesized netlist + P&R result later, per
  #391 Phase 5), so a future digital flow reuses this log unchanged.
- **The renderer never requires a live optimizer.** It operates purely on a
  JSONL file on disk, so a hand-written or human-curated log renders
  identically to a machine-emitted one (the original issue's "Notes").

Headless invariant: pure Python + the standard library. The objective-vs-turn
plot is emitted as a self-contained SVG string (renders inline in a GitHub
README, embeddable via ``<img>`` or committed as a file) rather than via a
raster-plot dependency -- no matplotlib, no GUI, no new runtime dependency,
runnable in CI.
"""

from __future__ import annotations

import json
import os
from typing import Any

__all__ = [
    "TrajectoryError",
    "SCHEMA_VERSION",
    "read_log",
    "detect_milestones",
    "render_markdown_table",
    "render_plot_svg",
    "build_trajectory",
]

#: Bumped only on a non-additive (breaking) change to this command's own JSON
#: shape -- see docs/json-contract.md. This is *this* command's own envelope
#: version; it is also the ``schema_version`` a trajectory-log record is
#: expected to carry (the record schema and the renderer's output schema are
#: versioned together, since this module owns both).
SCHEMA_VERSION = 1

#: Objective polarities. ``minimize`` (lower objective is better, e.g. gate
#: count / area / power) and ``maximize`` (higher is better, e.g. gain /
#: margin) determine the sign of an "improvement".
_POLARITIES = ("minimize", "maximize")


class TrajectoryError(Exception):
    """Raised when a trajectory log cannot be read/parsed, is empty, or holds
    a structurally invalid record.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


# --------------------------------------------------------------------------- #
# Reading + validating the JSONL log
# --------------------------------------------------------------------------- #


def read_log(source: str) -> list[dict[str, Any]]:
    """Read an append-only JSONL trajectory log and return its records.

    ``source`` is a file path. Each non-blank line is one JSON object -- a
    trajectory record (see the module docstring / ``docs/cli/trajectory.md``
    for the schema). Blank lines are ignored so a log can be
    ``echo``-appended by a shell loop without care about a trailing newline.

    Records are returned **sorted by ``turn`` ascending** so an out-of-order
    append (a parallel loop writing turns as they finish) still renders a
    monotonic trajectory. Ties keep their file order (stable sort).

    Raises :class:`TrajectoryError` if the file is missing, unreadable, holds
    no records at all, or any line is not a valid record (invalid JSON, not a
    JSON object, or missing/mistyped a required field) -- an empty or
    malformed log fails loudly rather than silently rendering an empty table.
    """
    if source == "-":
        raise TrajectoryError(
            "reading a trajectory log from stdin is not supported; pass a file path"
        )
    if not os.path.exists(source):
        raise TrajectoryError(f"file not found: {source}")
    if os.path.isdir(source):
        raise TrajectoryError(f"not a file: {source}")

    try:
        with open(source, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise TrajectoryError(
            f"could not read trajectory log '{source}': {exc}"
        ) from exc

    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrajectoryError(f"{source}:{lineno}: not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise TrajectoryError(
                f"{source}:{lineno}: record must be a JSON object, got "
                f"{type(record).__name__}"
            )
        _validate_record(record, source, lineno)
        records.append(record)

    if not records:
        raise TrajectoryError(
            f"trajectory log '{source}' contains no records "
            "(an empty log is not a valid trajectory)"
        )

    # Stable sort by turn so out-of-order appends still render monotonically.
    records.sort(key=lambda r: r["turn"])
    return records


def _validate_record(record: dict[str, Any], source: str, lineno: int) -> None:
    """Validate one record's required fields, raising :class:`TrajectoryError`
    with a ``file:line`` prefix on the first problem found."""

    def fail(msg: str) -> None:
        raise TrajectoryError(f"{source}:{lineno}: {msg}")

    turn = record.get("turn")
    if not isinstance(turn, int) or isinstance(turn, bool):
        fail("'turn' is required and must be an integer")

    if not isinstance(record.get("candidate_ref"), str) or not record["candidate_ref"]:
        fail("'candidate_ref' is required and must be a non-empty string")

    objective = record.get("objective")
    if not isinstance(objective, dict):
        fail("'objective' is required and must be an object")
    name = objective.get("name")  # type: ignore[union-attr]
    if not isinstance(name, str) or not name:
        fail("'objective.name' is required and must be a non-empty string")
    value = objective.get("value")  # type: ignore[union-attr]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("'objective.value' is required and must be a number")
    polarity = objective.get("polarity")  # type: ignore[union-attr]
    if polarity not in _POLARITIES:
        fail(f"'objective.polarity' is required and must be one of {list(_POLARITIES)}")

    gate_results = record.get("gate_results")
    if gate_results is not None:
        if not isinstance(gate_results, list):
            fail("'gate_results', when present, must be a list")
        for entry in gate_results:  # type: ignore[union-attr]
            if (
                not isinstance(entry, dict)
                or "check" not in entry
                or "status" not in entry
            ):
                fail(
                    "each 'gate_results' entry must be an object with 'check' "
                    "and 'status' keys"
                )

    wall = record.get("wall_clock_s")
    if wall is not None and (
        isinstance(wall, bool) or not isinstance(wall, (int, float))
    ):
        fail("'wall_clock_s', when present, must be a number")


# --------------------------------------------------------------------------- #
# Milestone detection
# --------------------------------------------------------------------------- #


def _is_improvement(value: float, best: float, polarity: str, threshold: float) -> bool:
    """Return True when ``value`` improves on the running ``best`` by strictly
    more than ``threshold`` (in objective units).

    For ``minimize`` a lower value is better, so the improvement is
    ``best - value``; for ``maximize`` a higher value is better, so it is
    ``value - best``. The comparison is *strict* (``> threshold``): an
    improvement of exactly ``threshold`` is deliberately **not** a milestone,
    so the threshold boundary is unambiguous (see the just-under/just-over
    tests).
    """
    delta = (best - value) if polarity == "minimize" else (value - best)
    return delta > threshold


def detect_milestones(
    records: list[dict[str, Any]], threshold: float = 0.0
) -> dict[str, Any]:
    """Walk ``records`` in turn order and flag milestones.

    The **first** record establishes the baseline (there is no prior record to
    improve on, so it is never itself a milestone). Each later record is a
    milestone when its objective improves on the *best objective value seen in
    any prior record* by strictly more than ``threshold`` (see
    :func:`_is_improvement`). The running best is updated on every strict
    improvement, threshold or not, so a milestone's ``objective_before`` is
    always the genuine best-so-far it beat.

    ``threshold`` is in objective units and defaults to ``0.0`` (any strict
    improvement is a milestone). All records must share one objective ``name``
    and ``polarity``; a log that mixes them is a malformed log and raises
    :class:`TrajectoryError`.

    Returns a summary dict::

        {
            "objective_name": <str>,
            "polarity": "minimize" | "maximize",
            "threshold": <float>,
            "baseline": {"turn": int, "candidate_ref": str, "objective": num},
            "best":     {"turn": int, "candidate_ref": str, "objective": num},
            "milestones": [
                {
                    "turn": int,              # this record's turn
                    "prior_turn": int,        # turn that held the beaten best
                    "candidate_ref": str,
                    "objective_before": num,  # the best-prior value beaten
                    "objective_after": num,   # this record's value
                    "delta": num,             # signed improvement magnitude (> 0)
                    "description": str,       # record's 'description', or ""
                },
                ...
            ],
        }
    """
    if not records:
        raise TrajectoryError("cannot detect milestones on an empty record list")

    name = records[0]["objective"]["name"]
    polarity = records[0]["objective"]["polarity"]
    for record in records:
        obj = record["objective"]
        if obj["name"] != name or obj["polarity"] != polarity:
            raise TrajectoryError(
                "trajectory log mixes objectives: expected every record's "
                f"objective to be name={name!r} polarity={polarity!r}, found "
                f"name={obj['name']!r} polarity={obj['polarity']!r} at turn "
                f"{record['turn']}"
            )

    baseline_record = records[0]
    best_value: float = baseline_record["objective"]["value"]
    best_turn: int = baseline_record["turn"]
    best_ref: str = baseline_record["candidate_ref"]

    milestones: list[dict[str, Any]] = []
    for record in records[1:]:
        value = record["objective"]["value"]
        if _is_improvement(value, best_value, polarity, threshold):
            delta = (
                (best_value - value) if polarity == "minimize" else (value - best_value)
            )
            milestones.append(
                {
                    "turn": record["turn"],
                    "prior_turn": best_turn,
                    "candidate_ref": record["candidate_ref"],
                    "objective_before": best_value,
                    "objective_after": value,
                    "delta": delta,
                    "description": str(record.get("description", "")),
                }
            )
        # Update running best on any strict improvement (threshold or not), so
        # a later milestone's `objective_before` reflects the true best-so-far.
        if _is_improvement(value, best_value, polarity, 0.0):
            best_value, best_turn, best_ref = (
                value,
                record["turn"],
                record["candidate_ref"],
            )

    return {
        "objective_name": name,
        "polarity": polarity,
        "threshold": threshold,
        "baseline": {
            "turn": baseline_record["turn"],
            "candidate_ref": baseline_record["candidate_ref"],
            "objective": baseline_record["objective"]["value"],
        },
        "best": {
            "turn": best_turn,
            "candidate_ref": best_ref,
            "objective": best_value,
        },
        "milestones": milestones,
    }


# --------------------------------------------------------------------------- #
# Rendering: markdown milestone table
# --------------------------------------------------------------------------- #


def _fmt_num(value: float) -> str:
    """Format an objective value compactly: an integral float renders without a
    trailing ``.0`` (``2010.0`` -> ``2010``), otherwise as-is."""
    if isinstance(value, bool):  # defensive; bools are rejected upstream
        return str(value)
    if isinstance(value, int):
        return str(value)
    if value == int(value):
        return str(int(value))
    return repr(value)


def _turn_range(prior_turn: int, turn: int) -> str:
    """Render a milestone's turn span: a single turn when prior==this,
    otherwise ``prior-this`` (mirroring the issue's ``35-48`` column)."""
    return str(turn) if prior_turn == turn else f"{prior_turn}-{turn}"


def render_markdown_table(summary: dict[str, Any]) -> str:
    """Render the milestone table as GitHub Flavored Markdown.

    Columns mirror the original issue's Context table: a turn range, the
    objective's before -> after values, and the human-written description.
    A log with a baseline but no milestones renders a one-line note instead
    of an empty header-only table.
    """
    name = summary["objective_name"]
    milestones = summary["milestones"]

    header = f"### Optimization milestones ({name}, {summary['polarity']})"
    if not milestones:
        baseline = summary["baseline"]
        return (
            f"{header}\n\n"
            f"No milestones: the objective never improved beyond the "
            f"threshold ({_fmt_num(summary['threshold'])}) past the baseline "
            f"of {_fmt_num(baseline['objective'])} at turn {baseline['turn']}."
        )

    lines = [
        header,
        "",
        f"| turns | {name} | what changed |",
        "| --- | --- | --- |",
    ]
    for ms in milestones:
        turns = _turn_range(ms["prior_turn"], ms["turn"])
        change = (
            f"{_fmt_num(ms['objective_before'])} -> {_fmt_num(ms['objective_after'])}"
        )
        desc = str(ms["description"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {turns} | {change} | {desc} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Rendering: objective-vs-turn plot (self-contained SVG, no dependency)
# --------------------------------------------------------------------------- #

_PLOT_WIDTH = 640
_PLOT_HEIGHT = 360
_MARGIN_LEFT = 64
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


def render_plot_svg(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    """Render an objective-vs-turn line plot as a self-contained SVG string.

    Every record is a point on the objective curve; milestone turns are
    marked with a filled dot so the table's rows line up visually with the
    curve. Pure string-built SVG -- no plotting dependency, deterministic
    output, safe to embed in a README via ``<img src="trajectory.svg">`` or by
    committing the file. Works on any log read by :func:`read_log`, with or
    without a live optimizer.
    """
    name = summary["objective_name"]
    turns = [r["turn"] for r in records]
    values = [float(r["objective"]["value"]) for r in records]
    milestone_turns = {ms["turn"] for ms in summary["milestones"]}

    t_min, t_max = min(turns), max(turns)
    v_min, v_max = min(values), max(values)
    t_span = (t_max - t_min) or 1
    v_span = (v_max - v_min) or 1

    plot_w = _PLOT_WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = _PLOT_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM

    def px(turn: int) -> float:
        return _MARGIN_LEFT + (turn - t_min) / t_span * plot_w

    def py(value: float) -> float:
        # SVG y grows downward; a higher objective value should sit higher on
        # the canvas, so invert.
        return _MARGIN_TOP + (v_max - value) / v_span * plot_h

    points = [(px(t), py(v)) for t, v in zip(turns, values, strict=True)]
    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_PLOT_WIDTH}" '
        f'height="{_PLOT_HEIGHT}" viewBox="0 0 {_PLOT_WIDTH} {_PLOT_HEIGHT}" '
        'font-family="sans-serif">'
    )
    parts.append(
        f'<rect width="{_PLOT_WIDTH}" height="{_PLOT_HEIGHT}" fill="#ffffff"/>'
    )
    # Title.
    parts.append(
        f'<text x="{_PLOT_WIDTH / 2:.0f}" y="24" text-anchor="middle" '
        f'font-size="16" fill="#111827">{_xml_escape(name)} vs turn</text>'
    )
    # Axes.
    axis_color = "#9ca3af"
    x_axis_y = _MARGIN_TOP + plot_h
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
    label_color = "#374151"
    parts.append(
        f'<text x="{_MARGIN_LEFT - 6}" y="{_MARGIN_TOP + 4:.0f}" '
        f'text-anchor="end" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(v_max))}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT - 6}" y="{x_axis_y:.0f}" '
        f'text-anchor="end" font-size="11" fill="{label_color}">'
        f"{_xml_escape(_fmt_num(v_min))}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT}" y="{x_axis_y + 16:.0f}" '
        f'text-anchor="middle" font-size="11" fill="{label_color}">'
        f"turn {t_min}</text>"
    )
    parts.append(
        f'<text x="{_MARGIN_LEFT + plot_w}" y="{x_axis_y + 16:.0f}" '
        f'text-anchor="middle" font-size="11" fill="{label_color}">'
        f"turn {t_max}</text>"
    )
    # Objective curve.
    parts.append(
        f'<polyline points="{polyline}" fill="none" stroke="#2563eb" stroke-width="2"/>'
    )
    # Points; milestones get a larger filled marker.
    for (x, y), turn in zip(points, turns, strict=True):
        if turn in milestone_turns:
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.5" fill="#dc2626"/>')
        else:
            parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#2563eb"/>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Top-level: read + analyze + render into one payload
# --------------------------------------------------------------------------- #


def build_trajectory(source: str, threshold: float = 0.0) -> dict[str, Any]:
    """Read a JSONL trajectory log and render its milestone table + plot.

    This is the single entry point the CLI calls. Returns a dict matching the
    documented JSON schema (see ``docs/cli/trajectory.md``)::

        {
            "schema_version": 1,
            "source": <str>,
            "record_count": <int>,
            "objective_name": <str>,
            "polarity": "minimize" | "maximize",
            "threshold": <float>,
            "baseline": {...},
            "best": {...},
            "milestone_count": <int>,
            "milestones": [ ... ],
            "markdown": <str>,    # the milestone table (GFM)
            "plot_svg": <str>,    # self-contained objective-vs-turn SVG
        }

    Raises :class:`TrajectoryError` for a missing/malformed/empty log, or a
    log that mixes objectives -- the CLI turns that into exit code 1.
    """
    records = read_log(source)
    summary = detect_milestones(records, threshold=threshold)
    markdown = render_markdown_table(summary)
    plot_svg = render_plot_svg(records, summary)

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "record_count": len(records),
        "objective_name": summary["objective_name"],
        "polarity": summary["polarity"],
        "threshold": summary["threshold"],
        "baseline": summary["baseline"],
        "best": summary["best"],
        "milestone_count": len(summary["milestones"]),
        "milestones": summary["milestones"],
        "markdown": markdown,
        "plot_svg": plot_svg,
    }
