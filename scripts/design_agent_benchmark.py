#!/usr/bin/env python3
"""Design-agent benchmark harness (issue #1719): a task set x PDK harness
scoring the S4 (topology selection) -> S6 (netlist authoring) -> S10
(`klt sim` corner sweep) design-pipeline slice with pass@k, modeled after
AnalogCoder (Lai et al., AAAI 2025, arXiv 2405.14918) -- the task set and
per-class checks are written fresh against this repo's own KB/toolchain,
never reproduced from that (unlicensed) paper's repo. See
``benchmarks/design-agent/README.md`` for the task-set shape and this
module's scope.

This module is pure orchestration over already-shipped verbs -- it never
reimplements SPICE simulation or scoring logic itself. Task descriptors
are validated against ``benchmarks/design-agent/schema/task.schema.json``
(mirrors :func:`klayout_tools.kb.validate_entries`'s validation shape), and
each task's pass criterion is exactly the ``klt eval`` descriptor its own
``reference.eval_descriptor`` field names -- scored by calling
:func:`klayout_tools.eval.run_eval` directly (the same library entry point
``klt eval`` itself calls), never a re-derived pass/fail rule.

Two subcommands:

``validate``
    Schema-validate every task under a tasks directory, and confirm each
    task's own reference solution passes its own ``eval_descriptor`` (the
    "task cannot be unsatisfiable" acceptance check from issue #1719).

``run``
    Run ``--attempts`` independent attempts per task through a *candidate
    provider* (pluggable via ``--provider``; defaults to
    :func:`reference_candidate_provider`, a deterministic stand-in that
    always hands back the task's own known-good reference solution -- see
    its docstring for what that alone does and does not prove),
    score each attempt with ``klt eval``, and report pass@1/pass@k per tier
    plus overall, using the standard unbiased pass@k estimator from Chen et
    al. 2021 ("Evaluating Large Language Models Trained on Code").

    ``--provider live-agent`` (:func:`make_live_agent_provider` /
    :data:`live_agent_candidate_provider`, issue #1732) drives one headless
    invocation per attempt through the S4 (topology selection) -> S5
    (sizing) -> S6 (netlist authoring) skill chain
    (``.claude/skills/design-{topology-selection,sizing,netlist-
    authoring}/SKILL.md``) and scores the agent's *own* proposed netlist(s),
    never the reference solution -- this is what turns a pass@k number from
    this harness into a real regression signal for the design pipeline
    itself, not just for the harness's own plumbing. See that function's
    docstring for the deliberate scope-narrowing (a single-turn text
    completion seeded with the skill text, not a multi-turn tool-using
    session with live ``klt kb``/``klt sim`` access).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from klayout_tools.eval import EvalError, run_eval  # noqa: E402

DEFAULT_TASKS_DIR = REPO_ROOT / "benchmarks" / "design-agent" / "tasks"
DEFAULT_SCHEMA_PATH = (
    REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "task.schema.json"
)

TIERS = ("easy", "medium", "hard")


class BenchmarkError(Exception):
    """Raised for problems that prevent the benchmark from running at all
    (missing/malformed task set, bad schema) -- never for an individual
    task attempt failing its own gate, which is a normal scored outcome."""


# --------------------------------------------------------------------------
# Task loading and schema validation
# --------------------------------------------------------------------------


def _task_paths(tasks_dir: Path) -> list[Path]:
    if not tasks_dir.is_dir():
        raise BenchmarkError(f"tasks directory not found: {tasks_dir}")
    return sorted(tasks_dir.glob("*.json"))


def load_task(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{path}: failed to load task: {exc}") from exc


def _task_errors(
    path: Path, validator: jsonschema.protocols.Validator, repo_root: Path
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        task = load_task(path)
    except BenchmarkError as exc:
        return None, [str(exc)]

    errors = []
    for error in sorted(validator.iter_errors(task), key=str):
        location = "/".join(str(part) for part in error.path)
        errors.append(f"{location}: {error.message}" if location else error.message)

    task_id = task.get("id")
    if task_id != path.stem:
        errors.append(f"id {task_id!r} does not match filename stem {path.stem!r}")

    if isinstance(task.get("reference"), dict):
        reference = task["reference"]
        candidate_fields = [
            reference.get("eval_descriptor"),
            *(reference.get("netlists") or []),
        ]
        for field in candidate_fields:
            if not isinstance(field, str):
                continue
            candidate = Path(field)
            if candidate.is_absolute() or ".." in candidate.parts:
                errors.append(
                    f"reference: must be a repository-relative path without "
                    f"'..' segments: {field}"
                )
            elif not (repo_root / candidate).is_file():
                errors.append(f"reference: referenced path does not exist: {field}")

    return task, errors


def validate_tasks(
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Schema-validate every task under ``tasks_dir`` and confirm every
    ``reference.eval_descriptor``/``reference.netlists`` path it names
    exists on disk. Never runs a simulation -- see
    :func:`check_reference_solutions` for the "does the reference actually
    pass its own gate" check.

    Mirrors :func:`klayout_tools.kb.validate_entries`'s shape and never-
    raises-for-task-level-problems posture; only raises
    :class:`BenchmarkError` for an environment problem (missing tasks dir
    or schema file).
    """
    if not schema_path.is_file():
        raise BenchmarkError(f"schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text())
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)

    results = []
    for path in _task_paths(tasks_dir):
        _task, errors = _task_errors(path, validator, repo_root)
        results.append({"id": path.stem, "valid": not errors, "errors": errors})

    return {
        "schema_version": 1,
        "valid": all(result["valid"] for result in results),
        "task_count": len(results),
        "tasks": results,
    }


def check_reference_solutions(
    tasks_dir: Path = DEFAULT_TASKS_DIR, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    """Run each task's own ``reference.eval_descriptor`` (no candidate
    substitution -- the reference netlist(s) it names are fixed paths) and
    confirm ``valid: true``. This is the literal "every task has a known-
    good reference netlist that passes its own criterion" acceptance check
    from issue #1719 -- it is what keeps a task from being unsatisfiable by
    construction.
    """
    results = []
    for path in _task_paths(tasks_dir):
        task = load_task(path)
        descriptor_path = repo_root / task["reference"]["eval_descriptor"]
        try:
            report = run_eval(str(descriptor_path))
            valid = bool(report.get("valid"))
            error = None
        except EvalError as exc:
            valid = False
            report = None
            error = str(exc)
        results.append(
            {
                "id": task["id"],
                "tier": task.get("tier"),
                "valid": valid,
                "error": error,
                "eval_report": report,
            }
        )
    return {
        "schema_version": 1,
        "valid": all(result["valid"] for result in results),
        "task_count": len(results),
        "tasks": results,
    }


# --------------------------------------------------------------------------
# Candidate providers and attempt scoring
# --------------------------------------------------------------------------

# A candidate provider is handed a task dict, the (0-based) attempt index,
# and the repo root, and returns ``(descriptor_arg, candidate_arg)`` -- the
# same two positional arguments :func:`klayout_tools.eval.run_eval` takes.
# ``candidate_arg`` is ``None`` when the descriptor needs no ``${name}``
# substitution (the reference provider's case).
CandidateProvider = Callable[[dict[str, Any], int, Path], tuple[str, str | None]]


def reference_candidate_provider(
    task: dict[str, Any], attempt_index: int, repo_root: Path
) -> tuple[str, str | None]:
    """Deterministic stand-in candidate provider: every attempt is scored
    against the task's own known-good reference solution, unmodified.

    **What this proves**: the runner's attempt-loop, `klt eval` invocation,
    and pass@k aggregation are wired correctly end-to-end -- a real
    regression in *this harness* (not the design pipeline) would show up
    here as a pass-rate drop below 100%.

    **What this does NOT prove**: nothing about the actual S4 (topology
    selection) -> S5 (sizing) -> S6 (netlist authoring) skill chain's own
    quality, since no skill is invoked -- every attempt is the answer key.
    Tracking *that* regression is what :func:`make_live_agent_provider` /
    :data:`live_agent_candidate_provider` (issue #1732) is for -- a
    candidate provider that drives a live agent through those skills and
    scores each attempt's own proposed netlist, never the reference
    solution.
    """
    del attempt_index  # every attempt is identical for this provider
    descriptor_path = repo_root / task["reference"]["eval_descriptor"]
    return str(descriptor_path), None


# --------------------------------------------------------------------------
# Live-agent candidate provider (issue #1732)
# --------------------------------------------------------------------------

#: Env var overriding the CLI :func:`_default_invoke_agent` shells out to
#: (default ``"claude"``) -- tests point this at a small stub script so the
#: subprocess/parsing plumbing is exercised without a real agent invocation
#: (no network credentials needed in CI for that slice); a real run needs
#: ``claude`` on ``PATH`` and an authenticated session (see
#: ``.loom/scripts/spawn-claude.sh``'s own token-pool convention for the
#: headless-invocation precedent this mirrors).
AGENT_CLI_ENV = "KLT_DESIGN_AGENT_BENCHMARK_AGENT_CLI"

#: Per-attempt wall-clock ceiling for a live-agent invocation (`run` mode's
#: ``--agent-timeout-s`` default). Generous relative to `klt sim`'s own
#: per-corner timeouts -- a single-turn completion authoring a handful of
#: SPICE lines should not need anywhere near this, but a live network call
#: has no upper bound of its own to lean on.
DEFAULT_AGENT_TIMEOUT_S = 600.0

#: The S4 -> S5 -> S6 skill files this provider drives an agent through, in
#: stage order -- see ``docs/design/design-pipeline.md`` for the stage graph
#: these files themselves are thin loaders of.
SKILL_CHAIN_PATHS: tuple[Path, ...] = (
    Path(".claude/skills/design-topology-selection/SKILL.md"),
    Path(".claude/skills/design-sizing/SKILL.md"),
    Path(".claude/skills/design-netlist-authoring/SKILL.md"),
)

_NETLIST_FENCE_RE = re.compile(
    r"```spice:(?P<stem>[A-Za-z0-9_.-]+)[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)

#: A generic (non-PDK) ``.model <name> {NMOS|PMOS}(...)`` corner card in a
#: task's own ``reference/<id>/models.lib`` -- what
#: :func:`_device_model_contract` reads the live-agent prompt's device
#: contract off of, rather than hardcoding one task set's device list.
_MODEL_CARD_RE = re.compile(
    r"^[ \t]*\.model[ \t]+(?P<name>[A-Za-z0-9_.]+)[ \t]+(?P<kind>nmos|pmos)\b",
    re.IGNORECASE | re.MULTILINE,
)


class AgentInvocationError(Exception):
    """Raised when a live-agent invocation itself fails to run, times out, or
    returns a response that cannot be parsed into the expected labeled
    netlist fence(s) -- distinct from a normal "candidate netlist fails its
    own `klt eval` gate" outcome, which is a scored ``valid: false``, not an
    error. Caught by :func:`run_attempt` exactly like
    :class:`~klayout_tools.eval.EvalError`: recorded as a failed attempt,
    never allowed to crash the whole benchmark sweep (the "an agent
    invocation that fails/times out should be recorded as a failed attempt,
    not crash the whole benchmark run" edge case from issue #1732's Test
    Plan)."""


#: An agent invoker takes the fully-built prompt and returns the agent's raw
#: response text. The swappable extension point tests stub out; the default,
#: :func:`_default_invoke_agent`, is the real `claude`-CLI-backed
#: implementation.
AgentInvoker = Callable[[str], str]


def _extract_agent_text(stdout: str) -> str:
    """Pull the agent's final response text out of `claude -p --output-format
    json`'s envelope (a top-level ``result`` string field) -- falls back to
    the raw stdout for a CLI/stub that just prints plain text, so a minimal
    test stub need not replicate the full envelope shape."""
    stripped = stdout.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stdout
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        return payload["result"]
    return stdout


def _default_invoke_agent(
    prompt: str, *, timeout_s: float = DEFAULT_AGENT_TIMEOUT_S
) -> str:
    """Drive a live agent in one headless, single-turn invocation: shells out
    to the `claude` CLI's print mode (``claude -p <prompt> --output-format
    json``) -- the same headless-invocation convention this repo's own Loom
    tooling already relies on everywhere else (``.loom/scripts/spawn-
    claude.sh``) -- rather than adding a network SDK dependency to this
    project just for this one benchmark script. Override the binary via
    :data:`AGENT_CLI_ENV` (a real run needs ``claude`` on ``PATH`` and an
    authenticated session/``CLAUDE_CODE_OAUTH_TOKEN``; tests point it at a
    small stub script instead).

    **Deliberate scope-narrowing** (documented here, and in
    ``benchmarks/design-agent/README.md``'s "Known limitations"): this is a
    single-turn text completion, not a full multi-turn tool-using agent
    session. The S4/S5/S6 skill files' text is embedded directly in the
    prompt (see :func:`_build_live_agent_prompt`) rather than left for the
    invoked agent to ``Read`` for itself, and no live ``klt kb``/``klt sim``
    tool access is wired into this call -- a benchmark script driving a
    fully interactive, tool-using nested agent session is a materially
    larger undertaking (sandboxing a per-attempt working tree, streaming
    tool-call transcripts, etc.) tracked as a follow-up rather than attempted
    here. What this milestone measures is still real: the agent's own
    single-shot topology/sizing/netlist-authoring judgment against the
    skill-file guidance and the task's testbench contract, scored by the
    exact same `klt eval` gate the reference solution is.
    """
    cli = os.environ.get(AGENT_CLI_ENV, "claude")
    cmd = [cli, "-p", prompt, "--output-format", "json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise AgentInvocationError(
            f"agent CLI {cli!r} not found on PATH -- install it, or point "
            f"{AGENT_CLI_ENV} at a stub for testing"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentInvocationError(
            f"agent invocation timed out after {timeout_s}s"
        ) from exc
    if proc.returncode != 0:
        raise AgentInvocationError(
            f"agent invocation failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:2000]}"
        )
    return _extract_agent_text(proc.stdout)


def _load_skill_chain_text(repo_root: Path) -> str:
    """Concatenate the S4->S5->S6 skill files' full text, in stage order, for
    embedding directly in the live-agent prompt."""
    sections = []
    for rel_path in SKILL_CHAIN_PATHS:
        path = repo_root / rel_path
        try:
            body = path.read_text()
        except OSError as exc:
            raise AgentInvocationError(f"skill file not found: {path}") from exc
        sections.append(f"----- {rel_path.as_posix()} -----\n{body}")
    return "\n\n".join(sections)


def _reference_testbenches(
    task: dict[str, Any], repo_root: Path
) -> list[dict[str, Any]]:
    """The block's declared `klt sim` request document(s) -- analysis,
    corners, and measurements only, **never the reference netlist body** --
    that the live agent's own candidate netlist(s) must satisfy. Keyed by
    the netlist stem each request names, deduplicated across a descriptor's
    gates/objective/metrics that happen to share one request file (mirrors
    :func:`_build_live_agent_descriptor`'s own walk of the same three
    fields)."""
    reference = task["reference"]
    reference_dir = (repo_root / reference["eval_descriptor"]).parent
    descriptor = json.loads((repo_root / reference["eval_descriptor"]).read_text())
    entries = [
        *descriptor["gates"],
        descriptor["objective"],
        *descriptor.get("metrics", []),
    ]
    testbenches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        args = entry.get("args") or {}
        request_ref = args.get("request")
        if not isinstance(request_ref, str) or request_ref in seen:
            continue
        seen.add(request_ref)
        sim_request = json.loads((reference_dir / request_ref).read_text())
        stem = Path(sim_request["netlist"]).stem
        testbenches.append({"netlist_stem": stem, "sim_request": sim_request})
    return testbenches


def _device_model_contract(
    task: dict[str, Any], repo_root: Path, testbenches: list[dict[str, Any]]
) -> str:
    """The live-agent prompt's device-model contract, **derived from the
    task's own model library** rather than hardcoded.

    The easy tier's `models.lib` defines a single NMOS card; the medium
    tier's complementary blocks (cascode load, CMOS Schmitt trigger) cannot
    be built without a PMOS, so their own `models.lib` adds one. Reading the
    `.model` cards out of whichever library the task's `klt sim` request
    names keeps the prompt from telling an agent "no PMOS model is defined"
    while handing it a testbench whose library defines one -- a contract the
    agent's candidate would then be scored against having been actively
    misled about.
    """
    lib_refs: list[str] = []
    for testbench in testbenches:
        models = testbench["sim_request"].get("models") or {}
        lib = models.get("lib")
        if isinstance(lib, str) and lib not in lib_refs:
            lib_refs.append(lib)

    reference_dir = (repo_root / task["reference"]["eval_descriptor"]).parent
    by_kind: dict[str, list[str]] = {"nmos": [], "pmos": []}
    for lib in lib_refs:
        path = Path(lib)
        if not path.is_absolute():
            path = reference_dir / path
        try:
            text = path.read_text()
        except OSError:
            continue
        for match in _MODEL_CARD_RE.finditer(text):
            kind = match.group("kind").lower()
            name = match.group("name")
            if name not in by_kind[kind]:
                by_kind[kind].append(name)

    if not by_kind["nmos"] and not by_kind["pmos"]:
        # No library to read (a PDK-model task, or an unreadable path) --
        # say nothing specific rather than assert a device list that may be
        # wrong.
        return (
            "Use only the device models this task's own `klt sim` request "
            "document (below) points its model library at; do not write your "
            "own `.model` card."
        )

    lines = []
    for kind in ("nmos", "pmos"):
        names = by_kind[kind]
        rendered = ", ".join(f"`{name}`" for name in names) if names else "none defined"
        lines.append(f"  {kind.upper()}: {rendered}")
    return (
        "This benchmark's model library defines exactly the SPICE models "
        "below (LEVEL=1), already swept across this task's own process/"
        "temperature/supply corner matrix:\n\n"
        + "\n".join(lines)
        + "\n\nUse only these model names for every MOSFET your netlist "
        "instantiates; do not write your own `.model` card, and do not "
        "reference any other model name."
    )


def _build_live_agent_prompt(
    task: dict[str, Any], repo_root: Path
) -> tuple[str, list[str]]:
    """Build the single-turn prompt :func:`_default_invoke_agent` sends, and
    the ordered list of netlist stems the agent must return one labeled
    fence per (:func:`_extract_labeled_netlists`). Never includes the
    reference solution's own netlist body -- only the block spec (S3's
    output) and the testbench contract (`klt sim` request documents, minus
    their `netlist` field's content) the agent's own candidate must satisfy.
    """
    stems = [Path(p).stem for p in task["reference"]["netlists"]]
    testbenches = _reference_testbenches(task, repo_root)
    skill_chain_text = _load_skill_chain_text(repo_root)
    device_model_contract = _device_model_contract(task, repo_root, testbenches)

    fence_block = "\n".join(
        f'```spice:{stem}\n<your netlist body for "{stem}">\n```' for stem in stems
    )

    prompt = f"""You are acting as the design agent for klayout-tools' staged
analog design pipeline (docs/design/design-pipeline.md), driving stages
S4 (topology selection) -> S5 (sizing) -> S6 (netlist authoring) for the
block below, exactly as this repo's own skills instruct.

=== S4/S5/S6 skill procedures (read these before answering) ===

{skill_chain_text}

=== Block to design ===

Task id: {task["id"]}
Title: {task["title"]}
Tier: {task["tier"]}
PDK (name only -- see the device model contract below; do not invent
device models of your own): {task["pdk"]}
Description: {task["description"]}
Block spec (S3 output, your S4 input):
{json.dumps(task["block_spec"], indent=2)}

=== Device model contract (harness-provided, not part of your design) ===

{device_model_contract}

=== Testbench contract your netlist(s) must satisfy ===

Per S6's "circuit body, not a full deck" constraint, your netlist is a
**circuit body only**: device/source instantiations, with no
`.control`/`.end`/`.model`/`.lib`/`.include` cards of your own. It will be
spliced, unmodified, into the `klt sim` request document(s) below in place
of the `netlist` field already shown there -- so every node name a `.meas`
line references (e.g. `out`, `vout#branch`) must exist, literally spelled,
in your netlist, and any voltage source a `dc` analysis sweeps by name
(e.g. `Vout` for a `"dc", "args": "Vout ..."` analysis) must exist as its
own independent source of that exact name:

{json.dumps(testbenches, indent=2)}

=== Output format ===

Respond with exactly {len(stems)} fenced code block(s), one per netlist
named below, using precisely this fence syntax -- a literal
"```spice:<stem>" opening line, the netlist body, then a bare "```"
closing line -- and no netlist content outside these fences:

{fence_block}

Briefly note your S4 topology choice and S5 sizing rationale in prose
before the fenced block(s); the fenced block(s) are the only part of your
response this harness parses.
"""
    return prompt, stems


def _extract_labeled_netlists(response_text: str, stems: list[str]) -> dict[str, str]:
    """Parse the ` ```spice:<stem> ` fenced blocks :func:`_build_live_agent_prompt`
    asked for out of the agent's raw response text. Raises
    :class:`AgentInvocationError` -- an attempt-level failure, not a crash --
    when any required stem's fence is missing, rather than silently scoring
    an incomplete response."""
    found: dict[str, str] = {}
    for match in _NETLIST_FENCE_RE.finditer(response_text):
        stem = match.group("stem")
        if stem in stems and stem not in found:
            found[stem] = match.group("body").strip() + "\n"
    missing = [stem for stem in stems if stem not in found]
    if missing:
        raise AgentInvocationError(
            "agent response missing labeled netlist fence(s) for: "
            f"{', '.join(missing)} (expected a '```spice:<stem>' block for "
            f"each of {stems})"
        )
    return found


def _build_live_agent_descriptor(
    task: dict[str, Any],
    repo_root: Path,
    netlists_by_stem: dict[str, str],
    scratch_dir: Path,
) -> dict[str, Any]:
    """Deep-copy the task's reference ``eval_descriptor``, rewriting every
    gate's/objective's/metric's ``request`` arg from a fixed reference
    ``sim_request.json`` path into a freshly-written request file (under
    ``scratch_dir``) pointing at the agent's own candidate netlist instead
    of the reference one -- everything else (corners, analysis,
    measurements, models) is carried over unmodified from the reference
    request, since that is the fixed testbench contract the candidate is
    scored against, not part of what the agent proposes.

    A rewritten *file* rather than an inline JSON object, even though
    ``docs/cli/eval.md`` documents inline-object ``request`` args as a
    ``"sim"`` gate's own supported form: ``run_sim``'s ``load_request``
    (unlike ``run_lvs``'s) only ever reads an actual file from disk (no
    ``"-"``/inline-JSON dispatch of its own) -- a pre-existing gap between
    that doc and ``sim.py``'s implementation, tracked separately rather
    than fixed here. Writing a real file sidesteps it either way.

    Only ``"sim"``-check gates are supported today -- matches this
    milestone's easy-tier task set (issue #1732's scope; a DRC/LVS-gated
    task would need this function extended, not this provider swapped
    out).
    """
    reference = task["reference"]
    reference_dir = (repo_root / reference["eval_descriptor"]).parent
    descriptor = json.loads((repo_root / reference["eval_descriptor"]).read_text())

    candidate_paths: dict[str, Path] = {}
    request_paths: dict[str, Path] = {}

    def _rewrite_args(args: dict[str, Any]) -> dict[str, Any]:
        request_ref = args.get("request")
        if not isinstance(request_ref, str):
            return args
        if request_ref in request_paths:
            return {**args, "request": str(request_paths[request_ref])}
        sim_request = json.loads((reference_dir / request_ref).read_text())
        stem = Path(sim_request["netlist"]).stem
        if stem not in netlists_by_stem:
            raise AgentInvocationError(
                f"agent produced no netlist labeled {stem!r}, required by {request_ref}"
            )
        if stem not in candidate_paths:
            candidate_path = scratch_dir / f"{stem}.spice"
            candidate_path.write_text(netlists_by_stem[stem])
            candidate_paths[stem] = candidate_path
        new_request = copy.deepcopy(sim_request)
        new_request["netlist"] = str(candidate_paths[stem])
        models = new_request.get("models")
        if isinstance(models, dict) and isinstance(models.get("lib"), str):
            lib_path = Path(models["lib"])
            if not lib_path.is_absolute():
                models["lib"] = str((reference_dir / lib_path).resolve())
        request_path = scratch_dir / Path(request_ref).name
        request_path.write_text(json.dumps(new_request))
        request_paths[request_ref] = request_path
        return {**args, "request": str(request_path)}

    for gate in descriptor["gates"]:
        if gate.get("check") != "sim":
            raise AgentInvocationError(
                "live-agent provider only supports 'sim' gate checks "
                f"currently, got {gate.get('check')!r}"
            )
        gate["args"] = _rewrite_args(gate.get("args") or {})
    descriptor["objective"]["args"] = _rewrite_args(
        descriptor["objective"].get("args") or {}
    )
    for metric in descriptor.get("metrics", []):
        metric["args"] = _rewrite_args(metric.get("args") or {})

    return descriptor


def make_live_agent_provider(
    *,
    invoke_agent: AgentInvoker = _default_invoke_agent,
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
    scratch_root: Path | None = None,
) -> CandidateProvider:
    """Build a live-agent :data:`CandidateProvider`: one headless
    ``invoke_agent`` call per attempt, seeded with the S4->S5->S6 skill-chain
    text and the task's own testbench contract (:func:`_build_live_agent_prompt`),
    parses the agent's proposed netlist(s) out of its response
    (:func:`_extract_labeled_netlists`), and returns a freshly-synthesized
    ``klt eval`` descriptor pointing at them (:func:`_build_live_agent_descriptor`)
    -- the agent's own answer, never the task's reference solution.

    ``invoke_agent`` is the swappable extension point tests stub out (issue
    #1732's Test Plan: "mock/stub the skill invocation boundary");
    :func:`_default_invoke_agent` (the real, `claude`-CLI-backed
    implementation) is the default, in which case ``agent_timeout_s`` is
    honored -- a caller-supplied stub owns its own timeout behavior, if any.

    Each attempt gets its own ``scratch_dir`` (a fresh directory under
    ``scratch_root``, or a fresh `tempfile.mkdtemp` when ``scratch_root`` is
    ``None``) to write its candidate netlist(s) into -- never cleaned up
    automatically, which is deliberate: they are useful evidence of what the
    agent actually proposed when a run's pass rate looks wrong, and an
    ephemeral CI runner reclaims them at job end regardless.
    """

    def _agent_call(prompt: str) -> str:
        if invoke_agent is _default_invoke_agent:
            return _default_invoke_agent(prompt, timeout_s=agent_timeout_s)
        return invoke_agent(prompt)

    def provider(
        task: dict[str, Any], attempt_index: int, repo_root: Path
    ) -> tuple[str, str | None]:
        prompt, stems = _build_live_agent_prompt(task, repo_root)
        response_text = _agent_call(prompt)
        netlists_by_stem = _extract_labeled_netlists(response_text, stems)

        if scratch_root is not None:
            scratch_dir = Path(scratch_root) / f"{task['id']}-attempt{attempt_index}"
            scratch_dir.mkdir(parents=True, exist_ok=True)
        else:
            scratch_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"design-agent-benchmark-{task['id']}-attempt{attempt_index}-"
                )
            )

        descriptor = _build_live_agent_descriptor(
            task, repo_root, netlists_by_stem, scratch_dir
        )
        return json.dumps(descriptor), None

    return provider


#: The default-configured live-agent provider -- usable directly by callers
#: that don't need CLI-flag-driven timeout/stub wiring (`run`'s own
#: ``--provider live-agent`` path builds a fresh one via
#: :func:`make_live_agent_provider` instead, so ``--agent-timeout-s`` takes
#: effect).
live_agent_candidate_provider: CandidateProvider = make_live_agent_provider()


def run_attempt(
    task: dict[str, Any],
    attempt_index: int,
    provider: CandidateProvider,
    repo_root: Path,
) -> dict[str, Any]:
    """Run one attempt for ``task`` through ``provider`` and score it via
    `klt eval`'s own library entry point. Never raises: a provider or
    `klt eval` failure is captured as ``valid: false`` with ``error`` set,
    exactly like a scored-but-failing attempt, so one bad attempt cannot
    abort the whole sweep.

    The provider call is wrapped separately, and more broadly, than the
    `klt eval` call below: `reference_candidate_provider` can only ever
    raise via a malformed task (a `BenchmarkError`-shaped problem that
    should surface, not be swallowed per-attempt), but a live-agent
    provider (issue #1732) can fail in ways `EvalError` was never meant to
    cover -- a timed-out/unreachable agent invocation
    (:class:`AgentInvocationError`), a subprocess error, an unparseable
    response. Catching ``Exception`` here (rather than enumerating every
    provider's own exception types) is what makes "an agent invocation that
    fails/times out is recorded as a failed attempt, not a crash" hold for
    *any* provider, present or future, per issue #1732's Test Plan."""
    start = time.monotonic()
    report: dict[str, Any] | None = None
    valid = False
    error: str | None = None
    try:
        descriptor_arg, candidate_arg = provider(task, attempt_index, repo_root)
    except Exception as exc:  # noqa: BLE001 -- see docstring: any provider failure is a scored attempt failure, never a crash
        error = f"candidate provider failed: {exc}"
    else:
        try:
            report = run_eval(descriptor_arg, candidate_arg)
            valid = bool(report.get("valid"))
        except EvalError as exc:
            error = str(exc)
    wall_clock_s = time.monotonic() - start
    return {
        "attempt": attempt_index,
        "valid": valid,
        "wall_clock_s": wall_clock_s,
        "error": error,
        "objective": (report or {}).get("objective"),
    }


def run_task_attempts(
    task: dict[str, Any],
    n_attempts: int,
    provider: CandidateProvider,
    repo_root: Path,
) -> list[dict[str, Any]]:
    return [run_attempt(task, i, provider, repo_root) for i in range(n_attempts)]


# --------------------------------------------------------------------------
# pass@k
# --------------------------------------------------------------------------


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator (Chen et al. 2021, "Evaluating Large
    Language Models Trained on Code", eq. 1 -- the same estimator HumanEval
    and AnalogCoder's own pass@1/pass@5 reporting use): given ``n``
    independent samples of which ``c`` pass, the probability that at least
    one of a random ``k``-sample subset passes.

    ``k`` must not exceed ``n`` (you cannot estimate pass@5 from 3
    attempts) -- raises :class:`ValueError` rather than silently clamping,
    since a silently-clamped k would misreport a lower budget than
    requested.
    """
    if k > n:
        raise ValueError(f"k ({k}) must not exceed n ({n}) attempts")
    if n - c < k:
        return 1.0
    return 1.0 - math.prod((n - c - i) / (n - i) for i in range(k))


def summarize_task(
    task: dict[str, Any], attempts: list[dict[str, Any]], ks: Iterable[int]
) -> dict[str, Any]:
    n = len(attempts)
    c = sum(1 for a in attempts if a["valid"])
    total_wall_clock_s = sum(a["wall_clock_s"] for a in attempts)
    return {
        "id": task["id"],
        "tier": task.get("tier"),
        "attempts": n,
        "solved": c,
        "pass_at_k": {str(k): pass_at_k(n, c, k) for k in ks if k <= n},
        "total_wall_clock_s": total_wall_clock_s,
    }


def summarize_tier(
    task_summaries: list[dict[str, Any]], ks: Iterable[int]
) -> dict[str, Any]:
    if not task_summaries:
        return {"task_count": 0, "solved_count": 0, "pass_at_k": {}}
    solved_count = sum(1 for t in task_summaries if t["solved"] == t["attempts"])
    pass_at_k_avg: dict[str, float] = {}
    for k in ks:
        key = str(k)
        values = [t["pass_at_k"][key] for t in task_summaries if key in t["pass_at_k"]]
        if values:
            pass_at_k_avg[key] = sum(values) / len(values)
    return {
        "task_count": len(task_summaries),
        "solved_count": solved_count,
        "pass_at_k": pass_at_k_avg,
    }


def run_benchmark(
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    repo_root: Path = REPO_ROOT,
    n_attempts: int = 5,
    ks: Iterable[int] = (1, 5),
    provider: CandidateProvider = reference_candidate_provider,
    provider_name: str = "reference",
) -> dict[str, Any]:
    """Run ``n_attempts`` per task under every task in ``tasks_dir``,
    score with ``provider``, and report pass@k per tier and overall.

    Loads tasks directly (does not re-run :func:`validate_tasks`) -- a
    caller wanting the schema-validity check too should call that
    separately, matching this module's CLI's own two-subcommand split.

    ``provider_name`` is an opaque label echoed back in the response's own
    ``provider`` field (default ``"reference"``, matching ``provider``'s own
    default) -- purely for the report/artifact to self-identify which
    candidate provider produced it (issue #1732: a live-agent run's JSON
    artifact and one-line summary need to say so), never read by this
    module itself.
    """
    ks = sorted(set(ks))
    start = time.monotonic()
    task_summaries = []
    for path in _task_paths(tasks_dir):
        task = load_task(path)
        attempts = run_task_attempts(task, n_attempts, provider, repo_root)
        task_summaries.append(summarize_task(task, attempts, ks))

    by_tier: dict[str, list[dict[str, Any]]] = {tier: [] for tier in TIERS}
    for summary in task_summaries:
        by_tier.setdefault(summary["tier"], []).append(summary)

    return {
        "schema_version": 1,
        "provider": provider_name,
        "n_attempts": n_attempts,
        "ks": ks,
        "tasks": task_summaries,
        "tiers": {
            tier: summarize_tier(summaries, ks) for tier, summaries in by_tier.items()
        },
        "overall": summarize_tier(task_summaries, ks),
        "wall_clock_s": time.monotonic() - start,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    schema_result = validate_tasks(
        Path(args.tasks_dir), Path(args.schema), Path(args.repo_root)
    )
    ref_result = (
        check_reference_solutions(Path(args.tasks_dir), Path(args.repo_root))
        if schema_result["valid"] and not args.schema_only
        else None
    )
    payload = {"schema": schema_result, "reference_solutions": ref_result}
    print(json.dumps(payload, indent=2))
    ok = schema_result["valid"] and (ref_result is None or ref_result["valid"])
    return 0 if ok else 1


def _resolve_provider(args: argparse.Namespace) -> CandidateProvider:
    """Build the `CandidateProvider` `run`'s ``--provider`` flag selected.
    ``live-agent`` is built fresh (rather than reusing the module-level
    :data:`live_agent_candidate_provider`) so ``--agent-timeout-s`` actually
    takes effect."""
    if args.provider == "live-agent":
        return make_live_agent_provider(agent_timeout_s=args.agent_timeout_s)
    return reference_candidate_provider


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_benchmark(
        tasks_dir=Path(args.tasks_dir),
        repo_root=Path(args.repo_root),
        n_attempts=args.attempts,
        ks=args.k,
        provider=_resolve_provider(args),
        provider_name=args.provider,
    )
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    overall = result["overall"]
    pass_at_1 = overall["pass_at_k"].get("1")
    summary_bits = ", ".join(
        f"{tier}={data['solved_count']}/{data['task_count']}"
        for tier, data in result["tiers"].items()
        if data["task_count"]
    )
    print(
        f"design-agent-benchmark ({result['provider']}): overall pass@1={pass_at_1} "
        f"solved={overall['solved_count']}/{overall['task_count']} ({summary_bits})",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="schema-validate the task set and check reference solutions"
    )
    validate_parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    validate_parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH))
    validate_parser.add_argument("--repo-root", default=str(REPO_ROOT))
    validate_parser.add_argument(
        "--schema-only",
        action="store_true",
        help="skip the (slower) reference-solution eval run",
    )
    validate_parser.set_defaults(func=_cmd_validate)

    run_parser = subparsers.add_parser(
        "run", help="run N attempts per task and report pass@k per tier"
    )
    run_parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    run_parser.add_argument("--repo-root", default=str(REPO_ROOT))
    run_parser.add_argument("--attempts", type=int, default=5)
    run_parser.add_argument("--k", type=int, nargs="+", default=[1, 5])
    run_parser.add_argument(
        "--out", default=None, help="also write the JSON report to this path"
    )
    run_parser.add_argument(
        "--provider",
        choices=("reference", "live-agent"),
        default="reference",
        help=(
            "candidate provider: 'reference' (deterministic answer-key "
            "stand-in, the default) or 'live-agent' (drives the S4->S5->S6 "
            "skill chain via the `claude` CLI, issue #1732)"
        ),
    )
    run_parser.add_argument(
        "--agent-timeout-s",
        type=float,
        default=DEFAULT_AGENT_TIMEOUT_S,
        help=(
            "per-attempt wall-clock timeout for the live-agent invocation "
            "(--provider live-agent only; ignored otherwise)"
        ),
    )
    run_parser.set_defaults(func=_cmd_run)

    parsed = parser.parse_args(argv)
    try:
        return parsed.func(parsed)
    except BenchmarkError as exc:
        print(f"design-agent-benchmark: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
