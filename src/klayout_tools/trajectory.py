"""Read, validate, and render an optimization **trajectory log**: the
append-only JSONL record of how a multi-candidate run got to its final
artifact, not just what the final artifact measures.

Our other evidence is artifact-shaped (a `klt drc`/`klt sim` envelope
pinned to a commit). That proves what the *final* design measures and says
nothing about how it got there -- how many candidates were evaluated, what
was abandoned, and where the real wins came from. A trajectory log is the
other half: one record per evaluation, from which a milestone table
("turns 35-48: 2,010 -> 1,304 gates, bypassed the REDUCE stage") is
*derived* rather than hand-written.

Pure library: every public function returns plain Python data (dicts of
JSON-serialisable primitives) or a string, and never prints -- mirroring
``report.py`` / ``drc.py``. Serialisation and console printing live in
``cli/trajectory_cmd.py``.

Design constraints this module holds to (issue #388):

- **No optimizer required.** Nothing here knows how candidates are
  proposed. A hand-written, human-curated JSONL log renders exactly like a
  machine-emitted one, so the format is usable before any proposer exists.
- **References, never inline artifacts.** A record points at its candidate
  (``candidate_ref``) and, optionally, at the evaluation envelope that
  scored it (``eval_ref``) by path. Gate output and layout snapshots are
  never embedded, so a 500-turn log stays a small text file. This module
  deliberately does **not** dereference ``eval_ref`` -- rendering a
  trajectory is an offline, no-I/O-beyond-the-log operation.
- **Generic gate/objective vocabulary.** ``gate_results`` is a list of
  ``{"check": ..., "status": ...}`` entries and ``objective`` is one scalar
  with a polarity -- the shape a scored-gate verb (`klt eval`, #387) emits
  and the shape a future digital synth+P&R candidate (#391) can reuse. No
  check-name enum is hardcoded anywhere in this module.

Record schema (one JSON object per line; see ``docs/cli/trajectory.md``)::

    {
      "record_schema_version": 1,          # optional; defaults to 1
      "turn": 22,                          # required, int >= 0, non-decreasing
      "candidate_ref": "runs/t22/out.gds", # required, path or identifier
      "objective": {                       # required
        "name": "gate_count",
        "value": 2010,
        "polarity": "minimize",            # or "maximize"
        "unit": "gates"                    # optional
      },
      "gate_results": [                    # optional
        {"check": "drc", "status": "clean"},
        {"check": "lvs", "status": "match"}
      ],
      "eval_ref": "runs/t22/eval.json",    # optional, path to a klt eval envelope
      "wall_clock_s": 41.2,                # optional on read, written by make_record
      "note": "iterative shift-subtract"   # optional, the "what changed" cell
    }

Malformed input is a **loud error** (:class:`TrajectoryError`), never a
silently empty render: an unparseable line, a missing required field, a log
whose records disagree about the objective, and an empty log all fail with
a message naming the offending line.
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

__all__ = [
    "RECORD_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "TrajectoryError",
    "analyze",
    "append_record",
    "build_trajectory_report",
    "make_record",
    "read_records",
    "render_plot_svg",
]

#: Bumped only on a non-additive (breaking) change to this command's own JSON
#: shape -- see docs/json-contract.md. Independent of RECORD_SCHEMA_VERSION.
SCHEMA_VERSION = 1

#: Bumped only on a non-additive (breaking) change to the *log record* shape
#: documented above. Written into every record built by :func:`make_record`;
#: optional when reading, so a hand-written log need not carry it.
RECORD_SCHEMA_VERSION = 1

#: The two objective directions. "Improvement" means a smaller value under
#: ``minimize`` and a larger value under ``maximize``.
POLARITIES = ("minimize", "maximize")

#: Gate statuses that count as "this candidate passed its checks". Kept
#: deliberately wide so the klt verbs' own vocabularies (`drc`'s "clean",
#: `lvs`'s "match", `sim`'s "pass") all land here without this module
#: knowing which check produced them. Anything else counts as a failure.
PASSING_GATE_STATUSES = frozenset({"pass", "passed", "clean", "match", "ok"})


class TrajectoryError(Exception):
    """Raised when a log cannot be read, parsed, or validated.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's error contract.
    """


# --------------------------------------------------------------------------- #
# Record construction + validation
# --------------------------------------------------------------------------- #


def make_record(
    turn: int,
    candidate_ref: str,
    objective_name: str,
    objective_value: float,
    polarity: str,
    wall_clock_s: float,
    *,
    unit: str | None = None,
    gate_results: list[dict[str, Any]] | None = None,
    eval_ref: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Build one validated trajectory record.

    ``wall_clock_s`` is required here (a programmatic emitter always knows
    how long its evaluation took) even though it is optional when *reading*
    a log, so a hand-curated log is still renderable. Raises
    :class:`TrajectoryError` if the resulting record is not valid.
    """
    record: dict[str, Any] = {
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "turn": turn,
        "candidate_ref": candidate_ref,
        "objective": {
            "name": objective_name,
            "value": objective_value,
            "polarity": polarity,
            "unit": unit,
        },
        "gate_results": list(gate_results) if gate_results is not None else None,
        "eval_ref": eval_ref,
        "wall_clock_s": wall_clock_s,
        "note": note,
    }
    _validate_record(record, "record")
    return record


def _validate_record(record: Any, where: str) -> dict[str, Any]:
    """Validate one decoded record, raising :class:`TrajectoryError` with a
    message prefixed by ``where`` (e.g. ``"log.jsonl:3"``)."""
    if not isinstance(record, dict):
        raise TrajectoryError(
            f"{where}: each line must be a JSON object, got {type(record).__name__}"
        )

    version = record.get("record_schema_version")
    if version is not None:
        if not _is_int(version):
            raise TrajectoryError(
                f"{where}: 'record_schema_version' must be an integer"
            )
        if version > RECORD_SCHEMA_VERSION:
            raise TrajectoryError(
                f"{where}: record_schema_version {version} is newer than this "
                f"klt understands (max {RECORD_SCHEMA_VERSION})"
            )

    turn = record.get("turn")
    if not _is_int(turn) or turn < 0:
        raise TrajectoryError(f"{where}: 'turn' must be an integer >= 0")

    candidate_ref = record.get("candidate_ref")
    if not isinstance(candidate_ref, str) or not candidate_ref.strip():
        raise TrajectoryError(
            f"{where}: 'candidate_ref' must be a non-empty string (a path or "
            "identifier pointing at the candidate -- never the artifact itself)"
        )

    objective = record.get("objective")
    if not isinstance(objective, dict):
        raise TrajectoryError(f"{where}: 'objective' must be an object")
    name = objective.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TrajectoryError(f"{where}: 'objective.name' must be a non-empty string")
    value = objective.get("value")
    if not _is_number(value) or not math.isfinite(value):
        raise TrajectoryError(f"{where}: 'objective.value' must be a finite number")
    polarity = objective.get("polarity")
    if polarity not in POLARITIES:
        raise TrajectoryError(
            f"{where}: 'objective.polarity' must be one of {list(POLARITIES)}, "
            f"got {polarity!r}"
        )
    unit = objective.get("unit")
    if unit is not None and not isinstance(unit, str):
        raise TrajectoryError(f"{where}: 'objective.unit' must be a string or null")

    gate_results = record.get("gate_results")
    if gate_results is not None:
        if not isinstance(gate_results, list):
            raise TrajectoryError(
                f"{where}: 'gate_results' must be a list of "
                '{"check": ..., "status": ...} objects or null'
            )
        for index, entry in enumerate(gate_results):
            if not isinstance(entry, dict):
                raise TrajectoryError(
                    f"{where}: 'gate_results[{index}]' must be an object"
                )
            if not isinstance(entry.get("check"), str) or not isinstance(
                entry.get("status"), str
            ):
                raise TrajectoryError(
                    f"{where}: 'gate_results[{index}]' needs string "
                    "'check' and 'status' fields"
                )

    eval_ref = record.get("eval_ref")
    if eval_ref is not None and not isinstance(eval_ref, str):
        raise TrajectoryError(f"{where}: 'eval_ref' must be a string or null")

    wall_clock_s = record.get("wall_clock_s")
    if wall_clock_s is not None:
        if not _is_number(wall_clock_s) or not math.isfinite(wall_clock_s):
            raise TrajectoryError(
                f"{where}: 'wall_clock_s' must be a finite number or null"
            )
        if wall_clock_s < 0:
            raise TrajectoryError(f"{where}: 'wall_clock_s' must not be negative")

    note = record.get("note")
    if note is not None and not isinstance(note, str):
        raise TrajectoryError(f"{where}: 'note' must be a string or null")

    return record


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# Log I/O
# --------------------------------------------------------------------------- #


def append_record(path: str, record: dict[str, Any]) -> None:
    """Append one validated record to the JSONL log at ``path``.

    The log is append-only by construction: this never rewrites existing
    lines, so a crashed run leaves every record it had already written.
    """
    _validate_record(record, path)
    line = json.dumps(record, sort_keys=False)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        raise TrajectoryError(
            f"could not append to trajectory log '{path}': {exc}"
        ) from exc


def read_records(source: str) -> list[dict[str, Any]]:
    """Read and validate every record in the JSONL log at ``source``.

    ``source == "-"`` reads the log from stdin. Blank lines are skipped
    (a hand-edited log often has them); every other line must be one JSON
    object matching the record schema. Raises :class:`TrajectoryError`
    naming the offending line on any failure, and on a log with no records
    at all -- an empty trajectory renders nothing meaningful, so it is an
    error rather than an empty table.
    """
    if source == "-":
        label = "<stdin>"
        try:
            text = sys.stdin.read()
        except OSError as exc:
            raise TrajectoryError(
                f"could not read trajectory log from stdin: {exc}"
            ) from exc
    else:
        label = source
        if not os.path.exists(source):
            raise TrajectoryError(f"file not found: {source}")
        if os.path.isdir(source):
            raise TrajectoryError(f"not a file: {source}")
        try:
            with open(source, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise TrajectoryError(
                f"could not read trajectory log '{source}': {exc}"
            ) from exc

    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        where = f"{label}:{line_number}"
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TrajectoryError(f"{where}: not valid JSON: {exc.msg}") from exc
        records.append(_validate_record(decoded, where))

    if not records:
        raise TrajectoryError(
            f"trajectory log '{label}' contains no records (a log must have at "
            "least one JSON object line)"
        )
    return records


# --------------------------------------------------------------------------- #
# Analysis: best-so-far tracking + milestone detection
# --------------------------------------------------------------------------- #


def analyze(
    records: list[dict[str, Any]],
    threshold: float = 0.0,
    threshold_pct: float = 0.0,
) -> dict[str, Any]:
    """Derive best-so-far tracking and milestones from validated ``records``.

    A record is a **milestone** when it improves the objective by *more
    than* both configured thresholds relative to the best prior eligible
    record: ``threshold`` in the objective's own units, and
    ``threshold_pct`` as a percentage of that best prior value. Both
    comparisons are strict, so an improvement landing exactly on a
    threshold is not a milestone (the boundary case is a documented,
    tested contract -- see docs/cli/trajectory.md).

    "Improvement" respects ``objective.polarity``: smaller is better under
    ``minimize``, larger under ``maximize``. A record whose ``gate_results``
    contain any non-passing status is **ineligible**: it is kept in the
    output (an abandoned candidate is part of the trajectory's evidence)
    but can neither become the best-so-far nor be flagged a milestone,
    because an objective win that fails its checks is not a win. A record
    with no gate information at all is treated as eligible with
    ``gate_status: "unknown"`` -- a hand-curated log is not obliged to
    restate gates it never ran.

    Raises :class:`TrajectoryError` if the records disagree about the
    objective (name or polarity), or if ``turn`` goes backwards -- a log is
    append-only, so a decreasing turn means two runs were concatenated.
    """
    if not records:
        raise TrajectoryError("no trajectory records to analyze")
    if threshold < 0:
        raise TrajectoryError("threshold must be >= 0")
    if threshold_pct < 0:
        raise TrajectoryError("threshold-pct must be >= 0")

    first_objective = records[0]["objective"]
    name = first_objective["name"]
    polarity = first_objective["polarity"]
    unit = first_objective.get("unit")

    previous_turn: int | None = None
    entries: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    wall_clock_total = 0.0
    saw_wall_clock = False

    for index, record in enumerate(records):
        objective = record["objective"]
        if objective["name"] != name:
            raise TrajectoryError(
                f"record {index + 1} changes the objective name from {name!r} to "
                f"{objective['name']!r} -- one log tracks one objective"
            )
        if objective["polarity"] != polarity:
            raise TrajectoryError(
                f"record {index + 1} changes the objective polarity from "
                f"{polarity!r} to {objective['polarity']!r}"
            )

        turn = record["turn"]
        if previous_turn is not None and turn < previous_turn:
            raise TrajectoryError(
                f"record {index + 1} has turn {turn} after turn {previous_turn} -- "
                "a trajectory log is append-only and turns must not go backwards"
            )
        previous_turn = turn

        value = objective["value"]
        gate_status = _gate_status(record.get("gate_results"))
        eligible = gate_status != "fail"

        wall_clock_s = record.get("wall_clock_s")
        if wall_clock_s is not None:
            wall_clock_total += float(wall_clock_s)
            saw_wall_clock = True

        entry = {
            "turn": turn,
            "candidate_ref": record["candidate_ref"],
            "value": value,
            "gate_status": gate_status,
            "eval_ref": record.get("eval_ref"),
            "wall_clock_s": wall_clock_s,
            "note": record.get("note"),
            "milestone": False,
            "improvement": None,
            "improvement_pct": None,
            "best_so_far": False,
        }

        if eligible:
            if best is None:
                baseline = entry
                best = entry
                entry["best_so_far"] = True
            else:
                improvement = _improvement(best["value"], value, polarity)
                relative = _relative(improvement, best["value"])
                entry["improvement"] = improvement
                entry["improvement_pct"] = relative
                if improvement > threshold and relative > threshold_pct:
                    entry["milestone"] = True
                    milestones.append(
                        {
                            "turn": turn,
                            "from_turn": best["turn"],
                            "from_value": best["value"],
                            "value": value,
                            "improvement": improvement,
                            "improvement_pct": relative,
                            "candidate_ref": record["candidate_ref"],
                            "gate_status": gate_status,
                            "note": record.get("note"),
                        }
                    )
                if improvement > 0:
                    best = entry
                    entry["best_so_far"] = True

        entries.append(entry)

    total_improvement = None
    total_improvement_pct = None
    if baseline is not None and best is not None:
        total_improvement = _improvement(baseline["value"], best["value"], polarity)
        total_improvement_pct = _relative(total_improvement, baseline["value"])

    return {
        "schema_version": SCHEMA_VERSION,
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "objective": {"name": name, "polarity": polarity, "unit": unit},
        "threshold": {"absolute": threshold, "percent": threshold_pct},
        "record_count": len(entries),
        "first_turn": entries[0]["turn"],
        "last_turn": entries[-1]["turn"],
        "baseline": _point(baseline),
        "best": _point(best),
        "total_improvement": total_improvement,
        "total_improvement_pct": total_improvement_pct,
        "wall_clock_s_total": wall_clock_total if saw_wall_clock else None,
        "milestone_count": len(milestones),
        "milestones": milestones,
        "records": entries,
    }


def _gate_status(gate_results: list[dict[str, Any]] | None) -> str:
    """Reduce a record's ``gate_results`` to ``pass``/``fail``/``unknown``."""
    if not gate_results:
        return "unknown"
    for entry in gate_results:
        if entry["status"].strip().lower() not in PASSING_GATE_STATUSES:
            return "fail"
    return "pass"


def _improvement(previous: float, current: float, polarity: str) -> float:
    """Signed improvement of ``current`` over ``previous`` (positive = better)."""
    delta = previous - current if polarity == "minimize" else current - previous
    return float(delta)


def _relative(improvement: float, reference: float) -> float:
    """``improvement`` as a percentage of ``|reference|``.

    A zero reference has no meaningful percentage: any positive improvement
    against it is reported as infinite (so a percentage threshold can never
    silently swallow it), and anything else as ``0.0``.
    """
    if reference == 0:
        return math.inf if improvement > 0 else 0.0
    return improvement / abs(reference) * 100.0


def _point(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "turn": entry["turn"],
        "value": entry["value"],
        "candidate_ref": entry["candidate_ref"],
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def build_trajectory_report(
    source: str,
    threshold: float = 0.0,
    threshold_pct: float = 0.0,
    plot_path: str | None = None,
) -> dict[str, Any]:
    """Read the log at ``source``, analyze it, and render it.

    Returns the analysis payload (see :func:`analyze`) plus ``source``, the
    rendered ``markdown``/``text`` milestone tables, and a ``plot`` block.
    When ``plot_path`` is given, an objective-vs-turn SVG is written there
    and ``plot`` describes it; otherwise ``plot`` is ``None``.
    """
    records = read_records(source)
    payload = analyze(records, threshold=threshold, threshold_pct=threshold_pct)
    payload["source"] = source

    plot: dict[str, Any] | None = None
    if plot_path is not None:
        svg = render_plot_svg(payload)
        try:
            with open(plot_path, "w", encoding="utf-8") as handle:
                handle.write(svg)
        except OSError as exc:
            raise TrajectoryError(f"could not write plot '{plot_path}': {exc}") from exc
        plot = {"format": "svg", "path": plot_path}
    payload["plot"] = plot

    payload["markdown"] = _render(payload, markup=True)
    payload["text"] = _render(payload, markup=False)
    return payload


def milestone_table_rows(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The milestone table's data rows: (turns, before -> after, what changed).

    Exactly the shape of the hand-written table this format exists to make
    derivable -- one row per milestone, its turn span, the objective value
    it moved from and to, and the record's own note.
    """
    rows: list[tuple[str, str, str]] = []
    for milestone in payload["milestones"]:
        from_turn = milestone["from_turn"]
        turn = milestone["turn"]
        turns = str(turn) if from_turn == turn else f"{from_turn}-{turn}"
        transition = (
            f"{_fmt_value(milestone['from_value'])} -> {_fmt_value(milestone['value'])}"
        )
        rows.append((turns, transition, milestone["note"] or ""))
    return rows


def _render(payload: dict[str, Any], markup: bool) -> str:
    objective = payload["objective"]
    unit = objective["unit"]
    label = objective["name"] + (f" ({unit})" if unit else "")
    title = f"Optimization Trajectory: {label}"

    lines: list[str] = []
    if markup:
        lines.append(f"## {title}")
    else:
        lines.append(title)
        lines.append("-" * len(title))

    for info in _summary_lines(payload):
        lines.append(f"- {info}" if markup else info)

    lines.append("")
    rows = milestone_table_rows(payload)
    headers = ("turns", label, "what changed")
    if rows:
        lines.append(
            _markdown_table(headers, rows) if markup else _plain_table(headers, rows)
        )
    else:
        lines.append(
            "No milestones: no candidate improved the objective by more than "
            "the configured threshold."
        )

    plot = payload.get("plot")
    if plot:
        lines.append("")
        lines.append(
            f"![objective vs turn]({plot['path']})"
            if markup
            else f"plot: {plot['path']}"
        )

    return "\n".join(lines) + "\n"


def _summary_lines(payload: dict[str, Any]) -> list[str]:
    objective = payload["objective"]
    threshold = payload["threshold"]
    lines = [
        f"Objective: {objective['name']} ({objective['polarity']})",
        (
            f"Records: {payload['record_count']} "
            f"(turns {payload['first_turn']}-{payload['last_turn']})"
        ),
        f"Milestones: {payload['milestone_count']}",
        (
            f"Milestone threshold: >{_fmt_value(threshold['absolute'])} absolute, "
            f">{_fmt_value(threshold['percent'])}% relative"
        ),
    ]

    baseline = payload["baseline"]
    best = payload["best"]
    if baseline is not None and best is not None:
        lines.append(
            f"Baseline: {_fmt_value(baseline['value'])} at turn {baseline['turn']}"
        )
        lines.append(f"Best: {_fmt_value(best['value'])} at turn {best['turn']}")
    if payload["total_improvement"] is not None:
        lines.append(
            f"Total improvement: {_fmt_value(payload['total_improvement'])} "
            f"({_fmt_percent(payload['total_improvement_pct'])})"
        )
    if payload["wall_clock_s_total"] is not None:
        lines.append(f"Wall clock: {payload['wall_clock_s_total']:.1f}s total")
    return lines


def _fmt_value(value: float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.6g}"
    return f"{int(value):,d}"


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf%"
    return f"{value:.1f}%"


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    def escape(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(escape(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(escape(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _plain_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [
        max(len(headers[col]), max((len(row[col]) for row in rows), default=0))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(row[col].ljust(widths[col]) for col in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Plot (dependency-free SVG)
# --------------------------------------------------------------------------- #

_PLOT_WIDTH = 720
_PLOT_HEIGHT = 320
_PLOT_MARGIN_LEFT = 78
_PLOT_MARGIN_RIGHT = 18
_PLOT_MARGIN_TOP = 34
_PLOT_MARGIN_BOTTOM = 42


def render_plot_svg(
    payload: dict[str, Any],
    width: int = _PLOT_WIDTH,
    height: int = _PLOT_HEIGHT,
) -> str:
    """Render an objective-vs-turn plot of ``payload`` as a standalone SVG.

    SVG, hand-emitted, on purpose: a README-embeddable chart must not drag
    a plotting stack (matplotlib and friends) into a toolkit whose runtime
    dependencies are KLayout and jsonschema, and whose every command must
    run headless in CI. The output is deterministic, diffable text.

    The line follows every eligible candidate in turn order; gate-failing
    candidates are drawn as hollow marks off the line (they are evidence,
    not progress), and milestones as filled marks with their turn labelled.
    """
    objective = payload["objective"]
    unit = objective["unit"]
    label = objective["name"] + (f" ({unit})" if unit else "")
    records = payload["records"]

    turns = [entry["turn"] for entry in records]
    values = [entry["value"] for entry in records]
    x_min, x_max = min(turns), max(turns)
    y_min, y_max = min(values), max(values)
    if x_max == x_min:
        x_max = x_min + 1
    if y_max == y_min:
        # A perfectly flat trajectory still deserves a readable axis.
        pad = abs(y_min) * 0.1 or 1.0
        y_min, y_max = y_min - pad, y_max + pad

    plot_left = _PLOT_MARGIN_LEFT
    plot_right = width - _PLOT_MARGIN_RIGHT
    plot_top = _PLOT_MARGIN_TOP
    plot_bottom = height - _PLOT_MARGIN_BOTTOM

    def sx(turn: float) -> float:
        return plot_left + (turn - x_min) / (x_max - x_min) * (plot_right - plot_left)

    def sy(value: float) -> float:
        return plot_bottom - (value - y_min) / (y_max - y_min) * (
            plot_bottom - plot_top
        )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{_esc(label)} versus turn">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{plot_left}" y="20" font-family="sans-serif" font-size="14" '
        f'fill="#111111">{_esc(label)} vs turn</text>',
        f'<path d="M {plot_left} {plot_top} L {plot_left} {plot_bottom} '
        f'L {plot_right} {plot_bottom}" fill="none" stroke="#666666" '
        f'stroke-width="1"/>',
    ]

    for value in (y_max, y_min):
        parts.append(
            f'<text x="{plot_left - 6}" y="{sy(value) + 4:.1f}" '
            f'text-anchor="end" font-family="sans-serif" font-size="11" '
            f'fill="#444444">{_esc(_fmt_value(value))}</text>'
        )
    for turn, anchor, x in (
        (x_min, "start", plot_left),
        (x_max, "end", plot_right),
    ):
        parts.append(
            f'<text x="{x}" y="{plot_bottom + 16}" text-anchor="{anchor}" '
            f'font-family="sans-serif" font-size="11" fill="#444444">'
            f"turn {turn}</text>"
        )

    eligible = [entry for entry in records if entry["gate_status"] != "fail"]
    if len(eligible) >= 2:
        points = " ".join(
            f"{sx(entry['turn']):.1f},{sy(entry['value']):.1f}" for entry in eligible
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="#2563eb" '
            f'stroke-width="2"/>'
        )

    for entry in records:
        cx, cy = sx(entry["turn"]), sy(entry["value"])
        if entry["gate_status"] == "fail":
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="none" '
                f'stroke="#dc2626" stroke-width="1.5"/>'
            )
        elif entry["milestone"]:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="#dc2626"/>'
            )
            parts.append(
                f'<text x="{cx:.1f}" y="{cy - 9:.1f}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="10" fill="#dc2626">'
                f"{entry['turn']}</text>"
            )
        else:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.5" fill="#2563eb"/>'
            )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
