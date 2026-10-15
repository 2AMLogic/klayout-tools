"""Tests for the mypy baseline ratchet (issue #3221).

Covers parsing, the compare semantics (new key fails, increased count fails,
decrease passes, line shift does not fail), baseline consistency, CI wiring,
and the committed py.typed marker.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_type_baseline.py"
BASELINE_PATH = REPO_ROOT / "type-baseline.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load():
    spec = importlib.util.spec_from_file_location("check_type_baseline", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ctb = _load()


def _line(file="src/a.py", line=1, code="arg-type", msg="bad", severity="error"):
    return json.dumps(
        {"file": file, "line": line, "column": 1, "message": msg,
         "code": code, "severity": severity}
    )  # fmt: skip


def test_parse_counts_by_file_code_message_ignoring_line():
    out = "\n".join([_line(line=3), _line(line=90), _line(msg="other")])
    counts = ctb.parse_mypy_json(out)
    assert counts[("src/a.py", "arg-type", "bad")] == 2
    assert counts[("src/a.py", "arg-type", "other")] == 1


def test_parse_ignores_notes_and_non_json():
    out = "\n".join([_line(severity="note", code=None), "garbage", _line()])
    assert sum(ctb.parse_mypy_json(out).values()) == 1


def test_line_shift_does_not_fail():
    base = ctb.parse_mypy_json(_line(line=10))
    cur = ctb.parse_mypy_json(_line(line=500))
    assert ctb.compare(cur, base) == ([], [])


def test_new_error_fails():
    base = ctb.parse_mypy_json(_line())
    cur = ctb.parse_mypy_json("\n".join([_line(), _line(msg="brand new")]))
    new, increased = ctb.compare(cur, base)
    assert [k for k, _ in new] == [("src/a.py", "arg-type", "brand new")]
    assert increased == []


def test_increased_count_fails():
    base = ctb.parse_mypy_json(_line())
    cur = ctb.parse_mypy_json("\n".join([_line(line=1), _line(line=2)]))
    new, increased = ctb.compare(cur, base)
    assert new == []
    assert increased == [(("src/a.py", "arg-type", "bad"), 1, 2)]


def test_decrease_and_removal_pass():
    base = Counter({("src/a.py", "arg-type", "bad"): 3, ("src/b.py", "index", "x"): 1})
    cur = Counter({("src/a.py", "arg-type", "bad"): 1})
    assert ctb.compare(cur, base) == ([], [])


def test_environment_problems_detected():
    counts = Counter(
        {
            ("src/a.py", "import-not-found", "m"): 1,
            ("src/a.py", "arg-type", "bad"): 1,
        }
    )
    assert ctb.environment_problems(counts) == [("src/a.py", "import-not-found", "m")]


def test_baseline_roundtrip(tmp_path):
    path = tmp_path / "b.json"
    counts = Counter({("src/a.py", "arg-type", "bad"): 2})
    ctb.write_baseline(path, counts)
    assert ctb.load_baseline(path) == counts


def test_missing_baseline_loads_empty(tmp_path):
    assert ctb.load_baseline(tmp_path / "nope.json") == Counter()


def test_committed_baseline_is_clean_of_environment_noise():
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == ctb.BASELINE_SCHEMA_VERSION
    codes = {d["code"] for d in data["diagnostics"]}
    assert not codes & ctb.ENVIRONMENT_CODES
    assert all(d["count"] >= 1 for d in data["diagnostics"])


def test_envelope_modules_have_no_baselined_entries():
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    files = {d["file"] for d in data["diagnostics"]}
    assert "src/klayout_tools/cli/output.py" not in files
    assert "src/klayout_tools/signoff_envelopes.py" not in files


def test_py_typed_present():
    assert (REPO_ROOT / "src" / "klayout_tools" / "py.typed").exists()


def test_workflow_runs_the_script():
    assert "scripts/check_type_baseline.py" in WORKFLOW.read_text(encoding="utf-8")


def test_end_to_end_against_this_checkout():
    pytest = __import__("pytest")
    pytest.importorskip("mypy")
    assert ctb.main([]) == 0
