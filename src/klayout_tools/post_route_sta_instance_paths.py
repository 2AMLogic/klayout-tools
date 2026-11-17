"""Per-instance worst-path queries for ``klt sta``: the
``constraints.report_instances`` request field (issue #5754), the
``instance_paths`` response key it produces, and the Tcl both sides need.

Split out of :mod:`klayout_tools.post_route_sta` to keep that module under its
file-size ceiling, the same way :mod:`klayout_tools.post_route_sta_electrical`
and :mod:`klayout_tools.post_route_sta_path_groups` were (issues #3005, #5241,
#5424). ``post_route_sta`` imports every name it calls from here; nothing in
this module imports ``post_route_sta``, so there is no import cycle.

The request field is a list of unique, literal, hierarchical **instance**
names -- the same key rules as ``constraints.report_ports`` (no wildcards, no
whole-bus/pattern forms, no Tcl), minus the clock-port rejection, which has no
meaning for instances. Selection is by literal name only because
``get_cells``/``get_pins`` re-parse their argument as a Tcl glob list (issue
#3050): every lookup below resolves objects through a dict keyed by
``get_full_name``, never through a re-parsed name.

Per instance the response carries ``into`` (the worst path **arriving at**
the instance's input pins) and ``out_of`` (the worst path **launching from**
its output pins), each with ``setup`` and ``hold`` records of the existing
path-record shape (:func:`post_route_sta_path_groups._worst_path_record`).
Four records per entry, fixed, regardless of the instance's pin count -- and
each record's ``startpoint``/``endpoint`` names the specific pin, so
pin-level detail survives without pin-level request granularity. There is no
per-instance aggregate to cross-check, so ``consistent_with_aggregate`` stays
``null`` and ``status`` is never ``"inconsistent"``, exactly as for
``port_paths``.

The additive-field contract: a request without ``report_instances`` (or with
``[]``) emits no Tcl and no ``instance_paths`` key -- the generated script is
byte-identical to a request without the field.
"""

from __future__ import annotations

from typing import Any

from .post_route_sta_path_groups import (
    _empty_path_record,
    _extract_block,
    _worst_path_record,
)


def _error(message: str) -> Exception:
    from .post_route_sta import PostRouteStaError

    return PostRouteStaError(message)


#: Issue #5754: brackets the per-instance worst-path records
#: (:func:`_instance_path_lines`) a ``constraints.report_instances`` request
#: emits. Per requested instance, one
#: ``IPATH\t<instance>\t<into|out_of>\t<max|min>\t<check_role>\t<slack_s>\t
#: <startpoint>\t<endpoint>`` row per side the engine found a path for, or a
#: single ``ERROR\t<instance>\t<message>`` row when that instance's query
#: failed.
_INSTANCE_PATHS_BEGIN = "===KLT_STA_INSTANCE_PATHS_BEGIN==="
_INSTANCE_PATHS_END = "===KLT_STA_INSTANCE_PATHS_END==="

_TCL_SPECIAL = frozenset(' \t\r\n\\"$[]{};#')


def _tcl_literal(text: str) -> str:
    """Backslash-quote ``text`` into one literal Tcl word: no word
    splitting, no ``$``/``[...]``/backslash substitution, no brace
    matching. Byte-for-byte the helper ``post_route_sta`` and
    ``lvs_netgen.py`` carry under the same name -- kept module-local per
    this package's self-contained convention."""
    return "".join("\\" + ch if ch in _TCL_SPECIAL else ch for ch in text)


def _validate_report_instances(constraints: dict[str, Any]) -> list[str] | None:
    """``constraints.report_instances`` (issue #5754): optional list of
    unique literal hierarchical instance names whose own worst setup/hold
    paths are reported as ``instance_paths``. Same key rules as
    :func:`post_route_sta._validate_report_ports` -- no wildcards, no Tcl --
    minus the clock-port rejection, which has no meaning for instances.
    Returns the list, or ``None`` when the field is omitted *or* empty
    (nothing is emitted, so the generated Tcl is byte-identical to a request
    without it). Existence is verified inside the engine session
    (:func:`_instance_check_lines`)."""
    field = "request.constraints.report_instances"
    value = constraints.get("report_instances")
    if value is None:
        return None
    if not isinstance(value, list):
        raise _error(f"{field} must be a list of instance names")
    seen: set[str] = set()
    for name in value:
        if not isinstance(name, str) or not name or name != name.strip():
            raise _error(
                f"{field} entries must be non-empty instance-name strings "
                f"without leading/trailing whitespace (got {name!r})"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
            raise _error(f"{field} entry {name!r} contains a control character")
        if name in seen:
            raise _error(f"{field} must not repeat an instance ({name!r})")
        seen.add(name)
    return list(value) or None


def _instance_check_lines(report_instances: list[str] | None) -> list[str]:
    """The instance half of the pre-timing name check: enumerate every
    instance of the loaded design once into ``klt_instances_by_name`` (keyed
    by exact ``get_full_name`` -- the same issue #3050-safe pattern the port
    check uses), then append one
    ``report_instance\t<name>\tnot_found`` error row per requested name that
    matches nothing. The rows land in ``klt_port_errors``, so the session
    exits with the port-check exit code *before any timing is reported*
    (``post_route_sta._port_check_error_message`` relabels them with the
    ``constraints.report_instances rejected`` prefix).

    No lines at all without ``report_instances`` -- and in particular no
    enumeration -- so an unrequested session is byte-identical. The
    ``klt_instances_by_name`` dict outlives this block: the instance-path
    query (:func:`_instance_path_lines`) resolves every instance through it,
    never through a re-parsed name.
    """
    if not report_instances:
        return []
    names = " ".join(_tcl_literal(name) for name in report_instances)
    return [
        "set klt_instances_by_name [dict create]",
        "foreach klt_cell [get_cells -hierarchical *] {",
        "    dict set klt_instances_by_name [get_full_name $klt_cell] $klt_cell",
        "}",
        "foreach klt_ri_name [list " + names + "] {",
        "    if {![dict exists $klt_instances_by_name $klt_ri_name]} {",
        "        lappend klt_port_errors [list report_instance $klt_ri_name not_found]",
        "    }",
        "}",
    ]


def _instance_path_lines(report_instances: list[str] | None) -> list[str]:
    """Per-instance worst setup (``max``) and hold (``min``) path ends,
    marker-delimited (issue #5754); no lines at all without
    ``report_instances``, so an unrequested session is byte-identical.

    For each instance: the worst path ``-to`` its input pins (``into``) and
    ``-from`` its output pins (``out_of``), each with
    ``-group_path_count 1 -endpoint_path_count 1`` -- so output is bounded
    by (instances x 2 sides x 2 check sides), never by design size or pin
    count. Pins are collected through ``get_pins -of_objects`` on the
    dict-resolved instance object (never a name re-parse) into plain Tcl
    lists, so a side whose direction the instance has no pins of is skipped
    (an empty pin list queries nothing -- it is ``no_paths``, not an error)
    instead of falling back to an unconstrained design-wide search. Every
    instance is ``catch``-wrapped on its own: a failed query yields an
    ``ERROR`` row for that instance (reported as ``status:
    "unavailable"``), never a fabricated path. Instance names were verified
    by :func:`_instance_check_lines` before this runs."""
    if not report_instances:
        return []
    names = " ".join(_tcl_literal(name) for name in report_instances)
    row = (
        'puts "IPATH\\t$klt_ri_name\\t$klt_ri_group\\t$klt_ri_side\\t'
        "[$klt_ri_pe check_role]\\t[$klt_ri_pe slack]\\t"
        "[get_full_name [get_property $klt_ri_pe startpoint]]\\t"
        '[get_full_name [get_property $klt_ri_pe endpoint]]"'
    )
    query = "find_timing_paths {flag} ${pins} -path_delay $klt_ri_side "
    query += "-group_path_count 1 -endpoint_path_count 1"
    lines = [
        f'puts "{_INSTANCE_PATHS_BEGIN}"',
        f"foreach klt_ri_name [list {names}] {{",
        "    if {[catch {",
        "        set klt_ri_cell [dict get $klt_instances_by_name $klt_ri_name]",
        "        set klt_ri_in_pins {}",
        "        foreach klt_ri_pin [get_pins -of_objects $klt_ri_cell "
        "-filter {direction == input}] {",
        "            lappend klt_ri_in_pins $klt_ri_pin",
        "        }",
        "        set klt_ri_out_pins {}",
        "        foreach klt_ri_pin [get_pins -of_objects $klt_ri_cell "
        "-filter {direction == output}] {",
        "            lappend klt_ri_out_pins $klt_ri_pin",
        "        }",
        "        foreach klt_ri_side {max min} {",
    ]
    for pins_var, group, flag in (
        ("klt_ri_in_pins", "into", "-to"),
        ("klt_ri_out_pins", "out_of", "-from"),
    ):
        lines += [
            f'            set klt_ri_group "{group}"',
            f"            if {{[llength ${pins_var}] > 0}} {{",
            "                foreach klt_ri_pe "
            f"[{query.format(flag=flag, pins=pins_var)}] {{",
            f"                    {row}",
            "                }",
            "            }",
        ]
    lines += [
        "        }",
        "    } klt_ri_error]} {",
        '        puts "ERROR\\t$klt_ri_name\\t'
        '[string map [list \\n { } \\t { }] $klt_ri_error]"',
        "    }",
        "}",
        f'puts "{_INSTANCE_PATHS_END}"',
    ]
    return lines


def _apply_instance_path_record(
    parts: list[str],
    line: str,
    instance_rows: dict[str, dict[str, list[dict[str, Any]]]],
) -> str | None:
    """Fold one ``IPATH``/``ERROR`` record into ``instance_rows``; return a
    failure reason when the record is bad, else ``None``."""
    if parts[0] == "ERROR":
        return "engine path introspection failed: " + "\t".join(parts[2:]).strip()
    if (
        len(parts) != 8
        or parts[2] not in instance_rows
        or parts[3] not in ("max", "min")
        or not all(parts[4:])
    ):
        return f"malformed instance path record: {line!r}"
    try:
        slack_s = float(parts[5])
    except ValueError:
        return f"malformed instance path slack: {line!r}"
    if slack_s != slack_s or abs(slack_s) == float("inf"):
        return f"non-finite instance path slack: {line!r}"
    instance_rows[parts[2]][parts[3]].append(
        {
            "engine_check_role": parts[4],
            "slack_ns": slack_s * 1e9,
            "startpoint": parts[6],
            "endpoint": parts[7],
        }
    )
    return None


def _parse_instance_path_rows(
    stdout: str, instances: list[str]
) -> dict[str, dict[str, tuple[dict[str, list[dict[str, Any]]] | None, str | None]]]:
    """``{instance: {group: (rows, reason)}}`` from
    :func:`_instance_path_lines`' block, in request order; each ``rows`` is
    ``{"max": [...], "min": [...]}`` (``None`` with a ``reason`` when the
    instance's query failed, its record was malformed, or the block is
    absent). Lines that are neither ``IPATH`` nor ``ERROR`` rows (stray
    engine warnings) are ignored."""
    block = _extract_block(stdout, _INSTANCE_PATHS_BEGIN, _INSTANCE_PATHS_END)
    if block is None:
        reason = "per-instance path records absent from engine output"
        return {
            instance: {group: (None, reason) for group in ("into", "out_of")}
            for instance in instances
        }
    rows: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        instance: {group: {"max": [], "min": []} for group in ("into", "out_of")}
        for instance in instances
    }
    failures: dict[str, str] = {}
    for line in block.splitlines():
        parts = line.split("\t")
        if parts[0] not in ("IPATH", "ERROR") or len(parts) < 2:
            continue
        instance = parts[1]
        if instance not in rows or instance in failures:
            continue
        failure = _apply_instance_path_record(parts, line, rows[instance])
        if failure is not None:
            failures[instance] = failure
    parsed: dict[
        str,
        dict[str, tuple[dict[str, list[dict[str, Any]]] | None, str | None]],
    ] = {}
    for instance in instances:
        if instance in failures:
            parsed[instance] = {
                group: (None, failures[instance]) for group in ("into", "out_of")
            }
        else:
            parsed[instance] = {
                group: (rows[instance][group], None) for group in ("into", "out_of")
            }
    return parsed


def _instance_paths(
    stdout: str, report_instances: list[str]
) -> dict[str, dict[str, Any]]:
    """``instance_paths`` for one session (issue #5754): per requested
    instance, in request order, the worst ``setup``/``hold`` records of its
    ``into`` (input pins) and ``out_of`` (output pins) sides. There is no
    per-instance aggregate to cross-check against, so
    ``consistent_with_aggregate`` stays ``null`` and ``inconsistent`` is
    never produced; a side with no constrained path (including an instance
    with no pins of that direction) is ``no_paths``."""
    parsed = _parse_instance_path_rows(stdout, report_instances)
    result: dict[str, Any] = {}
    for instance in report_instances:
        entry: dict[str, Any] = {}
        for group in ("into", "out_of"):
            rows, reason = parsed[instance][group]
            if rows is None:
                setup = _empty_path_record("unavailable", reason)
                hold = _empty_path_record("unavailable", reason)
            else:
                setup = _worst_path_record(rows["max"], None)
                hold = _worst_path_record(rows["min"], None)
            entry[group] = {"setup": setup, "hold": hold}
        result[instance] = entry
    return result


def _instance_paths_corner_field(
    stdout: str, report_instances: list[str] | None
) -> dict[str, Any]:
    """The ``{name: value}`` fragment ``post_route_sta`` folds into its
    per-corner fields: ``{"instance_paths": ...}`` when the request named
    ``report_instances``, ``{}`` when it did not -- so the caller needs no
    branch of its own and an unrequested corner's fields stay
    byte-identical."""
    if not report_instances:
        return {}
    return {"instance_paths": _instance_paths(stdout, report_instances)}
