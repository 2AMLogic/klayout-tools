"""Tests for `scripts/design_agent_benchmark.py` -- the design-agent
benchmark harness (issue #1719) and its live-agent candidate provider
(issue #1732).

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


def test_shipped_tasks_are_easy_or_hard_tier():
    """Easy-tier task set (issue #1719) plus the hard-tier oscillator/VCO/
    PLL family's first task (issue #1735). No medium-tier task has shipped
    yet -- see benchmarks/design-agent/README.md's "Known limitations"."""
    for path in dab._task_paths(TASKS_DIR):
        task = dab.load_task(path)
        assert task["tier"] in {"easy", "hard"}


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
    assert "bench_nmos" in prompt
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
