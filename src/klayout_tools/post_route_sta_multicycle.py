"""The ``klt sta``-side halves of ``constraints.multicycle_paths`` (issue
#6921): the stdout parsing (:func:`multicycle_request_error`,
:func:`parse_multicycle_measurements`), the ``multicycle_paths`` response
block (:func:`multicycle_response`), and the per-corner driver
(:func:`multicycle_corner_field`) that launches one "without" session per
entry -- the with-run's own script minus that entry's tagged
``set_multicycle_path`` pair, every other entry kept in place (the
unambiguous baseline), so a ``without`` number is never the global WNS and
never a re-run with all exceptions dropped.

Split from :mod:`klayout_tools._sta_multicycle` (which keeps the request
validator, the selector grammar and the paired Tcl emitter -- the halves
``place_and_route`` #6923 reuses) for the per-file size ratchet, the same
way :mod:`klayout_tools.post_route_sta_electrical` and related helpers were.
Request errors use the shared ``PostRouteStaError`` class."""

from __future__ import annotations

import os
from typing import Any

from ._openroad_engine import run_openroad
from ._openroad_io import write_script
from ._sta_multicycle import (
    MCP_BEGIN,
    MCP_END,
    MCPERR_BEGIN,
    MCPERR_END,
    without_script_lines,
)
from ._sta_request_diag import extract_block as _extract_block
from .post_route_sta_errors import PostRouteStaError

# --------------------------------------------------------------------------- #
# Stdout parsing
# --------------------------------------------------------------------------- #


def _selector_text(entry: dict[str, Any]) -> str:
    selection = f"from {entry['from']!r}"
    if entry["through"] is not None:
        selection += f" through {entry['through']!r}"
    return selection + f" to {entry['to']!r}"


def _format_resolution_error(index: str, message: str) -> str:
    field = f"constraints.multicycle_paths[{index}]"
    parts = message.split(None, 2)
    if parts[0] in ("from", "to", "through") and len(parts) >= 2:
        key, problem = parts[0], parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        if problem == "matched_nothing":
            noun = "pin" if key == "through" else "register or pin"
            return f"{field}.{key} matches no {noun} in the linked design"
        if problem == "unclocked_register":
            return (
                f"{field}.{key} selects unclocked register '{detail}' -- "
                "a multicycle exception needs a clocked launch and capture"
            )
    return f"{field}: {message}"


def multicycle_request_error(stdout: str) -> str | None:
    """The request-error message for a session whose selector resolution
    rejected entries (``MCPERR`` rows between :data:`MCPERR_BEGIN` /
    :data:`MCPERR_END`), or ``None``. Names the entry index, like
    ``post_route_sta``'s own port-check messages do."""
    block = _extract_block(stdout, MCPERR_BEGIN, MCPERR_END)
    if block is None:
        return None
    details = []
    for line in block.splitlines():
        parts = line.split("\t")
        if parts[0] != "MCPERR" or len(parts) < 3:
            continue
        details.append(_format_resolution_error(parts[1], parts[2].strip()))
    if not details:
        return None
    return "constraints.multicycle_paths rejected: " + "; ".join(details)


def no_timed_path_error(entry: dict[str, Any]) -> str:
    return (
        f"constraints.multicycle_paths[{entry['index']}] rejected: the "
        f"selection ({_selector_text(entry)}) matches existing objects but "
        "no timed path runs through it -- an exception must select at "
        "least one timed path"
    )


def parse_multicycle_measurements(
    stdout: str, entries: list[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """``{index: {"setup": rows, "hold": rows, "reason": str | None}}`` from
    one session's :data:`MCP_BEGIN`/:data:`MCP_END` block, where ``rows`` is
    a list of ``(slack_ns, endpoint)`` pairs and ``reason`` is set when a
    side's query failed or returned a malformed row (that side's rows are
    ``None`` then, never ``0`` and never the global WNS). A present-but-
    empty list is "no timed path on this side" -- distinct from a missing
    block, which leaves every side ``None`` with a reason."""
    failed: dict[int, dict[str, str]] = {entry["index"]: {} for entry in entries}
    rows: dict[int, dict[str, list[tuple[float, str]]]] = {
        entry["index"]: {"setup": [], "hold": []} for entry in entries
    }
    block = _extract_block(stdout, MCP_BEGIN, MCP_END)
    if block is None:
        return {
            index: {
                "setup": None,
                "hold": None,
                "reason": "multicycle path measurements absent from engine output",
            }
            for index in rows
        }
    for line in block.splitlines():
        _fold_measure_row(line, rows, failed)
    return {
        index: {
            "setup": None if "setup" in failed[index] else sides["setup"],
            "hold": None if "hold" in failed[index] else sides["hold"],
            "reason": "; ".join(
                f"{side}: {why}" for side, why in sorted(failed[index].items())
            )
            or None,
        }
        for index, sides in rows.items()
    }


def _fold_measure_row(
    line: str,
    rows: dict[int, dict[str, list[tuple[float, str]]]],
    failed: dict[int, dict[str, str]],
) -> None:
    """Fold one ``MCP``/``MCPERR`` row into ``rows``/``failed`` in place,
    ignoring every line that is neither (stray engine warnings) or names an
    unknown entry index."""
    parts = line.split("\t")
    if parts[0] not in ("MCP", "MCPERR") or len(parts) < 2:
        return
    try:
        index = int(parts[1])
    except ValueError:
        return
    if index not in rows:
        return
    side = {"max": "setup", "min": "hold"}.get(parts[2] if len(parts) > 2 else "")
    if side is None:
        if parts[0] == "MCPERR":  # side-less: both sides failed
            reason = "engine query failed: " + "\t".join(parts[3:]).strip()
            failed[index]["setup"] = reason
            failed[index]["hold"] = reason
        return
    if parts[0] == "MCPERR":
        failed[index][side] = "engine query failed: " + "\t".join(parts[3:]).strip()
        return
    problem = _measure_row_problem(parts)
    if problem is None:
        rows[index][side].append((float(parts[3]), parts[4]))
    else:
        failed[index][side] = f"{problem}: {line!r}"


def _measure_row_problem(parts: list[str]) -> str | None:
    """Why one ``MCP`` row cannot be measured, or ``None`` if it can."""
    if len(parts) != 5:
        return "malformed engine row"
    try:
        slack = float(parts[3])
    except ValueError:
        return "malformed engine slack"
    if slack != slack or abs(slack) == float("inf"):
        return "non-finite engine slack"
    if not parts[4].strip():
        return "malformed engine row"
    return None


# --------------------------------------------------------------------------- #
# The response block
# --------------------------------------------------------------------------- #


def _worst_slack(rows: list[tuple[float, str]] | None) -> float | None:
    if not rows:
        return None
    return round(min(slack for slack, _ in rows), 5)


def _distinct_endpoints(measurements: dict[str, Any]) -> int:
    return len(
        {
            endpoint
            for side in ("setup", "hold")
            for _, endpoint in (measurements[side] or ())
        }
    )


def _side_block(
    with_rows: list[tuple[float, str]] | None,
    without_rows: list[tuple[float, str]] | None,
    side: str,
    reasons: list[str],
) -> dict[str, float | None]:
    block: dict[str, float | None] = {}
    for run, run_rows in (("with", with_rows), ("without", without_rows)):
        if run_rows is None:
            reasons.append(f"{side}_slack_ns.{run} unmeasurable")
            block[run] = None
        else:
            block[run] = _worst_slack(run_rows)
            if not run_rows:
                reasons.append(f"{side}_slack_ns.{run}: no timed path")
    return block


def multicycle_response(
    entries: list[dict[str, Any]],
    with_measurements: dict[int, dict[str, Any]],
    without_measurements: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``multicycle_paths`` response block (issue #6921): one entry per
    request entry, in request order, carrying the selectors and multipliers
    as requested, the with-run's distinct matched endpoint count, and the
    worst setup/hold slack of the **selected paths** with and without this
    entry (the "without" run keeps every other entry in place). A side that
    could not be measured is ``null`` and appends to the entry's ``reason``
    -- never the global WNS and never ``0``."""
    result = []
    for entry in entries:
        index = entry["index"]
        with_rows = with_measurements[index]
        without_rows = without_measurements[index]
        reasons: list[str] = []
        if with_rows["reason"]:
            reasons.append(f"with: {with_rows['reason']}")
        if without_rows["reason"]:
            reasons.append(f"without: {without_rows['reason']}")
        record: dict[str, Any] = {
            "index": index,
            "from": entry["from"],
            "through": entry["through"],
            "to": entry["to"],
            "setup": entry["setup"],
            "hold": entry["hold"],
            "matched_endpoints": _distinct_endpoints(with_rows),
            "setup_slack_ns": _side_block(
                with_rows["setup"], without_rows["setup"], "setup", reasons
            ),
            "hold_slack_ns": _side_block(
                with_rows["hold"], without_rows["hold"], "hold", reasons
            ),
        }
        if reasons:
            record["reason"] = "; ".join(reasons)
        result.append(record)
    return result


def _run_without_session(
    script_lines: list[str],
    script_path: str,
    metrics_path: str,
    entries: list[dict[str, Any]],
    *,
    has_def: bool,
) -> dict[int, dict[str, Any]]:
    """One "without" session: write ``script_lines`` (the with-run's own
    script minus one entry's tagged pair), run the engine, and parse its
    measurements. The script write and the engine run go through the shared
    ``_openroad_io``/``_openroad_engine`` helpers; only this verb's own error
    formatter is lazy-imported from :mod:`klayout_tools.post_route_sta` --
    only a ``klt sta`` corner session ever reaches here, so nothing that
    imports this module for the validator or the emitter loads the verb
    module (the #6923 P&R reuse contract)."""
    from .post_route_sta import PostRouteStaError, _engine_error_message

    write_script(script_path, script_lines, label="sta", error_cls=PostRouteStaError)
    rerun = run_openroad(script_path, metrics_path, error_cls=PostRouteStaError)
    with rerun.diagnostics(PostRouteStaError):
        errors = multicycle_request_error(rerun.stdout or "")
        if errors is not None:
            raise PostRouteStaError(errors)
        if rerun.returncode != 0:
            raise PostRouteStaError(
                _engine_error_message(rerun, verilog_mode=not has_def)
            )
    return parse_multicycle_measurements(rerun.stdout or "", entries)


def multicycle_corner_field(
    stdout: str,
    entries: list[dict[str, Any]],
    lines: list[str],
    output_dir: str,
    script_tag: str,
    has_def: bool,
) -> list[dict[str, Any]]:
    """The per-corner ``multicycle_paths`` response block, assembling
    everything one with-run measured plus one "without" session per entry:

    1. Parse the with-run's measurements; a selection with **no timed path
       at all** is a request error naming the entry index.
    2. Launch one without-session per entry (:func:`_run_without_session`
       over the with-run's own script minus that entry's tagged
       ``set_multicycle_path`` pair, :func:`without_script_lines`) and parse
       its measurements the same way.
    3. Merge into the response block (:func:`multicycle_response`).
    """
    measurements = parse_multicycle_measurements(stdout, entries)
    for entry in entries:
        index = entry["index"]
        if measurements[index]["setup"] == [] and measurements[index]["hold"] == []:
            raise PostRouteStaError(no_timed_path_error(entry))
    without: dict[int, dict[str, Any]] = {}
    for entry in entries:
        index = entry["index"]
        script_path = os.path.join(output_dir, f"sta_{script_tag}__mcp{index}.tcl")
        metrics_path = os.path.join(
            output_dir, f"{script_tag}__mcp{index}_metrics.json"
        )
        # The without-session measures every entry (its script is the
        # with-run's own); only this entry's rows are its baseline.
        without[index] = _run_without_session(
            without_script_lines(lines, index),
            script_path,
            metrics_path,
            entries,
            has_def=has_def,
        )[index]
    return multicycle_response(entries, measurements, without)


def check_multicycle_errors(
    stdout: str, multicycle: list[dict[str, Any]] | None
) -> None:
    """Issue #6921: a rejected entry stops the session before any
    timing is reported, like a rejected per-port map key above."""
    if multicycle is None:
        return
    errors = multicycle_request_error(stdout)
    if errors is not None:
        raise PostRouteStaError(errors)


def multicycle_session_fields(
    stdout: str,
    multicycle: list[dict[str, Any]] | None,
    lines: list[str],
    output_dir: str,
    script_tag: str,
    has_def: bool,
) -> dict[str, Any]:
    """Measurements and the total engine sessions used by this corner.

    The with-run plus one comparison per entry contribute to the count.
    A corner without entries adds no fields to the existing response.
    """
    if not multicycle:
        return {}
    return {
        "multicycle_paths": multicycle_corner_field(
            stdout, multicycle, lines, output_dir, script_tag, has_def
        ),
        "multicycle_engine_sessions": 1 + len(multicycle),
    }
