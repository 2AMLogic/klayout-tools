"""Worst-path-record parsing for ``klt sta``: the per-path-group
``find_timing_paths`` block, the single ``worst_setup_path`` /
``worst_hold_path`` records reduced from it (issue #2740), the unreduced
``worst_paths_by_group`` list (issue #5424), and the per-port ``port_paths``
block and records a ``constraints.report_ports`` request emits (issue
#3042).

Split out of :mod:`klayout_tools.post_route_sta` to keep that module under its
file-size ceiling, the same way :mod:`klayout_tools.post_route_sta_electrical`
was (issues #3005, #5241). ``post_route_sta`` re-imports every name here, so
existing ``post_route_sta._parse_worst_path_rows`` etc. references keep
working.

The Tcl that produces these blocks (``_worst_path_lines`` and
``_port_path_lines``) stays in ``post_route_sta`` with the rest of the
generated script; only the markers they print and the parsers that read them
live here.
"""

from __future__ import annotations

from typing import Any


def _extract_block(text: str, begin: str, end: str) -> str | None:
    """The text between the ``begin``/``end`` markers, or ``None`` when either
    marker is absent (same contract as ``post_route_sta._extract_block``)."""
    try:
        start_idx = text.index(begin) + len(begin)
        stop_idx = text.index(end, start_idx)
    except ValueError:
        return None
    return text[start_idx:stop_idx]


#: Issue #2740: brackets the per-path-group worst setup/hold path records
#: (:func:`_worst_path_lines`), one
#: ``PATH\t<max|min>\t<check_role>\t<slack_s>\t<startpoint>\t<endpoint>`` row
#: per path group per side, or a single ``ERROR\t<message>`` row when the
#: engine's path introspection itself failed.
_WORST_PATHS_BEGIN = "===KLT_STA_WORST_PATHS_BEGIN==="
_WORST_PATHS_END = "===KLT_STA_WORST_PATHS_END==="

#: OpenSTA ``PathEnd::checkRole()`` names -> this command's ``check_type``
#: values. Verified live (OpenROAD ``openroad/orfs:latest`` image, 2026-10-10)
#: for ``setup``, ``hold``, ``recovery``, ``removal``, ``clock gating
#: setup``, ``clock gating hold`` and ``output setup``; ``output hold``,
#: ``latch setup`` and ``latch hold`` are the remaining OpenSTA
#: ``TimingRole`` names a path end can carry. Any role not listed here is
#: reported as ``check_type: "unknown"`` (with the engine's own spelling kept
#: in ``engine_check_role``) -- never guessed into a setup/hold bucket.
_CHECK_ROLE_TYPES = {
    "setup": "setup",
    "hold": "hold",
    "recovery": "recovery",
    "removal": "removal",
    "clock gating setup": "clock_gating_setup",
    "clock gating hold": "clock_gating_hold",
    "output setup": "output_setup",
    "output hold": "output_hold",
    "latch setup": "latch_setup",
    "latch hold": "latch_hold",
}

#: Largest |path slack - aggregate WNS| (ns) still treated as "the same
#: number": the aggregate comes from ``report_worst_slack_metric`` (printed
#: to 6 decimals in the engine's time unit) while the path slack is the
#: engine's own full-precision value in seconds.
_SLACK_CONSISTENCY_TOL_NS = 1e-4


def _empty_path_record(status: str, reason: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "check_type": None,
        "engine_check_role": None,
        "startpoint": None,
        "endpoint": None,
        "slack_ns": None,
        "consistent_with_aggregate": None,
        "reason": reason,
    }


def _parse_worst_path_rows(
    stdout: str,
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """``({"max": [...], "min": [...]}, None)`` from
    :func:`_worst_path_lines`' block, or ``(None, reason)`` when the block is
    absent, reports an engine error, or carries a malformed ``PATH`` row.
    Lines that are neither ``PATH`` nor ``ERROR`` rows (stray engine
    warnings) are ignored -- they can never be mistaken for a path."""
    block = _extract_block(stdout, _WORST_PATHS_BEGIN, _WORST_PATHS_END)
    if block is None:
        return None, "worst-path records absent from engine output"
    rows: dict[str, list[dict[str, Any]]] = {"max": [], "min": []}
    for line in block.splitlines():
        if line.startswith("ERROR\t"):
            return None, (
                "engine path introspection failed: " + line[len("ERROR\t") :].strip()
            )
        if not line.startswith("PATH\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 6 or parts[1] not in rows or not all(parts[2:]):
            return None, f"malformed worst-path record: {line!r}"
        try:
            slack_s = float(parts[3])
        except ValueError:
            return None, f"malformed worst-path slack: {line!r}"
        if slack_s != slack_s or abs(slack_s) == float("inf"):
            return None, f"non-finite worst-path slack: {line!r}"
        rows[parts[1]].append(
            {
                "engine_check_role": parts[2],
                "slack_ns": slack_s * 1e9,
                "startpoint": parts[4],
                "endpoint": parts[5],
            }
        )
    return rows, None


def _worst_path_record(
    rows: list[dict[str, Any]], aggregate_ns: float | None
) -> dict[str, Any]:
    """The worst (minimum-slack) record among one side's per-group rows,
    cross-checked against that side's aggregate WNS from the same session.

    ``status``:

    - ``"ok"`` -- a path was found and its slack equals the aggregate WNS
      (within :data:`_SLACK_CONSISTENCY_TOL_NS`), or the aggregate was not
      reported at all (``consistent_with_aggregate: null``).
    - ``"no_paths"`` -- the engine found no constrained path on this side,
      and the aggregate agrees (sentinel or absent). Identity fields null.
    - ``"inconsistent"`` -- the path population and the aggregate WNS
      disagree (no path but a real WNS, or a different slack). The record
      is still the engine's real worst *found* path, but it must not be
      read as the path that binds the aggregate.
    """
    aggregate_real = aggregate_ns is not None and abs(aggregate_ns) < 1e29
    if not rows:
        if aggregate_real:
            record = _empty_path_record(
                "inconsistent",
                "engine reported no constrained path but an aggregate WNS of "
                f"{aggregate_ns}",
            )
            record["consistent_with_aggregate"] = False
            return record
        return _empty_path_record("no_paths", None)
    worst = min(rows, key=lambda row: row["slack_ns"])
    role = worst["engine_check_role"]
    record = {
        "status": "ok",
        "check_type": _CHECK_ROLE_TYPES.get(role, "unknown"),
        "engine_check_role": role,
        "startpoint": worst["startpoint"],
        "endpoint": worst["endpoint"],
        "slack_ns": round(worst["slack_ns"], 5),
        "consistent_with_aggregate": None,
        "reason": None,
    }
    if aggregate_ns is None:
        return record
    consistent = aggregate_real and (
        abs(worst["slack_ns"] - aggregate_ns) <= _SLACK_CONSISTENCY_TOL_NS
    )
    record["consistent_with_aggregate"] = consistent
    if not consistent:
        record["status"] = "inconsistent"
        record["reason"] = (
            f"worst path slack {record['slack_ns']} ns differs from the "
            f"aggregate WNS {aggregate_ns}"
        )
    return record


def _worst_paths(
    stdout: str, setup_ws: float | None, hold_ws: float | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(worst_setup_path, worst_hold_path)`` for one session."""
    rows, reason = _parse_worst_path_rows(stdout)
    if rows is None:
        return (
            _empty_path_record("unavailable", reason),
            _empty_path_record("unavailable", reason),
        )
    return (
        _worst_path_record(rows["max"], setup_ws),
        _worst_path_record(rows["min"], hold_ws),
    )


def _path_group_entry(row: dict[str, Any]) -> dict[str, Any]:
    """One ``worst_paths_by_group`` entry from one parsed ``PATH`` row: the
    identity fields of :func:`_worst_path_record` without the
    aggregate-cross-check fields, which are only meaningful for the one
    record that may bind the session aggregate."""
    role = row["engine_check_role"]
    return {
        "check_type": _CHECK_ROLE_TYPES.get(role, "unknown"),
        "engine_check_role": role,
        "startpoint": row["startpoint"],
        "endpoint": row["endpoint"],
        "slack_ns": round(row["slack_ns"], 5),
    }


def _worst_paths_by_group(stdout: str) -> dict[str, Any]:
    """``worst_paths_by_group`` for one session (issue #5424): every
    per-path-group row :func:`_worst_path_lines` reported, unreduced.

    :func:`_worst_paths` keeps only the minimum-slack row per side, so a
    setup-side aggregate bound by an asynchronous *recovery* check leaves no
    field carrying the worst *data* setup slack -- the number most callers
    gate on. This block is the same parsed rows, each side sorted ascending
    by slack (worst first, engine order preserved on ties), so a caller
    reads a per-check-family slack out of the response instead of scraping
    the retained engine log.

    ``status`` is ``"ok"`` with ``reason: null`` whenever the rows parsed --
    a side the engine found no constrained path on is an empty list, not a
    failure -- and ``"unavailable"`` with both lists empty and the same
    ``reason`` the ``worst_*_path`` records carry when the parse itself
    failed. The rows are never partially reported.
    """
    rows, reason = _parse_worst_path_rows(stdout)
    if rows is None:
        return {"status": "unavailable", "reason": reason, "setup": [], "hold": []}
    return {
        "status": "ok",
        "reason": None,
        "setup": [
            _path_group_entry(row)
            for row in sorted(rows["max"], key=lambda row: row["slack_ns"])
        ],
        "hold": [
            _path_group_entry(row)
            for row in sorted(rows["min"], key=lambda row: row["slack_ns"])
        ],
    }


#: Issue #3042: brackets the per-port worst-path records
#: (:func:`post_route_sta._port_path_lines`) a ``constraints.report_ports``
#: request emits. Per requested port: one ``DIR\t<port>\t<direction>`` row,
#: then ``PATH\t<port>\t<max|min>\t<check_role>\t<slack_s>\t<startpoint>\t
#: <endpoint>`` rows (an ``inout`` port contributes its ``-from`` and its
#: ``-to`` paths), or a single ``ERROR\t<port>\t<message>`` row when that
#: port's query failed. Moved here verbatim (issue #5754) so the per-instance
#: parser in ``post_route_sta_instance_paths`` can sit beside its own
#: markers without growing ``post_route_sta`` past its file-size ceiling;
#: ``post_route_sta`` re-imports every name below.
_PORT_PATHS_BEGIN = "===KLT_STA_PORT_PATHS_BEGIN==="
_PORT_PATHS_END = "===KLT_STA_PORT_PATHS_END==="


def _apply_port_record(
    parts: list[str],
    line: str,
    directions: dict[str, str],
    port_rows: dict[str, list[dict[str, Any]]],
) -> str | None:
    """Fold one ``DIR``/``PATH``/``ERROR`` record into ``directions`` /
    ``port_rows``; return a failure reason when the record is bad, else
    ``None``."""
    kind, port = parts[0], parts[1]
    if kind == "ERROR":
        return "engine path introspection failed: " + "\t".join(parts[2:]).strip()
    if kind == "DIR":
        if len(parts) != 3 or not parts[2]:
            return f"malformed port record: {line!r}"
        directions[port] = "inout" if parts[2] == "bidirect" else parts[2]
        return None
    if len(parts) != 7 or parts[2] not in ("max", "min") or not all(parts[3:]):
        return f"malformed port path record: {line!r}"
    try:
        slack_s = float(parts[4])
    except ValueError:
        return f"malformed port path slack: {line!r}"
    if slack_s != slack_s or abs(slack_s) == float("inf"):
        return f"non-finite port path slack: {line!r}"
    port_rows[parts[2]].append(
        {
            "engine_check_role": parts[3],
            "slack_ns": slack_s * 1e9,
            "startpoint": parts[5],
            "endpoint": parts[6],
        }
    )
    return None


def _parse_port_path_rows(
    stdout: str, ports: list[str]
) -> dict[str, tuple[str | None, dict[str, list[dict[str, Any]]] | None, str | None]]:
    """``{port: (direction, rows, reason)}`` from ``post_route_sta``'s
    ``_port_path_lines`` block, in request order. ``rows`` is
    ``{"max": [...], "min": [...]}`` (``None`` with a ``reason`` when the
    port's query failed, its record was malformed, or the block/port is
    absent). Lines that are neither ``DIR``, ``PATH`` nor ``ERROR`` rows
    (stray engine warnings) are ignored."""
    block = _extract_block(stdout, _PORT_PATHS_BEGIN, _PORT_PATHS_END)
    if block is None:
        reason = "per-port path records absent from engine output"
        return {port: (None, None, reason) for port in ports}
    directions: dict[str, str] = {}
    rows: dict[str, dict[str, list[dict[str, Any]]]] = {
        port: {"max": [], "min": []} for port in ports
    }
    failures: dict[str, str] = {}
    for line in block.splitlines():
        parts = line.split("\t")
        if parts[0] not in ("DIR", "PATH", "ERROR") or len(parts) < 2:
            continue
        port = parts[1]
        if port not in rows or port in failures:
            continue
        failure = _apply_port_record(parts, line, directions, rows[port])
        if failure is not None:
            failures[port] = failure
    parsed: dict[
        str, tuple[str | None, dict[str, list[dict[str, Any]]] | None, str | None]
    ] = {}
    for port in ports:
        if port in failures:
            parsed[port] = (directions.get(port), None, failures[port])
        elif port not in directions:
            parsed[port] = (None, None, "no record emitted for this port")
        else:
            parsed[port] = (directions[port], rows[port], None)
    return parsed


def _port_paths(stdout: str, report_ports: list[str]) -> dict[str, dict[str, Any]]:
    """``port_paths`` for one session (issue #3042): per requested port, in
    request order, its ``direction`` and the worst ``setup``/``hold``
    records. There is no per-port aggregate to cross-check against, so
    ``consistent_with_aggregate`` stays ``null`` and ``inconsistent`` is
    never produced; a side with no constrained path is ``no_paths``."""
    result: dict[str, dict[str, Any]] = {}
    for port, (direction, rows, reason) in _parse_port_path_rows(
        stdout, report_ports
    ).items():
        if rows is None:
            setup = _empty_path_record("unavailable", reason)
            hold = _empty_path_record("unavailable", reason)
        else:
            setup = _worst_path_record(rows["max"], None)
            hold = _worst_path_record(rows["min"], None)
        result[port] = {"direction": direction, "setup": setup, "hold": hold}
    return result
