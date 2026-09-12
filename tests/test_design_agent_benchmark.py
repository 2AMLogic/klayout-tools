"""Tests for `scripts/design_agent_benchmark.py` -- the design-agent
benchmark harness (issue #1719).

Two tiers, mirroring `tests/test_gallery_signals.py`:

- **Unit tests** (always run) exercise `pass_at_k`'s estimator, the
  schema-validation path, and the tier-aggregation helpers without
  invoking `ngspice`.
- **Integration tests** (`@pytest.mark.skipif`, real `ngspice`) run the
  shipped task set's own reference solutions through `klt eval` -- the
  "every task has a known-good reference netlist that passes its own
  criterion" acceptance check from issue #1719 -- and prove the harness can
  fail: a deliberately-broken reference netlist must fail its own gate,
  and the `run_benchmark` pass@k aggregation must show that failure as a
  pass-rate drop (the "a deliberate regression is visible as a pass-rate
  drop" acceptance check).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import design_agent_benchmark as dab  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
TASKS_DIR = REPO_ROOT / "benchmarks" / "design-agent" / "tasks"
SCHEMA_PATH = REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "task.schema.json"

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


# --------------------------------------------------------------------------
# Unit tests: pass@k estimator
# --------------------------------------------------------------------------


def test_pass_at_k_all_pass_is_one():
    assert dab.pass_at_k(n=5, c=5, k=1) == 1.0
    assert dab.pass_at_k(n=5, c=5, k=5) == 1.0


def test_pass_at_k_all_fail_is_zero():
    assert dab.pass_at_k(n=5, c=0, k=1) == 0.0
    assert dab.pass_at_k(n=5, c=0, k=5) == 0.0


def test_pass_at_k_partial_matches_known_value():
    # n=5, c=1, k=1: probability a single random draw is the one passer.
    assert dab.pass_at_k(n=5, c=1, k=1) == pytest.approx(0.2)
    # n=5, c=1, k=5: drawing all 5 always includes the one passer.
    assert dab.pass_at_k(n=5, c=1, k=5) == 1.0


def test_pass_at_k_rejects_k_greater_than_n():
    with pytest.raises(ValueError):
        dab.pass_at_k(n=3, c=1, k=5)


# --------------------------------------------------------------------------
# Unit tests: schema validation
# --------------------------------------------------------------------------


def test_shipped_task_set_validates_against_schema():
    result = dab.validate_tasks(TASKS_DIR, SCHEMA_PATH, REPO_ROOT)
    assert result["valid"] is True, result
    assert result["task_count"] >= 4
    assert {t["id"] for t in result["tasks"]} >= {
        "common-source-amp",
        "source-follower",
        "current-mirror",
        "differential-pair",
    }


def test_shipped_tasks_are_all_easy_tier():
    for path in dab._task_paths(TASKS_DIR):
        task = dab.load_task(path)
        assert task["tier"] == "easy"


def test_validate_rejects_task_missing_required_field():
    with tempfile.TemporaryDirectory() as tmp:
        tasks_dir = Path(tmp) / "tasks"
        tasks_dir.mkdir()
        # Missing 'tier' and 'reference' -- required per the schema.
        (tasks_dir / "broken-task.json").write_text(
            json.dumps(
                {
                    "id": "broken-task",
                    "title": "Broken",
                    "pdk": "sky130",
                    "description": "missing required fields",
                    "block_spec": {"spec_class": "x", "io_nodes": ["a"]},
                }
            )
        )
        result = dab.validate_tasks(tasks_dir, SCHEMA_PATH, REPO_ROOT)
        assert result["valid"] is False
        assert result["task_count"] == 1
        entry = result["tasks"][0]
        assert entry["valid"] is False
        assert entry["errors"]


def test_validate_rejects_id_filename_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        tasks_dir = Path(tmp) / "tasks"
        tasks_dir.mkdir()
        task = dab.load_task(TASKS_DIR / "common-source-amp.json")
        task["id"] = "some-other-id"
        (tasks_dir / "common-source-amp.json").write_text(json.dumps(task))
        result = dab.validate_tasks(tasks_dir, SCHEMA_PATH, REPO_ROOT)
        assert result["valid"] is False
        errors = result["tasks"][0]["errors"]
        assert any("does not match filename stem" in e for e in errors)


def test_validate_rejects_missing_reference_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        tasks_dir = Path(tmp) / "tasks"
        tasks_dir.mkdir()
        task = dab.load_task(TASKS_DIR / "common-source-amp.json")
        task["reference"]["eval_descriptor"] = (
            "benchmarks/design-agent/reference/does-not-exist.json"
        )
        (tasks_dir / "common-source-amp.json").write_text(json.dumps(task))
        result = dab.validate_tasks(tasks_dir, SCHEMA_PATH, REPO_ROOT)
        assert result["valid"] is False
        assert any("does not exist" in e for e in result["tasks"][0]["errors"])


# --------------------------------------------------------------------------
# Unit tests: tier aggregation
# --------------------------------------------------------------------------


def test_summarize_tier_averages_pass_at_k_across_tasks():
    task_summaries = [
        {
            "id": "a",
            "tier": "easy",
            "attempts": 5,
            "solved": 5,
            "pass_at_k": {"1": 1.0},
        },
        {
            "id": "b",
            "tier": "easy",
            "attempts": 5,
            "solved": 0,
            "pass_at_k": {"1": 0.0},
        },
    ]
    tier = dab.summarize_tier(task_summaries, ks=[1])
    assert tier["task_count"] == 2
    assert tier["solved_count"] == 1
    assert tier["pass_at_k"]["1"] == pytest.approx(0.5)


def test_summarize_tier_empty_is_zero():
    tier = dab.summarize_tier([], ks=[1, 5])
    assert tier == {"task_count": 0, "solved_count": 0, "pass_at_k": {}}


# --------------------------------------------------------------------------
# Integration: real ngspice against the shipped reference solutions
# --------------------------------------------------------------------------


@_SKIP_NO_NGSPICE
def test_every_shipped_task_reference_solution_passes_its_own_gate():
    """Acceptance check: 'every task has a known-good reference netlist
    that passes its own criterion' (issue #1719) -- runs the real `klt
    eval` -> `klt sim` -> `ngspice` chain for every shipped task."""
    result = dab.check_reference_solutions(TASKS_DIR, REPO_ROOT)
    assert result["valid"] is True, json.dumps(result, indent=2)
    assert result["task_count"] >= 4
    for entry in result["tasks"]:
        assert entry["valid"] is True, entry


@_SKIP_NO_NGSPICE
def test_reference_candidate_provider_run_reports_perfect_pass_rate():
    """The deterministic reference provider always hands back the answer
    key, so a full `run_benchmark` sweep must report pass@1 == pass@5 ==
    1.0 for every easy-tier task -- this is what proves the runner's
    attempt loop and pass@k aggregation work end-to-end (not yet a live
    S4->S6->S10 agent regression signal -- see
    `reference_candidate_provider`'s docstring)."""
    result = dab.run_benchmark(TASKS_DIR, REPO_ROOT, n_attempts=5, ks=[1, 5])
    assert result["overall"]["pass_at_k"]["1"] == 1.0
    assert result["overall"]["pass_at_k"]["5"] == 1.0
    easy_tier = result["tiers"]["easy"]
    assert easy_tier["solved_count"] == easy_tier["task_count"]


@_SKIP_NO_NGSPICE
def test_deliberately_broken_reference_netlist_fails_its_gate_and_drops_pass_rate():
    """Acceptance check: 'a deliberate regression ... is visible as a
    pass-rate drop' (issue #1719). Mutates the common-source-amp reference
    netlist's feedback resistor into a value that collapses the DC bias
    point (Rf far too small pulls the self-bias point into cutoff), copies
    the task into a scratch tasks dir pointing at the broken copy, and
    confirms: (1) `klt eval` reports the broken reference as invalid, and
    (2) a `run_benchmark` sweep against the broken task now reports pass@1
    == 0.0 for it -- the harness detecting its own regression, not the
    design pipeline's."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scratch_repo = tmp_path / "repo"
        scratch_tasks = scratch_repo / "benchmarks" / "design-agent" / "tasks"
        ref_rel = Path("benchmarks", "design-agent", "reference", "common-source-amp")
        scratch_reference = scratch_repo / ref_rel
        scratch_tasks.mkdir(parents=True)
        scratch_reference.mkdir(parents=True)

        src_reference = REPO_ROOT / ref_rel
        ref_files = (
            "cs_amp.spice",
            "models.lib",
            "sim_request.json",
            "eval_descriptor.json",
        )
        for name in ref_files:
            shutil.copy(src_reference / name, scratch_reference / name)

        task = dab.load_task(TASKS_DIR / "common-source-amp.json")
        (scratch_tasks / "common-source-amp.json").write_text(json.dumps(task))

        # Break the reference: collapse the drain-to-gate feedback resistor
        # from 300k down to 300 ohms -- the self-bias loop can no longer
        # hold the transistor in saturation with any useful headroom, and
        # the stage's midband gain collapses well below the task's 6 dB
        # floor.
        broken_netlist = (scratch_reference / "cs_amp.spice").read_text()
        broken_netlist = broken_netlist.replace(".param rf=300k", ".param rf=300")
        assert broken_netlist != (scratch_reference / "cs_amp.spice").read_text()
        (scratch_reference / "cs_amp.spice").write_text(broken_netlist)

        broken_check = dab.check_reference_solutions(scratch_tasks, scratch_repo)
        assert broken_check["valid"] is False
        assert broken_check["tasks"][0]["valid"] is False

        broken_run = dab.run_benchmark(
            scratch_tasks, scratch_repo, n_attempts=3, ks=[1]
        )
        assert broken_run["overall"]["pass_at_k"]["1"] == 0.0
        assert broken_run["tiers"]["easy"]["solved_count"] == 0
