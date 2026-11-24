"""scripts/coverage_summary.py renders the report-only CI coverage table (#3223)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "coverage_summary",
    Path(__file__).resolve().parent.parent / "scripts" / "coverage_summary.py",
)
assert _SPEC and _SPEC.loader
coverage_summary = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(coverage_summary)

_REPORT = {
    "files": {
        "src/klayout_tools/a.py": {
            "summary": {
                "num_statements": 10,
                "covered_lines": 9,
                "num_branches": 4,
                "covered_branches": 2,
            }
        },
        "src/klayout_tools/b.py": {
            "summary": {
                "num_statements": 10,
                "covered_lines": 5,
                "num_branches": 0,
                "covered_branches": 0,
            }
        },
    },
    "totals": {
        "num_statements": 20,
        "covered_lines": 14,
        "num_branches": 4,
        "covered_branches": 2,
    },
}


def test_render_sorts_lowest_line_coverage_first_and_totals():
    out = coverage_summary.render(_REPORT)
    assert out.index("b.py") < out.index("a.py")
    assert "| `src/klayout_tools/b.py` | 10 | 50.0% | 0 | n/a |" in out
    assert "| `src/klayout_tools/a.py` | 10 | 90.0% | 4 | 50.0% |" in out
    assert "| **Total** | 20 | **70.0%** | 4 | **50.0%** |" in out


def test_main_missing_report_is_usage_error(tmp_path):
    assert coverage_summary.main(["x", str(tmp_path / "nope.json")]) == 2
