"""The bounded violating-endpoint list for ``klt sta`` (issue #7418): the
Tcl that enumerates every endpoint whose slack is negative -- the same
population ``setup_violation_count`` / ``hold_violation_count`` count --
and the parser that turns it into the ``violating_endpoints`` response
field a ``constraints.max_violating_endpoints`` request carries.

Own module for the same reason :mod:`klayout_tools.post_route_sta_stages`
was (that file's own header): ``post_route_sta.py`` sits at its
``file-size-baseline.json`` ceiling and ``post_route_sta_path_groups.py``
was already near the 500-line hard limit, so this feature's additions land
here and ``post_route_sta`` keeps only call-site changes plus a re-export
of every name its tests and script builders use.
"""

from __future__ import annotations

from typing import Any

from ._sta_request_diag import extract_block as _extract_block
from .post_route_sta_errors import PostRouteStaError
from .post_route_sta_path_groups import _path_group_entry

#: Issue #7418: brackets the bounded violating-endpoint list
#: (:func:`_violating_endpoint_lines`), emitted only when the request set
#: ``constraints.max_violating_endpoints`` -- one
#: ``VPATH\t<side>\t<check_role>\t<slack_s>\t<startpoint>\t<endpoint>\t<path_group>``
#: row per violating endpoint per path group per side, or a single
#: ``ERROR\t<message>`` row when the query itself failed.
_VIOLATING_ENDPOINTS_BEGIN = "===KLT_STA_VIOLATING_ENDPOINTS_BEGIN==="
_VIOLATING_ENDPOINTS_END = "===KLT_STA_VIOLATING_ENDPOINTS_END==="


def _validate_max_violating_endpoints(constraints: dict[str, Any]) -> int | None:
    """``constraints.max_violating_endpoints`` (issue #7418): optional
    positive integer bounding each ``violating_endpoints`` side; ``None``
    when omitted, so an unrequested session emits no Tcl and its response is
    byte-identical. Booleans are rejected explicitly (``bool`` subclasses
    ``int``, so ``True`` would otherwise pass as ``1``)."""
    field = "request.constraints.max_violating_endpoints"
    value = constraints.get("max_violating_endpoints")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PostRouteStaError(f"{field} must be a positive integer")
    return value


def _violating_endpoint_lines(max_violating_endpoints: int | None) -> list[str]:
    """Marker-delimited bounded list of violating endpoints per side (issue
    #7418); **no lines at all** without ``max_violating_endpoints``, so an
    unrequested session is byte-identical.

    Every engine fact below was verified live against OpenROAD 26Q3-1510
    (``tests/fixtures/sta_violating_endpoints/sg13g2_capture.json`` is the
    capture): path ends expose no ``path_group_name`` property, so the
    emitter iterates ``sta::path_group_names`` and queries each group with
    ``find_timing_paths -path_group [list $name]`` -- the ``[list ...]``
    quoting is load-bearing, because ``-path_group`` is list-valued and a
    bare multi-word name (``{gated clock}``, where clock-gating violators
    live) splits into unknown names that only *warn* and silently disable
    the filter. ``-group_path_count`` caps **per path group**, so N+1 rows
    are requested per group and the Python side re-sorts globally and trims
    to N -- N+1, not N, is what makes ``*_truncated`` derivable. The engine
    sorts only within a group (verified: the capture's hold side arrives
    group-major, not worst-first). ``sta::path_group_names`` can list one
    name twice -- a design clock named ``asynchronous`` or ``unconstrained``
    collides with the built-in group of that name -- and the name-keyed
    query returns that group's endpoints each time, duplicating rows before
    the global cap (so ``*_truncated`` and ``status`` go wrong). Each
    distinct name is therefore queried once per side, first-seen order, so
    tied slacks keep the engine's order. ``-slack_max`` is inclusive at the
    boundary, so the strict ``$klt_ve_s < 0`` filter is what keeps a
    zero-slack endpoint out of the list -- the same population
    ``report_check_types -violators`` counts (slack < 0), one row per
    endpoint per path group via ``-endpoint_path_count 1``. The whole block
    is ``catch``-wrapped: an engine without these commands yields a single
    ``ERROR`` row (reported as ``status: "unavailable"``), never a
    fabricated list."""
    if max_violating_endpoints is None:
        return []
    ask = max_violating_endpoints + 1
    return [
        f'puts "{_VIOLATING_ENDPOINTS_BEGIN}"',
        "if {[catch {",
        "    foreach klt_ve_side {max min} {",
        "        set klt_ve_seen {}",
        "        foreach klt_ve_group [sta::path_group_names] {",
        "            if {[lsearch -exact $klt_ve_seen $klt_ve_group] >= 0} {",
        "                continue",
        "            }",
        "            lappend klt_ve_seen $klt_ve_group",
        "            foreach klt_pe [find_timing_paths -path_delay "
        f"$klt_ve_side -path_group [list $klt_ve_group] "
        f"-group_path_count {ask} -endpoint_path_count 1 "
        "-slack_max 0 -sort_by_slack] {",
        "                set klt_ve_s [$klt_pe slack]",
        "                if {$klt_ve_s < 0} {",
        '                    puts "VPATH\\t$klt_ve_side\\t[$klt_pe check_role]\\t'
        "$klt_ve_s\\t[get_full_name [get_property $klt_pe startpoint]]\\t"
        '[get_full_name [get_property $klt_pe endpoint]]\\t$klt_ve_group"',
        "                }",
        "            }",
        "        }",
        "    }",
        "} klt_ve_error]} {",
        '    puts "ERROR\\t[string map [list \\n { } \\t { }] $klt_ve_error]"',
        "}",
        f'puts "{_VIOLATING_ENDPOINTS_END}"',
    ]


def _parse_violating_endpoint_rows(
    stdout: str,
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """``({"max": [...], "min": [...]}, None)`` from
    :func:`_violating_endpoint_lines`' block, or ``(None, reason)`` when the
    block is absent, reports an engine error, or carries a malformed
    ``VPATH`` row -- the same contract as ``_parse_worst_path_rows``, so a
    bad block is never partially reported. Lines that are neither ``VPATH``
    nor ``ERROR`` rows (stray engine warnings) are ignored."""
    block = _extract_block(stdout, _VIOLATING_ENDPOINTS_BEGIN, _VIOLATING_ENDPOINTS_END)
    if block is None:
        return None, "violating-endpoint records absent from engine output"
    rows: dict[str, list[dict[str, Any]]] = {"max": [], "min": []}
    for line in block.splitlines():
        if line.startswith("ERROR\t"):
            return None, (
                "engine violating-endpoint introspection failed: "
                + line[len("ERROR\t") :].strip()
            )
        if not line.startswith("VPATH\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 7 or parts[1] not in rows or not all(parts[2:]):
            return None, f"malformed violating-endpoint record: {line!r}"
        try:
            slack_s = float(parts[3])
        except ValueError:
            return None, f"malformed violating-endpoint slack: {line!r}"
        if slack_s != slack_s or abs(slack_s) == float("inf"):
            return None, f"non-finite violating-endpoint slack: {line!r}"
        rows[parts[1]].append(
            {
                "engine_check_role": parts[2],
                "slack_ns": slack_s * 1e9,
                "startpoint": parts[4],
                "endpoint": parts[5],
                "path_group": parts[6],
            }
        )
    return rows, None


def _violating_endpoint_entry(row: dict[str, Any]) -> dict[str, Any]:
    """One ``violating_endpoints`` entry: the identity fields of
    ``post_route_sta_path_groups._path_group_entry`` plus the
    ``path_group`` the row was found in (same ``_CHECK_ROLE_TYPES`` mapping
    and 5-decimal rounding as ``worst_paths_by_group``)."""
    entry = _path_group_entry(row)
    entry["path_group"] = row["path_group"]
    return entry


def _violating_endpoints_corner_field(
    stdout: str,
    max_violating_endpoints: int | None,
    setup_violation_count: int,
    hold_violation_count: int,
) -> dict[str, Any]:
    """The ``{name: value}`` fragment ``post_route_sta`` folds into its
    per-corner fields: ``{"violating_endpoints": ...}`` when the request
    named ``max_violating_endpoints``, ``{}`` when it did not -- so the
    caller needs no branch of its own and an unrequested corner's fields
    stay byte-identical (the ``_instance_paths_corner_field`` pattern)."""
    if max_violating_endpoints is None:
        return {}
    return {
        "violating_endpoints": _violating_endpoints(
            stdout,
            max_violating_endpoints,
            setup_violation_count,
            hold_violation_count,
        )
    }


def _violating_endpoints(
    stdout: str,
    max_violating_endpoints: int,
    setup_violation_count: int,
    hold_violation_count: int,
) -> dict[str, Any]:
    """``violating_endpoints`` for one session (issue #7418): per side, the
    at most N worst violating endpoints, worst slack first, each row unique
    per endpoint per path group -- the same population the session's
    ``setup_violation_count`` / ``hold_violation_count`` measure -- with a
    ``*_truncated`` flag separating "all of them" from "first N".

    The engine returns rows group-major with per-group worst-first order
    only, so both sides are re-sorted globally here (a stable sort, keeping
    engine order on ties) and trimmed to the cap. ``status`` is ``"ok"``
    when each side agrees with its violation count -- truncated, that means
    the count covers every row returned before trimming to the cap;
    untruncated, the list length equals the count -- and
    ``"inconsistent"`` with a ``reason`` naming the disagreeing side
    otherwise, mirroring the ``worst_*_path`` aggregate cross-check: the
    lists are still reported, never silently shortened. A failed parse is
    ``"unavailable"`` with a ``reason`` and both lists empty."""
    rows, reason = _parse_violating_endpoint_rows(stdout)
    if rows is None:
        return {
            "status": "unavailable",
            "reason": reason,
            "setup": [],
            "hold": [],
            "setup_truncated": False,
            "hold_truncated": False,
        }
    mismatches: list[str] = []
    sides: dict[str, Any] = {}
    for side, count, list_key in (
        ("max", setup_violation_count, "setup"),
        ("min", hold_violation_count, "hold"),
    ):
        ordered = sorted(rows[side], key=lambda row: row["slack_ns"])
        truncated = len(ordered) > max_violating_endpoints
        listed = [
            _violating_endpoint_entry(row) for row in ordered[:max_violating_endpoints]
        ]
        agrees = (
            count > max_violating_endpoints and count >= len(ordered)
            if truncated
            else len(listed) == count
        )
        if not agrees:
            observed = (
                f"returned {len(ordered)} before truncation"
                if truncated
                else f"listed {len(listed)}"
            )
            mismatches.append(
                f"{list_key} side {observed} but {list_key}_violation_count is {count}"
            )
        sides[list_key] = listed
        sides[f"{list_key}_truncated"] = truncated
    return {
        "status": "ok" if not mismatches else "inconsistent",
        "reason": "; ".join(mismatches) if mismatches else None,
        **sides,
    }
