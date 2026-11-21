#!/usr/bin/env python3
"""Per-module coverage table for the CI job summary (issue #3223).

Reads the JSON report written by ``pytest --cov-report=json:<path>`` and
prints a Markdown table of line and branch coverage per file, lowest line
coverage first, plus a total row. Report-only: it always exits 0 for a
readable report and never enforces a threshold -- a ratcheting floor is a
follow-up once there is data (see docs/guides/test-coverage.md).

Usage::

    python scripts/coverage_summary.py coverage.json >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _pct(covered: int, total: int) -> str:
    return "n/a" if total == 0 else f"{100.0 * covered / total:.1f}%"


def render(report: dict) -> str:
    """Render the coverage.py JSON report as a Markdown table."""
    rows = []
    for name, entry in report.get("files", {}).items():
        s = entry["summary"]
        rows.append(
            (
                name,
                s["num_statements"],
                s["covered_lines"],
                s.get("num_branches", 0),
                s.get("covered_branches", 0),
            )
        )
    rows.sort(key=lambda r: (r[2] / r[1] if r[1] else 1.0, r[0]))

    lines = [
        "### Python test coverage (report-only)",
        "",
        "| Module | Stmts | Line | Branches | Branch |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, stmts, cov, branches, cov_br in rows:
        line_pct, branch_pct = _pct(cov, stmts), _pct(cov_br, branches)
        lines.append(f"| `{name}` | {stmts} | {line_pct} | {branches} | {branch_pct} |")
    t = report.get("totals", {})
    lines.append(
        f"| **Total** | {t.get('num_statements', 0)} "
        f"| **{_pct(t.get('covered_lines', 0), t.get('num_statements', 0))}** "
        f"| {t.get('num_branches', 0)} "
        f"| **{_pct(t.get('covered_branches', 0), t.get('num_branches', 0))}** |"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"coverage_summary: no such report: {path}", file=sys.stderr)
        return 2
    sys.stdout.write(render(json.loads(path.read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
