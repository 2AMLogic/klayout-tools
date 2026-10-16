"""Detailed-route pass artifacts and authoritative final DRC metrics."""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

# Supports both TritonRoute's inline ``on Layer Metal2`` and its multiline
# ``Layer: met1`` spelling. Retain all four identity fields: two violations
# at the same coordinates can still differ in type, source, or layer.
_DRC_RECORD = re.compile(
    r"([^\n]+)\n\s*srcs:\s*(.*?)\s*bbox\s*=\s*(.*?)"
    r"\s*(?:on Layer\s+|Layer:\s*)(\S+)",
    re.DOTALL,
)


def detailed_route_lines(
    output_dir: str, top: str, seed: int, pass_index: int, final_pass: int
) -> list[str]:
    """Keep the historical paths for the final pass, isolate earlier passes.

    Delete this pass's artifacts first: rerunning the same request must not
    append yesterday's violations to today's report. Nonfinal metrics use
    OpenROAD's metrics-stage formatter so their counters cannot collide with
    the final pass's unqualified keys (including the entire iteration series).
    API: OpenROAD src/utl/include/utl/Logger.h, log_metric / metrics_stages_.
    """
    suffix = "" if pass_index == final_pass else f"_pass_{pass_index}"
    drc_report = os.path.join(output_dir, f"{top}_route{suffix}_drc.rpt")
    maze_log = os.path.join(output_dir, f"{top}_route{suffix}_maze.log")
    lines = [f"file delete -force {drc_report} {maze_log}"]
    if pass_index != final_pass:
        lines.append(f'utl::push_metrics_stage "route_pass:{pass_index}__{{}}"')
    lines.append(
        f"detailed_route -output_drc {drc_report} -output_maze {maze_log} "
        f"-or_seed {seed}"
    )
    if pass_index != final_pass:
        lines.append("utl::pop_metrics_stage")
    return lines


def count_route_drc_violations(drc_report_path: str) -> int | None:
    """Count distinct complete (type, sources, bbox, layer) records.

    OpenROAD can repeat a violation even within a single detailed-route pass
    (#2089). Header counts therefore overstate distinct sites. Incomplete
    records are counted individually, never collapsed on insufficient evidence.
    An empty report means zero; an unreadable report means unknown (None).
    Earlier passes must be isolated by the producer: deduplication alone cannot
    discard a distinct violation that was fixed before the final pass.
    """
    try:
        with open(drc_report_path, encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    records = re.split(r"(?m)^violation type:[ \t]*", content)[1:]
    distinct = set()
    incomplete = 0
    for record in records:
        match = _DRC_RECORD.match(record)
        if match is None:
            incomplete += 1
        else:
            distinct.add(tuple(" ".join(field.split()) for field in match.groups()))
    return len(distinct) + incomplete


def route_metrics_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Resolve repeated engine diagnostics explicitly and retain their history.

    Pass scoping prevents cross-pass collisions in newly generated scripts.
    Engine versions may still repeat keys within a pass. Expose the last emitted
    diagnostic while retaining *every* value for repeated keys; final DRC count
    is replaced separately from the authoritative report, never from this rule.
    """
    data: dict[str, Any] = {}
    repeated: dict[str, list[Any]] = {}
    for key, value in pairs:
        if key in data:
            repeated.setdefault(key, [data[key]]).append(value)
        data[key] = value
    if repeated:
        data["klt__repeated_metrics"] = repeated
    return data


def write_route_metrics(
    metrics_path: str,
    metrics: dict[str, Any],
    drc_count: int | None,
    *,
    error_cls: type[Exception],
) -> None:
    """Publish one authoritative DRC summary in the saved metrics artifact.

    None deliberately replaces an engine counter when the final report is
    unavailable: the saved metric and response must agree that count is unknown.
    Replace the inode atomically: a Docker engine may own the readable
    original file even though its containing directory belongs to this user.
    """
    metrics["route__drc_errors"] = drc_count
    try:
        with tempfile.TemporaryDirectory(
            prefix=".klt-route-metrics-", dir=os.path.dirname(metrics_path) or "."
        ) as temporary:
            replacement = os.path.join(temporary, "metrics.json")
            with open(replacement, "w", encoding="utf-8") as handle:
                json.dump(metrics, handle, indent=2)
                handle.write("\n")
            os.replace(replacement, metrics_path)
    except OSError as exc:
        raise error_cls(
            f"could not write final route metrics '{metrics_path}': {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Routability / congestion block (issue #2994)
# --------------------------------------------------------------------------- #

#: Marker pair isolating :func:`routing_completeness_lines`' one summary line
#: from the rest of the route stage's stdout, same convention as the module
#: constants ``place_and_route.py`` uses for its own scraped blocks.
ROUTING_COMPLETENESS_BEGIN = "===KLT_ROUTING_COMPLETENESS_BEGIN==="
ROUTING_COMPLETENESS_END = "===KLT_ROUTING_COMPLETENESS_END==="
_COMPLETENESS_RE = re.compile(
    r"KLT_ROUTING_COMPLETENESS signal_nets=(\d+) unrouted=(\d+)"
)

# One row of `global_route -verbose`'s "[INFO GRT-0096] Final congestion
# report:" table (verified live, OpenROAD 26Q3-1510-g6cb3f2b704):
#   met1   2811   695   24.72%   0 /  0 /  0
# The trailing triple is Max H / Max V / Total congestion (overflow).
_GRT_ROW_RE = re.compile(
    r"^(?P<layer>\S+)\s+(?P<resource>\d+)\s+(?P<demand>\d+)\s+"
    r"(?P<usage>\d+(?:\.\d+)?)%\s+(?P<max_h>\d+)\s*/\s*(?P<max_v>\d+)\s*/\s*"
    r"(?P<total>\d+)\s*$"
)
_GRT_ROUTED_NETS_RE = re.compile(r"GRT-0014\]\s+Routed nets:\s*(\d+)")
_GPL_OVERFLOW_RE = re.compile(r"GPL-0041\]\s+Total routing overflow:\s*([\d.eE+-]+)")
_GPL_TILES_RE = re.compile(r"GPL-0042\]\s+Number of overflowed tiles:\s*(\d+)")
_GPL_FINAL_RE = re.compile(
    r"GPL-1005\]\s+Routability final weighted congestion:\s*([\d.eE+-]+)"
)
_ITER_KEY_RE = re.compile(r"route__drc_errors__iter:(\d+)$")

_NOT_REPORTED = "the engine did not print this figure in the stage log"
_NO_GCELL_USAGE = (
    "OpenROAD's global router reports usage per routing layer, not per GCell"
)
_NO_PARTIAL_ROUTE_CHECK = (
    "OpenROAD exposes no per-net connectivity check; only nets with no routed "
    "wire at all are counted"
)


def routing_completeness_lines() -> list[str]:
    """Tcl run after the final ``detailed_route``: count signal nets with two
    or more terminals and how many of them carry no routed wire at all."""
    return [
        f'puts "{ROUTING_COMPLETENESS_BEGIN}"',
        "set klt_block [ord::get_db_block]",
        "set klt_total 0",
        "set klt_unrouted 0",
        "foreach klt_net [$klt_block getNets] {",
        '  if {[$klt_net getSigType] ne "SIGNAL"} continue',
        "  set klt_pins [expr {[llength [$klt_net getITerms]]"
        " + [llength [$klt_net getBTerms]]}]",
        "  if {$klt_pins < 2} continue",
        "  incr klt_total",
        '  if {[$klt_net getWire] eq "NULL"} { incr klt_unrouted }',
        "}",
        'puts "KLT_ROUTING_COMPLETENESS signal_nets=$klt_total unrouted=$klt_unrouted"',
        f'puts "{ROUTING_COMPLETENESS_END}"',
    ]


def parse_routing_completeness(stdout: str) -> dict[str, int] | None:
    """``{"signal_net_count", "unrouted_net_count"}`` from the marker-delimited
    block, or ``None`` when it is missing (never a defensive ``0``)."""
    try:
        start = stdout.index(ROUTING_COMPLETENESS_BEGIN)
        stop = stdout.index(ROUTING_COMPLETENESS_END, start)
    except ValueError:
        return None
    match = _COMPLETENESS_RE.search(stdout[start:stop])
    if match is None:
        return None
    return {
        "signal_net_count": int(match.group(1)),
        "unrouted_net_count": int(match.group(2)),
    }


def parse_global_route_congestion(stdout: str) -> dict[str, Any] | None:
    """Parse the first ``GRT-0096`` congestion table (and ``GRT-0014`` routed
    net count) from ``global_route -verbose`` output; ``None`` if absent."""
    marker = stdout.find("Final congestion report:")
    if marker < 0:
        return None
    layers: dict[str, dict[str, Any]] = {}
    total: dict[str, Any] | None = None
    for line in stdout[marker:].splitlines()[1:]:
        row = _GRT_ROW_RE.match(line.strip())
        if row is None:
            if layers and total is not None:
                break
            continue
        entry = {
            "resource": int(row["resource"]),
            "demand": int(row["demand"]),
            "usage_pct": float(row["usage"]),
            "max_horizontal_overflow": int(row["max_h"]),
            "max_vertical_overflow": int(row["max_v"]),
            "total_overflow": int(row["total"]),
        }
        if row["layer"] == "Total":
            total = entry
        else:
            layers[row["layer"]] = entry
    if total is None or not layers:
        return None
    routed = _GRT_ROUTED_NETS_RE.search(stdout[marker:])
    return {
        "total_overflow": total["total_overflow"],
        "max_horizontal_overflow": total["max_horizontal_overflow"],
        "max_vertical_overflow": total["max_vertical_overflow"],
        "usage_pct": total["usage_pct"],
        "max_layer_usage_pct": max(v["usage_pct"] for v in layers.values()),
        "overflow_per_layer": layers,
        "routed_net_count": int(routed.group(1)) if routed else None,
    }


def parse_placement_routability(stdout: str) -> dict[str, Any]:
    """Routability-driven placement figures from ``global_placement`` output:
    the last routability iteration's ``GPL-0041``/``GPL-0042`` and the final
    ``GPL-1005`` weighted congestion. Missing figures are ``None``."""
    overflow = _GPL_OVERFLOW_RE.findall(stdout)
    tiles = _GPL_TILES_RE.findall(stdout)
    final = _GPL_FINAL_RE.findall(stdout)
    return {
        "total_routing_overflow": float(overflow[-1]) if overflow else None,
        "overflowed_tile_count": int(tiles[-1]) if tiles else None,
        "final_weighted_congestion": float(final[-1]) if final else None,
    }


def detailed_route_convergence(metrics: dict[str, Any]) -> dict[str, Any]:
    """Per-iteration violation series of the *first* ``detailed_route`` pass
    (the primary route, before any antenna-repair reroute) from the saved
    metrics. That pass is scoped ``route_pass:0__`` when antenna-repair passes
    follow it (see :func:`detailed_route_lines`) and unqualified otherwise."""
    prefix = "route_pass:0__"
    series = _iteration_series(metrics, prefix)
    if not series:
        prefix = ""
        series = _iteration_series(metrics, prefix)
    if not series:
        return {
            "first_iteration_drc_violation_count": None,
            "iteration_count": None,
            "drc_violations_per_iteration": None,
            "converged": None,
        }
    return {
        "first_iteration_drc_violation_count": series[0],
        "iteration_count": len(series),
        "drc_violations_per_iteration": series,
        "converged": series[-1] == 0,
    }


def _iteration_series(metrics: dict[str, Any], prefix: str) -> list[int]:
    found: dict[int, int] = {}
    for key, value in metrics.items():
        if not key.startswith(prefix):
            continue
        match = _ITER_KEY_RE.match(key[len(prefix) :])
        if match and isinstance(value, int):
            found[int(match.group(1))] = value
    if not found or sorted(found) != list(range(len(found))):
        return []
    return [found[index] for index in range(len(found))]


def build_congestion(
    *,
    global_route: dict[str, Any] | None,
    completeness: dict[str, int] | None,
    placement: dict[str, Any],
    detailed: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the route-stage ``congestion`` block. Every field the engine
    could not produce is ``null`` and named, with its reason, in
    ``null_reasons`` -- a missing value is never silently ``0``."""
    reasons: dict[str, str] = {}
    gr_keys = (
        "total_overflow",
        "max_horizontal_overflow",
        "max_vertical_overflow",
        "usage_pct",
        "max_layer_usage_pct",
        "overflow_per_layer",
        "routed_net_count",
    )
    gr: dict[str, Any] = {}
    for key in gr_keys:
        gr[key] = None if global_route is None else global_route[key]
        if gr[key] is None:
            reasons[f"global_route.{key}"] = _NOT_REPORTED
    gr["max_gcell_usage_pct"] = None
    reasons["global_route.max_gcell_usage_pct"] = _NO_GCELL_USAGE

    plc = dict(placement)
    for key, value in plc.items():
        if value is None:
            reasons[f"placement.{key}"] = _NOT_REPORTED

    det = dict(detailed)
    for key, value in det.items():
        if value is None:
            reasons[f"detailed_route.{key}"] = (
                "the saved route metrics carry no per-iteration "
                "route__drc_errors__iter:N series"
            )

    comp = {
        "signal_net_count": None
        if completeness is None
        else completeness["signal_net_count"],
        "unrouted_net_count": None
        if completeness is None
        else completeness["unrouted_net_count"],
        "partially_routed_net_count": None,
    }
    if completeness is None:
        reasons["completeness.signal_net_count"] = _NOT_REPORTED
        reasons["completeness.unrouted_net_count"] = _NOT_REPORTED
    reasons["completeness.partially_routed_net_count"] = _NO_PARTIAL_ROUTE_CHECK

    return {
        "global_route": gr,
        "detailed_route": det,
        "completeness": comp,
        "placement": plc,
        "null_reasons": reasons,
    }
