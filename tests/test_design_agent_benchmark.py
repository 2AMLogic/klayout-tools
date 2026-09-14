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


def _scratch_task_with_patched_netlist(
    tmp_path: Path, task_id: str, netlist_name: str, replacements: list[tuple[str, str]]
) -> tuple[Path, Path]:
    """Copy one shipped task and its whole reference directory into a
    scratch repo root, applying ``replacements`` to the named netlist.
    Returns ``(scratch_tasks_dir, scratch_repo_root)`` ready to hand to
    :func:`dab.check_reference_solutions`."""
    scratch_repo = tmp_path / "repo"
    scratch_tasks = scratch_repo / "benchmarks" / "design-agent" / "tasks"
    ref_rel = Path("benchmarks", "design-agent", "reference", task_id)
    scratch_tasks.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / ref_rel, scratch_repo / ref_rel)

    task = dab.load_task(TASKS_DIR / f"{task_id}.json")
    (scratch_tasks / f"{task_id}.json").write_text(json.dumps(task))

    netlist_path = scratch_repo / ref_rel / netlist_name
    body = netlist_path.read_text()
    for old, new in replacements:
        assert old in body, f"{netlist_name}: pattern not found: {old!r}"
        body = body.replace(old, new)
    netlist_path.write_text(body)
    return scratch_tasks, scratch_repo


@_SKIP_NO_NGSPICE
@pytest.mark.parametrize(
    ("task_id", "netlist_name", "replacements", "why"),
    [
        pytest.param(
            "telescopic-cascode-amp",
            "casc_amp.spice",
            [
                (
                    "XM1 n1  gate 0   0   sky130_fd_pr__nfet_01v8 L=4 W=40  nf=1 "
                    "mult=1",
                    "XM1 out gate 0   0   sky130_fd_pr__nfet_01v8 L=4 W=40  nf=1 "
                    "mult=1",
                ),
                (
                    "XM2 out nbc  n1  0   sky130_fd_pr__nfet_01v8 L=4 W=40  nf=1 "
                    "mult=1\n",
                    "",
                ),
                (
                    "XM3 out pbc  n2  vdd sky130_fd_pr__pfet_01v8 L=4 W=100 nf=1 "
                    "mult=1\n",
                    "",
                ),
                (
                    "XM4 n2  pbs  vdd vdd sky130_fd_pr__pfet_01v8 L=4 W=100 nf=1 "
                    "mult=1",
                    "XM4 out pbs  vdd vdd sky130_fd_pr__pfet_01v8 L=4 W=100 nf=1 "
                    "mult=1",
                ),
            ],
            "both cascode devices removed -> plain common-source stage",
            id="cascode-devices-removed",
        ),
        pytest.param(
            "miller-integrator",
            "integrator.spice",
            [("Cf gate out {cf}", "Cf gate out 1f")],
            "integrating capacitor shrunk to 1 fF -> flat gain stage",
            id="integrating-cap-removed",
        ),
        pytest.param(
            "schmitt-trigger",
            "schmitt.spice",
            [
                (
                    "XMN3 vdd out na 0 sky130_fd_pr__nfet_01v8 L=1 W=10 nf=1 mult=1\n",
                    "",
                ),
                (
                    "XMP3 0 out nb vdd sky130_fd_pr__pfet_01v8 L=1 W=25 nf=1 mult=1\n",
                    "",
                ),
            ],
            "feedback devices deleted -> plain CMOS inverter",
            id="schmitt-feedback-deleted",
        ),
        pytest.param(
            "schmitt-trigger",
            "schmitt.spice",
            [
                (
                    "XMN3 vdd out na 0 sky130_fd_pr__nfet_01v8 L=1 W=10 nf=1 mult=1",
                    "XMN3 vdd out na 0 sky130_fd_pr__nfet_01v8 L=1 W=1 nf=1 mult=1",
                ),
                (
                    "XMP3 0 out nb vdd sky130_fd_pr__pfet_01v8 L=1 W=25 nf=1 mult=1",
                    "XMP3 0 out nb vdd sky130_fd_pr__pfet_01v8 L=1 W=2 nf=1 mult=1",
                ),
            ],
            "feedback devices under-sized -> ~0.1 V of hysteresis, not 0.3 V",
            id="schmitt-feedback-undersized",
        ),
    ],
)
def test_medium_tier_gates_reject_a_plausible_but_wrong_topology(
    task_id, netlist_name, replacements, why
):
    """Issue #1734's "thresholds actually discriminate correct from
    incorrect sizing" check, as a regression test rather than a one-off
    manual sweep.

    Each mutation below leaves a circuit that still simulates cleanly and
    still *looks* like an answer -- a well-biased common-source stage, a
    working gain stage, a working CMOS inverter -- and differs from the
    reference only in the property its task is actually about. A gate that
    passed any of these would not be measuring the named circuit class. The
    Schmitt trigger cases matter most: its criterion is behavioral
    (hysteresis width under a transient ramp), so a DC- or small-signal-
    shaped gate would wave both of them through."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch_tasks, scratch_repo = _scratch_task_with_patched_netlist(
            Path(tmp), task_id, netlist_name, replacements
        )
        result = dab.check_reference_solutions(scratch_tasks, scratch_repo)
        assert result["valid"] is False, f"{task_id} gate accepted: {why}"


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


def test_seed_agent_sandbox_writes_skills_models_and_testbench(tmp_path):
    task = dab.load_task(TASKS_DIR / "common-source-amp.json")
    layout = _sandbox(task, tmp_path)
    sandbox = layout["workdir"]

    assert layout["stems"] == ["cs_amp"]
    assert (sandbox / "skills" / "design-topology-selection.md").is_file()
    assert (sandbox / "skills" / "design-sizing.md").is_file()
    assert (sandbox / "skills" / "design-netlist-authoring.md").is_file()
    assert (sandbox / "models.lib").is_file()
    assert (sandbox / "TASK.md").is_file()

    request = json.loads((sandbox / "sim_request.json").read_text())
    # The netlist the agent is asked to author, as a sandbox-relative path.
    assert request["netlist"] == "cs_amp.spice"
    assert request["models"]["lib"] == "models.lib"
    # The measurement contract is carried over verbatim from the reference.
    reference_request = json.loads(
        (
            REPO_ROOT
            / "benchmarks/design-agent/reference/common-source-amp/sim_request.json"
        ).read_text()
    )
    assert request["measurements"] == reference_request["measurements"]
    assert request["corners"] == reference_request["corners"]


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
