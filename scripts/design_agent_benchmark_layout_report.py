"""Layout-flow benchmark reporting (issue #3027, epic #2252 phase 4).

Regenerates the comparison report from the durable ledgers alone -- the
executor's ``runs.jsonl`` (one ``layout_run/1`` record per finished run) and,
optionally, its ``journal.jsonl`` (start/finish timestamps). No candidate
sandbox, artifact file, simulator or credential is read, so a report can be
rebuilt on any machine from the two files, and the same ledgers always give a
byte-identical report (nothing here reads the clock).

The protocol (``layout-protocol.json``) decides what counts:

* only ``valid`` runs carry a score; every other status is shown, counted in
  the denominator unless the protocol says otherwise (``interrupted``), and
  **never** imputed or given partial credit;
* the comparison is the geometric mean of the per-pair area ratio
  ``arm / draw-only`` over *comparable pairs* -- both runs ``valid`` for the
  same (task, agent, replicate) under the same protocol and task fingerprint.
  Every excluded pair is listed with its reason, so the denominator is always
  disclosed.

Methodology after Ding, Zou, Sun, Cong, "Can Agents Design Better Chips with a
Higher Level Abstraction?" (ICCAD'26; artifact github.com/ZijD/AHRR, MIT).
Methodology only: no AHRR implementation code is copied.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import design_agent_benchmark_layout as layout
import design_agent_benchmark_layout_exec as ex

REPORT_SCHEMA = "klt.design_agent_benchmark.layout_report/1"
BASELINE_ARM = "draw-only"
#: Statuses a run can end with that are not part of any denominator.
_UNCOUNTED = "interrupted"
_ROUND = 6


class LayoutReportError(Exception):
    """The ledgers cannot be turned into a report (unreadable or corrupt)."""


def _r(value: float) -> float:
    return round(value, _ROUND)


def geomean(values: list[float]) -> float | None:
    """Geometric mean, order-independent (sorted, ``fsum``); ``None`` for no
    values -- an empty set is reported as such, never as 1.0."""
    if not values:
        return None
    logs = sorted(math.log(v) for v in values)
    return _r(math.exp(math.fsum(logs) / len(logs)))


def _mean(values: list[float]) -> float | None:
    return _r(math.fsum(sorted(values)) / len(values)) if values else None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_journal_times(out_dir: Path) -> dict[str, dict[str, str]]:
    """``{run_id: {"started_at", "finished_at"}}`` from ``journal.jsonl``.

    Optional: a ledger directory without a journal just has no timestamps. A
    torn final line is skipped, as the executor does."""
    times: dict[str, dict[str, str]] = defaultdict(dict)
    for entry in ex._read_log_lenient(out_dir / ex.JOURNAL_FILENAME):
        if entry.get("event") == "start":
            times[entry["identity"]["run_id"]]["started_at"] = entry["ts"]
        elif entry.get("event") == "finish":
            times[entry["run_id"]]["finished_at"] = entry["ts"]
    return dict(times)


def _split_records(
    records: list[dict[str, Any]], protocol: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Schema/protocol-valid records, and the rejected ones with the reason.
    A rejected record is listed, never scored."""
    accepted, rejected = [], []
    for record in records:
        result = layout.validate_run(record, protocol)
        if result["valid"]:
            accepted.append(record)
        else:
            rejected.append(
                {
                    "run_id": record.get("run_id")
                    if isinstance(record, dict)
                    else None,
                    "errors": result["errors"],
                }
            )
    return accepted, rejected


def _unit(record: dict[str, Any]) -> tuple[str, str, int]:
    return (record["task_id"], record["agent"]["id"], record["replicate"])


def _final_records(
    records: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str, str, int], dict[str, Any]], list[str]]:
    """One finished record per (task, arm, agent, replicate). ``interrupted``
    attempts are history, not outcomes. Two finished records for one key is a
    ledger defect: reported, and the key is left out rather than guessed."""
    by_key: dict[tuple[str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["status"] != _UNCOUNTED:
            key = (
                record["task_id"],
                record["arm"],
                record["agent"]["id"],
                record["replicate"],
            )
            by_key[key].append(record)
    final, errors = {}, []
    for key, group in sorted(by_key.items()):
        if len(group) == 1:
            final[key] = group[0]
        else:
            errors.append(
                f"{key}: {len(group)} finished records "
                f"({sorted(r['run_id'] for r in group)}); excluded"
            )
    return final, errors


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def _cell(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One (task, arm, agent) row: counts, failures, and score summary over the
    valid runs only."""
    statuses: dict[str, int] = defaultdict(int)
    for run in runs:
        statuses[run["status"]] += 1
    valid = [r for r in runs if r["status"] == layout.SCORED_STATUS]
    areas = [r["score"]["area_um2"] for r in valid]
    utils = [
        r["score"]["utilization"]
        for r in valid
        if r["score"]["utilization"] is not None
    ]
    margins = [
        r["score"]["sim_margin"] for r in valid if r["score"]["sim_margin"] is not None
    ]
    return {
        "runs": len(runs),
        "valid_runs": len(valid),
        "failures": {
            s: n for s, n in sorted(statuses.items()) if s != layout.SCORED_STATUS
        },
        "seed_superseded": sum(
            1 for r in valid if r["artifact"]["selected_round"] == 0
        ),
        "area_um2": {
            "best": _r(min(areas)) if areas else None,
            "mean": _mean(areas),
        },
        "utilization_mean": _mean(utils),
        "sim_margin_mean": _mean(margins),
    }


def _tables(
    final: dict[tuple[str, str, str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for (task, arm, agent, _), record in final.items():
        groups[(task, agent, arm)].append(record)
    return [
        {"task_id": task, "agent_id": agent, "arm": arm, **_cell(runs)}
        for (task, agent, arm), runs in sorted(groups.items())
    ]


def _pair_exclusion(
    base: dict[str, Any] | None, other: dict[str, Any] | None
) -> str | None:
    """Why this (task, agent, replicate) pair is not comparable, else ``None``."""
    if base is None or other is None:
        return "missing_" + (BASELINE_ARM if base is None else "arm")
    for run, label in ((base, BASELINE_ARM), (other, "arm")):
        if run["status"] != layout.SCORED_STATUS:
            return f"{label}_{run['status']}"
    if (base["protocol_sha256"], base["task_sha256"]) != (
        other["protocol_sha256"],
        other["task_sha256"],
    ):
        return "fingerprint_mismatch"
    return None


def _comparisons(
    final: dict[tuple[str, str, str, int], dict[str, Any]], arms: list[str]
) -> list[dict[str, Any]]:
    units = sorted({(t, a, rep) for (t, _, a, rep) in final})
    rows = []
    for arm in arms:
        if arm == BASELINE_ARM:
            continue
        for agent in sorted({u[1] for u in units}):
            comparable: list[tuple[str, int, float]] = []
            excluded: list[dict[str, Any]] = []
            for task, unit_agent, replicate in units:
                if unit_agent != agent:
                    continue
                base = final.get((task, BASELINE_ARM, agent, replicate))
                other = final.get((task, arm, agent, replicate))
                if base is None and other is None:
                    continue
                reason = _pair_exclusion(base, other)
                if reason or base is None or other is None:
                    excluded.append(
                        {
                            "task_id": task,
                            "replicate": replicate,
                            "reason": reason or "missing",
                        }
                    )
                else:
                    comparable.append(
                        (
                            task,
                            replicate,
                            other["score"]["area_um2"] / base["score"]["area_um2"],
                        )
                    )
            ratios = [ratio for *_, ratio in comparable]
            per_task = {
                task: geomean([r for t, _, r in comparable if t == task])
                for task in sorted({t for t, *_ in comparable})
            }
            rows.append(
                {
                    "arm": arm,
                    "agent_id": agent,
                    "baseline": BASELINE_ARM,
                    "metric": "area_um2",
                    "geomean_ratio": geomean(ratios),
                    "denominator": {
                        "comparable_pairs": len(comparable),
                        "excluded_pairs": len(excluded),
                        "attempted_pairs": len(comparable) + len(excluded),
                    },
                    "per_task_geomean_ratio": per_task,
                    "excluded": excluded,
                }
            )
    return rows


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def task_set_fingerprint(task_hashes: dict[str, set[str]]) -> str:
    """sha256 over the canonical ``{task_id: sorted task_sha256s}`` map, so a
    changed, added or removed task changes it."""
    return layout.document_sha256(
        {t: sorted(h) for t, h in sorted(task_hashes.items())}
    )


def _provenance(
    records: list[dict[str, Any]], times: dict[str, dict[str, str]]
) -> dict[str, Any]:
    task_hashes: dict[str, set[str]] = defaultdict(set)
    for r in records:
        task_hashes[r["task_id"]].add(r["task_sha256"])
    protocols = sorted({r["protocol_sha256"] for r in records})
    versions: dict[str, set[str]] = defaultdict(set)
    for r in records:
        for name, value in (r.get("versions") or {}).items():
            versions[name].add(value)
    agents = sorted(
        {
            (r["agent"]["id"], r["agent"]["provider"], r["agent"]["model"] or "")
            for r in records
        }
    )
    stamps = [t for run in times.values() for t in run.values()]
    return {
        "protocol_sha256": protocols,
        "task_set_fingerprint": task_set_fingerprint(task_hashes),
        "task_sha256": {t: sorted(h) for t, h in sorted(task_hashes.items())},
        # More than one fingerprint in one report is surfaced, never merged.
        "mixed_fingerprints": len(protocols) > 1
        or any(len(h) > 1 for h in task_hashes.values()),
        "versions": {name: sorted(values) for name, values in sorted(versions.items())},
        "agents": [{"id": i, "provider": p, "model": m or None} for i, p, m in agents],
        "first_started_at": min(
            (t["started_at"] for t in times.values() if "started_at" in t), default=None
        ),
        "last_finished_at": max(
            (t["finished_at"] for t in times.values() if "finished_at" in t),
            default=None,
        ),
        "timestamps_available": bool(stamps),
    }


def _run_index(
    records: list[dict[str, Any]], times: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": r["run_id"],
            "task_id": r["task_id"],
            "arm": r["arm"],
            "agent_id": r["agent"]["id"],
            "replicate": r["replicate"],
            "status": r["status"],
            "seed_sha256": r["seed_sha256"],
            "seed_from": r["seed_from"],
            "area_um2": r["score"]["area_um2"] if r["score"] else None,
            "started_at": times.get(r["run_id"], {}).get("started_at"),
            "finished_at": times.get(r["run_id"], {}).get("finished_at"),
        }
        for r in sorted(records, key=lambda r: r["run_id"])
    ]


def _sim_margin_note(
    protocol: dict[str, Any], tables: list[dict[str, Any]]
) -> dict[str, Any]:
    """``sim_margin`` is reported only when some valid run carries one; until
    its prerequisite (named by the protocol) exists the report says so."""
    field = next(f for f in protocol["score"]["fields"] if f["id"] == "sim_margin")
    present = any(t["sim_margin_mean"] is not None for t in tables)
    return {
        "reported": present,
        "prerequisite": None if present else field.get("deferred"),
    }


def build_report(
    records: list[dict[str, Any]],
    protocol: dict[str, Any],
    times: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The report document (``layout_report/1``) for a list of run records."""
    times = times or {}
    accepted, rejected = _split_records(records, protocol)
    final, duplicate_errors = _final_records(accepted)
    finished = list(final.values())
    arms = [a["id"] for a in protocol["arms"]]
    run_set = layout.validate_run_set(
        [r for r in accepted if r["status"] != _UNCOUNTED]
    )
    tables = _tables(final)
    return {
        "schema": REPORT_SCHEMA,
        "protocol_version": protocol["protocol_version"],
        "source": "ledgers_only",
        "score": {
            "primary": protocol["score"]["primary"],
            "direction": protocol["score"]["direction"],
            "relative_to": protocol["score"]["relative_to"],
            "sim_margin": _sim_margin_note(protocol, tables),
        },
        "provenance": _provenance(accepted, times),
        "totals": {
            "records": len(records),
            "rejected": len(rejected),
            "interrupted_attempts": sum(
                1 for r in accepted if r["status"] == _UNCOUNTED
            ),
            "counted_runs": len(finished),
            "valid_runs": sum(
                1 for r in finished if r["status"] == layout.SCORED_STATUS
            ),
        },
        "tables": tables,
        "comparisons": _comparisons(final, arms),
        "integrity": {
            "valid": not (rejected or duplicate_errors or run_set["errors"]),
            "rejected_records": rejected,
            "errors": duplicate_errors + run_set["errors"],
        },
        "runs": _run_index(accepted, times),
        "methodology": {
            "citation": "Ding, Zou, Sun, Cong, 'Can Agents Design Better Chips with "
            "a Higher Level Abstraction?', ICCAD'26; "
            "artifact github.com/ZijD/AHRR (MIT)",
            "code_copied": False,
            "note": "Methodology only; no AHRR implementation code is copied.",
        },
    }


def report_from_dir(out_dir: Path, protocol: dict[str, Any]) -> dict[str, Any]:
    """Report for an executor output directory -- ``runs.jsonl`` and
    ``journal.jsonl`` are the only files read."""
    try:
        records = ex.load_runs(out_dir)
    except (OSError, ex.LayoutExecutorError) as exc:
        raise LayoutReportError(str(exc)) from exc
    return build_report(records, protocol, load_journal_times(out_dir))


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def to_json(report: dict[str, Any]) -> str:
    """Stable serialization: same ledgers, same bytes."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def to_markdown(report: dict[str, Any]) -> str:
    prov = report["provenance"]
    protocols = ", ".join(f"`{p}`" for p in prov["protocol_sha256"]) or "-"
    lines = [
        f"# Layout-flow benchmark report (protocol {report['protocol_version']})",
        "",
        f"Score: `{report['score']['primary']}` ({report['score']['direction']}), "
        f"relative to `{report['score']['relative_to']}`.",
        f"Task-set fingerprint: `{prov['task_set_fingerprint']}`; "
        f"protocol sha256: {protocols}.",
    ]
    if prov["mixed_fingerprints"]:
        lines.append("**WARNING: ledgers mix protocol/task fingerprints.**")
    versions = "; ".join(f"{k} {', '.join(v)}" for k, v in prov["versions"].items())
    lines += [
        f"Versions: {versions or 'not recorded'}.",
        f"Window: {_fmt(prov['first_started_at'])} .. "
        f"{_fmt(prov['last_finished_at'])}.",
        "",
        "## Per-task / per-arm",
        "",
        "| task | agent | arm | runs | valid | best um2 | mean um2 | failures |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in report["tables"]:
        failures = ", ".join(f"{k}={v}" for k, v in t["failures"].items()) or "-"
        lines.append(
            f"| {t['task_id']} | {t['agent_id']} | {t['arm']} | {t['runs']} | "
            f"{t['valid_runs']} | {_fmt(t['area_um2']['best'])} | "
            f"{_fmt(t['area_um2']['mean'])} | {failures} |"
        )
    lines += [
        "",
        f"## Geometric-mean area ratio vs `{BASELINE_ARM}`",
        "",
        "| arm | agent | geomean | comparable | excluded | attempted |",
        "|---|---|---|---|---|---|",
    ]
    for c in report["comparisons"]:
        d = c["denominator"]
        lines.append(
            f"| {c['arm']} | {c['agent_id']} | {_fmt(c['geomean_ratio'])} | "
            f"{d['comparable_pairs']} | {d['excluded_pairs']} | "
            f"{d['attempted_pairs']} |"
        )
    sim = report["score"]["sim_margin"]
    if not sim["reported"]:
        lines += ["", f"`sim_margin` not reported: {sim['prerequisite']}."]
    if not report["integrity"]["valid"]:
        lines += ["", "**Integrity errors:**"]
        lines += [f"- {e}" for e in report["integrity"]["errors"]]
        lines += [
            f"- rejected record {r['run_id']}: {'; '.join(r['errors'])}"
            for r in report["integrity"]["rejected_records"]
        ]
    lines += [
        "",
        report["methodology"]["note"] + " " + report["methodology"]["citation"] + ".",
        "",
    ]
    return "\n".join(lines)
