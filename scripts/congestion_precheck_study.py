#!/usr/bin/env python3
"""Correlation + wall-clock study for the post-placement congestion
pre-check (issue #785, Epic #700 Phase 1;
`docs/design/place-and-route-improvements-survey.md` section 3.6).

This is the *runnable check* behind that issue's acceptance criteria 1 and
2: it reads a directory of already-completed `klt place-and-route` runs,
computes `klayout_tools.congestion`'s estimate from each run's
post-placement DEF, and correlates that estimate against OpenROAD's own
post-route DRC-violation count (from `detailed_route -output_drc`) plus two
secondary difficulty oracles (detailed-route wall-clock, routed
wirelength). It then models what a reject-before-route gate would have
saved on the same corpus.

It never runs OpenROAD itself -- `scripts/congestion-precheck-study.sh`
does that (via the same `openroad/orfs:latest` Docker path
`tests/corpus/place_and_route/regenerate.sh` uses) and leaves a run
directory this script consumes. Splitting the two keeps the expensive half
re-runnable without redoing the cheap half, and keeps this half importable
and testable with no Docker.

Layout expected under each `<runs-dir>/<run-id>/`:

    request.json                             the P&R request
    timings.json                             {"place_seconds", "full_route_seconds"}
    route_response.json                      the `klt place-and-route` response
    .klt/place-and-route/<top>_place.def     post-placement DEF (issue #785)
    .klt/place-and-route/<top>_route_drc.rpt `detailed_route -output_drc` report

Usage:

    uv run python scripts/congestion_precheck_study.py <runs-dir> [<runs-dir> ...] \\
        --tech-lef <path> --cell-lef <path> [--format json|text]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from typing import Any

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from klayout_tools import congestion  # noqa: E402

#: TritonRoute's `-output_drc` report names each violation with this line.
#: An empty report file means a DRC-clean route (OpenROAD writes the file
#: unconditionally), which is 0 violations, not a missing measurement.
_VIOLATION_RE = re.compile(r"^\s*violation type:", re.MULTILINE)

#: Congestion statistics considered as the gate's scalar. Reporting every
#: candidate's correlation (rather than only the one this module happens to
#: return as `congestion_score`) is the point: the go/no-go decision needs
#: to see whether *any* cheap statistic tracks the oracle, not just the
#: default one.
_CANDIDATE_STATISTICS = (
    "congestion_score",
    "peak_utilization",
    "p99_utilization",
    "p95_utilization",
    "mean_utilization",
    "overflow_um",
    "overflow_bin_fraction",
    "rsmt_um",
    "hpwl_um",
)

_ORACLES = ("drc_violation_count", "route_seconds", "routed_wirelength_um")


def _drc_violation_count(path: str) -> int | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as handle:
        return len(_VIOLATION_RE.findall(handle.read()))


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    count = len(xs)
    if count < 3:
        return None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denominator = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / denominator


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
            stop += 1
        average = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[order[position]] = average
        index = stop + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def collect_run(
    run_dir: str,
    *,
    tech_lef: str,
    cell_lefs: list[str],
    routing_layers: list[str] | None,
) -> dict[str, Any] | None:
    """Read one completed run directory into a flat record, or return
    ``None`` when it holds no usable measurement (no placement DEF, or the
    route never completed)."""
    request = _load_json(os.path.join(run_dir, "request.json"))
    if not isinstance(request, dict):
        return None
    top = request.get("hdl_toplevel", "top")
    artifacts = os.path.join(run_dir, ".klt", "place-and-route")
    def_path = os.path.join(artifacts, f"{top}_place.def")
    if not os.path.exists(def_path):
        return None

    timings = _load_json(os.path.join(run_dir, "timings.json")) or {}
    response = _load_json(os.path.join(run_dir, "route_response.json"))
    drc = _drc_violation_count(os.path.join(artifacts, f"{top}_route_drc.rpt"))

    # Both clocks on purpose: this study is routinely run on a machine with
    # other jobs on it, where wall-clock overstates the estimator's cost by
    # a contention factor that has nothing to do with the estimator. CPU
    # time is the honest "how expensive is this pre-check" number; wall
    # clock is what a sweep actually pays on the same loaded host.
    started = time.perf_counter()
    started_cpu = time.process_time()
    estimate = congestion.estimate_congestion_from_def(
        def_path,
        tech_lef=tech_lef,
        cell_lefs=cell_lefs,
        routing_layers=routing_layers,
    )
    estimate_seconds = time.perf_counter() - started
    estimate_cpu_seconds = time.process_time() - started_cpu

    record: dict[str, Any] = {
        "run_id": os.path.basename(os.path.normpath(run_dir)),
        "design": top,
        "utilization_pct": (request.get("floorplan") or {}).get("utilization_pct"),
        "seed": request.get("seed"),
        "place_seconds": timings.get("place_seconds"),
        "route_seconds": timings.get("full_route_seconds"),
        "route_returncode": timings.get("route_returncode"),
        "drc_violation_count": drc,
        "estimate_seconds": estimate_seconds,
        "estimate_cpu_seconds": estimate_cpu_seconds,
    }
    if isinstance(response, dict):
        record["routed_wirelength_um"] = response.get("wirelength_um")
        record["worst_slack_ns"] = response.get("worst_slack_ns")
        record["antenna_violation_count"] = response.get("antenna_violation_count")
    for key in _CANDIDATE_STATISTICS:
        record[key] = estimate.get(key)
    record["instance_count"] = estimate.get("instance_count")
    record["routed_net_count"] = estimate.get("routed_net_count")
    record["rsmt_over_hpwl"] = estimate.get("rsmt_over_hpwl")
    return record


def correlations(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pearson + Spearman of every candidate statistic against every oracle,
    computed per design (a cross-design pooled correlation would mostly
    measure design size, not congestion) and over the pooled set."""
    groups: dict[str, list[dict[str, Any]]] = {"__all__": records}
    for record in records:
        groups.setdefault(record["design"], []).append(record)

    out: dict[str, Any] = {}
    for group, rows in groups.items():
        group_out: dict[str, Any] = {"sample_count": len(rows)}
        for oracle in _ORACLES:
            pairs_source = [
                row
                for row in rows
                if isinstance(row.get(oracle), (int, float))
                and not isinstance(row.get(oracle), bool)
            ]
            oracle_values = [float(row[oracle]) for row in pairs_source]
            entry: dict[str, Any] = {
                "sample_count": len(pairs_source),
                "distinct_values": len(set(oracle_values)),
                "min": min(oracle_values) if oracle_values else None,
                "max": max(oracle_values) if oracle_values else None,
            }
            for statistic in _CANDIDATE_STATISTICS:
                xs = [
                    float(row[statistic])
                    for row in pairs_source
                    if isinstance(row.get(statistic), (int, float))
                ]
                if len(xs) != len(oracle_values):
                    continue
                entry[statistic] = {
                    "pearson_r": _pearson(xs, oracle_values),
                    "spearman_rho": _spearman(xs, oracle_values),
                }
            group_out[oracle] = entry
        out[group] = group_out
    return out


def gate_savings(
    records: list[dict[str, Any]], *, statistic: str, threshold: float
) -> dict[str, Any]:
    """Model a reject-before-route gate at ``threshold`` on ``statistic``.

    Reports the wall-clock a DSE sweep would have spent with and without
    the gate, plus how many genuinely-bad (nonzero-DRC) candidates it
    caught and how many DRC-clean candidates it wrongly rejected -- the
    false-reject count issue #785's AC3 turns on.
    """
    baseline = 0.0
    gated = 0.0
    rejected: list[str] = []
    false_rejects: list[str] = []
    missed_bad: list[str] = []
    for record in records:
        place_seconds = record.get("place_seconds") or 0.0
        route_seconds = record.get("route_seconds") or 0.0
        value = record.get(statistic)
        drc = record.get("drc_violation_count")
        baseline += route_seconds
        if isinstance(value, (int, float)) and value > threshold:
            rejected.append(record["run_id"])
            gated += place_seconds + (record.get("estimate_seconds") or 0.0)
            if drc == 0:
                false_rejects.append(record["run_id"])
        else:
            gated += route_seconds + (record.get("estimate_seconds") or 0.0)
            if isinstance(drc, int) and drc > 0:
                missed_bad.append(record["run_id"])
    return {
        "statistic": statistic,
        "threshold": threshold,
        "candidate_count": len(records),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "false_reject_count": len(false_rejects),
        "false_rejects": false_rejects,
        "missed_bad_count": len(missed_bad),
        "missed_bad": missed_bad,
        "baseline_seconds": baseline,
        "gated_seconds": gated,
        "seconds_saved": baseline - gated,
        "fraction_saved": (baseline - gated) / baseline if baseline else None,
    }


def _text_report(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("== per-candidate measurements ==")
    header = (
        f"{'run_id':<20} {'util%':>6} {'seed':>4} {'score':>8} {'peak':>7} "
        f"{'rsmt_um':>10} {'drc':>5} {'place_s':>8} {'route_s':>8} {'est_cpu':>7}"
    )
    lines.append(header)
    for row in payload["records"]:
        drc = row.get("drc_violation_count")
        drc = -1 if drc is None else drc
        lines.append(
            f"{row['run_id']:<20} {row.get('utilization_pct') or 0:>6} "
            f"{row.get('seed') or 0:>4} {row.get('congestion_score') or 0:>8.3f} "
            f"{row.get('peak_utilization') or 0:>7.3f} "
            f"{row.get('rsmt_um') or 0:>10.1f} "
            f"{drc:>5} "
            f"{row.get('place_seconds') or 0:>8.1f} "
            f"{row.get('route_seconds') or 0:>8.1f} "
            f"{row.get('estimate_cpu_seconds') or 0:>7.2f}"
        )
    lines.append("")
    lines.append("== correlations (spearman rho / pearson r) ==")
    for group, group_out in payload["correlations"].items():
        lines.append(f"-- {group} (n={group_out['sample_count']})")
        for oracle in _ORACLES:
            entry = group_out.get(oracle, {})
            lines.append(
                f"   oracle {oracle}: n={entry.get('sample_count')} "
                f"distinct={entry.get('distinct_values')} "
                f"range=[{entry.get('min')}, {entry.get('max')}]"
            )
            for statistic in _CANDIDATE_STATISTICS:
                stats = entry.get(statistic)
                if not isinstance(stats, dict):
                    continue
                rho = stats.get("spearman_rho")
                r = stats.get("pearson_r")
                lines.append(
                    f"      {statistic:<22} "
                    f"rho={'n/a' if rho is None else f'{rho:+.3f}'}  "
                    f"r={'n/a' if r is None else f'{r:+.3f}'}"
                )
    lines.append("")
    lines.append("== modelled gate savings ==")
    for gate in payload["gates"]:
        fraction = gate["fraction_saved"]
        lines.append(
            f"   {gate['statistic']} > {gate['threshold']}: "
            f"rejected {gate['rejected_count']}/{gate['candidate_count']}, "
            f"false-rejects {gate['false_reject_count']}, "
            f"missed-bad {gate['missed_bad_count']}, "
            f"saved {gate['seconds_saved']:.1f}s of {gate['baseline_seconds']:.1f}s "
            f"({'n/a' if fraction is None else f'{fraction:.1%}'})"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dirs", nargs="+", help="directories of completed runs")
    parser.add_argument("--tech-lef", required=True)
    parser.add_argument("--cell-lef", action="append", default=[])
    parser.add_argument(
        "--routing-layer",
        action="append",
        default=[],
        help="signal routing layer (repeatable); default = every routing layer",
    )
    parser.add_argument(
        "--gate-threshold",
        action="append",
        type=float,
        default=[],
        help="congestion_score threshold to model (repeatable)",
    )
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)

    records: list[dict[str, Any]] = []
    for runs_dir in args.runs_dirs:
        for name in sorted(os.listdir(runs_dir)):
            run_dir = os.path.join(runs_dir, name)
            if not os.path.isdir(run_dir):
                continue
            record = collect_run(
                run_dir,
                tech_lef=args.tech_lef,
                cell_lefs=args.cell_lef,
                routing_layers=args.routing_layer or None,
            )
            if record is not None:
                records.append(record)

    if not records:
        print("no usable runs found", file=sys.stderr)
        return 1

    thresholds = args.gate_threshold or [0.8, 0.9, 1.0, 1.1]
    payload = {
        "records": records,
        "correlations": correlations(records),
        "gates": [
            gate_savings(records, statistic="congestion_score", threshold=threshold)
            for threshold in thresholds
        ],
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(_text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
