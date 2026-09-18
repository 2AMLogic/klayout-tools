"""Tests for the cyclomatic-complexity ratchet (issue #2034, decomposed from
#2011 item 3).

`scripts/check_complexity_baseline.py` is a dedicated CI step, not a plain
`ruff check .` rule: `C901` is deliberately excluded from `pyproject.toml`'s
`[tool.ruff.lint]` `select` list (see that file's `[tool.ruff.lint.mccabe]`
comment), so this script -- and its committed `complexity-baseline.json` --
is the only thing that actually enforces complexity.

Four things are asserted here:

1. **Core mechanics** -- `run_ruff_c901` correctly parses ruff's own C901
   JSON output, and `compare` correctly classifies a fresh measurement
   against a baseline into new violations (fail), regressions (fail), and
   improvements/removals (never fail).
2. **Baseline/config consistency** -- the committed `complexity-baseline.json`
   is internally consistent with the script's own `MAX_COMPLEXITY`, and that
   constant is kept in sync with `pyproject.toml`'s own recorded threshold
   (this project's floor is Python 3.10, where `tomllib` does not exist --
   see `tests/test_design_evidence_tiers.py` for the same
   importorskip-on-3.11+ tradeoff made elsewhere in this repo).
3. **Workflow wiring** -- `.github/workflows/ci.yml`'s `lint` job actually
   runs this script, so a future edit that silently drops the step loses
   this ratchet without any test noticing here instead.
4. **End to end against this checkout** -- the acceptance criterion that CI
   passes on current `main` with the baseline in place: this test runs the
   exact script CI runs and asserts it exits 0.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_complexity_baseline.py"
BASELINE_PATH = REPO_ROOT / "complexity-baseline.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_complexity_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules *before* exec_module: the script defines
    # frozen dataclasses, and dataclasses' own field-type resolution looks
    # the module up via `sys.modules[cls.__module__]` -- without this it
    # finds nothing and raises AttributeError during class creation.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ccb = _load_script_module()


# --------------------------------------------------------------------------
# Core mechanics: parsing ruff's output
# --------------------------------------------------------------------------


def test_run_ruff_c901_finds_a_synthetic_complex_function(monkeypatch, tmp_path):
    monkeypatch.setattr(ccb, "REPO_ROOT", tmp_path)
    complex_fn = "def f(x):\n" + "".join(
        f"    if x == {i}:\n        return {i}\n" for i in range(12)
    )
    (tmp_path / "scratch.py").write_text(complex_fn, encoding="utf-8")

    violations = ccb.run_ruff_c901(max_complexity=5, paths=["."])

    assert len(violations) == 1
    v = violations[0]
    assert v.file == "scratch.py"
    assert v.function == "f"
    assert v.complexity > 5


def test_run_ruff_c901_reports_nothing_under_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(ccb, "REPO_ROOT", tmp_path)
    (tmp_path / "scratch.py").write_text(
        "def f(x):\n    return x + 1\n", encoding="utf-8"
    )

    violations = ccb.run_ruff_c901(max_complexity=5, paths=["."])

    assert violations == []


# --------------------------------------------------------------------------
# Core mechanics: baseline round-trip + comparison
# --------------------------------------------------------------------------


def _v(file: str, function: str, complexity: int, line: int = 1) -> ccb.Violation:
    return ccb.Violation(file=file, function=function, complexity=complexity, line=line)


def test_write_and_load_baseline_roundtrip(tmp_path):
    path = tmp_path / "baseline.json"
    violations = [_v("a.py", "foo", 12), _v("b.py", "bar", 30)]

    ccb.write_baseline(path, violations, max_complexity=10)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == ccb.BASELINE_SCHEMA_VERSION
    assert data["max_complexity"] == 10
    assert len(data["violations"]) == 2

    loaded = ccb.load_baseline(path)
    assert loaded == {("a.py", "foo"): [12], ("b.py", "bar"): [30]}


def test_load_baseline_missing_file_is_empty(tmp_path):
    assert ccb.load_baseline(tmp_path / "does-not-exist.json") == {}


def test_compare_flags_a_new_violation_not_in_baseline():
    current = [_v("a.py", "new_fn", 15)]
    result = ccb.compare(current, baseline={})

    assert not result.ok
    assert result.new_violations == [("a.py", "new_fn", 15)]
    assert result.regressions == []


def test_compare_flags_a_regression_on_a_baselined_function():
    current = [_v("a.py", "foo", 20)]
    baseline = {("a.py", "foo"): [12]}
    result = ccb.compare(current, baseline)

    assert not result.ok
    assert result.new_violations == []
    assert result.regressions == [("a.py", "foo", 12, 20)]


def test_compare_allows_a_baselined_function_to_improve():
    current = [_v("a.py", "foo", 8)]
    baseline = {("a.py", "foo"): [12]}
    result = ccb.compare(current, baseline)

    assert result.ok
    assert result.improved_or_removed == 1


def test_compare_allows_a_baselined_function_to_disappear_entirely():
    current: list[ccb.Violation] = []
    baseline = {("a.py", "foo"): [12]}
    result = ccb.compare(current, baseline)

    assert result.ok
    assert result.improved_or_removed == 1


def test_compare_allows_equal_complexity():
    current = [_v("a.py", "foo", 12)]
    baseline = {("a.py", "foo"): [12]}
    result = ccb.compare(current, baseline)

    assert result.ok


def test_compare_pairs_duplicate_keys_positionally():
    # Two distinct functions happen to share (file, name) -- e.g. two
    # ambiguous nested closures. Pairing is positional after sorting both
    # sides ascending: a rare, best-effort fallback (see the module
    # docstring), not something this repo's real baseline currently needs
    # (test_baseline_has_no_duplicate_keys below).
    current = [_v("a.py", "foo", 11, line=1), _v("a.py", "foo", 25, line=50)]
    baseline = {("a.py", "foo"): [10, 20]}
    result = ccb.compare(current, baseline)

    assert not result.ok
    assert result.regressions == [("a.py", "foo", 10, 11), ("a.py", "foo", 20, 25)]


# --------------------------------------------------------------------------
# Baseline / config consistency
# --------------------------------------------------------------------------


def test_baseline_file_exists_and_is_well_formed():
    assert BASELINE_PATH.exists(), "complexity-baseline.json must be committed"
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == ccb.BASELINE_SCHEMA_VERSION
    assert data["max_complexity"] == ccb.MAX_COMPLEXITY
    assert data["violations"], "expected at least one baselined function"


def test_baseline_entries_all_exceed_the_threshold():
    # An entry at or below MAX_COMPLEXITY would be a no-op baseline row --
    # nothing enforces anything about it, and it would silently mask a
    # regression up to its (already-legal) recorded value.
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    offenders = [
        entry
        for entry in data["violations"]
        if entry["complexity"] <= ccb.MAX_COMPLEXITY
    ]
    assert offenders == []


def test_baseline_has_no_duplicate_keys():
    # Not a correctness requirement (compare() handles duplicates), but
    # documents that this repo's real baseline needs none of that fallback
    # logic today.
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    keys = [(entry["file"], entry["function"]) for entry in data["violations"]]
    assert len(keys) == len(set(keys))


def test_pyproject_max_complexity_matches_script_constant():
    # `tomllib` is stdlib only from Python 3.11 (PEP 680) and this project's
    # floor is 3.10 (pyproject.toml's own `requires-python`) -- scoped to
    # this one test rather than a module-level import, same tradeoff
    # tests/test_design_evidence_tiers.py makes for the same reason.
    tomllib = pytest.importorskip("tomllib")

    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    configured = pyproject["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]
    assert configured == ccb.MAX_COMPLEXITY


# --------------------------------------------------------------------------
# Workflow wiring
# --------------------------------------------------------------------------


def test_ci_workflow_runs_the_complexity_check():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check_complexity_baseline.py" in text

    # Must live in the `lint` job, immediately alongside the plain
    # `ruff check .` step it deliberately does not fold C901 into.
    lint_job = text.split("\n  native:", 1)[0]
    assert "scripts/check_complexity_baseline.py" in lint_job, (
        "the complexity ratchet step must live in the lint job"
    )


# --------------------------------------------------------------------------
# End to end against this checkout (the issue's own acceptance criterion:
# CI passes on current main with the baseline in place)
# --------------------------------------------------------------------------


def test_checker_passes_against_this_repository_checkout():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


def test_checker_fails_on_a_synthetic_new_violation(tmp_path):
    # Regression coverage for the issue's own test plan: a synthetic
    # over-threshold function in a scratch location, never committed, must
    # fail the check. Run against a throwaway copy of the repo's baseline
    # (pointed elsewhere via --baseline) plus a scratch file placed under
    # REPO_ROOT (ruff/the script always scope paths under REPO_ROOT).
    scratch_dir = REPO_ROOT / "_complexity_ratchet_scratch_test"
    scratch_dir.mkdir(exist_ok=True)
    try:
        complex_fn = "def scratch_fn(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(12)
        )
        (scratch_dir / "scratch.py").write_text(complex_fn, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(scratch_dir.relative_to(REPO_ROOT)),
                "--baseline",
                str(BASELINE_PATH),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "scratch_fn" in result.stdout
    finally:
        (scratch_dir / "scratch.py").unlink(missing_ok=True)
        scratch_dir.rmdir()
