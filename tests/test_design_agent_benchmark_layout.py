"""Tests for the layout-flow benchmark protocol (issue #3024, epic #2252):
`scripts/design_agent_benchmark_layout.py` and the `validate-layout`
subcommand of `scripts/design_agent_benchmark.py`.

Everything here is deterministic and credential-free: no `ngspice`, no agent,
no live provider. Negative cases are built by mutating the shipped protocol and
the synthetic fixtures under `benchmarks/design-agent/fixtures/layout/`.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import design_agent_benchmark as dab  # noqa: E402
import design_agent_benchmark_layout as layout  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = REPO_ROOT / "benchmarks" / "design-agent" / "fixtures" / "layout"


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


@pytest.fixture
def protocol() -> dict[str, Any]:
    return layout.load_protocol()


@pytest.fixture
def task() -> dict[str, Any]:
    return _load(FIXTURES / "task-five-transistor-ota-layout.json")


@pytest.fixture
def runs() -> list[dict[str, Any]]:
    return _load(FIXTURES / "runs-valid.json")


def _run(runs: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    return next(r for r in runs if r["arm"] == arm)


def _reseal(
    run: dict[str, Any], protocol: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    """Recompute a mutated run's seed identity so a test isolates one defect."""
    link = run["seed_from"]
    run["seed_sha256"] = layout.run_seed_sha256(
        protocol,
        task_id=run["task_id"],
        task_sha256=run["task_sha256"],
        arm=run["arm"],
        agent_id=run["agent"]["id"],
        replicate=run["replicate"],
        seed_artifact_sha256=link["gds_sha256"] if link else None,
    )
    return run


def _errors(result: dict[str, Any]) -> str:
    assert not result["valid"], "expected the document to be rejected"
    return "\n".join(result["errors"])


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


def test_shipped_protocol_is_valid(protocol: dict[str, Any]) -> None:
    result = layout.validate_protocol(protocol)
    assert result["valid"], result["errors"]
    assert len(result["protocol_sha256"]) == 64


def test_protocol_defines_the_three_arms_and_their_tool_surfaces(
    protocol: dict[str, Any],
) -> None:
    arms = {arm["id"]: arm for arm in protocol["arms"]}
    assert set(arms) == {"draw-only", "gen-only", "gen+refine"}

    def classes(arm_id: str) -> set[str]:
        return {c for p in arms[arm_id]["phases"] for c in p["allowed_classes"]}

    assert "generate" not in classes("draw-only")
    assert "draw" not in classes("gen-only")
    assert {"generate", "draw"} <= classes("gen+refine")
    assert arms["gen+refine"]["seed"]["source_arm"] == "gen-only"
    assert arms["gen+refine"]["seed"]["policy"] == "same_run_best_valid"


def test_protocol_records_seeding_and_refinement_decisions(
    protocol: dict[str, Any],
) -> None:
    # Issue #3024: prefer the same run's best valid gen-only result.
    assert protocol["seeding_decision"]["decision"] == "same_run_best_valid_gen_only"
    sufficiency = protocol["refinement_sufficiency"]
    assert sufficiency["verdict"] == "sufficient"
    assert sufficiency["gap"] is None and sufficiency["follow_up"] is None


def test_protocol_rejects_unknown_field(protocol: dict[str, Any]) -> None:
    protocol["surprise"] = True
    assert "surprise" in _errors(layout.validate_protocol(protocol))


def test_protocol_rejects_unknown_arm_tool_class(protocol: dict[str, Any]) -> None:
    arm = next(a for a in protocol["arms"] if a["id"] == "gen-only")
    arm["phases"][0]["allowed_classes"].append("sorcery")
    assert "sorcery" in _errors(layout.validate_protocol(protocol))


@pytest.mark.parametrize(
    ("arm_id", "bad_class", "message"),
    [
        ("draw-only", "generate", "must not allow the 'generate' class"),
        ("gen-only", "draw", "must not allow the 'draw' class"),
    ],
)
def test_protocol_rejects_cross_arm_tool_leak(
    protocol: dict[str, Any], arm_id: str, bad_class: str, message: str
) -> None:
    arm = next(a for a in protocol["arms"] if a["id"] == arm_id)
    arm["phases"][0]["allowed_classes"].append(bad_class)
    assert message in _errors(layout.validate_protocol(protocol))


def test_protocol_rejects_verb_in_two_classes(protocol: dict[str, Any]) -> None:
    protocol["tool_classes"]["draw"].append("gen")
    assert "classes must be disjoint" in _errors(layout.validate_protocol(protocol))


def test_protocol_rejects_seeding_unseeded_arm(protocol: dict[str, Any]) -> None:
    arm = next(a for a in protocol["arms"] if a["id"] == "gen-only")
    arm["seed"] = copy.deepcopy(
        next(a for a in protocol["arms"] if a["id"] == "gen+refine")["seed"]
    )
    assert "unseeded arm must have seed: null" in _errors(
        layout.validate_protocol(protocol)
    )


def test_protocol_rejects_seed_from_wrong_arm(protocol: dict[str, Any]) -> None:
    arm = next(a for a in protocol["arms"] if a["id"] == "gen+refine")
    arm["seed"]["source_arm"] = "draw-only"
    assert "source_arm" in _errors(layout.validate_protocol(protocol))


def test_protocol_rejects_scoring_a_failed_status(protocol: dict[str, Any]) -> None:
    status = next(
        s for s in protocol["failure_semantics"]["statuses"] if s["id"] == "invalid_drc"
    )
    status["scored"] = True
    assert "only 'valid' may be scored" in _errors(layout.validate_protocol(protocol))


def test_protocol_rejects_missing_validity_check(protocol: dict[str, Any]) -> None:
    protocol["validity"]["required_checks"] = ["drc", "lvs"]
    assert "required_checks" in _errors(layout.validate_protocol(protocol))


def test_protocol_budget_must_match_the_interactive_provider(
    protocol: dict[str, Any],
) -> None:
    protocol["budget"]["per_run"]["tool_call_budget"] += 1
    assert "DEFAULT_TOOL_CALL_BUDGET" in _errors(layout.validate_protocol(protocol))


def test_protocol_deterministic_steps_need_no_credentials(
    protocol: dict[str, Any],
) -> None:
    budget = protocol["budget"]
    assert budget["credentialed_steps"] == ["live agent runs"]
    assert "validate-layout" in budget["credential_free_steps"]
    assert budget["ci"]["live_default"] == "off"


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------


def test_fixture_task_is_valid(task: dict[str, Any]) -> None:
    assert layout.validate_task(task)["valid"]


def test_layout_task_extends_the_existing_block_spec(task: dict[str, Any]) -> None:
    del task["block_spec"]["io_nodes"]  # required by task.schema.json's block_spec
    assert "io_nodes" in _errors(layout.validate_task(task))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.update(surprise=1),
        lambda t: t.update(negative_controls=[]),
        lambda t: t.update(pdk="proprietary"),
        lambda t: t["frozen_netlist"].update(sha256="nothex"),
        lambda t: t["negative_controls"][0].update(expected_failure="sim"),
        lambda t: t.pop("generator_recipe"),
        lambda t: t.pop("reference_layout"),
    ],
)
def test_layout_task_rejects_malformed(task: dict[str, Any], mutate: Any) -> None:
    mutate(task)
    assert not layout.validate_task(task)["valid"]


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../outside/netlist.spice"])
def test_layout_task_rejects_unsafe_paths(task: dict[str, Any], bad_path: str) -> None:
    task["frozen_netlist"]["path"] = bad_path
    assert not layout.validate_task(task)["valid"]


def test_layout_task_rejects_id_not_matching_filename(task: dict[str, Any]) -> None:
    assert "does not match filename stem" in _errors(
        layout.validate_task(task, filename_stem="other-name")
    )


def test_layout_task_checks_frozen_files_when_given_a_root(
    task: dict[str, Any], tmp_path: Path
) -> None:
    assert "does not exist" in _errors(layout.validate_task(task, repo_root=tmp_path))
    for label_path in (
        task["frozen_netlist"]["path"],
        task["reference_layout"]["path"],
        task["generator_recipe"]["path"],
        *(c["path"] for c in task["negative_controls"]),
    ):
        target = tmp_path / label_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not what was hashed")
    assert "does not match the recorded value" in _errors(
        layout.validate_task(task, repo_root=tmp_path)
    )


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def test_fixture_runs_are_valid(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    for run in runs:
        result = layout.validate_run(run, protocol, task)
        assert result["valid"], (run["run_id"], result["errors"])
    assert layout.validate_run_set(runs)["valid"]


@pytest.mark.parametrize(
    ("arm", "phase", "verb", "klass"),
    [
        ("gen-only", "generate", "draw", "draw"),
        ("gen-only", "generate", "clip", "draw"),
        ("draw-only", "draw", "gen", "generate"),
        ("draw-only", "draw", "gen-compose", "generate"),
        ("gen-only", "refine", "drc", "verify"),  # a phase gen-only does not have
        ("draw-only", "generate", "drc", "verify"),
    ],
)
def test_run_rejects_cross_arm_contamination(
    protocol: dict[str, Any],
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    arm: str,
    phase: str,
    verb: str,
    klass: str,
) -> None:
    run = _run(runs, arm)
    run["tool_calls"].append(
        {
            "seq": len(run["tool_calls"]) + 1,
            "phase": phase,
            "verb": verb,
            "class": klass,
            "exit_code": 0,
        }
    )
    assert "cross-arm contamination" in _errors(
        layout.validate_run(run, protocol, task)
    )


def test_run_rejects_misclassified_verb(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "gen-only")
    run["tool_calls"][0]["class"] = "inspect"  # `gen` is a generate-class verb
    assert "recorded as class" in _errors(layout.validate_run(run, protocol, task))


def test_run_rejects_unknown_verb_and_out_of_order_seq(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "draw-only")
    run["tool_calls"][1]["verb"] = "python3"
    run["tool_calls"][2]["seq"] = 1
    message = _errors(layout.validate_run(run, protocol, task))
    assert "not in any tool class" in message
    assert "strictly increasing" in message


def test_run_rejects_unstable_seed_identity(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "gen-only")
    run["replicate"] = 2  # same seed hash, different identity
    assert "seed identity" in _errors(layout.validate_run(run, protocol, task))
    _reseal(run, protocol, task)
    assert layout.validate_run(run, protocol, task)["valid"]


def test_seed_identity_is_deterministic_and_covers_the_seed_artifact(
    protocol: dict[str, Any], task: dict[str, Any]
) -> None:
    kwargs: dict[str, Any] = {
        "task_id": task["id"],
        "task_sha256": layout.document_sha256(task),
        "arm": "gen+refine",
        "agent_id": "a",
        "replicate": 1,
    }
    a = layout.run_seed_sha256(protocol, seed_artifact_sha256="a" * 64, **kwargs)
    assert a == layout.run_seed_sha256(
        protocol, seed_artifact_sha256="a" * 64, **kwargs
    )
    assert a != layout.run_seed_sha256(
        protocol, seed_artifact_sha256="b" * 64, **kwargs
    )
    assert a != layout.run_seed_sha256(protocol, **kwargs)


def test_run_rejects_stale_protocol_or_task_fingerprint(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "draw-only")
    protocol["protocol_version"] = "1.0.1"
    assert "protocol_sha256" in _errors(layout.validate_run(run, protocol, task))
    protocol = layout.load_protocol()
    task["title"] = "changed"
    assert "task_sha256" in _errors(layout.validate_run(run, protocol, task))


def test_run_with_failed_status_carries_no_score(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "draw-only")
    run["status"] = "timeout"  # still has the valid run's score
    assert not layout.validate_run(run, protocol, task)["valid"]
    run["score"] = None
    run["artifact"] = None
    run["validity"] = None
    assert layout.validate_run(run, protocol, task)["valid"]


def test_valid_run_requires_a_score(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "draw-only")
    run["score"] = None
    assert not layout.validate_run(run, protocol, task)["valid"]
    run["score"] = {"area_um2": 0, "utilization": None, "sim_margin": None}
    assert not layout.validate_run(run, protocol, task)["valid"]


@pytest.mark.parametrize("check", ["drc", "lvs"])
def test_valid_status_requires_passing_checks(
    protocol: dict[str, Any],
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    check: str,
) -> None:
    run = _run(runs, "gen-only")
    run["validity"][check]["status"] = "violations" if check == "drc" else "mismatch"
    assert f"{check} status" in _errors(layout.validate_run(run, protocol, task))


def test_valid_status_requires_checks_bound_to_the_scored_gds(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "gen-only")
    run["validity"]["drc"]["gds_sha256"] = "f" * 64
    assert "different GDS" in _errors(layout.validate_run(run, protocol, task))


def test_valid_status_requires_the_frozen_netlist(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "gen-only")
    run["validity"]["lvs"]["netlist_sha256"] = "e" * 64
    assert "frozen one" in _errors(layout.validate_run(run, protocol, task))


def test_failed_check_statuses_need_a_matching_failure(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "gen-only")
    run["status"] = "invalid_drc"
    run["score"] = None
    assert "no failing drc" in _errors(layout.validate_run(run, protocol, task))
    run["validity"]["drc"]["status"] = "violations"
    assert layout.validate_run(run, protocol, task)["valid"]


def test_unseeded_arm_rejects_seed_and_seeded_arm_requires_it(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    gen = _run(runs, "gen-only")
    gen["seed_from"] = {"run_id": "x", "gds_sha256": "a" * 64}
    _reseal(gen, protocol, task)
    assert "seed_from must be null" in _errors(layout.validate_run(gen, protocol, task))

    refine = _run(runs, "gen+refine")
    refine["seed_from"] = None
    _reseal(refine, protocol, task)
    assert "seed_from is required" in _errors(
        layout.validate_run(refine, protocol, task)
    )


def test_refine_without_a_valid_seed_is_recorded_not_scored(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    refine = _run(runs, "gen+refine")
    refine.update(
        status="not_run_no_seed",
        seed_from=None,
        tool_calls=[],
        artifact=None,
        validity=None,
        score=None,
    )
    _reseal(refine, protocol, task)
    assert layout.validate_run(refine, protocol, task)["valid"]


def test_refused_tool_call_forces_invalid_tool_surface(
    protocol: dict[str, Any], task: dict[str, Any], runs: list[dict[str, Any]]
) -> None:
    run = _run(runs, "draw-only")
    run["tool_calls"][0]["exit_code"] = 126
    assert "invalid_tool_surface" in _errors(layout.validate_run(run, protocol, task))


# --------------------------------------------------------------------------
# Run set (seed linkage)
# --------------------------------------------------------------------------


def test_run_set_rejects_duplicate_ids(runs: list[dict[str, Any]]) -> None:
    assert "duplicate run_id" in _errors(layout.validate_run_set([*runs, runs[0]]))


def test_run_set_requires_the_seed_run(runs: list[dict[str, Any]]) -> None:
    only = [r for r in runs if r["arm"] != "gen-only"]
    assert "not in the run set" in _errors(layout.validate_run_set(only))


def test_run_set_requires_a_valid_gen_only_seed(runs: list[dict[str, Any]]) -> None:
    seed = _run(runs, "gen-only")
    seed["status"] = "invalid_drc"
    assert "not valid" in _errors(layout.validate_run_set(runs))
    seed["status"] = "valid"
    seed["arm"] = "draw-only"
    assert "not 'gen-only'" in _errors(layout.validate_run_set(runs))


@pytest.mark.parametrize(
    ("key", "value"), [("task_id", "other-task"), ("replicate", 2)]
)
def test_run_set_requires_same_task_and_replicate(
    runs: list[dict[str, Any]], key: str, value: Any
) -> None:
    _run(runs, "gen-only")[key] = value
    assert key in _errors(layout.validate_run_set(runs))


def test_run_set_requires_same_agent(runs: list[dict[str, Any]]) -> None:
    _run(runs, "gen-only")["agent"] = {
        "id": "other",
        "provider": "live-agent",
        "model": None,
    }
    assert "same agent" in _errors(layout.validate_run_set(runs))


def test_run_set_requires_seed_hash_to_match_seed_artifact(
    runs: list[dict[str, Any]],
) -> None:
    _run(runs, "gen+refine")["seed_from"]["gds_sha256"] = "d" * 64
    assert "differs from the seed run's artifact" in _errors(
        layout.validate_run_set(runs)
    )


def test_run_set_rejects_refine_worse_than_its_seed(runs: list[dict[str, Any]]) -> None:
    _run(runs, "gen+refine")["score"]["area_um2"] = (
        _run(runs, "gen-only")["score"]["area_um2"] + 1.0
    )
    assert "scores worse than its seed" in _errors(layout.validate_run_set(runs))


# --------------------------------------------------------------------------
# Tool-surface measurement (issue #3024: does `klt draw` suffice for refinement?)
# --------------------------------------------------------------------------


def _klt(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-m", "klayout_tools.cli", *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_klt_draw_can_edit_an_existing_stream(tmp_path: Path) -> None:
    """The measurement behind protocol `refinement_sufficiency.verdict`
    'sufficient': `klt draw` starts from an existing stream (`base_gds`) and
    erases a window of it (`erase_um`). If this stops passing, the recorded
    verdict is stale and the protocol version must be bumped."""
    layer = [66, 20]
    seed, out = tmp_path / "seed.gds", tmp_path / "out.gds"
    made = _klt(
        "draw",
        "--params",
        json.dumps({"shapes": [{"layer": layer, "rect_um": [0, 0, 1, 1]}]}),
        "-o",
        str(seed),
        "--format",
        "json",
    )
    assert made.returncode == 0, (made.stdout, made.stderr)
    edit = {
        "base_gds": str(seed),
        "erase_um": [{"rect_um": [-1, -1, 2, 2], "layer": layer}],
        "shapes": [{"layer": layer, "rect_um": [3, 0, 4, 1]}],
    }
    proc = _klt(
        "draw", "--params", json.dumps(edit), "-o", str(out), "--format", "json"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)["erased_shape_count"] >= 1
    assert out.exists()


# --------------------------------------------------------------------------
# Backward compatibility and CLI
# --------------------------------------------------------------------------


def test_legacy_task_validation_is_unaffected() -> None:
    result = dab.validate_tasks()
    assert result["valid"], result
    assert result["task_count"] > 0
    # Layout documents never sit in the legacy tasks directory.
    assert not (dab.DEFAULT_TASKS_DIR / "layout-protocol.json").exists()
    assert dab.main(["validate", "--schema-only"]) == 0


def test_validate_layout_cli_accepts_fixtures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = dab.main(
        [
            "validate-layout",
            "--task",
            str(FIXTURES / "task-five-transistor-ota-layout.json"),
            "--runs",
            str(FIXTURES / "runs-valid.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["protocol"]["valid"] and payload["run_set"]["valid"]
    assert len(payload["runs"]) == 3


def test_validate_layout_cli_rejects_a_contaminated_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    runs: list[dict[str, Any]],
) -> None:
    run = _run(runs, "gen-only")
    run["tool_calls"].append(
        {
            "seq": 99,
            "phase": "generate",
            "verb": "draw",
            "class": "draw",
            "exit_code": 0,
        }
    )
    bad = tmp_path / "runs.json"
    bad.write_text(json.dumps(runs))
    code = dab.main(
        [
            "validate-layout",
            "--task",
            str(FIXTURES / "task-five-transistor-ota-layout.json"),
            "--runs",
            str(bad),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert any(
        "cross-arm contamination" in e for r in payload["runs"] for e in r["errors"]
    )


# --------------------------------------------------------------------------
# Frozen task corpus and validity gate (issue #3025)
# --------------------------------------------------------------------------

CORPUS = layout.CORPUS_DIR
CORPUS_TASK_IDS = sorted(
    p.name for p in CORPUS.iterdir() if (p / "task.json").is_file()
)


def _pdk_available() -> bool:
    from klayout_tools import pdk

    try:
        pdk.find_pdk("sky130A")
    except pdk.PdkNotFoundError:
        return False
    return True


needs_pdk = pytest.mark.skipif(
    not _pdk_available(), reason="sky130A PDK not resolvable (klt gen needs it)"
)


def _corpus_task(task_id: str) -> dict[str, Any]:
    return _load(CORPUS / task_id / "task.json")


def test_corpus_has_four_to_six_tasks() -> None:
    assert layout.CORPUS_TASKS_MIN <= len(CORPUS_TASK_IDS) <= layout.CORPUS_TASKS_MAX


@pytest.mark.parametrize("task_id", CORPUS_TASK_IDS)
def test_corpus_task_descriptor_is_bound_and_complete(task_id: str) -> None:
    task = _corpus_task(task_id)
    result = layout.validate_task(
        task, filename_stem=task_id, repo_root=layout.REPO_ROOT
    )
    assert result["valid"], result["errors"]
    assert not layout._agent_input_errors(task)
    assert not layout._control_coverage_errors(task)
    # Eligibility is recorded for every protocol rule and every arm.
    protocol = layout.load_protocol()
    rule_ids = {r["id"] for r in protocol["task_eligibility"]["rules"]}
    assert set(task["eligibility"]["rules"]) == rule_ids
    assert set(task["eligibility"]["arms"]) == {a["id"] for a in protocol["arms"]}


@pytest.mark.parametrize("task_id", CORPUS_TASK_IDS)
def test_corpus_ships_no_scored_candidate_layout(task_id: str) -> None:
    task = _corpus_task(task_id)
    assert all(not p.endswith((".gds", ".oas")) for p in task["agent_inputs"])
    assert task["agent_inputs"] == [task["frozen_netlist"]["path"]]


def test_agent_inputs_reject_layout_and_scorer_only_files() -> None:
    task = _corpus_task(CORPUS_TASK_IDS[0])
    for leaked in (task["reference_layout"]["path"], task["generator_recipe"]["path"]):
        bad = copy.deepcopy(task)
        bad["agent_inputs"].append(leaked)
        assert any(leaked in e for e in layout._agent_input_errors(bad))
    bad = copy.deepcopy(task)
    bad["agent_inputs"] = ["somewhere/candidate.gds"]
    assert any("layout stream" in e for e in layout._agent_input_errors(bad))
    bad = copy.deepcopy(task)
    del bad["agent_inputs"]
    assert layout._agent_input_errors(bad)


def test_task_needs_a_control_for_each_failing_check() -> None:
    task = _corpus_task(CORPUS_TASK_IDS[0])
    task["negative_controls"] = [
        c for c in task["negative_controls"] if c["expected_failure"] == "drc"
    ]
    assert any("no lvs control" in e for e in layout._control_coverage_errors(task))


def test_corpus_exclusions_are_machine_readable() -> None:
    document = _load(CORPUS / "exclusions.json")
    protocol = layout.load_protocol()
    rule_ids = {r["id"] for r in protocol["task_eligibility"]["rules"]}
    assert document["exclusions"]
    assert {e["reason"] for e in document["exclusions"]} <= rule_ids
    assert not layout._schema_errors(
        layout._validator(layout.EXCLUSIONS_SCHEMA_NAME), document
    )


def test_exclusion_cannot_also_be_a_scored_task(tmp_path: Path) -> None:
    document = _load(CORPUS / "exclusions.json")
    document["exclusions"][0]["id"] = CORPUS_TASK_IDS[0]
    errors = layout._exclusion_errors(
        document, set(CORPUS_TASK_IDS), layout.REPO_ROOT, tmp_path, layout.SCHEMA_DIR
    )
    assert any("all arms or none" in e for e in errors)


def test_exclusion_rejects_unknown_reason(tmp_path: Path) -> None:
    document = _load(CORPUS / "exclusions.json")
    document["exclusions"][0]["reason"] = "too_hard"
    errors = layout._exclusion_errors(
        document, set(), layout.REPO_ROOT, tmp_path, layout.SCHEMA_DIR
    )
    assert errors


@needs_pdk
def test_generator_reachable_exclusion_is_still_unreachable(tmp_path: Path) -> None:
    document = _load(CORPUS / "exclusions.json")
    errors = layout._exclusion_errors(
        document, set(CORPUS_TASK_IDS), layout.REPO_ROOT, tmp_path, layout.SCHEMA_DIR
    )
    assert errors == []
    # Pointing the exclusion at a recipe that does reach a layout makes it stale.
    reachable = CORPUS / CORPUS_TASK_IDS[0] / "recipe.json"
    document["exclusions"][0]["recipe"] = {
        "path": str(reachable.relative_to(layout.REPO_ROOT)),
        "sha256": layout._sha256_path(reachable),
    }
    stale = layout._exclusion_errors(
        document, set(CORPUS_TASK_IDS), layout.REPO_ROOT, tmp_path, layout.SCHEMA_DIR
    )
    assert any("stale" in e for e in stale)


@needs_pdk
@pytest.mark.parametrize("task_id", CORPUS_TASK_IDS)
def test_every_reference_passes_its_frozen_gate(task_id: str, tmp_path: Path) -> None:
    result = layout.check_task_gate(_corpus_task(task_id), layout.REPO_ROOT, tmp_path)
    assert result["valid"], result["errors"]
    assert result["reference"]["failed"] == []
    assert result["recipe"]["failed"] == []


@needs_pdk
@pytest.mark.parametrize("task_id", CORPUS_TASK_IDS)
def test_negative_controls_are_killed_by_their_own_check(
    task_id: str, tmp_path: Path
) -> None:
    result = layout.check_task_gate(_corpus_task(task_id), layout.REPO_ROOT, tmp_path)
    by_check = {c["expected_failure"]: c for c in result["negative_controls"]}
    assert by_check["drc"]["failed"] == ["drc"]
    assert by_check["lvs"]["failed"] == ["lvs"]
    assert result["provenance_controls"] == {
        "swapped_gds_rejected": True,
        "wrong_netlist_rejected": True,
    }


@needs_pdk
def test_replacing_the_scored_gds_after_the_checks_is_rejected(tmp_path: Path) -> None:
    task = _corpus_task(CORPUS_TASK_IDS[0])
    netlist = layout.REPO_ROOT / task["frozen_netlist"]["path"]
    reference = layout.REPO_ROOT / task["reference_layout"]["path"]
    checks = layout.run_checks(
        reference, netlist, task["reference_layout"]["cell_name"], tmp_path
    )
    honest = layout.gate_verdict(
        task["reference_layout"]["sha256"], checks, task["frozen_netlist"]["sha256"]
    )
    assert honest["failed"] == []
    # The recorded reports vouch for the reference, not for a different stream.
    other = task["negative_controls"][0]["sha256"]
    swapped = layout.gate_verdict(other, checks, task["frozen_netlist"]["sha256"])
    assert swapped["failed"] == ["provenance"]
    assert len(swapped["provenance_errors"]) == 2
    # ...nor for a different netlist.
    wrong = layout.gate_verdict(task["reference_layout"]["sha256"], checks, "0" * 64)
    assert wrong["failed"] == ["provenance"]


@needs_pdk
def test_drc_dirty_layout_is_rejected_by_the_gate(tmp_path: Path) -> None:
    task = _corpus_task(CORPUS_TASK_IDS[0])
    control = next(
        c for c in task["negative_controls"] if c["expected_failure"] == "drc"
    )
    checks = layout.run_checks(
        layout.REPO_ROOT / control["path"],
        layout.REPO_ROOT / task["frozen_netlist"]["path"],
        task["reference_layout"]["cell_name"],
        tmp_path,
    )
    verdict = layout.gate_verdict(
        control["sha256"], checks, task["frozen_netlist"]["sha256"]
    )
    assert verdict["failed"] == ["drc"]


def test_top_cell_check_catches_a_renamed_wrapper(tmp_path: Path) -> None:
    import klayout.db as pya

    lay = pya.Layout()
    inner = lay.create_cell("blk")
    top = lay.create_cell("blk$1")
    top.insert(pya.CellInstArray(inner.cell_index(), pya.Trans()))
    path = tmp_path / "x.gds"
    lay.write(str(path))
    assert layout.top_cell_errors(path, "blk$1") == []
    assert layout.top_cell_errors(path, "blk")


@needs_pdk
def test_validate_layout_corpus_cli_emits_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = dab.main(["validate-layout", "--corpus"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["corpus"]["schema"] == layout.CORPUS_REPORT_SCHEMA
    assert payload["corpus"]["valid"] is True
    assert sorted(t["task_id"] for t in payload["corpus"]["tasks"]) == CORPUS_TASK_IDS
