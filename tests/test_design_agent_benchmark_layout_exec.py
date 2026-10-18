"""Tests for the layout-flow benchmark executor (issue #3026, epic #2252):
`scripts/design_agent_benchmark_layout_exec.py` and `run-layout`.

Credential-free and agent-free: every provider is a stub driving the policed
`klt` the same way a live session's `bin/klt` shim does. The one real
computation is the scorer (`klt drc` / `klt lvs` on the frozen `nfet-unit`
corpus task), because "every arm goes through the same gate" is the point.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import design_agent_benchmark as dab  # noqa: E402
import design_agent_benchmark_layout as layout  # noqa: E402
import design_agent_benchmark_layout_exec as ex  # noqa: E402
from design_agent_benchmark import AgentInvocationError  # noqa: E402
from design_agent_benchmark_interactive import AgentSessionResult  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
TASK_ID = "nfet-unit"
AGENT = ex.AgentSpec("stub", "reference", None)


@pytest.fixture(scope="module")
def protocol() -> dict[str, Any]:
    return layout.load_protocol()


@pytest.fixture(scope="module")
def tasks() -> dict[str, dict[str, Any]]:
    return ex.load_tasks(
        REPO_ROOT / "benchmarks" / "design-agent" / "layout-corpus",
        REPO_ROOT,
        [TASK_ID],
    )


def _gen_nfet(session: ex.LayoutSession) -> int:
    step = json.loads(
        (REPO_ROOT / session.task["generator_recipe"]["path"]).read_text()
    )["steps"][0]
    return session.klt(
        [
            "gen", step["generator"], "--params", json.dumps(step["params"]),
            "--cell-name", step["cell_name"], "--pdk", "sky130A",
            "-o", str(session.deliverable), "--format", "json",
        ]
    )  # fmt: skip


def _draw_rect(session: ex.LayoutSession) -> int:
    params = {"shapes": [{"layer": [65, 20], "rect_um": [0, 0, 1, 1]}]}
    return session.klt(
        [
            "draw", "--params", json.dumps(params),
            "--cell-name", session.cell_name, "-o", str(session.deliverable),
            "--format", "json",
        ]
    )  # fmt: skip


def stub_provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
    """draw-only draws a (bad) rectangle; the generator arms use `klt gen`;
    gen+refine keeps its seed."""
    if session.arm == "draw-only":
        _draw_rect(session)
    elif session.arm == "gen-only":
        _gen_nfet(session)
    else:
        shutil.copyfile(session.seed_gds, session.deliverable)  # type: ignore[arg-type]
    return ex.LayoutSubmission(gds=session.deliverable, usage={"tool_calls": 1})


def _sweep(
    tmp_path: Path,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    provider: ex.LayoutProvider = stub_provider,
    **kwargs: Any,
) -> dict[str, Any]:
    return ex.run_layout_benchmark(
        protocol=protocol, tasks=tasks, agents=[(AGENT, provider)],
        out_dir=tmp_path / "out", repo_root=REPO_ROOT, **kwargs,
    )  # fmt: skip


def _by_arm(tmp_path: Path) -> dict[str, dict[str, Any]]:
    return {r["arm"]: r for r in ex.load_runs(tmp_path / "out")}


@pytest.fixture(scope="module")
def full_sweep(
    tmp_path_factory: pytest.TempPathFactory,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    tmp = tmp_path_factory.mktemp("sweep")
    summary = _sweep(tmp, protocol, tasks)
    return tmp, summary


# --------------------------------------------------------------------------
# One executor, three arms, one gate
# --------------------------------------------------------------------------


def test_all_arms_produce_protocol_valid_ledgers(
    full_sweep: tuple[Path, dict[str, Any]],
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
) -> None:
    tmp, summary = full_sweep
    assert summary["run_set"]["valid"], summary["run_set"]["errors"]
    runs = ex.load_runs(tmp / "out")
    assert {r["arm"] for r in runs} == {"draw-only", "gen-only", "gen+refine"}
    for run in runs:
        result = layout.validate_run(run, protocol, tasks[run["task_id"]])
        assert result["valid"], result["errors"]
        assert run["schema"] == "klt.design_agent_benchmark.layout_run/1"


def test_every_arm_is_scored_by_the_same_gate(
    full_sweep: tuple[Path, dict[str, Any]],
) -> None:
    runs = _by_arm(full_sweep[0])
    # The bad draw-only rectangle fails the same DRC/LVS/provenance gate and
    # carries no partial score; the generator arms pass it.
    assert runs["draw-only"]["status"] in {"invalid_drc", "invalid_lvs"}
    assert runs["draw-only"]["score"] is None
    assert runs["gen-only"]["status"] == "valid"
    assert runs["gen-only"]["score"]["area_um2"] > 0
    for run in runs.values():
        assert run["diagnostics"]["gate"].keys() >= {"drc", "lvs", "provenance"}
        assert run["versions"]["protocol"] == "1.0.0"
        assert run["runtime_s"] >= 0


def test_scoring_runs_outside_the_sandbox_on_a_private_copy(
    full_sweep: tuple[Path, dict[str, Any]],
) -> None:
    tmp, _ = full_sweep
    run = _by_arm(tmp)["gen-only"]
    run_dir = tmp / "out" / "runs" / run["run_id"]
    private = run_dir / ex.ARTIFACT_FILENAME
    assert private.is_file() and not str(private).startswith(str(run_dir / "sandbox"))
    assert (run_dir / "score" / "scored.gds").is_file()
    assert ex._sha256_file(private) == run["artifact"]["gds_sha256"]
    # Policy and tool log (the evidence) are outside the agent's reach.
    assert (run_dir / ex.POLICY_FILENAME).is_file()
    assert (run_dir / ex.TOOL_LOG_FILENAME).is_file()
    assert not list((run_dir / "sandbox").rglob(ex.TOOL_LOG_FILENAME))


def test_ledger_records_provider_seeds_versions_usage_and_diagnostics(
    full_sweep: tuple[Path, dict[str, Any]],
) -> None:
    run = _by_arm(full_sweep[0])["gen-only"]
    assert run["agent"] == {"id": "stub", "provider": "reference", "model": None}
    assert len(run["seed_sha256"]) == 64 and run["seed_from"] is None
    assert run["usage"] == {"tool_calls": 1}
    assert run["tool_calls"][0]["verb"] == "gen"
    assert run["tool_calls"][0]["phase"] == "generate"
    assert run["diagnostics"]["tool_log"] == ex.TOOL_LOG_FILENAME
    lines = (full_sweep[0] / "out" / ex.RUNS_FILENAME).read_text().splitlines()
    assert len(lines) == 3  # append-only: one line per finished run


# --------------------------------------------------------------------------
# gen+refine seeding
# --------------------------------------------------------------------------


def test_refine_is_seeded_from_the_same_agents_gen_only_artifact(
    full_sweep: tuple[Path, dict[str, Any]],
) -> None:
    runs = _by_arm(full_sweep[0])
    seed, refine = runs["gen-only"], runs["gen+refine"]
    assert refine["seed_from"] == {
        "run_id": seed["run_id"],
        "gds_sha256": seed["artifact"]["gds_sha256"],
    }
    assert refine["status"] == "valid"
    assert refine["score"]["area_um2"] <= seed["score"]["area_um2"]


def test_refine_without_a_valid_seed_is_not_run(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    _sweep(tmp_path, protocol, tasks, arms=["gen+refine"])
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "not_run_no_seed"
    assert run["tool_calls"] == [] and run["score"] is None and run["seed_from"] is None


def test_refine_fails_closed_when_the_seed_artifact_changed(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    seed = ex.load_runs(tmp_path / "out")[0]
    stored = tmp_path / "out" / "runs" / seed["run_id"] / ex.ARTIFACT_FILENAME
    stored.write_bytes(stored.read_bytes() + b"\0")  # hash no longer matches
    _sweep(tmp_path, protocol, tasks, arms=["gen+refine"])
    refine = _by_arm(tmp_path)["gen+refine"]
    assert refine["status"] == "not_run_no_seed" and refine["score"] is None


def test_refinement_worse_than_its_seed_is_superseded_by_the_seed(
    tmp_path: Path,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = ex.score_artifact

    def inflated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        scored = real(*args, **kwargs)
        if scored["score"] and "gen+refine" in str(args[-1]):
            scored["score"]["area_um2"] += 1.0
        return scored

    monkeypatch.setattr(ex, "score_artifact", inflated)
    summary = _sweep(tmp_path, protocol, tasks, arms=["gen-only", "gen+refine"])
    runs = _by_arm(tmp_path)
    refine = runs["gen+refine"]
    assert refine["artifact"]["selected_round"] == 0
    assert (
        refine["artifact"]["gds_sha256"] == runs["gen-only"]["artifact"]["gds_sha256"]
    )
    assert refine["score"] == runs["gen-only"]["score"]
    assert refine["diagnostics"]["refined_area_um2"] > refine["score"]["area_um2"]
    assert summary["run_set"]["valid"], summary["run_set"]["errors"]


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arm", "forbidden", "call"),
    [
        ("draw-only", "gen", _gen_nfet),
        ("gen-only", "draw", _draw_rect),
    ],
)
def test_forbidden_tool_is_recorded_and_invalidates_the_run(
    tmp_path: Path,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    arm: str,
    forbidden: str,
    call: Any,
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        assert call(session) == ex.EXIT_REFUSED
        # The refused call wrote nothing, so the agent has nothing to submit...
        return ex.LayoutSubmission(gds=session.deliverable)

    _sweep(tmp_path, protocol, tasks, provider, arms=[arm])
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "invalid_tool_surface"
    assert run["score"] is None and run["validity"] is None
    assert [(c["verb"], c["exit_code"]) for c in run["tool_calls"]] == [
        (forbidden, 126)
    ]
    assert layout.validate_run(run, protocol, tasks[TASK_ID])["valid"]


def test_unclassified_verb_is_refused_and_invalidates_the_run(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        _gen_nfet(session)
        assert session.klt(["sim", "x.json"]) == ex.EXIT_REFUSED
        return ex.LayoutSubmission(gds=session.deliverable)

    _sweep(tmp_path, protocol, tasks, provider, arms=["gen-only"])
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "invalid_tool_surface" and run["score"] is None
    assert "sim" in run["notes"]


def test_refine_may_use_both_tool_surfaces(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        if session.arm == "gen-only":
            _gen_nfet(session)
        else:
            assert _draw_rect(session) == 0  # draw is allowed in `refine`
            assert session.klt(["layers", str(session.deliverable)]) == 0
            shutil.copyfile(session.seed_gds, session.deliverable)  # type: ignore[arg-type]
        return ex.LayoutSubmission(gds=session.deliverable)

    _sweep(tmp_path, protocol, tasks, provider, arms=["gen-only", "gen+refine"])
    refine = _by_arm(tmp_path)["gen+refine"]
    assert [c["verb"] for c in refine["tool_calls"]] == ["draw", "layers"]
    assert {c["phase"] for c in refine["tool_calls"]} == {"refine"}
    assert refine["status"] == "valid"


def test_stream_not_written_by_a_recorded_call_is_invalid(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        # A geometry writer outside the recorded path: copy a ready-made stream.
        shutil.copyfile(
            REPO_ROOT / session.task["reference_layout"]["path"], session.deliverable
        )
        return ex.LayoutSubmission(gds=session.deliverable)

    _sweep(tmp_path, protocol, tasks, provider, arms=["gen-only"])
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "invalid_tool_surface" and run["score"] is None
    assert "not produced by a recorded" in run["notes"]


def test_tool_call_budget_is_enforced_by_the_shim(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        _gen_nfet(session)
        assert session.klt(["layers", str(session.deliverable)]) == ex.EXIT_OVER_BUDGET
        return ex.LayoutSubmission(gds=session.deliverable)

    _sweep(tmp_path, protocol, tasks, provider, arms=["gen-only"], tool_call_budget=1)
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "budget_exhausted" and run["score"] is None
    assert len(run["tool_calls"]) == 1


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (
            AgentInvocationError("interactive agent session timed out after 1s"),
            "timeout",
        ),
        (
            AgentInvocationError("session exceeded its tool-call budget (80)"),
            "budget_exhausted",
        ),
        (AgentInvocationError("agent CLI not found"), "agent_error"),
        (RuntimeError("boom"), "agent_error"),
    ],
)
def test_provider_failure_is_a_recorded_unscored_status(
    tmp_path: Path,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    error: Exception,
    status: str,
) -> None:
    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        _gen_nfet(
            session
        )  # even with a layout on disk, a failed session scores nothing
        raise error

    _sweep(tmp_path, protocol, tasks, provider, arms=["gen-only"])
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == status and run["score"] is None
    assert run["artifact"] is None and str(error) in run["notes"]


def test_missing_deliverable_is_an_agent_error(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    _sweep(
        tmp_path, protocol, tasks,
        lambda s: ex.LayoutSubmission(gds=None), arms=["gen-only"],
    )  # fmt: skip
    (run,) = ex.load_runs(tmp_path / "out")
    assert run["status"] == "agent_error" and run["score"] is None


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_sandboxes_hold_task_inputs_only_and_are_unshared(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    task = tasks[TASK_ID]
    secret = {
        task["reference_layout"]["sha256"],
        *(c["sha256"] for c in task["negative_controls"]),
        task["generator_recipe"]["sha256"],
    }
    seen: dict[str, set[Path]] = {}

    def provider(session: ex.LayoutSession) -> ex.LayoutSubmission:
        files = {p for p in session.workdir.rglob("*") if p.is_file()}
        for path in files:
            # (A gen-only result is byte-identical to the recipe-built
            # reference, so the agent's *own* seed is exempt from the hash test.)
            if path.name != "seed.gds":
                assert ex._sha256_file(path) not in secret, path
            assert path.name not in {"reference.gds", "recipe.json", "neg-drc.gds"}
        assert str(tmp_path / "out" / "runs") in str(session.workdir)
        assert ex.TOOL_LOG_FILENAME not in {p.name for p in files}
        seen[session.arm] = {session.workdir}
        return stub_provider(session)

    _sweep(tmp_path, protocol, tasks, provider)
    assert len({next(iter(v)) for v in seen.values()}) == 3  # one sandbox per run
    refine = _by_arm(tmp_path)["gen+refine"]
    sandbox = tmp_path / "out" / "runs" / refine["run_id"] / "sandbox"
    seed_file = sandbox / ex.SEED_RELPATH
    assert ex._sha256_file(seed_file) == refine["seed_from"]["gds_sha256"]


def test_real_shim_refuses_forbidden_verbs_and_logs_outside_the_sandbox(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    run_dir = tmp_path / "run"
    sandbox = run_dir / "sandbox"
    run_dir.mkdir()
    policy = ex.tool_policy(
        protocol, "draw-only", log_path=run_dir / ex.TOOL_LOG_FILENAME,
        sandbox=sandbox, budget=5,
    )  # fmt: skip
    (run_dir / ex.POLICY_FILENAME).write_text(json.dumps(policy))
    ex._seed_sandbox(
        tasks[TASK_ID], REPO_ROOT, sandbox, run_dir / ex.POLICY_FILENAME, None
    )
    shim = sandbox / "bin" / "klt"
    refused = subprocess.run(  # noqa: S603
        [str(shim), "gen", "mos_array"], cwd=sandbox, capture_output=True, text=True,
        check=False,
    )  # fmt: skip
    assert refused.returncode == ex.EXIT_REFUSED
    ref_layout = tasks[TASK_ID]["reference_layout"]["path"]
    allowed = subprocess.run(  # noqa: S603
        [str(shim), "layers", str(REPO_ROOT / ref_layout)],
        cwd=sandbox, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert allowed.returncode == 0
    log = ex._read_log(run_dir / ex.TOOL_LOG_FILENAME)
    assert [(e["verb"], e["exit_code"]) for e in log] == [("gen", 126), ("layers", 0)]
    assert not (sandbox / ex.TOOL_LOG_FILENAME).exists()


def test_live_provider_runs_the_agent_through_the_policed_shim(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    prompts: list[str] = []

    def invoker(request: Any) -> AgentSessionResult:
        prompts.append(request.prompt)
        assert (
            "Bash(klt:*)" in request.allowed_tools
            and "Bash(python:*)" not in request.allowed_tools
        )
        shim = str(request.workdir / "bin" / "klt")
        bad = subprocess.run(  # noqa: S603
            [shim, "draw", "--params", "{}"], cwd=request.workdir, check=False,
            capture_output=True,
        )  # fmt: skip
        assert bad.returncode == 0 or bad.returncode == ex.EXIT_REFUSED
        step = json.loads(
            (REPO_ROOT / tasks[TASK_ID]["generator_recipe"]["path"]).read_text()
        )["steps"][0]
        subprocess.run(  # noqa: S603
            [shim, "gen", step["generator"], "--params", json.dumps(step["params"]),
             "--cell-name", step["cell_name"],
             "-o", str(request.workdir / ex.DELIVERABLE),
             "--pdk", "sky130A", "--format", "json"],
            cwd=request.workdir, check=True, capture_output=True,
        )  # fmt: skip
        return AgentSessionResult(
            text="done", tool_calls=2, turns=3, usage={"input_tokens": 7}
        )

    live = ex.make_live_layout_provider(invoke_agent=invoker)
    _sweep(tmp_path, protocol, tasks, live, arms=["gen-only"])
    (run,) = ex.load_runs(tmp_path / "out")
    # `draw` was attempted in a gen-only sandbox: refused, so the run is invalid
    # even though a clean layout was produced afterwards.
    assert run["status"] == "invalid_tool_surface" and run["score"] is None
    assert run["usage"] == {"input_tokens": 7, "tool_calls": 2, "turns": 3}
    assert "reference.gds" not in prompts[0] and "recipe" not in prompts[0]
    assert "gen-compose" in prompts[0] and "draw," not in prompts[0]


# --------------------------------------------------------------------------
# Interruption and resume
# --------------------------------------------------------------------------


def test_interrupted_run_is_recorded_unscored_and_resumed(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def killed(session: ex.LayoutSession) -> ex.LayoutSubmission:
        _gen_nfet(session)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _sweep(tmp_path, protocol, tasks, killed, arms=["gen-only"])
    (first,) = ex.load_runs(tmp_path / "out")
    assert first["status"] == "interrupted" and first["score"] is None
    assert first["artifact"] is None and [c["verb"] for c in first["tool_calls"]] == [
        "gen"
    ]

    summary = _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    runs = ex.load_runs(tmp_path / "out")
    assert [r["status"] for r in runs] == ["interrupted", "valid"]
    assert runs[1]["run_id"].endswith(".a2") and summary["skipped_finished"] == 0
    assert summary["run_set"]["valid"]

    again = _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    assert (
        again["executed"] == [] and again["skipped_finished"] == 1
    )  # finished: not rerun


def test_killed_sweep_is_reconciled_from_the_journal(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    out = tmp_path / "out"
    plan = ex._Plan(protocol, tasks[TASK_ID], "gen-only", AGENT, 1, 1, None)
    ex._append_jsonl(
        out / ex.JOURNAL_FILENAME, {"event": "start", "identity": plan.base_record()}
    )
    summary = _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    assert summary["reconciled_interrupted"] == [plan.run_id]
    statuses = [r["status"] for r in ex.load_runs(out)]
    assert statuses == ["interrupted", "valid"]


def test_failed_runs_are_not_retried(
    tmp_path: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> None:
    def failing(session: ex.LayoutSession) -> ex.LayoutSubmission:
        raise RuntimeError("boom")

    _sweep(tmp_path, protocol, tasks, failing, arms=["gen-only"])
    second = _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    assert second["executed"] == [] and len(ex.load_runs(tmp_path / "out")) == 1


def test_torn_final_ledger_line_is_ignored_but_midfile_corruption_fails(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    ledger = out / ex.RUNS_FILENAME
    ledger.write_text('{"run_id": "a"}\n{"run_id": "b", "sta')
    assert ex.load_runs(out) == [{"run_id": "a"}]
    ledger.write_text('{"run_id": "a"\n{"run_id": "b"}\n')
    with pytest.raises(ex.LayoutExecutorError, match="corrupt"):
        ex.load_runs(out)


def test_executor_refuses_to_record_a_run_the_validator_rejects(
    tmp_path: Path,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = ex._finish

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record = real(*args, **kwargs)
        record["score"] = {"area_um2": 1.0, "utilization": None, "sim_margin": None}
        record["status"] = "agent_error"  # an unscored status must not carry a score
        return record

    monkeypatch.setattr(ex, "_finish", broken)
    with pytest.raises(ex.LayoutExecutorError, match="invalid run"):
        _sweep(tmp_path, protocol, tasks, arms=["gen-only"])
    assert not (tmp_path / "out" / ex.RUNS_FILENAME).exists()


# --------------------------------------------------------------------------
# CLI and compatibility
# --------------------------------------------------------------------------


def test_run_layout_cli_then_validate_layout_accepts_the_jsonl_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code = dab.main(
        ["run-layout", "--out-dir", str(out), "--task", TASK_ID,
         "--arm", "gen-only", "--arm", "gen+refine"]
    )  # fmt: skip
    assert code == 0
    capsys.readouterr()
    assert [r["status"] for r in ex.load_runs(out)] == ["valid", "valid"]
    task_json = (
        REPO_ROOT / "benchmarks/design-agent/layout-corpus" / TASK_ID / "task.json"
    )
    assert (
        dab.main(
            [
                "validate-layout",
                "--task",
                str(task_json),
                "--runs",
                str(out / ex.RUNS_FILENAME),
            ]
        )
        == 0
    )


def test_round_ledger_schema_and_legacy_run_are_untouched() -> None:
    schema = json.loads(
        (REPO_ROOT / "benchmarks/design-agent/schema/ledger.schema.json").read_text()
    )
    assert (
        schema["properties"]["schema"]["const"] == "klt.design_agent_benchmark.ledger/1"
    )
    assert "arm" not in schema["properties"]  # non-layout ledgers keep their shape
    # The additive layout-run fields are optional: shipped fixtures stay valid.
    runs = json.loads(
        (
            REPO_ROOT / "benchmarks/design-agent/fixtures/layout/runs-valid.json"
        ).read_text()
    )
    schema_run = json.loads(
        (
            REPO_ROOT / "benchmarks/design-agent/schema/layout-run.schema.json"
        ).read_text()
    )
    assert not {"runtime_s", "versions", "diagnostics"} & set(schema_run["required"])
    assert layout.validate_run_set(runs)["valid"]
