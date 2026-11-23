"""``constraints.multicycle_paths`` (issue #6921): the standalone-STA half
of the four-phase-sequencer timing exception #6908 asked for -- declare a
setup/hold multiplier pair on a selected set of register-to-register paths,
apply it as paired ``set_multicycle_path`` commands, and measure what it
bought on exactly those paths, with and without the entry.

Born split out of :mod:`klayout_tools.post_route_sta` (the per-file size
ratchet, issue #2766) in the shape of
:mod:`klayout_tools._sta_clock_uncertainty`: pure functions, a lazy
``PostRouteStaError`` import, and no verb-module import at load time -- so
``place_and_route`` (issue #6923) can reuse the validator and the paired
emitter without loading the ``klt sta`` verb. It also hosts the older
``constraints.false_path_from`` validator and ``set_false_path`` emitter
(issue #2968), moved here unchanged for the same ratchet: both fields are
``klt sta`` timing exceptions emitted between the I/O delays and the report
block.

Selector grammar (bounded, data-only): ``from``/``to``/``through`` are each
one non-empty string over ``[A-Za-z0-9_./*?\\[\\]]`` -- ``*`` and ``?`` are
the only wildcards, ``[`` and ``]`` are literal (bus names such as
``q[0]``), never character classes, and anything else (whitespace, ``{ } ;
$ \\ "`` or a leading ``-``) is rejected as malformed before the engine
runs. ``from``/``to`` name register instances or pins; ``through`` names
pins. Roles are resolved with the engine inside the session
(``get_cells``/``get_pins -quiet``; a register is an instance with a
liberty clock pin, decided by the engine's own ``is_clock`` pin property --
never by a regex on the netlist), and every selector is emitted through
:func:`klayout_tools._tcl.tcl_literal` so it is quoted as data.

The measurement itself -- the per-entry ``find_timing_paths`` over the
identical ``-from/-through/-to`` selection in the with-run and in one extra
"without" session per entry that keeps every other entry in place, the
``matched_endpoints`` count, and the "no timed path" / null-with-reason
rules -- is parsed and assembled by
:mod:`klayout_tools.post_route_sta_multicycle`.
"""

from __future__ import annotations

from typing import Any, Literal

from ._tcl import tcl_literal as _tcl_literal
from .post_route_sta_errors import PostRouteStaError

#: Entry keys: ``from``/``to``/``setup``/``hold`` required, ``through``
#: optional. Anything else is a request error.
MULTICYCLE_KEYS = ("from", "through", "to", "setup", "hold")

#: The bounded selector alphabet: letters, digits, ``_ . / [ ]`` plus the
#: two wildcards ``*`` and ``?``. ``[``/``]`` are literal (bus names), so a
#: selector can never be a Tcl character class.
_SELECTOR_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./[]*?"
)

#: Brackets the in-session selector-resolution error rows (one
#: ``MCPERR\\t<index>\\t<field> <problem> [<detail>]`` per rejected entry);
#: a non-empty block ends the session with :data:`_MCP_EXIT_CODE`.
MCPERR_BEGIN = "===KLT_STA_MCPERR_BEGIN==="
MCPERR_END = "===KLT_STA_MCPERR_END==="

#: Brackets the per-entry path measurements: one
#: ``MCP\\t<index>\\t<max|min>\\t<slack_ns>\\t<endpoint>`` row per timed
#: path end of the selection, or ``MCPERR\\t<index>\\tmeasure\\t<error>``
#: when that side's query itself failed.
MCP_BEGIN = "===KLT_STA_MCP_BEGIN==="
MCP_END = "===KLT_STA_MCP_END==="

#: The in-session request-error exit code -- the same code
#: ``post_route_sta``'s port check uses, so both name-check failures are
#: "rejected request", not "engine crash".
_MCP_EXIT_CODE = 3

#: One row per endpoint per path group per side: enough to enumerate every
#: distinct endpoint of the selection without an unbounded report, the same
#: ``-group_path_count``/``-endpoint_path_count`` bounding
#: ``post_route_sta_instance_paths`` established (issue #5754).
_MCP_GROUP_PATH_COUNT = 100000


#: The trailing comment tag on every applied ``set_multicycle_path`` line
#: (``... ;# klt mcp <index>``): the marker the per-entry "without" session
#: filters its own two lines out by. Comments are legal Tcl, and a request
#: without entries never sees this block, so legacy byte-identity holds.
def _mcp_tag(index: int) -> str:
    return f" ;# klt mcp {index}"


# --------------------------------------------------------------------------- #
# Request validation (pure Python, before any engine runs)
# --------------------------------------------------------------------------- #


def _validate_selector(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PostRouteStaError(f"{field} must be a non-empty selector string")
    bad = sorted(set(value) - _SELECTOR_CHARS)
    if bad:
        raise PostRouteStaError(
            f"{field} is a malformed selector ({value!r}: disallowed "
            "character(s) "
            + " ".join(repr(ch) for ch in bad)
            + " -- allowed are letters, digits, '_', '.', '/', '[', ']', "
            "'*' and '?')"
        )
    return value


def _validate_multiplier(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PostRouteStaError(f"{field} must be a positive integer (got {value!r})")
    return value


def _validate_multicycle_entry(entry: Any, index: int) -> dict[str, Any]:
    field = f"constraints.multicycle_paths[{index}]"
    if not isinstance(entry, dict):
        raise PostRouteStaError(f"{field} must be an object")
    unknown = sorted(set(entry) - set(MULTICYCLE_KEYS))
    if unknown:
        raise PostRouteStaError(
            f"{field} has unknown key(s): "
            + ", ".join(unknown)
            + f" (allowed: {', '.join(MULTICYCLE_KEYS)})"
        )
    through = entry.get("through")
    if through is not None:
        through = _validate_selector(through, f"{field}.through")
    return {
        "index": index,
        "from": _validate_selector(entry.get("from"), f"{field}.from"),
        "through": through,
        "to": _validate_selector(entry.get("to"), f"{field}.to"),
        "setup": _validate_multiplier(entry.get("setup"), f"{field}.setup"),
        "hold": _validate_multiplier(entry.get("hold"), f"{field}.hold"),
    }


def validate_multicycle_paths(
    constraints: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """``constraints.multicycle_paths`` (issue #6921): optional list of
    ``{from, to, setup, hold, through?}`` entries. Returns the normalized
    entries (each carrying its request ``index``), or ``None`` when the
    field is omitted *or* empty -- no Tcl, no response field, byte-identical
    script. Every malformed shape is a request error naming the entry
    index; selector resolution against the linked design happens in the
    engine session (:func:`multicycle_lines`)."""
    value = constraints.get("multicycle_paths")
    if value is None:
        return None
    if not isinstance(value, list):
        raise PostRouteStaError(
            "constraints.multicycle_paths must be a list of objects"
        )
    return [
        _validate_multicycle_entry(entry, i) for i, entry in enumerate(value)
    ] or None


# --------------------------------------------------------------------------- #
# The session Tcl: resolution + paired emission + measurement
# --------------------------------------------------------------------------- #


# Inside the selector-resolution catch, before any physical floorplan work.
# One path on either side suffices; P&R never enumerates endpoint slacks.
_CHECK_CONNECTED = (
    "        set klt_args [list -from $klt_mcp_from($klt_i) -to $klt_mcp_to($klt_i)]",
    '        if {$klt_thr ne ""} { lappend klt_args -through $klt_mcp_thr($klt_i) }',
    "        foreach klt_side {max min} {",
    "            set klt_paths [find_timing_paths {*}$klt_args -path_delay $klt_side "
    "-group_path_count 1 -endpoint_path_count 1]",
    "            if {[llength $klt_paths]} { break }",
    "        }",
    "        if {![llength $klt_paths]} {",
    '            error "no timed path connects the selection"',
    "        }",
)


def multicycle_lines(
    entries: list[dict[str, Any]] | None,
    *,
    mode: Literal["measure", "validate", "apply"] = "measure",
) -> list[str]:
    """The complete multicycle block for one OpenSTA session, emitted after
    the I/O-delay/false-path lines and before the report block:

    1. **Resolve** every entry's selectors against the linked design
       (``klt_mcp_end_objs``): ``get_cells -quiet <sel>`` filtered to
       registers (an instance with a liberty clock pin -- the engine's own
       ``is_clock`` pin property) plus ``get_pins -quiet <sel>`` for
       ``from``/``to``; pins only for ``through``. An unclocked register, a
       selector matching nothing, and a ``through`` matching no pin are
       ``MCPERR`` rows that end the session with exit 3 -- request errors
       naming the entry index, surfaced by
       :func:`multicycle_request_error`.
    2. **Apply** each entry as a paired ``set_multicycle_path <setup>
       -setup ...`` / ``<hold> -hold ...`` over the identical selection,
       each line tagged with its entry index (the tag the "without"
       session's script is derived from, :func:`without_script_lines`).
    3. **Measure**: per entry, ``find_timing_paths`` over the identical
       selection, ``max`` and ``min`` separately, one row per endpoint.

    P&R uses ``validate`` to check for a connecting timed path at floorplan,
    then ``apply`` to restore the paired constraints in later sessions.
    ``None``/``[]`` emit nothing at all, so a request without entries
    generates a byte-identical script."""
    if not entries:
        return []
    resolved = " ".join(
        f"{entry['index']} {_tcl_literal(entry['from'])} "
        + (_tcl_literal(entry["through"]) if entry["through"] is not None else "{}")
        + f" {_tcl_literal(entry['to'])}"
        for entry in entries
    )
    lines = [
        "set klt_mcp_errors {}",
        # A register is an instance with a liberty clock pin (is_clock);
        # "clocked" adds the applied-clock test (clocks), so an unclocked
        # register is named as such instead of silently excepting nothing.
        "proc klt_mcp_end_objs {klt_sel} {",
        "    set klt_regs {}",
        "    set klt_unclk {}",
        "    foreach klt_c [get_cells -quiet $klt_sel] {",
        "        set klt_seq 0",
        "        set klt_clk 0",
        "        foreach klt_p [get_pins -of_objects $klt_c] {",
        "            if {[get_property $klt_p is_clock]} { set klt_seq 1 }",
        "            if {[llength [get_property $klt_p clocks]] > 0} { set klt_clk 1 }",
        "        }",
        "        if {$klt_seq} {",
        "            if {$klt_clk} { lappend klt_regs $klt_c } "
        "else { lappend klt_unclk $klt_c }",
        "        }",
        "    }",
        "    return [list $klt_regs $klt_unclk [get_pins -quiet $klt_sel]]",
        "}",
        "foreach {klt_i klt_from klt_thr klt_to} [list " + resolved + "] {",
        "    if {[catch {",
        "        lassign [klt_mcp_end_objs $klt_from] klt_regs klt_unclk klt_pins",
        "        if {[llength $klt_unclk] > 0} {",
        '            error "from unclocked_register '
        '[get_full_name [lindex $klt_unclk 0]]"',
        "        }",
        "        if {[llength $klt_regs] == 0 && [llength $klt_pins] == 0} {",
        '            error "from matched_nothing"',
        "        }",
        "        set klt_mcp_from($klt_i) [concat $klt_regs $klt_pins]",
        '        if {$klt_thr ne ""} {',
        "            set klt_mcp_thr($klt_i) [get_pins -quiet $klt_thr]",
        "            if {[llength $klt_mcp_thr($klt_i)] == 0} { "
        'error "through matched_nothing" }',
        "        }",
        "        lassign [klt_mcp_end_objs $klt_to] klt_regs klt_unclk klt_pins",
        "        if {[llength $klt_unclk] > 0} {",
        '            error "to unclocked_register '
        '[get_full_name [lindex $klt_unclk 0]]"',
        "        }",
        "        if {[llength $klt_regs] == 0 && [llength $klt_pins] == 0} {",
        '            error "to matched_nothing"',
        "        }",
        "        set klt_mcp_to($klt_i) [concat $klt_regs $klt_pins]",
        *(_CHECK_CONNECTED if mode == "validate" else ()),
        "    } klt_mcp_msg]} {",
        "        lappend klt_mcp_errors [list $klt_i $klt_mcp_msg]",
        "    }",
        "}",
        f'puts "{MCPERR_BEGIN}"',
        "foreach klt_e $klt_mcp_errors {",
        '    puts "MCPERR\\t[lindex $klt_e 0]\\t[lindex $klt_e 1]"',
        "}",
        f'puts "{MCPERR_END}"',
        "if {[llength $klt_mcp_errors] > 0} {",
        f"    exit {_MCP_EXIT_CODE}",
        "}",
    ]
    for entry in entries:
        index = entry["index"]
        through = (
            f" -through $klt_mcp_thr({index})" if entry["through"] is not None else ""
        )
        for multiplier, kind in ((entry["setup"], "-setup"), (entry["hold"], "-hold")):
            lines.append(
                f"set_multicycle_path {multiplier} {kind} "
                f"-from $klt_mcp_from({index}){through} "
                f"-to $klt_mcp_to({index}){_mcp_tag(index)}"
            )
    if mode != "measure":
        return lines
    lines.append(f'puts "{MCP_BEGIN}"')
    for entry in entries:
        index = entry["index"]
        through = (
            f" -through $klt_mcp_thr({index})" if entry["through"] is not None else ""
        )
        for side in ("max", "min"):
            lines += [
                "if {[catch {",
                "    foreach klt_pe [find_timing_paths "
                f"-from $klt_mcp_from({index}){through} "
                f"-to $klt_mcp_to({index}) -path_delay {side} "
                f"-group_path_count {_MCP_GROUP_PATH_COUNT} "
                "-endpoint_path_count 1] {",
                '                puts "MCP\\t' + str(index) + f"\\t{side}\\t"
                "[expr {[$klt_pe slack] * 1e9}]\\t"
                '[get_full_name [get_property $klt_pe endpoint]]"',
                "    }",
                "} klt_mcp_measure_msg]} {",
                '    puts "MCPERR\\t' + str(index) + f"\\t{side}\\t"
                '[string map [list \\n { } \\t { }] $klt_mcp_measure_msg]"',
                "}",
            ]
    lines.append(f'puts "{MCP_END}"')
    return lines


def without_script_lines(lines: list[str], index: int) -> list[str]:
    """The per-entry "without" session's script: the with-run's own lines
    minus that entry's tagged ``set_multicycle_path`` pair (every other
    entry stays applied -- the unambiguous baseline) and minus any
    ``write_sdf`` (the SDF of record is the with-run's own)."""
    tag = _mcp_tag(index)
    return [
        line
        for line in lines
        if not line.endswith(tag) and not line.startswith("write_sdf ")
    ]


# --------------------------------------------------------------------------- #
# false_path_from (issue #2968), moved from post_route_sta.py unchanged for
# the per-file size ratchet when this module was added (issue #6921).
# --------------------------------------------------------------------------- #


def _validate_false_path_from(
    constraints: dict[str, Any], clock_port: str
) -> list[str] | None:
    """``constraints.false_path_from`` (issue #2968): optional list of unique
    literal input-port names (one scalar port or one bus bit, e.g.
    ``cfg[3]`` -- the same key rules as ``_validate_report_ports``) whose
    paths are excluded from timing with ``set_false_path -from``. Returns the
    list, or ``None`` when the field is omitted *or* empty (nothing is
    emitted, so the generated Tcl is byte-identical to a request without
    it). Existence and direction are verified inside the engine session
    (``_port_check_lines``); the clock port is rejected here."""
    field = "request.constraints.false_path_from"
    value = constraints.get("false_path_from")
    if value is None:
        return None
    if not isinstance(value, list):
        raise PostRouteStaError(f"{field} must be a list of input port names")
    seen: set[str] = set()
    for port in value:
        if not isinstance(port, str) or not port or port != port.strip():
            raise PostRouteStaError(
                f"{field} entries must be non-empty port-name strings without "
                f"leading/trailing whitespace (got {port!r})"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in port):
            raise PostRouteStaError(
                f"{field} entry {port!r} contains a control character"
            )
        if port == clock_port:
            raise PostRouteStaError(
                f"{field} must not name the clock port {port!r} -- "
                "a false path from the clock would untime every register"
            )
        if port in seen:
            raise PostRouteStaError(f"{field} must not repeat a port ({port!r})")
        seen.add(port)
    return list(value) or None


def _false_path_lines(false_path_from: list[str] | None) -> list[str]:
    """``set_false_path -from`` per ``constraints.false_path_from`` port
    (issue #2968), emitted after the I/O-delay lines. Nothing when unset."""
    return [
        f"set_false_path -from [get_ports {_tcl_literal(port)}]"
        for port in false_path_from or ()
    ]
