#!/usr/bin/env python3
"""Per-module coverage floor, a ratchet not a target (issue #3312).

Follow-up to the report-only coverage measurement of #3223
(`docs/guides/test-coverage.md`). `coverage-baseline.json` (repo root) records
a line and a branch floor, in percent, per `src/` module, mirroring
`scripts/check_type_baseline.py` / `type-baseline.json`:

* Check mode (default) reads the `coverage.json` that the Python 3.12 CI leg
  writes and fails when a baselined module's line or branch coverage drops
  more than `tolerance_pct` below its floor, or when the *total* does, or when
  a module absent from the baseline lands below `new_module_floor_pct`. A
  module that improves, or that disappears, never fails: the floor is only
  re-tightened on purpose.
* `--update-baseline` rewrites the floors from one or more reports. With
  several reports each floor is the *minimum* across them, because coverage of
  skip-gated tests (Docker/oracle-only) varies run to run; seed it from
  completed `main` runs (the `coverage-report` artifact), not one run.

Floors are rounded *down* to one decimal so a rerun of the same report always
passes. Modules with no branches have no branch floor.

Usage::

    # CI's step (reads the report the pytest step just wrote)
    python3 scripts/check_coverage_baseline.py coverage.json

    # re-seed from completed main runs (download the coverage-report artifacts)
    python3 scripts/check_coverage_baseline.py --update-baseline \
        run1/coverage.json run2/coverage.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE_PATH = REPO_ROOT / "coverage-baseline.json"
BASELINE_SCHEMA_VERSION = 1
DEFAULT_TOLERANCE_PCT = 1.0
DEFAULT_NEW_MODULE_FLOOR_PCT = 80.0

# (line %, branch % or None when the module has no branches)
Pcts = tuple[float, "float | None"]


def _pct(covered: int, total: int) -> float | None:
    return None if total == 0 else 100.0 * covered / total


def _floor1(value: float) -> float:
    return math.floor(value * 10 + 1e-9) / 10


def module_pcts(report: dict) -> dict[str, Pcts]:
    """Per-file `(line %, branch %)` from a coverage.py JSON report."""
    out: dict[str, Pcts] = {}
    for name, entry in report.get("files", {}).items():
        # Tests that import generated modules from tmp dirs get measured too;
        # only repo-relative paths are stable enough to baseline.
        if Path(name).is_absolute():
            continue
        s = entry["summary"]
        line = _pct(s["covered_lines"], s["num_statements"])
        # A file with no statements (e.g. an empty __init__) is fully covered.
        out[Path(name).as_posix()] = (
            100.0 if line is None else line,
            _pct(s.get("covered_branches", 0), s.get("num_branches", 0)),
        )
    return out


def total_pcts(report: dict) -> Pcts:
    t = report.get("totals", {})
    line = _pct(t.get("covered_lines", 0), t.get("num_statements", 0))
    return (100.0 if line is None else line,
            _pct(t.get("covered_branches", 0), t.get("num_branches", 0)))  # fmt: skip


def _min_opt(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return a if b is None else b
    return min(a, b)


def build_baseline(
    reports: list[dict],
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    new_module_floor_pct: float = DEFAULT_NEW_MODULE_FLOOR_PCT,
) -> dict:
    """Floors = per-module minimum over `reports`, rounded down to 0.1."""
    modules: dict[str, Pcts] = {}
    for report in reports:
        for name, (line, branch) in module_pcts(report).items():
            if name in modules:
                prev_line, prev_branch = modules[name]
                modules[name] = (min(prev_line, line), _min_opt(prev_branch, branch))
            else:
                modules[name] = (line, branch)
    totals = [total_pcts(r) for r in reports]
    total_line = min(t[0] for t in totals)
    branches = [t[1] for t in totals if t[1] is not None]
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_by": (
            f"scripts/check_coverage_baseline.py --update-baseline "
            f"({len(reports)} report(s))"
        ),
        "tolerance_pct": tolerance_pct,
        "new_module_floor_pct": new_module_floor_pct,
        "total": {
            "line": _floor1(total_line),
            "branch": _floor1(min(branches)) if branches else None,
        },
        "modules": {
            name: {
                "line": _floor1(line),
                "branch": None if branch is None else _floor1(branch),
            }
            for name, (line, branch) in sorted(modules.items())
        },
    }


def compare(report: dict, baseline: dict) -> tuple[list[str], list[str]]:
    """Return `(regressions, new_module_failures)` as human-readable lines."""
    tol = baseline.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)
    new_floor = baseline.get("new_module_floor_pct", DEFAULT_NEW_MODULE_FLOOR_PCT)
    regressions: list[str] = []
    new_failures: list[str] = []

    def check(label: str, kind: str, cur: float | None, floor: float | None) -> None:
        if floor is None or cur is None:
            return
        if cur < floor - tol:
            regressions.append(
                f"{label}: {kind} {cur:.1f}% < floor {floor:.1f}% (tolerance {tol:.1f})"
            )

    base_modules = baseline.get("modules", {})
    for name, (line, branch) in sorted(module_pcts(report).items()):
        floors = base_modules.get(name)
        if floors is None:
            if line < new_floor:
                new_failures.append(
                    f"{name}: line {line:.1f}% < new-module floor {new_floor:.1f}%"
                )
            continue
        check(name, "line", line, floors.get("line"))
        check(name, "branch", branch, floors.get("branch"))
    t_line, t_branch = total_pcts(report)
    check("TOTAL", "line", t_line, baseline.get("total", {}).get("line"))
    check("TOTAL", "branch", t_branch, baseline.get("total", {}).get("branch"))
    return regressions, new_failures


def _load_reports(paths: list[Path]) -> list[dict] | None:
    reports = []
    for p in paths:
        if not p.is_file():
            print(f"check_coverage_baseline: no such report: {p}", file=sys.stderr)
            return None
        reports.append(json.loads(p.read_text(encoding="utf-8")))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="coverage.json file(s)")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the floors from the report(s) instead of checking",
    )
    args = parser.parse_args(argv)

    reports = _load_reports(args.reports)
    if reports is None:
        return 2
    if args.update_baseline:
        old = {}
        if args.baseline.exists():
            old = json.loads(args.baseline.read_text(encoding="utf-8"))
        data = build_baseline(
            reports,
            old.get("tolerance_pct", DEFAULT_TOLERANCE_PCT),
            old.get("new_module_floor_pct", DEFAULT_NEW_MODULE_FLOOR_PCT),
        )
        args.baseline.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote floors for {len(data['modules'])} module(s) to {args.baseline}.")
        return 0

    if len(reports) != 1:
        print("check mode takes exactly one coverage.json", file=sys.stderr)
        return 2
    if not args.baseline.exists():
        print(
            f"check_coverage_baseline: baseline missing: {args.baseline}",
            file=sys.stderr,
        )
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    regressions, new_failures = compare(reports[0], baseline)
    if regressions or new_failures:
        summary = (f"{len(regressions)} coverage regression(s), "
                   f"{len(new_failures)} under-covered new module(s):")  # fmt: skip
        print(f"::error::{summary}", file=sys.stderr)
        print(summary, file=sys.stderr)
        for line in regressions + new_failures:
            print(f"  {line}")
        print(
            "\nAdd tests. If the drop is intended, with reviewer sign-off, "
            "re-seed from completed main runs:\n"
            "  python3 scripts/check_coverage_baseline.py --update-baseline "
            "<coverage.json>...",
            file=sys.stderr,
        )
        return 1
    print(f"OK: coverage at or above the floors in {args.baseline.name} "
          f"({len(baseline.get('modules', {}))} modules).")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
