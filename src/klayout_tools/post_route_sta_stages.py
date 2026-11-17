"""Per-stage delay breakdown of ``klt sta``'s reported worst paths (issue
#5783): the Tcl that makes the engine re-report the one worst path each side
already selected, and the parser that turns that report into the ``stages``
list attached to ``worst_setup_path`` / ``worst_hold_path``.

Split out of :mod:`klayout_tools.post_route_sta` for the same reason
:mod:`klayout_tools.post_route_sta_path_groups` was: that module sits exactly
at its ``file-size-baseline.json`` ceiling, so this feature's additions land
here and ``post_route_sta`` keeps only call-site changes (with
:func:`_worst_path_lines` moved here unchanged and re-imported under the same
name, so ``post_route_sta._worst_path_lines`` references keep working).
``post_route_sta`` re-imports every name it uses.

The engine mechanism, verified live (OpenROAD 26Q3-1510, 2026-11-16): the
worst-path block (:func:`_worst_path_lines`) and the stage query below both
run ``find_timing_paths -path_delay <side> -group_path_count 1
-endpoint_path_count 1`` in the same session with the same constraints, so
re-reducing it to the minimum-slack path end selects the same path the
worst-path record already reports (the attach step re-checks that equality
via the slack-consistency gate rather than assuming it). The per-side stage
report is then ``report_checks`` pinned to *that* path's endpoint, so the
table cannot drift to another path: its rows are cut at the record's own
``startpoint``/``endpoint`` pins, and a table whose arrival/required times do
not reproduce the record's ``slack_ns`` is reported as ``unavailable``,
never attached.
"""

from __future__ import annotations

import re
from typing import Any

from .post_route_sta_path_groups import (
    _SLACK_CONSISTENCY_TOL_NS,
    _WORST_PATHS_BEGIN,
    _WORST_PATHS_END,
    _extract_block,
)

#: Issue #5783: brackets the per-side worst-path stage reports
#: (:func:`_worst_path_stage_lines`), emitted only when the request set
#: ``constraints.report_path_stages`` -- so an unrequested session prints
#: nothing between these markers and its stdout is unchanged.
_PATH_STAGES_BEGIN = "===KLT_STA_PATH_STAGES_BEGIN==="
_PATH_STAGES_END = "===KLT_STA_PATH_STAGES_END==="

#: Tab-delimited row bracketing one side's ``report_checks`` output inside
#: the stage block: ``PATH_STAGES\t<max|min>\tBEGIN`` / ``...\tEND``.
_PATH_STAGES_ROW = "PATH_STAGES"


def _worst_path_lines() -> list[str]:
    """Per-path-group worst setup (``max``) and hold (``min``) path ends,
    marker-delimited (issue #2740).

    ``find_timing_paths -group_path_count 1 -endpoint_path_count 1`` returns
    the single worst constrained path end of every path group (data clock
    groups, ``asynchronous``, ``gated clock``, ...); every constrained path
    end belongs to exactly one group, so the minimum over these records is
    the same population ``report_worst_slack_metric`` reduces over --
    :func:`_worst_path_record` re-checks that equality per run rather than
    assuming it. ``check_role`` is OpenSTA's own name for the check at the
    endpoint (``setup``, ``recovery``, ``clock gating hold``, ...), so
    recovery/removal are never relabelled as data setup/hold. Output is
    bounded by the number of path groups, not design size. The whole query
    is ``catch``-wrapped: an engine without these commands yields an
    ``ERROR`` row (reported as ``status: "unavailable"``), never a fabricated
    path.

    Moved verbatim out of :mod:`klayout_tools.post_route_sta` (issue #5783)
    to keep that module under its file-size ceiling; ``post_route_sta``
    re-imports this name, so its call sites and tests are unchanged."""
    return [
        f'puts "{_WORST_PATHS_BEGIN}"',
        "if {[catch {",
        "    foreach klt_path_side {max min} {",
        "        foreach klt_pe [find_timing_paths -path_delay $klt_path_side "
        "-group_path_count 1 -endpoint_path_count 1] {",
        '            puts "PATH\\t$klt_path_side\\t[$klt_pe check_role]\\t'
        "[$klt_pe slack]\\t[get_full_name [get_property $klt_pe startpoint]]\\t"
        '[get_full_name [get_property $klt_pe endpoint]]"',
        "        }",
        "    }",
        "} klt_worst_path_error]} {",
        '    puts "ERROR\\t[string map [list \\n { } \\t { }] $klt_worst_path_error]"',
        "}",
        f'puts "{_WORST_PATHS_END}"',
    ]


def _worst_path_stage_lines(report_path_stages: bool) -> list[str]:
    """Marker-delimited per-stage ``report_checks`` for the one worst path
    each side already selected (issue #5783); **no lines at all** without
    ``report_path_stages``, so an unrequested session is byte-identical.

    For each side (``max`` then ``min``): the same
    ``find_timing_paths -group_path_count 1 -endpoint_path_count 1`` query
    :func:`_worst_path_lines` runs (same session, same constraints -- the
    same path-end population the worst-path records reduce), the
    minimum-slack path end of that population (the one
    ``_worst_path_record`` reports), then ``report_checks -to <its
    endpoint>`` for that side, so the printed table is that path's own
    per-stage delay report, not a separately chosen path. ``-digits 6`` is
    the resolution the stage parser's consistency checks need. A side with
    no constrained path prints no region at all -- the ``no_paths`` worst
    -path record is where that fact is reported. The whole block is
    ``catch``-wrapped: a failed query yields a single ``ERROR`` row
    (reported as ``status: "unavailable"`` on both records), never a
    fabricated table."""
    if not report_path_stages:
        return []
    return [
        f'puts "{_PATH_STAGES_BEGIN}"',
        "if {[catch {",
        "    foreach klt_stage_side {max min} {",
        "        set klt_stage_pes [find_timing_paths -path_delay "
        "$klt_stage_side -group_path_count 1 -endpoint_path_count 1]",
        "        if {[llength $klt_stage_pes] > 0} {",
        '            set klt_stage_worst ""',
        "            set klt_stage_slack 0",
        "            foreach klt_stage_pe $klt_stage_pes {",
        "                set klt_stage_s [$klt_stage_pe slack]",
        '                if {$klt_stage_worst eq "" '
        "|| $klt_stage_s < $klt_stage_slack} {",
        "                    set klt_stage_worst $klt_stage_pe",
        "                    set klt_stage_slack $klt_stage_s",
        "                }",
        "            }",
        "            set klt_stage_end [get_full_name "
        "[get_property $klt_stage_worst endpoint]]",
        '            puts "' + _PATH_STAGES_ROW + '\\t$klt_stage_side\\tBEGIN"',
        "            report_checks -to $klt_stage_end "
        "-path_delay $klt_stage_side -group_path_count 1 "
        "-endpoint_path_count 1 -format full -digits 6",
        '            puts "' + _PATH_STAGES_ROW + '\\t$klt_stage_side\\tEND"',
        "        }",
        "    }",
        "} klt_stage_error]} {",
        '    puts "ERROR\\t[string map [list \\n { } \\t { }] $klt_stage_error]"',
        "}",
        f'puts "{_PATH_STAGES_END}"',
    ]


# -- parsing ---------------------------------------------------------------- #

#: One ``report_checks -format full`` stage row:
#: ``<incr> <path> <edge chars> <pin> (<cell>)`` -- both numbers mandatory,
#: which is what separates data rows from the table's one-number description
#: rows (``data arrival time``) and its one-number capture rows
#: (``^ ff_b/CLK (...)`` has no ``Delay`` column value). The edge field is
#: optional in the pattern (all rows observed live carry ``^``/``v``, but
#: the identity that matters -- ``pin`` -- is exact-matched against the
#: worst-path record's own ``startpoint``/``endpoint``, so a loose pattern
#: can only ever over-*match*, never mis-attribute a row: the description
#: rows that share this shape (``clock clk (rise edge)``,
#: ``clock network delay (ideal)``) carry pin names that never equal a
#: data path's own startpoint or endpoint, and they sit outside the
#: startpoint..endpoint window anyway).
_STAGE_ROW_RE = re.compile(
    r"^(?P<incr>-?\d+\.\d+)\s+(?P<path>-?\d+\.\d+)\s*(?P<edges>[\^v]{0,2})\s*"
    r"(?P<pin>\S+)\s+\((?P<cell>[^)]+)\)$"
)

#: The table's own arrival/required summary rows; the first match of each
#: is the table-internal value (``data required time`` repeats in the
#: closing summary with the same value, and ``data arrival time``'s
#: closing-summary repeat is sign-flipped, so the first occurrence is the
#: one to read).
_ARRIVAL_RE = re.compile(r"^\s*(-?\d+\.\d+)\s+data arrival time\s*$")
_REQUIRED_RE = re.compile(r"^\s*(-?\d+\.\d+)\s+data required time\s*$")


def _unavailable_stage_block(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "stages": None}


def _parse_path_stage_block(
    stdout: str,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """``({side: [lines]}, None)`` from :func:`_worst_path_stage_lines`'s
    block -- each side's ``report_checks`` output verbatim -- or
    ``(None, reason)`` when the block is absent or reports an engine error
    (the same contract as ``_parse_worst_path_rows``). Lines outside any
    ``BEGIN``/``END`` region (stray engine warnings) are ignored; a side
    the emitter printed no region for maps to no entry."""
    block = _extract_block(stdout, _PATH_STAGES_BEGIN, _PATH_STAGES_END)
    if block is None:
        return None, "path-stage reports absent from engine output"
    regions: dict[str, list[str]] = {}
    current: str | None = None
    for line in block.splitlines():
        if line.startswith("ERROR\t"):
            return None, (
                "engine path-stage introspection failed: "
                + line[len("ERROR\t") :].strip()
            )
        parts = line.split("\t")
        if (
            len(parts) == 3
            and parts[0] == _PATH_STAGES_ROW
            and parts[1] in ("max", "min")
            and parts[2] in ("BEGIN", "END")
        ):
            if parts[2] == "BEGIN":
                current = parts[1]
                regions[current] = []
            else:
                current = None
            continue
        if current is not None:
            regions[current].append(line)
    return regions, None


def _stage_rows(
    region: list[str], startpoint: str, endpoint: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``([stage rows], None)`` from one side's ``report_checks`` output, or
    ``(None, reason)`` when the report names no path, does not contain the
    record's own path end pins, or is malformed.

    Only rows between the ``startpoint`` pin row and the ``endpoint`` pin
    row become stages -- the same rows, in launch-to-capture order, the
    table prints its ``Delay``/``Time`` columns for. Everything before the
    startpoint row is the launch clock network and I/O delay (whose delay
    the table has *already accumulated* into the startpoint row's ``Time``,
    so the window's increments never re-sum to the final ``path_ns``);
    everything after the endpoint row is the capture-side check summary.
    ``cell`` is the engine's parenthesised description: the liberty cell
    name, or ``in``/``out`` for a top-level port pin (verified live,
    OpenROAD 26Q3-1510)."""
    rows: list[dict[str, Any]] = []
    started = False
    for line in region:
        match = _STAGE_ROW_RE.match(line.strip())
        if match is None:
            continue
        pin = match.group("pin")
        if not started:
            if pin != startpoint:
                continue
            started = True
        rows.append(
            {
                "pin": pin,
                "cell": match.group("cell"),
                "incr_ns": float(match.group("incr")),
                "path_ns": float(match.group("path")),
            }
        )
        if pin == endpoint:
            break
    if not started:
        return None, f"stage report has no {startpoint!r} startpoint row"
    if rows[-1]["pin"] != endpoint:
        return None, f"stage report has no {endpoint!r} endpoint row"
    return rows, None


def _parse_path_stage_region(
    region: list[str], record: dict[str, Any], side: str
) -> dict[str, Any]:
    """The ``stages`` block for one side's record (issue #5783):
    ``{status, reason, stages}`` where ``stages`` is the
    ``{pin, cell, incr_ns, path_ns}`` list in launch-to-capture order, or
    ``None`` with a stated ``reason``.

    Two consistency gates, both against the record the stages are attached
    to, decide ``ok`` vs ``unavailable`` (never a silent mismatch):

    - each stage's ``path_ns`` equals the previous stage's ``path_ns`` plus
      its own ``incr_ns`` (the engine's ``Delay``/``Time`` columns are
      internally consistent; the *first* stage is exempt -- its window
      opens after the launch clock network / external delay, whose delay
      the table has already accumulated into the first stage's ``path_ns``),
    - the table's ``data arrival time`` minus ``data required time`` must
      reproduce the record's ``slack_ns`` with the sign the side mandates,
      both verified live (OpenROAD 26Q3-1510, 2026-11-16, the boundary
      fixture session): a setup (``max``) check prints
      ``slack = required - arrival``, so ``arrival - required ==
      -slack_ns``; a hold/removal (``min``) check prints
      ``slack = arrival - required``, so ``arrival - required ==
      slack_ns``. Either form uses the same
      :data:`_SLACK_CONSISTENCY_TOL_NS` tolerance the worst-path record
      itself uses against the session aggregate. This is what binds the
      table to *that* path: any other path's arrival/required pair fails
      it, and a failed gate is reported as ``unavailable`` with the
      numbers in ``reason``, never as a table for the wrong path."""
    status = record["status"]
    if status != "ok":
        reason = f"worst path status is {status}"
        if record["reason"]:
            reason += f": {record['reason']}"
        return _unavailable_stage_block(reason)
    rows, reason = _stage_rows(region, record["startpoint"], record["endpoint"])
    if rows is None:
        return _unavailable_stage_block(reason or "stage report unparseable")
    arrival = _first_number(region, _ARRIVAL_RE)
    required = _first_number(region, _REQUIRED_RE)
    if arrival is None or required is None:
        return _unavailable_stage_block(
            "stage report is missing its data arrival/required time"
        )
    for row in rows:
        row["incr_ns"] = round(row["incr_ns"], 6)
        row["path_ns"] = round(row["path_ns"], 6)
    for prev, row in zip(rows, rows[1:], strict=False):
        if abs(prev["path_ns"] + row["incr_ns"] - row["path_ns"]) > (
            _SLACK_CONSISTENCY_TOL_NS
        ):
            return _unavailable_stage_block(
                f"stage row {row['pin']!r} adds {row['incr_ns']} ns to "
                f"{prev['path_ns']} ns but its path time is "
                f"{row['path_ns']} ns"
            )
    arrival_minus_required = arrival - required
    expected = -record["slack_ns"] if side == "max" else record["slack_ns"]
    slack_ns = record["slack_ns"]
    if abs(arrival_minus_required - expected) > _SLACK_CONSISTENCY_TOL_NS:
        return _unavailable_stage_block(
            f"stage report arrival {arrival} ns minus required {required} ns "
            f"({arrival_minus_required} ns) disagrees with the worst path "
            f"slack {slack_ns} ns"
        )
    return {"status": "ok", "reason": None, "stages": rows}


def _first_number(region: list[str], pattern: re.Pattern[str]) -> float | None:
    """The first ``float`` ``pattern`` extracts from the region's lines, or
    ``None`` when no line matches."""
    for line in region:
        match = pattern.match(line)
        if match is not None:
            return float(match.group(1))
    return None


def _attach_worst_path_stages(
    stdout: str, setup_record: dict[str, Any], hold_record: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach each record's ``stages`` block in place (issue #5783) and
    return ``(setup_record, hold_record)``. Called only when the request set
    ``constraints.report_path_stages`` -- the records are returned with the
    field added, never without it."""
    regions, reason = _parse_path_stage_block(stdout)
    if regions is None:
        setup_record["stages"] = _unavailable_stage_block(reason)
        hold_record["stages"] = _unavailable_stage_block(reason)
        return setup_record, hold_record
    for record, side in ((setup_record, "max"), (hold_record, "min")):
        if side not in regions:
            record["stages"] = _unavailable_stage_block(
                f"no path-stage report for the {side} side"
            )
            continue
        record["stages"] = _parse_path_stage_region(regions[side], record, side)
    return setup_record, hold_record
