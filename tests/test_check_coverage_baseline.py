"""Tests for the per-module coverage floor ratchet (issue #3312)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_coverage_baseline.py"
BASELINE_PATH = REPO_ROOT / "coverage-baseline.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load():
    spec = importlib.util.spec_from_file_location("check_coverage_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ccb = _load()


def _report(files: dict[str, tuple[int, int, int, int]]) -> dict:
    """files: name -> (statements, covered, branches, covered_branches)."""
    out = {}
    ts = tc = tb = tcb = 0
    for name, (s, c, b, cb) in files.items():
        out[name] = {
            "summary": {
                "num_statements": s,
                "covered_lines": c,
                "num_branches": b,
                "covered_branches": cb,
            }
        }
        ts, tc, tb, tcb = ts + s, tc + c, tb + b, tcb + cb
    totals = {
        "num_statements": ts,
        "covered_lines": tc,
        "num_branches": tb,
        "covered_branches": tcb,
    }
    return {"files": out, "totals": totals}


A = "src/klayout_tools/a.py"


def test_build_takes_minimum_across_reports_rounded_down():
    hi = _report({A: (100, 95, 10, 9)})
    lo = _report({A: (100, 90, 10, 7)})
    base = ccb.build_baseline([hi, lo])
    assert base["modules"][A] == {"line": 90.0, "branch": 70.0}
    assert base["total"] == {"line": 90.0, "branch": 70.0}
    third = _report({A: (3, 2, 0, 0)})
    assert ccb.build_baseline([third])["modules"][A] == {"line": 66.6, "branch": None}


def test_same_report_passes_its_own_baseline():
    rep = _report({A: (7, 5, 3, 1)})
    assert ccb.compare(rep, ccb.build_baseline([rep])) == ([], [])


def test_drop_within_tolerance_passes_beyond_fails():
    base = ccb.build_baseline([_report({A: (100, 90, 0, 0)})])
    assert ccb.compare(_report({A: (100, 89, 0, 0)}), base)[0] == []
    regressions, _ = ccb.compare(_report({A: (100, 80, 0, 0)}), base)
    assert any(r.startswith(A) and "line" in r for r in regressions)
    assert any(r.startswith("TOTAL") for r in regressions)


def test_branch_regression_fails():
    base = ccb.build_baseline([_report({A: (100, 100, 10, 10)})])
    regressions, _ = ccb.compare(_report({A: (100, 100, 10, 5)}), base)
    assert any("branch" in r for r in regressions)


def test_improvement_and_removed_module_never_fail():
    base = ccb.build_baseline(
        [_report({A: (100, 50, 0, 0), "src/b.py": (10, 5, 0, 0)})]
    )
    assert ccb.compare(_report({A: (100, 100, 0, 0)}), base) == ([], [])


def test_new_module_needs_floor():
    base = ccb.build_baseline([_report({A: (100, 100, 0, 0)})])
    _, bad = ccb.compare(
        _report({A: (100, 100, 0, 0), "src/new.py": (10, 5, 0, 0)}), base
    )
    assert len(bad) == 1 and "src/new.py" in bad[0]
    _, ok = ccb.compare(
        _report({A: (100, 100, 0, 0), "src/new.py": (10, 9, 0, 0)}), base
    )
    assert ok == []


def test_absolute_tmp_paths_ignored():
    assert ccb.module_pcts(_report({"/tmp/x/gen.py": (1, 1, 0, 0)})) == {}


def test_main_exit_codes(tmp_path, capsys):
    rep = tmp_path / "coverage.json"
    rep.write_text(json.dumps(_report({A: (100, 90, 0, 0)})))
    bl = tmp_path / "bl.json"
    assert ccb.main(["--baseline", str(bl), "--update-baseline", str(rep)]) == 0
    assert ccb.main(["--baseline", str(bl), str(rep)]) == 0
    rep.write_text(json.dumps(_report({A: (100, 10, 0, 0)})))
    assert ccb.main(["--baseline", str(bl), str(rep)]) == 1
    assert ccb.main(["--baseline", str(bl), str(tmp_path / "missing.json")]) == 2


def test_committed_baseline_is_consistent():
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == ccb.BASELINE_SCHEMA_VERSION
    assert data["modules"]
    for name, floors in data["modules"].items():
        assert name.startswith("src/klayout_tools/"), name
        assert 0.0 <= floors["line"] <= 100.0
        assert floors["branch"] is None or 0.0 <= floors["branch"] <= 100.0
        assert (REPO_ROOT / name).exists(), f"baselined module no longer exists: {name}"


def test_ci_runs_the_check_on_the_coverage_leg():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check_coverage_baseline.py coverage.json" in text
