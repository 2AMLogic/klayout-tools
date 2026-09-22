"""Tests for `scripts/design_agent_benchmark.py` -- the design-agent
benchmark harness (issue #1719), its single-turn live-agent candidate
provider (issue #1732), and its multi-turn tool-using *interactive* agent
provider (issue #1739).

Two tiers, mirroring `tests/test_gallery_signals.py`:

- **Unit tests** (always run) exercise `pass_at_k`'s estimator, the
  schema-validation path, the tier-aggregation helpers, and the live-agent
  provider's prompt-building/response-parsing/subprocess-invocation
  boundaries -- all without invoking `ngspice` or a real agent.
- **Integration tests** (`@pytest.mark.skipif`, real `ngspice`) run the
  shipped task set's own reference solutions through `klt eval` -- the
  "every task has a known-good reference netlist that passes its own
  criterion" acceptance check from issue #1719 -- and prove the harness can
  fail: a deliberately-broken reference netlist must fail its own gate,
  and the `run_benchmark` pass@k aggregation must show that failure as a
  pass-rate drop (the "a deliberate regression is visible as a pass-rate
  drop" acceptance check). The live-agent provider's own integration tests
  stub the actual agent invocation (per issue #1732's Test Plan: "a live
  full agent invocation likely can't run in CI -- mock/stub the skill
  invocation boundary") while still exercising the real `klt eval`/
  `ngspice` scoring path underneath it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import design_agent_benchmark as dab  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
TASKS_DIR = REPO_ROOT / "benchmarks" / "design-agent" / "tasks"
SCHEMA_PATH = REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "task.schema.json"

HAVE_NGSPICE = shutil.which("ngspice") is not None


def _have_sky130_ngspice_pdk() -> bool:
    """Whether a real, full sky130A ngspice model deck (not just the
    liberty-only timing view `.github/workflows/ci.yml`'s main `test` job
    provisions via `PDK_ROOT`/`PDK`, per that job's own `pytest` step
    comment) is resolvable right now. Every reference solution that has
    real MOSFETs resolves `sky130_fd_pr__nfet_01v8`/`pfet_01v8` via
    `models.pdk`/`models.lib` (issue #1736), and every `@_SKIP_NO_NGSPICE`-
    gated test in this module runs `klt sim`/`klt eval` against at least
    one such task (directly, or transitively via the full shipped task
    set) -- so `_SKIP_NO_NGSPICE` below folds this check in rather than
    gating each test individually."""
    try:
        from klayout_tools.pdk import find_pdk

        resolution = find_pdk(variant="sky130A")
    except Exception:  # noqa: BLE001 -- any resolution failure means "no"
        return False
    variant_dir = Path(resolution["root"]) / resolution["variant"]
    return (variant_dir / "libs.tech" / "ngspice" / "sky130.lib.spice").is_file()


HAVE_SKY130_NGSPICE_PDK = HAVE_NGSPICE and _have_sky130_ngspice_pdk()
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_SKY130_NGSPICE_PDK,
    reason=(
        "ngspice is not installed, or no full sky130A ngspice model deck is "
        "resolvable via $PDK_ROOT/$PDK -- the latter is expected in ci.yml's "
        "main `test` job, which deliberately provisions only the "
        "sky130_fd_sc_hd liberty timing view, not the full ngspice PDK "
        "every reference solution with real MOSFETs needs as of issue "
        "#1736; .github/workflows/design-agent-benchmark.yml provisions "
        "the real thing and runs these tests there"
    ),
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
# Unit tests: deterministic-provider attempt caching (issue #1781)
# --------------------------------------------------------------------------
#
# The deterministic `reference_candidate_provider` returns byte-identical
# output for every `attempt_index` by construction (its own docstring: "del
# attempt_index -- every attempt is identical for this provider"), so
# `run_task_attempts` running it (and scoring it via `klt eval`) more than
# once per task multiplies wall-clock cost for zero additional pass@k
# signal -- issue #1781's core waste. These tests exercise
# `run_task_attempts`'s caching shortcut directly, with `klt eval` itself
# stubbed out (`dab.run_eval` monkeypatched), so they run everywhere
# without `ngspice`/a real sky130A PDK.


def _counting_provider(call_log: list[int]):
    def provider(_task: dict, attempt_index: int, _repo_root: Path):
        call_log.append(attempt_index)
        return f"descriptor-{attempt_index}", None

    return provider


def test_run_task_attempts_runs_a_deterministic_provider_exactly_once(monkeypatch):
    """A provider opted into `is_deterministic = True` must be invoked (and
    scored) for real exactly once per task, regardless of `n_attempts` --
    every remaining attempt slot is a replicated copy of that one real
    result, not a fresh `klt eval` run."""
    monkeypatch.setattr(
        dab, "run_eval", lambda *_a, **_k: {"valid": True, "objective": {"v": 1}}
    )
    call_log: list[int] = []
    provider = _counting_provider(call_log)
    provider.is_deterministic = True

    task = {"id": "fake-task", "tier": "easy"}
    attempts = dab.run_task_attempts(task, 5, provider, Path("/repo"))

    assert call_log == [0]  # the provider itself only ran for attempt 0
    assert [a["attempt"] for a in attempts] == [0, 1, 2, 3, 4]
    assert all(a["valid"] for a in attempts)
    assert attempts[0]["cached"] is False
    assert all(a["cached"] is True for a in attempts[1:])
    assert all(a["wall_clock_s"] == 0.0 for a in attempts[1:])


def test_run_task_attempts_caching_preserves_a_failing_outcome(monkeypatch):
    """The cached copies must replicate whatever the one real attempt
    scored -- including a failure -- so pass@k for a deterministic provider
    against a broken reference is unaffected by this shortcut (matches
    `test_deliberately_broken_reference_netlist_fails_its_gate_and_drops_
    pass_rate`'s real-ngspice integration coverage of the same claim)."""
    monkeypatch.setattr(dab, "run_eval", lambda *_a, **_k: {"valid": False})
    call_log: list[int] = []
    provider = _counting_provider(call_log)
    provider.is_deterministic = True

    task = {"id": "fake-task", "tier": "easy"}
    attempts = dab.run_task_attempts(task, 3, provider, Path("/repo"))

    assert call_log == [0]
    assert all(not a["valid"] for a in attempts)
    summary = dab.summarize_task(task, attempts, ks=[1, 3])
    assert summary["solved"] == 0
    assert summary["pass_at_k"]["1"] == 0.0


def test_run_task_attempts_runs_every_attempt_for_a_non_deterministic_provider(
    monkeypatch,
):
    """A provider that does not opt in (the default -- every agent-backed
    provider, issues #1732/#1739) must run every attempt for real; the
    caching shortcut above must never engage for it, since a genuinely
    non-deterministic provider's later attempts can legitimately differ
    from its first."""
    monkeypatch.setattr(
        dab, "run_eval", lambda *_a, **_k: {"valid": True, "objective": {"v": 1}}
    )
    call_log: list[int] = []
    provider = _counting_provider(call_log)  # is_deterministic left unset

    task = {"id": "fake-task", "tier": "easy"}
    attempts = dab.run_task_attempts(task, 3, provider, Path("/repo"))

    assert call_log == [0, 1, 2]
    assert not any(a.get("cached") for a in attempts)


def test_run_task_attempts_zero_attempts_returns_empty_list():
    task = {"id": "fake-task", "tier": "easy"}
    attempts = dab.run_task_attempts(
        task, 0, dab.reference_candidate_provider, Path("/repo")
    )
    assert attempts == []


def test_reference_candidate_provider_is_marked_deterministic():
    assert dab.reference_candidate_provider.is_deterministic is True


def test_live_and_interactive_agent_providers_are_not_marked_deterministic():
    """The agent-backed providers must never opt into the caching shortcut
    above -- each attempt drives a genuinely non-deterministic live
    invocation."""
    assert getattr(dab.make_live_agent_provider(), "is_deterministic", False) is False
    assert (
        getattr(dab.make_interactive_agent_provider(), "is_deterministic", False)
        is False
    )


# --------------------------------------------------------------------------
# Unit tests: schema validation
# --------------------------------------------------------------------------


def test_shipped_task_set_validates_against_schema():
    result = dab.validate_tasks(TASKS_DIR, SCHEMA_PATH, REPO_ROOT)
    assert result["valid"] is True, result
    assert result["task_count"] >= 7
    assert {t["id"] for t in result["tasks"]} >= {
        "common-source-amp",
        "source-follower",
        "current-mirror",
        "differential-pair",
        "telescopic-cascode-amp",
        "miller-integrator",
        "schmitt-trigger",
    }


def test_shipped_tasks_use_only_known_tiers():
    # As of issue #1733 (medium-tier amplifiers), issue #1734 (medium-tier
    # cascode/integrator/Schmitt trigger), and issue #1735 (hard-tier
    # oscillator), the shipped task set spans easy, medium, and hard tiers --
    # this only asserts every task declares one of the schema's recognized
    # tiers, not that they're all "easy" anymore.
    tiers = {}
    for path in dab._task_paths(TASKS_DIR):
        task = dab.load_task(path)
        assert task["tier"] in dab.TIERS
        tiers.setdefault(task["tier"], []).append(task["id"])
    assert len(tiers["easy"]) >= 4
    assert len(tiers["medium"]) >= 5


def test_shipped_task_set_includes_medium_tier_amplifier_tasks():
    tasks_by_id = {
        dab.load_task(path)["id"]: dab.load_task(path)
        for path in dab._task_paths(TASKS_DIR)
    }
    for task_id in ("five-transistor-ota", "two-stage-miller-ota"):
        assert task_id in tasks_by_id
        assert tasks_by_id[task_id]["tier"] == "medium"


def test_shipped_task_set_includes_medium_tier_cascode_integrator_schmitt_tasks():
    tasks_by_id = {
        dab.load_task(path)["id"]: dab.load_task(path)
        for path in dab._task_paths(TASKS_DIR)
    }
    for task_id in ("telescopic-cascode-amp", "miller-integrator", "schmitt-trigger"):
        assert task_id in tasks_by_id
        assert tasks_by_id[task_id]["tier"] == "medium"


def test_shipped_task_set_includes_hard_tier_oscillator_task():
    tasks_by_id = {
        dab.load_task(path)["id"]: dab.load_task(path)
        for path in dab._task_paths(TASKS_DIR)
    }
    assert "rc-relaxation-oscillator" in tasks_by_id
    assert tasks_by_id["rc-relaxation-oscillator"]["tier"] == "hard"


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
# Unit tests: per-task mutation gates (issue #2262)
#
# These exercise the mutation-gate *mechanism* -- schema validation, anchor
# uniqueness, the killed/survived/unbuildable/declared-equivalent
# classification, and `validate`'s own pass/fail wiring -- with `klt eval`
# stubbed out (`dab.run_eval` monkeypatched), so they run everywhere without
# `ngspice`/a real sky130A PDK. Whether the *shipped* mutants are actually
# killed by the *real* gates is the real-ngspice integration test further
# down (`test_declared_mutation_gate_kills_every_mutant`).
# --------------------------------------------------------------------------

MUTATIONS_SCHEMA_PATH = (
    REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "mutations.schema.json"
)

#: Every task shipping a mutation gate today, derived from the tasks
#: directory rather than hardcoded -- issue #2263 adds more.
MUTATION_TASK_IDS = sorted(
    path.name[: -len(dab.MUTATIONS_SUFFIX)]
    for path in TASKS_DIR.glob(f"*{dab.MUTATIONS_SUFFIX}")
)

#: The exact (task, mutant) set migrated out of this module's former
#: hand-written `parametrize` list (issue #1734's discrimination check) into
#: per-task mutations documents by issue #2262. Asserted below so the
#: migration cannot silently change *which* mutants are exercised.
MIGRATED_MUTANTS = {
    ("telescopic-cascode-amp", "cascode-devices-removed"),
    ("miller-integrator", "integrating-cap-removed"),
    ("schmitt-trigger", "schmitt-feedback-deleted"),
    ("schmitt-trigger", "schmitt-feedback-undersized"),
}


def _stub_eval(valid: bool):
    """A `dab.run_eval` stand-in returning a fixed verdict -- `valid: True`
    means "the gate accepted this mutant" (i.e. a survivor)."""

    def _run_eval(*_args, **_kwargs):
        return {"valid": valid, "gates": [], "objective": None}

    return _run_eval


def _mutations_tasks_dir(tmp_path: Path, task_id: str, document: dict) -> Path:
    """A scratch tasks directory holding one shipped task plus a (possibly
    deliberately broken) mutations document for it. Reference paths stay
    repository-relative, so `REPO_ROOT` remains the repo root to pass."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(TASKS_DIR / f"{task_id}.json", tasks_dir / f"{task_id}.json")
    (tasks_dir / f"{task_id}{dab.MUTATIONS_SUFFIX}").write_text(json.dumps(document))
    return tasks_dir


def _shipped_mutations(task_id: str) -> dict:
    return json.loads((TASKS_DIR / f"{task_id}{dab.MUTATIONS_SUFFIX}").read_text())


def test_task_paths_skips_mutations_documents():
    """A `<id>.mutations.json` sits beside the task it belongs to, so the
    task glob must not pick it up as a task descriptor."""
    names = {path.name for path in dab._task_paths(TASKS_DIR)}
    assert names
    assert not any(name.endswith(dab.MUTATIONS_SUFFIX) for name in names)
    assert "telescopic-cascode-amp.json" in names


def test_shipped_mutations_documents_validate_against_their_schema():
    validator = dab._mutations_validator(MUTATIONS_SCHEMA_PATH)
    assert MUTATION_TASK_IDS
    for task_id in MUTATION_TASK_IDS:
        task = dab.load_task(TASKS_DIR / f"{task_id}.json")
        path = TASKS_DIR / f"{task_id}{dab.MUTATIONS_SUFFIX}"
        document, errors = dab._mutations_errors(path, task, validator)
        assert errors == [], f"{task_id}: {errors}"
        assert document is not None


def test_shipped_mutation_files_declare_exactly_the_migrated_mutants():
    """Issue #2262 is a *mechanical* port of the four mutants issue #1734's
    hand-written `parametrize` list exercised -- not a redesign of which
    mutants are tested. This pins that set."""
    declared = {
        (task_id, mutant["name"])
        for task_id in MUTATION_TASK_IDS
        for mutant in _shipped_mutations(task_id)["targeted"]
    }
    assert declared == MIGRATED_MUTANTS


def test_mutation_gate_kills_every_declared_mutant_when_the_gate_rejects(monkeypatch):
    """The aggregate happy path over the whole shipped tasks directory: the
    three tasks with a mutations document are checked, the nine without are
    skipped entirely (neither a pass nor a failure)."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(False))
    result = dab.check_mutation_gates(TASKS_DIR, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is True, result
    assert result["task_count"] == len(MUTATION_TASK_IDS)
    assert result["mutant_count"] == len(MIGRATED_MUTANTS)
    assert result["survived_count"] == 0
    assert {t["id"] for t in result["tasks"]} == set(MUTATION_TASK_IDS)
    for task_result in result["tasks"]:
        assert task_result["killed"] == task_result["total"]
        assert all(m["status"] == "killed" for m in task_result["mutants"])


def test_mutation_gate_fails_on_a_survivor(monkeypatch, tmp_path):
    """A mutant the task's own gate *accepts* is a survivor: the gate is not
    discriminating what the mutant changes, and `validate` must fail."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    tasks_dir = _mutations_tasks_dir(
        tmp_path, "miller-integrator", _shipped_mutations("miller-integrator")
    )
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert result["survived_count"] == 1
    mutant = result["tasks"][0]["mutants"][0]
    assert mutant["status"] == "survived"
    assert mutant["name"] == "integrating-cap-removed"


def test_mutation_gate_accepts_a_survivor_declared_equivalent(monkeypatch, tmp_path):
    """A survivor is only forgiven when `equivalent[]` declares it, with a
    written reason AND a code anchor that still occurs in the mutated
    netlist."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    document = _shipped_mutations("miller-integrator")
    document["equivalent"] = [
        {
            "name": "integrating-cap-removed",
            "code": "Cf gate out 1f",
            "reason": "fixture: not a real equivalence argument",
        }
    ]
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is True, result
    mutant = result["tasks"][0]["mutants"][0]
    assert mutant["status"] == "declared-equivalent"
    assert mutant["equivalence_declared"] is True
    assert result["tasks"][0]["declared_equivalent"] == 1


def test_mutation_gate_reverts_to_survived_on_a_stale_equivalence_anchor(
    monkeypatch, tmp_path
):
    """Issue #2254's requirement 2: a declaration whose code anchor no
    longer occurs reverts to SURVIVED until re-verified -- an equivalence
    argument cannot outlive the text it was written about."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    document = _shipped_mutations("miller-integrator")
    document["equivalent"] = [
        {
            "name": "integrating-cap-removed",
            "code": "Cf gate out 4711f",
            "reason": "fixture: anchor no longer present",
        }
    ]
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    mutant = result["tasks"][0]["mutants"][0]
    assert mutant["status"] == "survived"
    assert "stale" in mutant["note"]


def test_mutation_gate_reports_unverified_for_an_anchorless_equivalence(
    monkeypatch, tmp_path
):
    """A declaration with an explicitly null anchor is UNVERIFIED, and still
    fails -- it is never a way to silence a survivor."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    document = _shipped_mutations("miller-integrator")
    document["equivalent"] = [
        {
            "name": "integrating-cap-removed",
            "code": None,
            "reason": "fixture: no anchor given",
        }
    ]
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert result["tasks"][0]["unverified"] == 1
    assert result["tasks"][0]["mutants"][0]["status"] == "unverified"


def test_mutation_gate_rejects_an_equivalence_naming_no_targeted_mutant(tmp_path):
    document = _shipped_mutations("miller-integrator")
    document["equivalent"] = [
        {"name": "no-such-mutant", "code": "Cf", "reason": "fixture"}
    ]
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert any(
        "names no targeted mutant" in error for error in result["tasks"][0]["errors"]
    )


def test_mutation_gate_rejects_a_find_anchor_matching_several_sites(tmp_path):
    """Issue #2254's requirement 1: an anchor must match exactly one site in
    the netlist it targets, or validation errors out -- a mutant applied to
    two sites at once is not the mutant that was declared."""
    document = _shipped_mutations("schmitt-trigger")
    document["targeted"] = [
        {
            "name": "ambiguous-anchor",
            "netlist": "schmitt.spice",
            "find": "L=1 W=10 nf=1 mult=1",
            "replace": "L=1 W=1 nf=1 mult=1",
            "why": "fixture: matches all three NMOS cards",
        }
    ]
    tasks_dir = _mutations_tasks_dir(tmp_path, "schmitt-trigger", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert any(
        "must match exactly one" in error for error in result["tasks"][0]["errors"]
    )
    assert result["tasks"][0]["mutants"][0]["status"] == "unapplied"


def test_mutation_gate_rejects_a_find_anchor_matching_nothing(tmp_path):
    document = _shipped_mutations("miller-integrator")
    document["targeted"][0]["find"] = "Cf gate out {this-is-not-in-the-netlist}"
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert any("matches 0 sites" in error for error in result["tasks"][0]["errors"])


def test_mutation_gate_rejects_an_empty_targeted_list(tmp_path):
    """Issue #2254's requirement 3: a task shipping a mutations document
    cannot pass its own discrimination gate vacuously."""
    document = _shipped_mutations("miller-integrator")
    document["targeted"] = []
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert result["tasks"][0]["total"] == 0
    assert result["tasks"][0]["errors"]


def test_mutation_gate_rejects_a_mismatched_task_field(tmp_path):
    document = _shipped_mutations("miller-integrator")
    document["task"] = "some-other-task"
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert any(
        "does not match filename stem" in error
        for error in result["tasks"][0]["errors"]
    )


def test_mutation_gate_rejects_a_netlist_outside_the_tasks_reference_netlists(tmp_path):
    document = _shipped_mutations("miller-integrator")
    document["targeted"][0]["netlist"] = "not_a_declared_netlist.spice"
    tasks_dir = _mutations_tasks_dir(tmp_path, "miller-integrator", document)
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is False
    assert any(
        "reference.netlists entries" in error for error in result["tasks"][0]["errors"]
    )


def test_mutation_gate_reports_an_unscorable_mutant_as_unbuildable(
    monkeypatch, tmp_path
):
    """A mutant whose gate cannot be scored at all (`EvalError`: ngspice
    refused the deck, a measurement had nothing to read) is reported in its
    own bucket. It is certainly not a survivor, so it does not fail
    validation -- but it is not counted as a kill either."""

    def _raise(*_args, **_kwargs):
        raise dab.EvalError("fixture: simulator refused the deck")

    monkeypatch.setattr(dab, "run_eval", _raise)
    tasks_dir = _mutations_tasks_dir(
        tmp_path, "miller-integrator", _shipped_mutations("miller-integrator")
    )
    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["valid"] is True, result
    task_result = result["tasks"][0]
    assert task_result["unbuildable"] == 1
    assert task_result["killed"] == 0
    assert task_result["mutants"][0]["status"] == "unbuildable"


def test_mutation_gate_never_touches_the_reference_solution_cache(
    monkeypatch, tmp_path
):
    """A mutant's `klt eval` result must never land in the cross-step
    reference-solution cache (issue #1783) -- a later `run` step would then
    score the *mutant* as if it were the reference."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(False))
    writes: list[str] = []
    monkeypatch.setattr(
        dab,
        "_write_reference_cache",
        lambda *args, **kwargs: writes.append(str(args)),
    )
    tasks_dir = _mutations_tasks_dir(
        tmp_path, "miller-integrator", _shipped_mutations("miller-integrator")
    )
    dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert writes == []


def test_validate_cli_fails_and_reports_the_mutation_gate_block(
    monkeypatch, tmp_path, capsys
):
    """End-to-end through `validate`'s own CLI entry point: a stubbed gate
    that accepts everything makes the reference-solution check pass and every
    mutant survive, so `validate` must exit non-zero and say why in its
    `mutation_gates` block."""
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    # Own repo root, so the reference-solution cache this writes lands in a
    # throwaway tree rather than the checkout's own .klt/ directory.
    scratch_repo = tmp_path / "repo"
    reference_rel = Path("benchmarks", "design-agent", "reference", "miller-integrator")
    (scratch_repo / reference_rel).parent.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / reference_rel, scratch_repo / reference_rel)
    tasks_dir = _mutations_tasks_dir(
        tmp_path, "miller-integrator", _shipped_mutations("miller-integrator")
    )

    exit_code = dab.main(
        [
            "validate",
            "--tasks-dir",
            str(tasks_dir),
            "--repo-root",
            str(scratch_repo),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["reference_solutions"]["valid"] is True
    assert payload["mutation_gates"]["valid"] is False
    assert payload["mutation_gates"]["survived_count"] == 1


def test_validate_cli_can_skip_the_mutation_gates(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dab, "run_eval", _stub_eval(True))
    scratch_repo = tmp_path / "repo"
    reference_rel = Path("benchmarks", "design-agent", "reference", "miller-integrator")
    (scratch_repo / reference_rel).parent.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / reference_rel, scratch_repo / reference_rel)
    tasks_dir = _mutations_tasks_dir(
        tmp_path, "miller-integrator", _shipped_mutations("miller-integrator")
    )

    exit_code = dab.main(
        [
            "validate",
            "--tasks-dir",
            str(tasks_dir),
            "--repo-root",
            str(scratch_repo),
            "--skip-mutation-gates",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["mutation_gates"] is None


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
# Unit tests: round mode -- ledger schema, best(), scoring, isolation
# (issue #2253)
#
# All of these stub `dab.run_eval` (never touch `ngspice`) -- what matters
# here is the ledger/scoring/isolation *plumbing*, exactly the same tiering
# rationale as the reference-solution-cache tests below.
# --------------------------------------------------------------------------

LEDGER_SCHEMA_PATH = (
    REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "ledger.schema.json"
)


def _ledger_validator():
    import jsonschema

    schema = json.loads(LEDGER_SCHEMA_PATH.read_text())
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def _counting_round_provider(call_log: list[dict]):
    def provider(task: dict, round_index: int, _repo_root: Path):
        call_log.append(
            {
                "round": round_index,
                "history": list(task.get(dab.ROUND_HISTORY_CONTEXT_KEY, [])),
            }
        )
        return f"descriptor-{round_index}", None

    return provider


def test_run_task_round_valid_entry_matches_ledger_schema(monkeypatch):
    _stub_run_eval(monkeypatch, valid=True)
    validator = _ledger_validator()
    task = {"id": "fake-task"}
    entry = dab.run_task_round(task, 1, _counting_round_provider([]), Path("/repo"), [])
    validator.validate(entry)
    assert entry["schema"] == dab.LEDGER_SCHEMA
    assert entry["valid"] is True
    assert entry["score"] is not None
    assert entry["notes"] is None


def test_run_task_round_invalid_entry_matches_ledger_schema_and_scores_null(
    monkeypatch,
):
    monkeypatch.setattr(
        dab,
        "run_eval",
        lambda *_a, **_k: {
            "schema_version": 1,
            "valid": False,
            "gates": [{"check": "sim", "name": "the_gate", "status": "fail"}],
            "objective": {"name": "obj", "value": 1.0, "polarity": "maximize"},
            "metrics": {},
        },
    )
    validator = _ledger_validator()
    task = {"id": "fake-task"}
    entry = dab.run_task_round(task, 1, _counting_round_provider([]), Path("/repo"), [])
    validator.validate(entry)
    assert entry["valid"] is False
    assert entry["score"] is None  # never a partial score
    assert "the_gate" in entry["notes"]


def test_run_task_round_provider_exception_records_invalid_round_not_crash():
    def _raising_provider(_task, _round_index, _repo_root):
        raise RuntimeError("boom")

    entry = dab.run_task_round(
        {"id": "fake-task"}, 1, _raising_provider, Path("/repo"), []
    )
    assert entry["valid"] is False
    assert entry["score"] is None
    assert entry["submission_sha256"] is None
    assert "boom" in entry["notes"]
    assert entry["functional"] is None
    assert entry["ppa"] is None


def test_run_task_round_detects_timeout_from_exception_message():
    def _timeout_provider(_task, _round_index, _repo_root):
        raise dab.AgentInvocationError("agent invocation timed out after 600.0s")

    entry = dab.run_task_round(
        {"id": "fake-task"}, 1, _timeout_provider, Path("/repo"), []
    )
    assert entry["timed_out"] is True
    assert entry["valid"] is False


def test_run_task_round_non_timeout_failure_leaves_timed_out_false():
    def _failing_provider(_task, _round_index, _repo_root):
        raise dab.AgentInvocationError(
            "agent response missing labeled netlist fence(s)"
        )

    entry = dab.run_task_round(
        {"id": "fake-task"}, 1, _failing_provider, Path("/repo"), []
    )
    assert entry["timed_out"] is False


def test_run_task_round_passes_trailing_three_history_entries_to_provider(
    monkeypatch,
):
    """ "Round N's prompt includes the last 3 ledger entries" (issue #2253) --
    exercised at the provider-input boundary: `run_task_round` attaches
    `history[-3:]` to the task dict it hands the provider, under
    `ROUND_HISTORY_CONTEXT_KEY`."""
    _stub_run_eval(monkeypatch, valid=True)
    call_log: list[dict] = []
    provider = _counting_round_provider(call_log)
    task = {"id": "fake-task"}

    entries: list[dict] = []
    for i in range(1, 6):
        entry = dab.run_task_round(task, i, provider, Path("/repo"), entries)
        entries.append(entry)

    assert call_log[0]["history"] == []  # round 1: no history yet
    # Round 5 should see rounds 2, 3, 4 (the trailing 3), not round 1.
    round5_history_rounds = [e["round"] for e in call_log[-1]["history"]]
    assert round5_history_rounds == [2, 3, 4]


def test_format_round_history_empty_is_blank():
    assert dab._format_round_history(None) == ""
    assert dab._format_round_history([]) == ""


def test_format_round_history_renders_last_three_and_mentions_ledger_file():
    history = [
        {"round": i, "valid": True, "score": float(i), "notes": None}
        for i in range(1, 6)
    ]
    section = dab._format_round_history(history)
    assert "round 3" in section
    assert "round 4" in section
    assert "round 5" in section
    assert "round 1" not in section  # only the trailing 3 are rendered
    assert "round 2" not in section
    assert "../ledger.jsonl" in section


def test_build_live_agent_prompt_includes_round_history_when_present():
    task = dict(dab.load_task(TASKS_DIR / "common-source-amp.json"))
    task[dab.ROUND_HISTORY_CONTEXT_KEY] = [
        {"round": 1, "valid": False, "score": None, "notes": "invalid submission: x"}
    ]
    prompt, _stems = dab._build_live_agent_prompt(task, REPO_ROOT)
    assert "Previous rounds" in prompt
    assert "round 1" in prompt


def test_build_live_agent_prompt_omits_round_history_section_when_absent():
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    prompt, _stems = dab._build_live_agent_prompt(task, REPO_ROOT)
    assert "Previous rounds" not in prompt


def test_best_round_selects_highest_scoring_valid_round():
    entries = [
        {"round": 1, "valid": True, "score": 0.2},
        {"round": 2, "valid": False, "score": None},
        {"round": 3, "valid": True, "score": 0.9},
        {"round": 4, "valid": True, "score": 0.5},
    ]
    best = dab.best_round(entries)
    assert best["round"] == 3


def test_best_round_returns_none_when_no_valid_round():
    entries = [{"round": 1, "valid": False, "score": None}]
    assert dab.best_round(entries) is None


def test_best_round_keeps_earliest_round_on_tie():
    entries = [
        {"round": 1, "valid": True, "score": 0.5},
        {"round": 2, "valid": True, "score": 0.5},
    ]
    assert dab.best_round(entries)["round"] == 1


def test_summarize_rounds_reports_count_best_and_series():
    entries = [
        {"round": 1, "valid": True, "score": 0.2, "notes": None},
        {"round": 2, "valid": True, "score": 0.9, "notes": None},
    ]
    summary = dab.summarize_rounds(entries)
    assert summary["count"] == 2
    assert summary["best"]["round"] == 2
    assert summary["series"] == [
        {"round": 1, "valid": True, "score": 0.2},
        {"round": 2, "valid": True, "score": 0.9},
    ]


def test_round_score_descriptor_adds_margin_metric_for_worst_case_value_objective():
    descriptor = {
        "gates": [{"check": "sim", "args": {"request": "sim_request.json"}}],
        "objective": {
            "check": "sim",
            "metric": "measurements.0.worst_case.value",
            "polarity": "maximize",
            "args": {"request": "sim_request.json"},
        },
    }
    new_arg, margin_name = dab._round_score_descriptor(json.dumps(descriptor))
    assert margin_name is not None
    augmented = json.loads(new_arg)
    added = [m for m in augmented["metrics"] if m["name"] == margin_name]
    assert len(added) == 1
    assert added[0]["metric"] == "measurements.0.worst_case.margin"
    assert added[0]["check"] == "sim"
    assert added[0]["args"] == {"request": "sim_request.json"}


def test_round_score_descriptor_leaves_non_sim_objective_unmodified():
    descriptor = {
        "gates": [{"check": "drc", "args": {"file": "x.gds", "deck": "sky130"}}],
        "objective": {"check": "drc", "metric": "count", "polarity": "minimize"},
    }
    arg = json.dumps(descriptor)
    new_arg, margin_name = dab._round_score_descriptor(arg)
    assert margin_name is None
    assert new_arg == arg


def test_round_score_descriptor_leaves_non_value_suffixed_metric_unmodified():
    descriptor = {
        "gates": [{"check": "sim", "args": {"request": "sim_request.json"}}],
        "objective": {
            "check": "sim",
            "metric": "measurements.0.worst_case.margin",
            "polarity": "maximize",
            "args": {"request": "sim_request.json"},
        },
    }
    arg = json.dumps(descriptor)
    new_arg, margin_name = dab._round_score_descriptor(arg)
    assert margin_name is None
    assert new_arg == arg


def test_round_score_prefers_margin_metric_when_present():
    report = {
        "valid": True,
        "objective": {"value": 10.0, "polarity": "maximize"},
        "metrics": {"__the_margin__": 3.5},
    }
    assert dab._round_score(report, "__the_margin__") == pytest.approx(3.5)


def test_round_score_falls_back_to_polarity_oriented_objective_value():
    maximize_report = {
        "valid": True,
        "objective": {"value": 10.0, "polarity": "maximize"},
        "metrics": {},
    }
    minimize_report = {
        "valid": True,
        "objective": {"value": 10.0, "polarity": "minimize"},
        "metrics": {},
    }
    assert dab._round_score(maximize_report, None) == pytest.approx(10.0)
    assert dab._round_score(minimize_report, None) == pytest.approx(-10.0)


def test_round_score_none_when_no_margin_and_no_objective_value():
    assert (
        dab._round_score({"valid": True, "objective": {}, "metrics": {}}, None) is None
    )


def test_submission_sha256_stable_for_identical_reference_style_arguments():
    """The deterministic reference provider's descriptor_arg is a fixed,
    repository-relative file path every round -- `_submission_dependency_files`
    deliberately skips its relative `request` refs, so the hash falls back to
    the argument strings themselves, which are still stable across calls."""
    h1 = dab._submission_sha256(
        "benchmarks/design-agent/reference/x/eval_descriptor.json", None
    )
    h2 = dab._submission_sha256(
        "benchmarks/design-agent/reference/x/eval_descriptor.json", None
    )
    assert h1 == h2


def test_submission_sha256_content_hashes_absolute_referenced_files(tmp_path):
    netlist = tmp_path / "candidate.spice"
    netlist.write_text("* v1\n")
    request = tmp_path / "sim_request.json"
    request.write_text(json.dumps({"netlist": str(netlist)}))
    descriptor_arg = json.dumps(
        {
            "gates": [{"check": "sim", "args": {"request": str(request)}}],
            "objective": {
                "check": "sim",
                "metric": "measurements.0.worst_case.value",
                "args": {"request": str(request)},
            },
        }
    )
    h1 = dab._submission_sha256(descriptor_arg, None)
    netlist.write_text("* v2 -- different content\n")
    h2 = dab._submission_sha256(descriptor_arg, None)
    assert h1 != h2  # content changed under the same path -- hash must move


def test_freeze_directory_readonly_blocks_further_writes(tmp_path):
    target = tmp_path / "sandbox"
    target.mkdir()
    victim = target / "netlist.spice"
    victim.write_text("* original\n")

    dab._freeze_directory_readonly(target)

    assert not os.access(victim, os.W_OK)
    with pytest.raises(PermissionError):
        victim.write_text("* tampered\n")
    # Restore write perms so pytest's own tmp_path cleanup can remove it.
    os.chmod(victim, 0o644)
    os.chmod(target, 0o755)


def test_run_task_round_freezes_provider_created_scratch_directory(
    monkeypatch, tmp_path
):
    _stub_run_eval(monkeypatch, valid=True)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    def _provider(_task, round_index, _repo_root):
        d = scratch_root / f"attempt{round_index}"
        d.mkdir()
        (d / "netlist.spice").write_text("* candidate\n")
        return "descriptor", None

    dab.run_task_round(
        {"id": "fake-task"}, 1, _provider, Path("/repo"), [], scratch_root=scratch_root
    )

    frozen_file = scratch_root / "attempt1" / "netlist.spice"
    assert not os.access(frozen_file, os.W_OK)
    os.chmod(frozen_file, 0o644)
    os.chmod(scratch_root / "attempt1", 0o755)


def test_usage_from_scratch_dir_reads_tool_calls_and_turns(tmp_path):
    (tmp_path / dab.SESSION_SUMMARY_FILENAME).write_text(
        json.dumps({"tool_calls": 4, "turns": 2, "wall_clock_s": 12.0})
    )
    usage = dab._usage_from_scratch_dir(tmp_path)
    assert usage == {"tool_calls": 4, "turns": 2}


def test_usage_from_scratch_dir_none_when_no_summary_file(tmp_path):
    assert dab._usage_from_scratch_dir(tmp_path) is None
    assert dab._usage_from_scratch_dir(None) is None


def test_run_task_rounds_appends_fsynced_ledger_and_locks_it_when_done(
    monkeypatch, tmp_path
):
    _stub_run_eval(monkeypatch, valid=True)
    task = {"id": "fake-task"}
    entries = dab.run_task_rounds(
        task, 3, _counting_round_provider([]), Path("/repo"), rounds_root=tmp_path
    )
    assert [e["round"] for e in entries] == [1, 2, 3]

    ledger_path = tmp_path / "fake-task" / dab.LEDGER_FILENAME
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == 3
    for line, entry in zip(lines, entries, strict=True):
        assert json.loads(line) == entry

    assert not os.access(ledger_path, os.W_OK)
    os.chmod(ledger_path, 0o644)  # restore so tmp_path cleanup can remove it


def test_run_task_rounds_zero_rounds_returns_empty_list():
    assert (
        dab.run_task_rounds(
            {"id": "x"}, 0, dab.reference_candidate_provider, Path("/repo")
        )
        == []
    )


def test_reference_provider_round_mode_resubmits_identical_reference(monkeypatch):
    """The required "plumbing test" (issue #2253): the deterministic
    reference provider ignores round-history context entirely, so round 1
    submits the task's own reference and every later round resubmits the
    identical thing -- `submission_sha256` must not move across rounds."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    _stub_run_eval(monkeypatch, valid=True)

    entries = [
        dab.run_task_round(task, i, dab.reference_candidate_provider, REPO_ROOT, [])
        for i in range(1, 4)
    ]
    hashes = {e["submission_sha256"] for e in entries}
    assert len(hashes) == 1, "reference provider must resubmit byte-identical rounds"
    assert all(e["valid"] for e in entries)


def test_run_benchmark_rounds_are_additive_when_requested(monkeypatch, tmp_path):
    task = _write_reference_cache_fixture(tmp_path)
    _stub_run_eval(monkeypatch, valid=True)

    result = dab.run_benchmark(
        tasks_dir=tmp_path / "tasks",
        repo_root=tmp_path,
        n_attempts=0,
        ks=[1],
        provider=dab.reference_candidate_provider,
        provider_name="reference",
        n_rounds=2,
        rounds_root=tmp_path / "rounds",
    )
    assert result["n_rounds"] == 2
    assert len(result["tasks"]) == 1
    rounds_summary = result["tasks"][0]["rounds"]
    assert rounds_summary["count"] == 2
    assert rounds_summary["best"] is not None
    assert len(rounds_summary["series"]) == 2
    assert Path(rounds_summary["ledger_path"]).is_file()
    _ = task  # fixture return value unused beyond building the tree on disk


def test_run_benchmark_without_rounds_output_is_byte_for_byte_unchanged(
    monkeypatch, tmp_path
):
    """Regression guard for "pass@k output is unchanged (additive JSON
    fields only)" -- omitting the three new parameters must not add
    `"rounds"`/`"n_rounds"` anywhere in the report."""
    _write_reference_cache_fixture(tmp_path)
    _stub_run_eval(monkeypatch, valid=True)

    result = dab.run_benchmark(
        tasks_dir=tmp_path / "tasks",
        repo_root=tmp_path,
        n_attempts=1,
        ks=[1],
        provider=dab.reference_candidate_provider,
        provider_name="reference",
    )
    assert "n_rounds" not in result
    assert "rounds" not in result["tasks"][0]


def _restore_writable(root: Path) -> None:
    """Undo `_freeze_directory_readonly` so pytest's own `tmp_path` retention
    policy can delete the tree later. `0o555` directories are still
    listable/traversable, so a top-down walk reaches everything."""
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o644)


def _agent_style_descriptor(sandbox: Path) -> str:
    """An `eval` descriptor shaped exactly like the one an agent-backed
    provider returns (`_build_live_agent_descriptor`): absolute `request`
    paths into the provider's own writable sandbox, and a request document
    naming the candidate netlist by absolute path."""
    sandbox.mkdir(parents=True, exist_ok=True)
    netlist = sandbox / "cand.spice"
    netlist.write_text("* candidate v1\n")
    request = sandbox / "sim_request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": str(netlist),
                "models": {"pdk": "sky130A", "lib": "libs.tech/ngspice/x.lib.spice"},
            }
        )
    )
    return json.dumps(
        {
            "gates": [{"check": "sim", "name": "g", "args": {"request": str(request)}}],
            "objective": {
                "check": "sim",
                "metric": "measurements.0.worst_case.value",
                "polarity": "maximize",
                "args": {"request": str(request)},
            },
        }
    )


def test_snapshot_submission_copies_referenced_files_and_rewrites_descriptor(tmp_path):
    """ "The harness keeps its own copy of each round's submission; scoring
    never reads a path the agent can still write" (issue #2253): every
    absolute-path file the descriptor names is copied out of the provider's
    sandbox, and the descriptor handed to `klt eval` points only at copies."""
    sandbox = tmp_path / "sandbox"
    descriptor_arg = _agent_style_descriptor(sandbox)
    dest = tmp_path / "submissions" / "round-1"

    new_arg, copied = dab._snapshot_submission(descriptor_arg, dest)

    rewritten = json.loads(new_arg)
    scored_request = Path(rewritten["gates"][0]["args"]["request"])
    assert scored_request.parent == dest
    # The same request referenced twice is snapshotted once, not twice.
    assert rewritten["objective"]["args"]["request"] == str(scored_request)
    assert len(copied) == 2  # the request document and the netlist it names

    scored_doc = json.loads(scored_request.read_text())
    scored_netlist = Path(scored_doc["netlist"])
    assert scored_netlist.parent == dest
    assert scored_netlist.read_text() == "* candidate v1\n"
    # `models` is never rewritten: it resolves against $PDK_ROOT, and is not
    # a file any agent can write.
    assert scored_doc["models"] == {
        "pdk": "sky130A",
        "lib": "libs.tech/ngspice/x.lib.spice",
    }

    # The copy is genuinely independent of the agent's own file.
    (sandbox / "cand.spice").write_text("* tampered after submission\n")
    assert scored_netlist.read_text() == "* candidate v1\n"


def test_snapshot_submission_leaves_relative_reference_descriptor_untouched(tmp_path):
    """The deterministic `reference` provider hands back a repository-
    committed descriptor with *relative* request paths -- nothing an agent
    can write, so nothing to snapshot, and the descriptor must come back
    byte-identical (the reference round stays exactly what `--attempts` mode
    would have scored)."""
    descriptor_arg = json.dumps(
        {"gates": [{"check": "sim", "args": {"request": "sim_request.json"}}]}
    )
    new_arg, copied = dab._snapshot_submission(descriptor_arg, tmp_path / "dest")
    assert new_arg == descriptor_arg
    assert copied == []
    assert not (tmp_path / "dest").exists()


def test_run_task_round_scores_the_harness_copy_not_the_agent_sandbox(
    monkeypatch, tmp_path
):
    scored_calls = _stub_run_eval(monkeypatch, valid=True)
    sandbox = tmp_path / "sandbox"
    descriptor_arg = _agent_style_descriptor(sandbox)
    submission_dir = tmp_path / "submissions" / "round-1"

    entry = dab.run_task_round(
        {"id": "fake-task"},
        1,
        lambda *_a: (descriptor_arg, None),
        Path("/repo"),
        [],
        submission_dir=submission_dir,
    )

    scored_descriptor = json.loads(scored_calls[-1][0])
    scored_request = Path(scored_descriptor["gates"][0]["args"]["request"])
    assert scored_request.parent == submission_dir
    assert sandbox not in scored_request.parents
    # ... and the harness's copy is frozen once the round is scored.
    assert not os.access(scored_request, os.W_OK)
    assert entry["valid"] is True
    _restore_writable(tmp_path)


def test_run_task_rounds_sandbox_sits_beside_the_ledger(monkeypatch, tmp_path):
    """Round N's prompt tells the session it may read `../ledger.jsonl`; that
    only holds if each round's sandbox is a direct child of the task's own
    round directory. Also guards that the harness's pre-created
    `submissions/` tree is never mistaken for a provider-created sandbox by
    the before/after subdirectory diff (which would attribute the wrong
    `usage` to the round, and freeze the wrong directory)."""
    _stub_run_eval(monkeypatch, valid=True)
    task_root = tmp_path / "fake-task"

    def _provider(_task, round_index, _repo_root):
        sandbox = task_root / f"round-{round_index}"
        sandbox.mkdir(parents=True)
        (sandbox / dab.SESSION_SUMMARY_FILENAME).write_text(
            json.dumps({"tool_calls": round_index, "turns": 1})
        )
        return "descriptor", None

    entries = dab.run_task_rounds(
        {"id": "fake-task"},
        2,
        _provider,
        Path("/repo"),
        rounds_root=tmp_path,
        scratch_root=task_root,
    )

    assert [e["usage"] for e in entries] == [
        {"tool_calls": 1, "turns": 1},
        {"tool_calls": 2, "turns": 1},
    ]
    ledger = task_root / dab.LEDGER_FILENAME
    assert (task_root / "round-1" / ".." / dab.LEDGER_FILENAME).resolve() == (
        ledger.resolve()
    )
    assert (task_root / dab.SUBMISSIONS_DIRNAME).is_dir()
    _restore_writable(tmp_path)


def test_run_benchmark_builds_one_round_provider_per_task_round_root(
    monkeypatch, tmp_path
):
    _write_reference_cache_fixture(tmp_path)
    _stub_run_eval(monkeypatch, valid=True)
    seen: list[Path] = []

    def _factory(root: Path):
        seen.append(root)
        return dab.reference_candidate_provider

    result = dab.run_benchmark(
        tasks_dir=tmp_path / "tasks",
        repo_root=tmp_path,
        n_attempts=0,
        ks=[1],
        provider=dab.reference_candidate_provider,
        provider_name="reference",
        n_rounds=2,
        rounds_root=tmp_path / "rounds",
        round_provider_factory=_factory,
    )

    assert seen == [tmp_path / "rounds" / "fixture-task"]
    assert result["tasks"][0]["rounds"]["ledger_path"] == str(
        tmp_path / "rounds" / "fixture-task" / dab.LEDGER_FILENAME
    )


def test_summarize_tier_does_not_count_zero_attempt_tasks_as_solved():
    """`--attempts 0 --rounds R` (round mode only) must not report every task
    as solved just because it passed all zero of its attempts."""
    summaries = [
        {
            "id": "t",
            "tier": "easy",
            "attempts": 0,
            "solved": 0,
            "pass_at_k": {},
            "total_wall_clock_s": 0.0,
        }
    ]
    assert dab.summarize_tier(summaries, [1]) == {
        "task_count": 1,
        "solved_count": 0,
        "pass_at_k": {},
    }


def test_cli_run_passes_round_flags_through_to_run_benchmark(monkeypatch, tmp_path):
    captured: dict = {}

    def _fake_run_benchmark(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "provider": "reference",
            "n_attempts": 0,
            "ks": [1],
            "tasks": [
                {
                    "id": "t",
                    "tier": "easy",
                    "attempts": 0,
                    "solved": 0,
                    "pass_at_k": {},
                    "total_wall_clock_s": 0.0,
                    "rounds": {
                        "count": 2,
                        "best": {"round": 2, "score": 0.5},
                        "series": [],
                        "ledger_path": "x",
                    },
                }
            ],
            "tiers": {},
            "overall": {"task_count": 1, "solved_count": 0, "pass_at_k": {}},
            "wall_clock_s": 0.0,
            "n_rounds": 2,
        }

    monkeypatch.setattr(dab, "run_benchmark", _fake_run_benchmark)
    rc = dab.main(
        [
            "run",
            "--attempts",
            "0",
            "--rounds",
            "2",
            "--rounds-root",
            str(tmp_path / "r"),
        ]
    )
    assert rc == 0
    assert captured["n_rounds"] == 2
    assert captured["rounds_root"] == tmp_path / "r"
    assert captured["round_provider_factory"] is not None


def test_cli_run_without_rounds_passes_no_round_provider_factory(monkeypatch):
    captured: dict = {}

    def _fake_run_benchmark(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "provider": "reference",
            "n_attempts": 1,
            "ks": [1],
            "tasks": [],
            "tiers": {},
            "overall": {"task_count": 0, "solved_count": 0, "pass_at_k": {}},
            "wall_clock_s": 0.0,
        }

    monkeypatch.setattr(dab, "run_benchmark", _fake_run_benchmark)
    assert dab.main(["run", "--attempts", "1", "--k", "1"]) == 0
    assert captured["n_rounds"] == 0
    assert captured["rounds_root"] is None
    assert captured["round_provider_factory"] is None


# --------------------------------------------------------------------------
# Unit tests: cross-step reference-solution cache (issue #1783)
#
# These stub `run_eval` (a spy counting calls) rather than running real
# `ngspice` -- what matters here is the cache-hit/-miss *decision*, not the
# simulation itself (already covered by the real-ngspice integration tests
# below and by `test_sim.py`). A fixture task/descriptor/request/netlist/
# model-library tree is built fresh per test under `tmp_path`.
# --------------------------------------------------------------------------


def _write_reference_cache_fixture(
    repo_root: Path,
    *,
    corners: dict | None = None,
    timeout_s: float = 60.0,
    netlist_body: str = "* fixture netlist v1\n",
    models_body: str = "* fixture models v1\n",
) -> dict:
    """Build a minimal reference-solution tree (descriptor -> sim request ->
    netlist + model library) under ``repo_root``, matching the shape every
    shipped task's own ``reference/<id>/`` directory uses, and return the
    task dict `check_reference_solutions`/`run_attempt` expect. Uses a
    plain ``{"lib": "models.lib"}`` (no ``pdk``/``pdk_root`` key) so
    `_resolve_models_lib` resolves it as a local file, never touching the
    real PDK discovery path -- these tests must not depend on a PDK being
    installed."""
    ref_dir = repo_root / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "netlist.spice").write_text(netlist_body)
    (ref_dir / "models.lib").write_text(models_body)
    request = {
        "netlist": "netlist.spice",
        "engine": "ngspice",
        "models": {"lib": "models.lib"},
        "corners": corners or {"process": ["tt"]},
        "analysis": {"kind": "op"},
        "measurements": [],
        "options": {"timeout_s": timeout_s},
    }
    (ref_dir / "sim_request.json").write_text(json.dumps(request))
    descriptor = {
        "gates": [
            {
                "check": "sim",
                "name": "fixture_gate",
                "args": {"request": "sim_request.json"},
            }
        ],
    }
    (ref_dir / "descriptor.json").write_text(json.dumps(descriptor))

    tasks_dir = repo_root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": "fixture-task",
        "tier": "easy",
        "reference": {
            "eval_descriptor": "ref/descriptor.json",
            "netlists": ["ref/netlist.spice"],
        },
    }
    (tasks_dir / "fixture-task.json").write_text(json.dumps(task))
    return task


def _stub_run_eval(monkeypatch, *, valid: bool = True):
    """Replace `dab.run_eval` with a call-counting stub that never touches
    `ngspice`, returning a fixed report shaped like a real one. Returns the
    call-count list (mutated in place) so a test can assert exactly how
    many times -- if any -- the "real" simulation was invoked."""
    calls: list[tuple[str, str | None]] = []

    def _fake_run_eval(descriptor_arg, candidate_arg=None):
        calls.append((descriptor_arg, candidate_arg))
        return {
            "schema_version": 1,
            "valid": valid,
            "gates": [],
            "objective": {"name": "obj", "value": 1.0, "polarity": "maximize"},
            "metrics": {},
        }

    monkeypatch.setattr(dab, "run_eval", _fake_run_eval)
    return calls


def test_check_reference_solutions_writes_cache_after_eval(tmp_path, monkeypatch):
    task = _write_reference_cache_fixture(tmp_path)
    calls = _stub_run_eval(monkeypatch, valid=True)

    result = dab.check_reference_solutions(tmp_path / "tasks", tmp_path)

    assert result["valid"] is True
    assert len(calls) == 1

    cache_path = dab._reference_cache_path(tmp_path, task["id"])
    assert cache_path.is_file()
    cached = json.loads(cache_path.read_text())
    assert cached["valid"] is True
    assert cached["fingerprint"] == dab._reference_solution_fingerprint(task, tmp_path)


def test_run_attempt_reference_provider_cache_hit_skips_run_eval(tmp_path, monkeypatch):
    task = _write_reference_cache_fixture(tmp_path)
    calls = _stub_run_eval(monkeypatch, valid=True)

    # `validate` (or whichever step runs first) populates the cache.
    dab.check_reference_solutions(tmp_path / "tasks", tmp_path)
    assert len(calls) == 1
    calls.clear()

    # `run`'s deterministic provider must now hit the cache -- `run_eval`
    # must NOT be called a second time.
    attempt = dab.run_attempt(task, 0, dab.reference_candidate_provider, tmp_path)

    assert calls == []  # the real simulation was never re-invoked
    assert attempt["valid"] is True
    assert attempt["error"] is None


def test_run_attempt_reference_provider_cache_miss_calls_run_eval(
    tmp_path, monkeypatch
):
    """No prior `validate` run -> no cache yet -> `run` must still work
    exactly like today (a cache miss is transparent, not an error)."""
    task = _write_reference_cache_fixture(tmp_path)
    calls = _stub_run_eval(monkeypatch, valid=True)

    attempt = dab.run_attempt(task, 0, dab.reference_candidate_provider, tmp_path)

    assert len(calls) == 1
    assert attempt["valid"] is True


def test_run_attempt_non_reference_provider_never_consults_cache(tmp_path, monkeypatch):
    """The cache is only meaningful for the deterministic reference
    provider -- an agent-backed provider produces a genuinely different
    candidate per attempt, so it must always call `run_eval` even after
    the cache has been populated for this task."""
    task = _write_reference_cache_fixture(tmp_path)
    calls = _stub_run_eval(monkeypatch, valid=True)
    dab.check_reference_solutions(tmp_path / "tasks", tmp_path)
    calls.clear()

    def _agent_stub(_task, _attempt_index, _repo_root):
        return str(tmp_path / "ref" / "descriptor.json"), None

    attempt = dab.run_attempt(task, 0, _agent_stub, tmp_path)

    assert len(calls) == 1  # never served from the reference-provider cache
    assert attempt["valid"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda tmp_path: (tmp_path / "ref" / "netlist.spice").write_text(
                "* fixture netlist v2 -- edited\n"
            ),
            id="netlist-edit",
        ),
        pytest.param(
            lambda tmp_path: (tmp_path / "ref" / "models.lib").write_text(
                "* fixture models v2 -- edited\n"
            ),
            id="model-library-edit",
        ),
        pytest.param(
            lambda tmp_path: (tmp_path / "ref" / "sim_request.json").write_text(
                json.dumps(
                    {
                        **json.loads(
                            (tmp_path / "ref" / "sim_request.json").read_text()
                        ),
                        "corners": {"process": ["tt", "ss", "ff"]},
                    }
                )
            ),
            id="corner-list-change",
        ),
        pytest.param(
            lambda tmp_path: (tmp_path / "ref" / "sim_request.json").write_text(
                json.dumps(
                    {
                        **json.loads(
                            (tmp_path / "ref" / "sim_request.json").read_text()
                        ),
                        "options": {"timeout_s": 999.0},
                    }
                )
            ),
            id="timeout-change",
        ),
    ],
)
def test_cache_invalidated_when_reference_solution_changes(
    tmp_path, monkeypatch, mutate
):
    task = _write_reference_cache_fixture(tmp_path)
    calls = _stub_run_eval(monkeypatch, valid=True)
    dab.check_reference_solutions(tmp_path / "tasks", tmp_path)
    assert len(calls) == 1
    calls.clear()

    mutate(tmp_path)

    attempt = dab.run_attempt(task, 0, dab.reference_candidate_provider, tmp_path)

    # A fingerprint mismatch must force a real re-simulation -- a stale
    # cache entry is never silently reused.
    assert len(calls) == 1
    assert attempt["valid"] is True


def test_reference_solution_fingerprint_stable_for_unchanged_files(tmp_path):
    task = _write_reference_cache_fixture(tmp_path)
    first = dab._reference_solution_fingerprint(task, tmp_path)
    second = dab._reference_solution_fingerprint(task, tmp_path)
    assert first == second


def test_load_reference_cache_returns_none_when_no_cache_file_exists(tmp_path):
    task = _write_reference_cache_fixture(tmp_path)
    fingerprint = dab._reference_solution_fingerprint(task, tmp_path)
    assert dab._load_reference_cache(tmp_path, task["id"], fingerprint) is None


def test_load_reference_cache_returns_none_on_malformed_json(tmp_path):
    task = _write_reference_cache_fixture(tmp_path)
    fingerprint = dab._reference_solution_fingerprint(task, tmp_path)
    cache_path = dab._reference_cache_path(tmp_path, task["id"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{not valid json")
    assert dab._load_reference_cache(tmp_path, task["id"], fingerprint) is None


def test_check_reference_solutions_cache_write_failure_is_swallowed(
    tmp_path, monkeypatch
):
    """A cache write failure (read-only filesystem, disk full, or -- as
    forced here -- a plain file sitting where the cache directory needs to
    be created) must not fail the `validate` step itself; it is purely a
    CI cost optimization, never a correctness requirement."""
    task = _write_reference_cache_fixture(tmp_path)
    _stub_run_eval(monkeypatch, valid=True)

    # `.klt` existing as a regular file (not a directory) forces
    # `_write_reference_cache`'s own `mkdir(parents=True)` to raise
    # `OSError` when it tries to create `.klt/design-agent-benchmark-cache`
    # underneath it.
    (tmp_path / ".klt").write_text("not a directory")

    result = dab.check_reference_solutions(tmp_path / "tasks", tmp_path)
    assert result["valid"] is True
    assert result["tasks"][0]["id"] == task["id"]
    assert not dab._reference_cache_path(tmp_path, task["id"]).exists()


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
    assert result["task_count"] >= 7
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
    for tier_name in ("easy", "medium"):
        tier = result["tiers"][tier_name]
        assert tier["task_count"] > 0, tier_name
        assert tier["solved_count"] == tier["task_count"], tier_name
        assert tier["pass_at_k"]["1"] == 1.0, tier_name
        assert tier["pass_at_k"]["5"] == 1.0, tier_name


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
        # No local models.lib: common-source-amp's sim_request.json now
        # resolves a real sky130A device library via $PDK_ROOT (issue
        # #1736), per docs/cli/sim.md's models.pdk/models.lib convention.
        ref_files = (
            "cs_amp.spice",
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


@_SKIP_NO_NGSPICE
@pytest.mark.parametrize("task_id", MUTATION_TASK_IDS)
def test_declared_mutation_gate_kills_every_mutant(task_id, tmp_path):
    """Issue #1734's "thresholds actually discriminate correct from
    incorrect sizing" check, now driven off each task's own
    ``<id>.mutations.json`` through the same :func:`dab.check_mutation_gates`
    enforcement path ``design_agent_benchmark.py validate`` uses (issue
    #2262) -- rather than off a hand-written ``parametrize`` list of
    find/replace pairs living in this test file.

    Every declared mutant leaves a circuit that still simulates cleanly and
    still *looks* like an answer -- a well-biased common-source stage, a
    working gain stage, a working CMOS inverter -- and differs from the
    reference only in the property its task is actually about. A gate that
    passed any of them would not be measuring the named circuit class. The
    Schmitt trigger's mutants matter most: its criterion is behavioral
    (hysteresis width under a transient ramp), so a DC- or small-signal-
    shaped gate would wave both of them through.

    Generic by construction: a task added to ``MUTATION_TASK_IDS`` simply by
    shipping a mutations file is exercised here with no change to this test
    (issue #2263 adds the currently-uncovered tasks)."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    shutil.copy(TASKS_DIR / f"{task_id}.json", tasks_dir / f"{task_id}.json")
    shutil.copy(
        TASKS_DIR / f"{task_id}{dab.MUTATIONS_SUFFIX}",
        tasks_dir / f"{task_id}{dab.MUTATIONS_SUFFIX}",
    )

    result = dab.check_mutation_gates(tasks_dir, REPO_ROOT, MUTATIONS_SCHEMA_PATH)
    assert result["task_count"] == 1
    task_result = result["tasks"][0]
    survivors = [
        f"{m['name']} ({m['why']}): {m['status']}"
        for m in task_result["mutants"]
        if m["status"] != "killed"
    ]
    assert not survivors, f"{task_id} gate accepted: {survivors}"
    assert task_result["total"] >= 1
    assert task_result["killed"] == task_result["total"]
    assert result["valid"] is True, task_result["errors"]


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732): response parsing
# --------------------------------------------------------------------------


def test_extract_labeled_netlists_parses_single_fence():
    text = "some prose\n```spice:cs_amp\nline one\nline two\n```\nmore prose"
    result = dab._extract_labeled_netlists(text, ["cs_amp"])
    assert result == {"cs_amp": "line one\nline two\n"}


def test_extract_labeled_netlists_parses_multiple_fences_in_order():
    text = (
        "```spice:diff_pair_diff\nA\n```\n"
        "some prose in between\n"
        "```spice:diff_pair_cm\nB\n```"
    )
    result = dab._extract_labeled_netlists(text, ["diff_pair_diff", "diff_pair_cm"])
    assert result == {"diff_pair_diff": "A\n", "diff_pair_cm": "B\n"}


def test_extract_labeled_netlists_raises_on_missing_stem():
    text = "```spice:cs_amp\nonly one\n```"
    with pytest.raises(dab.AgentInvocationError, match="sf"):
        dab._extract_labeled_netlists(text, ["cs_amp", "sf"])


def test_extract_labeled_netlists_ignores_unrequested_fences():
    text = "```spice:cs_amp\nkeep\n```\n```spice:not-requested\ndrop\n```"
    result = dab._extract_labeled_netlists(text, ["cs_amp"])
    assert result == {"cs_amp": "keep\n"}


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732): prompt building
# --------------------------------------------------------------------------


def test_build_live_agent_prompt_embeds_skill_chain_and_task_spec():
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    prompt, stems = dab._build_live_agent_prompt(task, REPO_ROOT)
    assert stems == ["cs_amp"]
    assert "design-topology-selection/SKILL.md" in prompt
    assert "design-sizing/SKILL.md" in prompt
    assert "design-netlist-authoring/SKILL.md" in prompt
    assert task["description"] in prompt
    # common-source-amp's sim_request.json now resolves a real sky130A
    # device library via $PDK_ROOT (issue #1736) rather than a repo-local
    # generic models.lib -- the device-model contract falls back to a
    # library-agnostic instruction rather than naming specific models.
    assert "do not write your own `.model` card" in prompt
    assert "```spice:cs_amp" in prompt


def test_build_live_agent_prompt_never_leaks_reference_netlist_body():
    """Only the testbench contract (measurements/corners/analysis from the
    reference `sim_request.json`) and the block spec are legitimate input to
    a live agent -- the reference solution's own component values/topology
    lines must never appear in the prompt, or the "benchmark" would just be
    grading whether the agent can copy its answer key."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    prompt, _stems = dab._build_live_agent_prompt(task, REPO_ROOT)
    reference_netlist = (REPO_ROOT / task["reference"]["netlists"][0]).read_text()
    for line in reference_netlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("*"):
            assert stripped not in prompt, f"leaked reference line: {stripped!r}"


def test_build_live_agent_prompt_multi_netlist_task_lists_both_stems():
    task = dab.load_task(TASKS_DIR / "differential-pair.json")
    _prompt, stems = dab._build_live_agent_prompt(task, REPO_ROOT)
    assert stems == ["diff_pair_diff", "diff_pair_cm"]


@pytest.mark.parametrize(
    "task_id",
    [
        "common-source-amp",
        "miller-integrator",
        "telescopic-cascode-amp",
        "schmitt-trigger",
    ],
)
def test_device_model_contract_falls_back_for_every_shipped_pdk_backed_task(task_id):
    """Every shipped task's `klt sim` request now resolves a real sky130A
    device library via $PDK_ROOT (issue #1736) rather than a repo-local
    generic `models.lib` -- the device-model contract must fall back to the
    library-agnostic instruction for all of them, never assert a specific
    (and now-nonexistent) model name like the old `bench_nmos`/`bench_pmos`
    stand-ins.
    """
    task = dab.load_task(TASKS_DIR / f"{task_id}.json")
    testbenches = dab._reference_testbenches(task, REPO_ROOT)
    contract = dab._device_model_contract(task, REPO_ROOT, testbenches)
    assert "bench_nmos" not in contract
    assert "bench_pmos" not in contract
    assert "do not write your own `.model` card" in contract


def test_device_model_contract_reads_model_cards_from_a_local_library(tmp_path):
    """Regression coverage for the local-`.model`-card-scanning branch of
    :func:`dab._device_model_contract` (still real production code -- a
    future task could ship its own generic stand-in library the same way
    every task in this benchmark did before issue #1736's sky130 swap),
    using a synthetic on-disk library rather than any shipped task's own
    (now sky130-PDK-backed) `models.lib`.
    """
    lib_path = tmp_path / "models.lib"
    lib_path.write_text(
        ".model bench_nmos NMOS(LEVEL=1 VTO=0.5 KP=120u)\n"
        ".model bench_pmos PMOS(LEVEL=1 VTO=-0.5 KP=48u)\n"
    )
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    testbenches = [
        {"netlist_stem": "x", "sim_request": {"models": {"lib": str(lib_path)}}}
    ]
    contract = dab._device_model_contract(task, REPO_ROOT, testbenches)
    assert "`bench_nmos`" in contract
    assert "PMOS: `bench_pmos`" in contract


def test_device_model_contract_falls_back_when_no_model_library_is_readable():
    """A task whose request points at a PDK model library (or an unreadable
    path) must not have a device list asserted for it -- say nothing
    specific rather than something wrong."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    testbenches = [{"netlist_stem": "x", "sim_request": {"models": {"pdk": "sky130"}}}]
    contract = dab._device_model_contract(task, REPO_ROOT, testbenches)
    assert "bench_nmos" not in contract
    assert "do not write your own `.model` card" in contract


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732): `_default_invoke_agent`
# subprocess boundary, exercised against a fake CLI (never a real agent)
# --------------------------------------------------------------------------

_FAKE_AGENT_CLI = """#!/usr/bin/env python3
import json
import sys
import time

prompt = sys.argv[sys.argv.index("-p") + 1]
if prompt == "FAIL":
    print("stub failure", file=sys.stderr)
    sys.exit(1)
if prompt == "PLAIN":
    print("plain text response, no envelope")
    sys.exit(0)
if prompt == "SLEEP":
    time.sleep(5)
    sys.exit(0)
print(json.dumps({"type": "result", "result": f"ECHO:{prompt}"}))
"""


def _fake_agent_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-claude"
    script.write_text(_FAKE_AGENT_CLI)
    script.chmod(0o755)
    return script


def test_default_invoke_agent_parses_output_format_json_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_agent_cli(tmp_path)))
    assert dab._default_invoke_agent("hello", timeout_s=10) == "ECHO:hello"


def test_default_invoke_agent_falls_back_to_raw_stdout_for_plain_text(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_agent_cli(tmp_path)))
    result = dab._default_invoke_agent("PLAIN", timeout_s=10)
    assert result.strip() == "plain text response, no envelope"


def test_default_invoke_agent_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_agent_cli(tmp_path)))
    with pytest.raises(dab.AgentInvocationError, match="stub failure"):
        dab._default_invoke_agent("FAIL", timeout_s=10)


def test_default_invoke_agent_raises_on_missing_cli(monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, "/no/such/binary-does-not-exist")
    with pytest.raises(dab.AgentInvocationError, match="not found"):
        dab._default_invoke_agent("hello", timeout_s=10)


def test_default_invoke_agent_raises_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_agent_cli(tmp_path)))
    with pytest.raises(dab.AgentInvocationError, match="timed out"):
        dab._default_invoke_agent("SLEEP", timeout_s=0.2)


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732): edge case -- a failed/timed-
# out provider must be a scored attempt failure, never a crash
# --------------------------------------------------------------------------


def test_run_attempt_records_agent_invocation_error_as_failed_attempt_not_crash():
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _boom(_task, _attempt_index, _repo_root):
        raise dab.AgentInvocationError("agent invocation timed out after 600s")

    result = dab.run_attempt(task, 0, _boom, REPO_ROOT)
    assert result["valid"] is False
    assert "timed out" in result["error"]


def test_run_attempt_records_arbitrary_provider_exception_as_failed_attempt():
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _boom(_task, _attempt_index, _repo_root):
        raise RuntimeError("unexpected provider bug")

    result = dab.run_attempt(task, 0, _boom, REPO_ROOT)
    assert result["valid"] is False
    assert "unexpected provider bug" in result["error"]


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732): end-to-end plumbing
# --------------------------------------------------------------------------
#
# These stub the agent's *response* (never invoke a real agent -- see the
# module docstring) but exercise the real prompt -> parse -> synthesized-
# descriptor -> `klt eval` -> `ngspice` path underneath it, proving the
# provider's wiring produces a genuinely scoreable candidate.


def _reference_netlist_response(task: dict) -> str:
    """A stub agent response that "cheats" by echoing each of ``task``'s own
    reference netlist(s) back, correctly labeled -- exercises the full
    plumbing without needing a real agent; a separate, non-automatable
    manual verification step (this issue's Test Plan) covers actual agent
    quality against a live invocation."""
    parts = ["S4/S5: stub response for wiring tests -- reuses the reference topology."]
    for rel_path in task["reference"]["netlists"]:
        stem = Path(rel_path).stem
        body = (REPO_ROOT / rel_path).read_text()
        parts.append(f"```spice:{stem}\n{body}\n```")
    return "\n\n".join(parts)


@_SKIP_NO_NGSPICE
def test_live_agent_provider_stub_returning_reference_netlist_scores_valid():
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    response = _reference_netlist_response(task)
    provider = dab.make_live_agent_provider(invoke_agent=lambda _prompt: response)
    result = dab.run_attempt(task, 0, provider, REPO_ROOT)
    assert result["valid"] is True, result


@_SKIP_NO_NGSPICE
def test_live_agent_provider_multi_netlist_task_scores_valid():
    task = dab.load_task(TASKS_DIR / "differential-pair.json")
    response = _reference_netlist_response(task)
    provider = dab.make_live_agent_provider(invoke_agent=lambda _prompt: response)
    result = dab.run_attempt(task, 0, provider, REPO_ROOT)
    assert result["valid"] is True, result


@_SKIP_NO_NGSPICE
def test_live_agent_provider_with_bad_netlist_scores_invalid_not_raise():
    """A candidate netlist that elaborates cleanly but misses the spec (a
    passive resistive divider has no gain at all, well under the 6 dB
    floor) must be a normal scored failure -- `valid: false`, `error: None`
    -- never raised, distinguishing "the harness correctly scored a bad
    design" from "the harness broke"."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    response = (
        "```spice:cs_amp\n"
        "Vdd vdd 0 DC 1.8\n"
        "Vin in 0 DC 0.9 AC 1\n"
        "Rtop vdd out 1k\n"
        "Rbot out 0 1k\n"
        "Rin in out 1meg\n"
        "```"
    )
    provider = dab.make_live_agent_provider(invoke_agent=lambda _prompt: response)
    result = dab.run_attempt(task, 0, provider, REPO_ROOT)
    assert result["valid"] is False
    assert result["error"] is None


@_SKIP_NO_NGSPICE
def test_cli_run_with_live_agent_provider_selects_it_end_to_end(
    tmp_path, monkeypatch, capsys
):
    """`run --provider live-agent` selects the live-agent provider through
    the real CLI (`main` -> `_resolve_provider` -> `run_benchmark`), with
    only the agent CLI itself stubbed (`AGENT_CLI_ENV`, never a real network
    call) -- proves the provider-selection flag from issue #1732's
    acceptance criteria actually reaches `run_benchmark`.

    ``--repo-root`` stays the real repo root (unlike the broken-reference
    test above) so the live-agent provider can find the real
    ``.claude/skills/`` chain; only ``--tasks-dir`` is scoped down, to one
    task, so the run stays fast -- the task's own `reference.*` fields are
    left untouched and still resolve against the real repo's reference
    solution directory."""
    scratch_tasks = tmp_path / "tasks"
    scratch_tasks.mkdir()
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    (scratch_tasks / "common-source-amp.json").write_text(json.dumps(task))

    reference_body = (REPO_ROOT / task["reference"]["netlists"][0]).read_text()
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"NETLIST = {reference_body!r}\n"
        'print(json.dumps({"result": "```spice:cs_amp\\n" + NETLIST + "\\n```"}))\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(script))

    exit_code = dab.main(
        [
            "run",
            "--tasks-dir",
            str(scratch_tasks),
            "--repo-root",
            str(REPO_ROOT),
            "--provider",
            "live-agent",
            "--attempts",
            "1",
            "--k",
            "1",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "live-agent"
    assert payload["overall"]["pass_at_k"]["1"] == 1.0


# --------------------------------------------------------------------------
# Interactive (multi-turn, tool-using) agent provider (issue #1739)
# --------------------------------------------------------------------------
#
# These never invoke a real agent: every test either stubs the session
# boundary (`invoke_agent`) or points `AGENT_CLI_ENV` at the fake
# stream-json CLI below, so the sandbox seeding, tool-call/wall-clock
# bounding, transcript capture, and netlist-collection plumbing are all
# exercised without network credentials.

_FAKE_INTERACTIVE_CLI = r'''#!/usr/bin/env python3
"""Fake `claude` CLI speaking `--output-format stream-json`.

Behavior is picked by the KLT_FAKE_AGENT_MODE env var so one stub covers
every interactive-session test.
"""
import json
import os
import pathlib
import shutil
import sys
import time

argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
mode = os.environ.get("KLT_FAKE_AGENT_MODE", "ok")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def tool_use(name, cmd):
    emit(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t",
                        "name": name,
                        "input": {"command": cmd},
                    }
                ]
            },
        }
    )


emit({"type": "system", "subtype": "init", "cwd": os.getcwd()})

if mode == "fail":
    sys.stderr.write("stub interactive failure\n")
    sys.exit(1)

if mode == "sleep":
    time.sleep(30)
    sys.exit(0)

if mode == "api-error":
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 1,
            "result": "Not logged in \u00b7 Please run /login",
        }
    )
    sys.exit(0)

if mode == "many-tools":
    for i in range(40):
        tool_use("Bash", "klt sim sim_request.json --format json")
    time.sleep(30)
    sys.exit(0)

if mode == "probe":
    pathlib.Path("probe.json").write_text(
        json.dumps(
            {
                "cwd": os.getcwd(),
                "klt": shutil.which("klt"),
                "argv": argv,
                "prompt": prompt,
            }
        )
    )

if mode == "write":
    for stem, body in json.loads(os.environ["KLT_FAKE_AGENT_NETLISTS"]).items():
        pathlib.Path(f"{stem}.spice").write_text(body)

if mode == "tamper":
    request = pathlib.Path(os.environ["KLT_FAKE_AGENT_TAMPER_REQUEST"])
    payload = json.loads(request.read_text())
    payload["measurements"] = []
    payload["corners"] = {"process": ["tt"]}
    request.write_text(json.dumps(payload))
    for stem, body in json.loads(os.environ["KLT_FAKE_AGENT_NETLISTS"]).items():
        pathlib.Path(f"{stem}.spice").write_text(body)

tool_use("Bash", "klt kb search --where av_db>=6 --format json")
tool_use("Bash", "klt sim sim_request.json --format json")
emit(
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "sized it"}]},
    }
)
emit(
    {
        "type": "result",
        "subtype": "success",
        "num_turns": 3,
        "result": os.environ.get("KLT_FAKE_AGENT_RESULT", "done"),
    }
)
'''


def _fake_interactive_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-claude-interactive"
    script.write_text(_FAKE_INTERACTIVE_CLI)
    script.chmod(0o755)
    return script


def _sandbox(task: dict, tmp_path: Path) -> dict:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    return dab._seed_agent_sandbox(task, REPO_ROOT, sandbox)


# --- sandbox seeding ------------------------------------------------------


def test_seed_agent_sandbox_writes_skills_and_testbench_for_pdk_backed_task(
    tmp_path,
):
    """common-source-amp's `models` is PDK-backed (issue #1736:
    `{"pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice"}`) --
    there is no repo-local model file to copy into the sandbox, so
    `_seed_agent_sandbox` must leave `models` untouched (never invent a
    local `models.lib` copy that would resolve to nothing) rather than
    rewriting it the way it does for a repo-local library (see the sibling
    test below, against a still-local-library oscillator-family task)."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    sandbox = layout["workdir"]

    assert layout["stems"] == ["cs_amp"]
    assert (sandbox / "skills" / "design-topology-selection.md").is_file()
    assert (sandbox / "skills" / "design-sizing.md").is_file()
    assert (sandbox / "skills" / "design-netlist-authoring.md").is_file()
    assert not (sandbox / "models.lib").exists()
    assert layout["model_libs"] == []
    assert (sandbox / "TASK.md").is_file()

    request = json.loads((sandbox / "sim_request.json").read_text())
    # The netlist the agent is asked to author, as a sandbox-relative path.
    assert request["netlist"] == "cs_amp.spice"
    assert request["models"] == {
        "pdk": "sky130A",
        "lib": "libs.tech/ngspice/sky130.lib.spice",
    }
    # The measurement contract is carried over verbatim from the reference.
    reference_request = json.loads(
        (
            REPO_ROOT
            / "benchmarks/design-agent/reference/common-source-amp/sim_request.json"
        ).read_text()
    )
    assert request["measurements"] == reference_request["measurements"]
    assert request["corners"] == reference_request["corners"]


def test_seed_agent_sandbox_copies_a_repo_local_model_library(tmp_path):
    """The oscillator/VCO/PLL family's `models` is still a repo-local file
    (issue #1736 left it untouched -- those reference netlists are
    behavioral-only, no device model to swap), so `_seed_agent_sandbox`
    must still copy it into the sandbox and rewrite `models.lib` to the
    sandbox-relative copy -- the pre-#1736 behavior every task exercised,
    now only exercised by this family."""
    task = dab.load_task(TASKS_DIR / "rc-relaxation-oscillator.json")
    layout = _sandbox(task, tmp_path)
    sandbox = layout["workdir"]

    assert (sandbox / "models.lib").is_file()
    assert layout["model_libs"] == ["models.lib"]

    request = json.loads((sandbox / "sim_request.json").read_text())
    assert request["netlist"] == "oscillator.spice"
    assert request["models"]["lib"] == "models.lib"


def test_seed_agent_sandbox_never_seeds_the_reference_netlist(tmp_path):
    """The sandbox is a working area, not an answer key: the reference
    solution's own netlist body must not be reachable from inside it."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    reference_netlist = (REPO_ROOT / task["reference"]["netlists"][0]).read_text()
    seeded = "\n".join(
        path.read_text()
        for path in layout["workdir"].rglob("*")
        if path.is_file() and path.suffix in {".spice", ".json", ".md", ".lib"}
    )
    for line in reference_netlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("*"):
            assert stripped not in seeded, f"leaked reference line: {stripped!r}"


def test_seed_agent_sandbox_multi_testbench_task_writes_every_request(tmp_path):
    task = dab.load_task(TASKS_DIR / "differential-pair.json")
    layout = _sandbox(task, tmp_path)
    sandbox = layout["workdir"]
    assert sorted(layout["stems"]) == ["diff_pair_cm", "diff_pair_diff"]
    assert {"sim_request_diff.json", "sim_request_cm.json"} <= {
        path.name for path in sandbox.glob("*.json")
    }
    diff = json.loads((sandbox / "sim_request_diff.json").read_text())
    assert diff["netlist"] == "diff_pair_diff.spice"


def test_sandbox_klt_shim_runs_kb_against_the_repo_corpus(tmp_path):
    """The whole point of the sandbox is live `klt kb`/`klt sim` access:
    `klt kb` resolves its corpus relative to the *package* it is imported
    from, so an installed-elsewhere `klt` on PATH cannot see this repo's
    `kb/`. The seeded `bin/klt` shim must, from a working directory that is
    nowhere near the checkout."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    shim = layout["workdir"] / "bin" / "klt"
    assert os.access(shim, os.X_OK)
    proc = subprocess.run(
        [str(shim), "kb", "list", "--format", "json"],
        cwd=str(layout["workdir"]),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["count"] > 0


# --- prompt ---------------------------------------------------------------


def test_build_interactive_agent_prompt_advertises_live_tools_and_deliverable(tmp_path):
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    prompt = dab._build_interactive_agent_prompt(task, layout)
    assert "klt kb search" in prompt
    assert "klt sim" in prompt
    assert "--op-lint" in prompt
    assert "skills/design-sizing.md" in prompt
    assert "cs_amp.spice" in prompt
    assert "sim_request.json" in prompt
    assert task["description"] in prompt
    # Bounds are stated to the agent, not just enforced behind its back.
    assert str(int(layout["timeout_s"])) in prompt
    assert str(layout["tool_call_budget"]) in prompt


def test_build_interactive_agent_prompt_never_leaks_reference_netlist_body(tmp_path):
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    prompt = dab._build_interactive_agent_prompt(task, layout)
    reference_netlist = (REPO_ROOT / task["reference"]["netlists"][0]).read_text()
    for line in reference_netlist.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("*"):
            assert stripped not in prompt, f"leaked reference line: {stripped!r}"


# --- netlist collection ---------------------------------------------------


def test_interactive_provider_scores_the_netlist_written_into_the_sandbox(tmp_path):
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    body = "* written by the session\nVdd vdd 0 DC 1.8\n"

    def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
        (request.workdir / "cs_amp.spice").write_text(body)
        return dab.AgentSessionResult(text="no fences here, I wrote the file")

    provider = dab.make_interactive_agent_provider(
        invoke_agent=_session, sandbox_root=tmp_path
    )
    descriptor_arg, candidate_arg = provider(task, 0, REPO_ROOT)
    assert candidate_arg is None
    descriptor = json.loads(descriptor_arg)
    request = json.loads(Path(descriptor["gates"][0]["args"]["request"]).read_text())
    assert Path(request["netlist"]).read_text() == body


def test_interactive_provider_falls_back_to_a_labeled_fence(tmp_path):
    """A session that never manages to write a file, but names its netlist
    in a labeled fence, is still scoreable -- same fence contract the
    single-turn provider uses."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
        del request
        return dab.AgentSessionResult(text="```spice:cs_amp\nVdd vdd 0 DC 1.8\n```")

    provider = dab.make_interactive_agent_provider(
        invoke_agent=_session, sandbox_root=tmp_path
    )
    descriptor = json.loads(provider(task, 0, REPO_ROOT)[0])
    request = json.loads(Path(descriptor["gates"][0]["args"]["request"]).read_text())
    assert "Vdd vdd 0 DC 1.8" in Path(request["netlist"]).read_text()


def test_interactive_provider_raises_when_session_produced_no_netlist(tmp_path):
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
        del request
        return dab.AgentSessionResult(text="I thought about it and gave up")

    provider = dab.make_interactive_agent_provider(
        invoke_agent=_session, sandbox_root=tmp_path
    )
    with pytest.raises(dab.AgentInvocationError, match="cs_amp"):
        provider(task, 0, REPO_ROOT)


def test_interactive_provider_records_session_failure_as_failed_attempt(tmp_path):
    """A blown wall-clock/tool budget inside the session must be a scored
    attempt failure, never a crashed sweep."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
        del request
        raise dab.AgentInvocationError(
            "interactive agent session timed out after 1800s"
        )

    provider = dab.make_interactive_agent_provider(
        invoke_agent=_session, sandbox_root=tmp_path
    )
    result = dab.run_attempt(task, 0, provider, REPO_ROOT)
    assert result["valid"] is False
    assert "timed out" in result["error"]


# --- per-attempt isolation ------------------------------------------------


def test_interactive_provider_isolates_concurrent_attempts(tmp_path):
    """Two attempts running at the same time must not see each other's
    files -- the per-attempt sandbox is what makes a multi-turn,
    file-writing session safe to run more than once per task."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    started = threading.Barrier(2, timeout=30)
    workdirs: dict[int, Path] = {}

    def _make_session(index: int):
        def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
            workdirs[index] = request.workdir
            (request.workdir / "cs_amp.spice").write_text(f"* attempt {index}\n")
            (request.workdir / f"scratch-{index}.txt").write_text("mine")
            started.wait()  # both sessions are live simultaneously here
            peer = 1 - index
            assert not (request.workdir / f"scratch-{peer}.txt").exists()
            return dab.AgentSessionResult(text="")

        return _session

    results: dict[int, str] = {}

    def _run(index: int) -> None:
        provider = dab.make_interactive_agent_provider(
            invoke_agent=_make_session(index), sandbox_root=tmp_path
        )
        results[index] = provider(task, index, REPO_ROOT)[0]

    threads = [threading.Thread(target=_run, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert set(results) == {0, 1}
    assert workdirs[0] != workdirs[1]
    for index in (0, 1):
        descriptor = json.loads(results[index])
        request = json.loads(
            Path(descriptor["gates"][0]["args"]["request"]).read_text()
        )
        assert Path(request["netlist"]).read_text() == f"* attempt {index}\n"


def test_interactive_provider_scoring_ignores_sandbox_testbench_edits(tmp_path):
    """The sandbox copy of the `klt sim` request is the agent's *feedback*
    loop, not the grading contract: a session that weakens its local copy
    (drops the measurements, shrinks the corner matrix) must still be scored
    against the benchmark's own frozen reference request."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")

    def _session(request: dab.AgentSessionRequest) -> dab.AgentSessionResult:
        local = request.workdir / "sim_request.json"
        payload = json.loads(local.read_text())
        payload["measurements"] = []
        payload["corners"] = {"process": ["tt"]}
        local.write_text(json.dumps(payload))
        (request.workdir / "cs_amp.spice").write_text("Vdd vdd 0 DC 1.8\n")
        return dab.AgentSessionResult(text="")

    provider = dab.make_interactive_agent_provider(
        invoke_agent=_session, sandbox_root=tmp_path
    )
    descriptor = json.loads(provider(task, 0, REPO_ROOT)[0])
    scored_request = json.loads(
        Path(descriptor["gates"][0]["args"]["request"]).read_text()
    )
    reference_request = json.loads(
        (
            REPO_ROOT
            / "benchmarks/design-agent/reference/common-source-amp/sim_request.json"
        ).read_text()
    )
    assert scored_request["measurements"] == reference_request["measurements"]
    assert scored_request["corners"] == reference_request["corners"]


# --- the real streaming session boundary ----------------------------------


def _session_request(tmp_path: Path, **overrides) -> dab.AgentSessionRequest:
    workdir = overrides.pop("workdir", None) or (tmp_path / "work")
    workdir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "prompt": "design it",
        "workdir": workdir,
        "timeout_s": 30.0,
        "tool_call_budget": 20,
    }
    kwargs.update(overrides)
    return dab.AgentSessionRequest(**kwargs)


def test_default_interactive_session_counts_tool_calls_and_writes_transcript(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_RESULT", "final answer")
    request = _session_request(tmp_path)
    result = dab._default_invoke_interactive_agent(request)
    assert result.text == "final answer"
    assert result.tool_calls == 2
    assert result.turns >= 1
    transcript = (request.workdir / dab.TRANSCRIPT_FILENAME).read_text().splitlines()
    assert len(transcript) >= 4
    assert json.loads(transcript[0])["type"] == "system"


def test_default_interactive_session_enforces_tool_call_budget(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "many-tools")
    request = _session_request(tmp_path, tool_call_budget=3, timeout_s=60.0)
    start = time.monotonic()
    with pytest.raises(dab.AgentInvocationError, match="tool-call budget"):
        dab._default_invoke_interactive_agent(request)
    # Killed on the budget, not by waiting out the stub's 30 s sleep.
    assert time.monotonic() - start < 25


def test_default_interactive_session_enforces_wall_clock(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "sleep")
    request = _session_request(tmp_path, timeout_s=1.0)
    with pytest.raises(dab.AgentInvocationError, match="timed out"):
        dab._default_invoke_interactive_agent(request)


def test_default_interactive_session_raises_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "fail")
    with pytest.raises(dab.AgentInvocationError, match="stub interactive failure"):
        dab._default_invoke_interactive_agent(_session_request(tmp_path))


def test_default_interactive_session_raises_on_is_error_result(tmp_path, monkeypatch):
    """The real `claude` CLI reports an unusable session (no credentials, an
    API error) as a `{"type": "result", "is_error": true}` event and still
    exits 0 -- observed directly against `claude` 2.1.221. Without this the
    attempt would be misreported as "the agent produced no netlist" rather
    than "the agent never ran"."""
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "api-error")
    with pytest.raises(dab.AgentInvocationError, match="Not logged in"):
        dab._default_invoke_interactive_agent(_session_request(tmp_path))


def test_default_interactive_session_raises_on_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setenv(dab.AGENT_CLI_ENV, "/no/such/binary-does-not-exist")
    with pytest.raises(dab.AgentInvocationError, match="not found"):
        dab._default_invoke_interactive_agent(_session_request(tmp_path))


def test_default_interactive_session_runs_in_the_sandbox_with_its_klt_on_path(
    tmp_path, monkeypatch
):
    """The session's own cwd is the per-attempt sandbox, and the sandbox's
    `bin/klt` shim shadows any other `klt` on PATH -- otherwise `klt kb`
    inside the session would resolve against whatever install happens to be
    first on the runner's PATH, not this checkout."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "probe")
    dab._default_invoke_interactive_agent(
        _session_request(tmp_path, workdir=layout["workdir"])
    )
    probe = json.loads((layout["workdir"] / "probe.json").read_text())
    assert Path(probe["cwd"]).resolve() == layout["workdir"].resolve()
    assert Path(probe["klt"]).resolve() == (layout["workdir"] / "bin" / "klt").resolve()
    assert "--output-format" in probe["argv"]
    assert "stream-json" in probe["argv"]
    # Tool access is declared explicitly rather than left wide open.
    joined = " ".join(probe["argv"])
    assert "Bash(klt kb:*)" in joined
    assert "Bash(klt sim:*)" in joined


# --- CLI wiring -----------------------------------------------------------


def test_resolve_agent_timeout_defaults_per_provider():
    assert dab._resolve_agent_timeout("live-agent", None) == dab.DEFAULT_AGENT_TIMEOUT_S
    assert (
        dab._resolve_agent_timeout("interactive-agent", None)
        == dab.DEFAULT_INTERACTIVE_TIMEOUT_S
    )
    assert dab._resolve_agent_timeout("interactive-agent", 42.0) == 42.0


def test_cli_run_with_interactive_agent_provider_is_selectable(tmp_path, monkeypatch):
    """`run --provider interactive-agent` reaches `run_benchmark` with the
    interactive provider, and the existing `live-agent` choice is untouched
    (its own end-to-end test above still passes)."""
    captured: dict = {}

    def _fake_run_benchmark(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "provider": kwargs["provider_name"],
            "tasks": [],
            "tiers": {},
            "overall": {"task_count": 0, "solved_count": 0, "pass_at_k": {}},
        }

    monkeypatch.setattr(dab, "run_benchmark", _fake_run_benchmark)
    exit_code = dab.main(
        [
            "run",
            "--provider",
            "interactive-agent",
            "--agent-tool-budget",
            "7",
            "--agent-sandbox-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    assert captured["provider_name"] == "interactive-agent"
    assert callable(captured["provider"])


@_SKIP_NO_NGSPICE
def test_interactive_provider_end_to_end_with_stubbed_cli(tmp_path, monkeypatch):
    """Full path with only the agent binary stubbed: sandbox seeding ->
    streamed session (which writes a netlist into its own sandbox) ->
    collection -> synthesized descriptor -> real `klt eval`/`ngspice`."""
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    reference_body = (REPO_ROOT / task["reference"]["netlists"][0]).read_text()
    monkeypatch.setenv(dab.AGENT_CLI_ENV, str(_fake_interactive_cli(tmp_path)))
    monkeypatch.setenv("KLT_FAKE_AGENT_MODE", "write")
    monkeypatch.setenv(
        "KLT_FAKE_AGENT_NETLISTS", json.dumps({"cs_amp": reference_body})
    )
    provider = dab.make_interactive_agent_provider(
        sandbox_root=tmp_path / "sandboxes", agent_timeout_s=300.0
    )
    result = dab.run_attempt(task, 0, provider, REPO_ROOT)
    assert result["valid"] is True, result
