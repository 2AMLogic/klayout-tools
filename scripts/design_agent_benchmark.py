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

    ``--provider interactive-agent``
    (:func:`make_interactive_agent_provider` /
    :data:`interactive_agent_candidate_provider`, issue #1739) closes that
    gap: each attempt gets its own seeded sandbox directory (skill files,
    the task's model library, and a working copy of its `klt sim` request
    documents) plus a ``bin/klt`` shim on ``PATH``, and drives a *multi-turn,
    tool-using* headless session inside it -- so S4 can run real ``klt kb
    search`` queries and S5's Loop A can iterate against real ``klt sim``/
    ``klt sim --op-lint`` corner feedback instead of guessing once. The
    session is bounded by both a wall-clock ceiling and a tool-call budget,
    and scoring always re-derives the testbench from the benchmark's own
    frozen reference request, never from the agent's (writable) sandbox
    copy.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


def _reference_request_refs(
    task: dict[str, Any], repo_root: Path
) -> list[tuple[str, dict[str, Any]]]:
    """Every distinct ``args.request`` reference the task's reference
    ``eval_descriptor`` names, paired with that request document's own
    loaded JSON, in descriptor order. Deduplicated across the descriptor's
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
    refs: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in entries:
        args = entry.get("args") or {}
        request_ref = args.get("request")
        if not isinstance(request_ref, str) or request_ref in seen:
            continue
        seen.add(request_ref)
        sim_request = json.loads((reference_dir / request_ref).read_text())
        refs.append((request_ref, sim_request))
    return refs


def _reference_testbenches(
    task: dict[str, Any], repo_root: Path
) -> list[dict[str, Any]]:
    """The block's declared `klt sim` request document(s) -- analysis,
    corners, and measurements only, **never the reference netlist body** --
    that the live agent's own candidate netlist(s) must satisfy. Keyed by
    the netlist stem each request names."""
    return [
        {"netlist_stem": Path(sim_request["netlist"]).stem, "sim_request": sim_request}
        for _ref, sim_request in _reference_request_refs(task, repo_root)
    ]


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


# --------------------------------------------------------------------------
# Interactive (multi-turn, tool-using) agent provider (issue #1739)
# --------------------------------------------------------------------------

#: Per-attempt wall-clock ceiling for an *interactive* session (`run` mode's
#: ``--agent-timeout-s`` default when ``--provider interactive-agent`` is
#: selected). Deliberately far larger than
#: :data:`DEFAULT_AGENT_TIMEOUT_S`: a single Loop-A iteration here is a real
#: `klt sim` corner sweep (18 ngspice processes for the shipped easy tier),
#: and the whole point of this provider is that the agent runs several.
DEFAULT_INTERACTIVE_TIMEOUT_S = 1800.0

#: Per-attempt tool-call ceiling. The second half of the session bound: a
#: session that is *making calls* but converging nowhere burns wall clock
#: slowly and would otherwise sit until the timeout; one that is stuck in a
#: tight retry loop trips this first. Either bound tripping ends the attempt
#: as a failure (:class:`AgentInvocationError`), never as a crashed sweep.
DEFAULT_TOOL_CALL_BUDGET = 80

#: The tool surface the interactive session is allowed, in `claude
#: --allowedTools` syntax. `klt kb` covers S4's "numeric first" query flow
#: (`.claude/skills/design-topology-selection/SKILL.md`), `klt sim` covers
#: S5's Loop A and its `--op-lint` pre-check
#: (`.claude/skills/design-sizing/SKILL.md`); the file tools are what let the
#: session author and re-run a netlist across turns. Anything not listed
#: here is denied by the CLI itself rather than prompted for -- headless
#: runs must never block on a permission prompt.
INTERACTIVE_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(klt kb:*)",
    "Bash(klt sim:*)",
    "Bash(cat:*)",
    "Bash(ls:*)",
)

#: Permission mode for the headless session: edits inside its own sandbox
#: are auto-accepted, every other tool request falls back to the allow-list
#: above. Never ``bypassPermissions`` -- the allow-list *is* the sandbox's
#: second wall, after the per-attempt directory itself.
INTERACTIVE_PERMISSION_MODE = "acceptEdits"

#: Files the session driver writes into each attempt's sandbox: the raw
#: stream-json event log (one JSON object per line), the CLI's stderr, a
#: small end-of-session summary, and the human-readable task brief.
TRANSCRIPT_FILENAME = "agent-transcript.jsonl"
SESSION_STDERR_FILENAME = "agent-stderr.log"
SESSION_SUMMARY_FILENAME = "agent-session.json"
SANDBOX_BRIEF_FILENAME = "TASK.md"


@dataclass(frozen=True)
class AgentSessionRequest:
    """Everything an interactive-session invoker needs: the opening prompt,
    the per-attempt sandbox to run in (already seeded by
    :func:`_seed_agent_sandbox`), and the two bounds the session must be
    held to."""

    prompt: str
    workdir: Path
    timeout_s: float = DEFAULT_INTERACTIVE_TIMEOUT_S
    tool_call_budget: int = DEFAULT_TOOL_CALL_BUDGET
    allowed_tools: tuple[str, ...] = INTERACTIVE_ALLOWED_TOOLS


@dataclass
class AgentSessionResult:
    """What a finished interactive session reports back. ``text`` is the
    final response (the fence fallback is parsed out of it when the session
    wrote no netlist file); the counters are recorded as run evidence, not
    scored."""

    text: str
    tool_calls: int = 0
    turns: int = 0
    transcript_path: Path | None = None
    wall_clock_s: float = 0.0


#: An interactive-session invoker. The swappable extension point tests stub
#: out; :func:`_default_invoke_interactive_agent` is the real,
#: `claude`-CLI-backed implementation.
InteractiveAgentInvoker = Callable[[AgentSessionRequest], AgentSessionResult]


def _write_sandbox_klt_shim(workdir: Path, repo_root: Path) -> Path:
    """Put a ``klt`` on the session's ``PATH`` that is guaranteed to be *this
    checkout's*.

    `klt kb` resolves its corpus relative to the package it was imported
    from (``klayout_tools.kb.DEFAULT_KB_ROOT``), so an installed-elsewhere
    `klt` (a `uv tool install`, a virtualenv on the runner's PATH) reports
    "kb entries directory not found" from inside a sandbox and S4's whole
    query flow is dead on arrival. The shim runs the benchmark process's own
    interpreter against ``<repo_root>/src``, so `klt kb`/`klt sim` behave
    exactly as they do from a repo checkout regardless of cwd.
    """
    bin_dir = workdir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    entry = bin_dir / "_klt_entry.py"
    entry.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root / 'src')!r})\n"
        "from klayout_tools.cli import main\n"
        "raise SystemExit(main())\n"
    )
    shim = bin_dir / "klt"
    shim.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(entry))} "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def _seed_agent_sandbox(
    task: dict[str, Any],
    repo_root: Path,
    workdir: Path,
    *,
    timeout_s: float = DEFAULT_INTERACTIVE_TIMEOUT_S,
    tool_call_budget: int = DEFAULT_TOOL_CALL_BUDGET,
    allowed_tools: tuple[str, ...] = INTERACTIVE_ALLOWED_TOOLS,
) -> dict[str, Any]:
    """Seed one attempt's sandbox and return its layout.

    What lands in ``workdir``:

    - ``bin/klt`` -- this checkout's CLI (:func:`_write_sandbox_klt_shim`).
    - ``skills/design-*.md`` -- the S4/S5/S6 skill files, for the agent to
      ``Read`` itself rather than have pasted into a prompt.
    - the task's own model library, copied verbatim.
    - a **working copy** of each `klt sim` request the task's reference
      ``eval_descriptor`` names, rewritten to point at the netlist file the
      agent is asked to author (``<stem>.spice``) and at the local model
      library, so `klt sim <request>.json` runs the real corner sweep the
      moment the agent writes a netlist.
    - ``TASK.md`` -- the same brief the opening prompt carries, on disk so a
      long session can re-read it.

    The reference *solution* is never seeded -- only the testbench contract.
    And the seeded request copy is the agent's feedback loop, not the
    grading contract: :func:`make_interactive_agent_provider` re-derives the
    scored descriptor from the repo's frozen reference request, so editing
    the sandbox copy changes nothing except what the agent sees.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _write_sandbox_klt_shim(workdir, repo_root)

    skills_dir = workdir / "skills"
    skills_dir.mkdir(exist_ok=True)
    skill_files: list[str] = []
    for rel_path in SKILL_CHAIN_PATHS:
        source = repo_root / rel_path
        try:
            body = source.read_text()
        except OSError as exc:
            raise AgentInvocationError(f"skill file not found: {source}") from exc
        name = f"{rel_path.parent.name}.md"
        (skills_dir / name).write_text(body)
        skill_files.append(f"skills/{name}")

    reference_dir = (repo_root / task["reference"]["eval_descriptor"]).parent
    requests: list[dict[str, str]] = []
    model_libs: list[str] = []
    for request_ref, sim_request in _reference_request_refs(task, repo_root):
        local = copy.deepcopy(sim_request)
        stem = Path(sim_request["netlist"]).stem
        local["netlist"] = f"{stem}.spice"
        models = local.get("models")
        if isinstance(models, dict) and isinstance(models.get("lib"), str):
            lib_source = Path(models["lib"])
            if not lib_source.is_absolute():
                lib_source = reference_dir / lib_source
            if lib_source.is_file():
                shutil.copyfile(lib_source, workdir / lib_source.name)
                local["models"] = {**models, "lib": lib_source.name}
                if lib_source.name not in model_libs:
                    model_libs.append(lib_source.name)
        request_name = Path(request_ref).name
        (workdir / request_name).write_text(json.dumps(local, indent=2) + "\n")
        requests.append({"name": request_name, "netlist_stem": stem})

    layout: dict[str, Any] = {
        "task_id": task["id"],
        "workdir": workdir,
        "repo_root": repo_root,
        "stems": [Path(p).stem for p in task["reference"]["netlists"]],
        "requests": requests,
        "skill_files": skill_files,
        "model_libs": model_libs,
        "timeout_s": timeout_s,
        "tool_call_budget": tool_call_budget,
        "allowed_tools": tuple(allowed_tools),
    }
    (workdir / SANDBOX_BRIEF_FILENAME).write_text(
        _build_interactive_agent_prompt(task, layout)
    )
    return layout


def _build_interactive_agent_prompt(
    task: dict[str, Any], layout: dict[str, Any]
) -> str:
    """The opening prompt for a multi-turn session in a seeded sandbox.

    Unlike :func:`_build_live_agent_prompt` (which must paste the skill text
    and testbench JSON inline, because a single-turn completion has no way
    to fetch them), this points the agent at files it can read for itself
    and at the tools it may call -- then states the deliverable, the
    body-only netlist constraint, and both session bounds. Same
    never-leak-the-answer-key rule: nothing here or in the sandbox carries
    the reference netlist's body.
    """
    repo_root = Path(layout["repo_root"])
    testbenches = _reference_testbenches(task, repo_root)
    device_model_contract = _device_model_contract(task, repo_root, testbenches)

    skill_lines = "\n".join(f"  - ./{name}" for name in layout["skill_files"])
    request_lines = "\n".join(
        f"  - ./{entry['name']}  (scores ./{entry['netlist_stem']}.spice)"
        for entry in layout["requests"]
    )
    model_lines = "\n".join(f"  - ./{name}" for name in layout["model_libs"]) or (
        "  (this task's request points at a PDK model library, not a local file)"
    )
    deliverable_lines = "\n".join(f"  - ./{stem}.spice" for stem in layout["stems"])
    first_request = layout["requests"][0]["name"] if layout["requests"] else "<request>"

    return f"""You are the design agent for klayout-tools' staged analog design
pipeline (docs/design/design-pipeline.md), driving stages S4 (topology
selection) -> S5 (sizing) -> S6 (netlist authoring) for the block below.

This is an interactive session with live tools, not a one-shot answer.
Your current directory is a private sandbox for this attempt; work in it
freely. It is already seeded with everything you need:

S4/S5/S6 procedures -- read these first, they are this repo's own skills:
{skill_lines}

Device model library (read it; do not write `.model` cards of your own):
{model_lines}

`klt sim` request document(s) -- the frozen testbench contract you are
scored against, already pointed at the netlist file(s) you must author:
{request_lines}

=== Tools you may call (everything else is denied) ===

- `klt kb list|show <id>|search ... --format json` -- this repo's topology
  knowledge base. Per the S4 skill, query it **numeric first**:
  `klt kb search --where <figure><op><value> [--where ...] [--pdk <pdk>]
  --format json` filters on entries' measured figures, a far stronger fit
  signal than a keyword hit; fall back to a keyword query only after that.
- `klt sim {first_request} --format json` -- the real corner sweep. This is
  S5's Loop A: size, run it, read each measurement's per-corner
  `status`/`worst_case`, resize, repeat until the aggregate `status` is
  `"pass"` across the whole declared matrix. Do not stop at one blind guess.
- `klt sim {first_request} --op-lint --format json` -- the per-device
  operating-point sanity lint. Much cheaper than a full sweep; run it first
  when a fresh sizing may simply be biased into the wrong region.
- `Read`/`Write`/`Edit`/`Glob`/`Grep`, plus `cat`/`ls`, inside this
  directory.

=== Deliverable ===

Write your final netlist body to exactly these path(s) in this directory,
and leave them in place when you finish:
{deliverable_lines}

Per S6's "circuit body, not a full deck" constraint, each file is a
**circuit body only**: device/source instantiations, with no
`.control`/`.end`/`.model`/`.lib`/`.include` cards of your own. It is
spliced, unmodified, into the request document(s) above -- so every node a
`.meas` line names (e.g. `out`, `vout#branch`) must exist, literally
spelled, in your netlist, and any voltage source a `dc` analysis sweeps by
name must exist as its own independent source of that exact name.
If you are somehow unable to write files, emit each netlist instead as a
fenced block labeled ```spice:<stem> in your final message.

Editing the seeded request document or model library only changes what
*you* see: scoring re-runs the benchmark's own frozen copy of both against
whatever netlist file you leave behind. Weakening them locally cannot make
a gate pass, it only blinds your own feedback loop.

=== Session bounds ===

This session is bounded at about {int(layout["timeout_s"])} seconds of wall
clock and {layout["tool_call_budget"]} tool calls; exceeding either ends
the attempt as a failure. A full corner sweep is not free -- budget your
Loop A iterations, and make sure your best netlist is written to disk
before you run low rather than saving it for a final message.

=== Block to design ===

Task id: {task["id"]}
Title: {task["title"]}
Tier: {task["tier"]}
PDK (name only -- see the device model contract below): {task["pdk"]}
Description: {task["description"]}
Block spec (S3 output, your S4 input):
{json.dumps(task["block_spec"], indent=2)}

=== Device model contract (harness-provided, not part of your design) ===

{device_model_contract}
"""


def _iter_stream_events(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _default_invoke_interactive_agent(
    request: AgentSessionRequest,
) -> AgentSessionResult:
    """Drive one bounded, multi-turn, tool-using headless session.

    Shells out to the `claude` CLI in streaming print mode (``-p ...
    --output-format stream-json --verbose``) with the session's cwd set to
    the attempt's sandbox and that sandbox's ``bin/`` first on ``PATH``, so
    `klt kb`/`klt sim` resolve to this checkout. Same headless-invocation
    convention (and the same :data:`AGENT_CLI_ENV` override) the single-turn
    :func:`_default_invoke_agent` uses -- tests point it at a stub emitting
    the same stream-json event shape.

    Streaming (rather than `--output-format json`) is what makes the session
    *bounded*: every event is appended to ``agent-transcript.jsonl`` as it
    arrives, tool calls are counted as they happen, and the child is killed
    the moment either the tool-call budget or the wall-clock deadline is
    hit. Both cases raise :class:`AgentInvocationError`, which
    :func:`run_attempt` records as a failed attempt rather than letting it
    abort the sweep.
    """
    cli = os.environ.get(AGENT_CLI_ENV, "claude")
    cmd = [
        cli,
        "-p",
        request.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        INTERACTIVE_PERMISSION_MODE,
        "--allowedTools",
        *request.allowed_tools,
    ]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [str((request.workdir / "bin").resolve()), env.get("PATH", "")]
    )
    transcript_path = request.workdir / TRANSCRIPT_FILENAME
    stderr_path = request.workdir / SESSION_STDERR_FILENAME

    start = time.monotonic()
    timed_out = threading.Event()
    tool_calls = 0
    turns = 0
    result_text: str | None = None
    assistant_text: list[str] = []
    budget_exceeded = False
    session_error: str | None = None

    try:
        stderr_file = stderr_path.open("w")
    except OSError as exc:  # pragma: no cover -- unwritable sandbox
        raise AgentInvocationError(f"cannot write session log: {exc}") from exc

    try:
        try:
            proc = subprocess.Popen(  # noqa: S603 -- caller-controlled agent CLI
                cmd,
                cwd=str(request.workdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise AgentInvocationError(
                f"agent CLI {cli!r} not found on PATH -- install it, or point "
                f"{AGENT_CLI_ENV} at a stub for testing"
            ) from exc

        watchdog = threading.Timer(request.timeout_s, lambda: _kill(proc, timed_out))
        watchdog.start()
        try:
            with transcript_path.open("w") as transcript:
                assert proc.stdout is not None
                for line in proc.stdout:
                    transcript.write(line)
                    transcript.flush()
                    event = _iter_stream_events(line)
                    if event is None:
                        continue
                    if event.get("type") == "assistant":
                        turns += 1
                        content = (event.get("message") or {}).get("content") or []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                tool_calls += 1
                            elif block.get("type") == "text" and isinstance(
                                block.get("text"), str
                            ):
                                assistant_text.append(block["text"])
                    elif event.get("type") == "result":
                        if isinstance(event.get("result"), str):
                            result_text = event["result"]
                        if event.get("is_error"):
                            # An unusable session (missing credentials, an API
                            # error) is reported as `is_error` on an otherwise
                            # ordinary result event -- and the CLI still exits
                            # 0. Observed directly against `claude` 2.1.221
                            # ("Not logged in - Please run /login"). Without
                            # this, the attempt would be misattributed as "the
                            # agent produced no netlist" rather than "the agent
                            # never ran".
                            session_error = (
                                result_text or "agent reported an unspecified error"
                            )
                    if tool_calls > request.tool_call_budget:
                        budget_exceeded = True
                        proc.kill()
                        break
        finally:
            watchdog.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait()
    finally:
        stderr_file.close()

    wall_clock_s = time.monotonic() - start
    if timed_out.is_set():
        raise AgentInvocationError(
            f"interactive agent session timed out after {request.timeout_s}s "
            f"(transcript: {transcript_path})"
        )
    if budget_exceeded:
        raise AgentInvocationError(
            f"interactive agent session exceeded its tool-call budget "
            f"({request.tool_call_budget}) (transcript: {transcript_path})"
        )
    if proc.returncode != 0:
        stderr_tail = ""
        try:
            stderr_tail = stderr_path.read_text().strip()[-2000:]
        except OSError:  # pragma: no cover
            pass
        raise AgentInvocationError(
            f"interactive agent session failed (exit {proc.returncode}): {stderr_tail}"
        )
    if session_error is not None:
        raise AgentInvocationError(
            f"interactive agent session reported an error: "
            f"{session_error.strip()[:2000]} (transcript: {transcript_path})"
        )

    return AgentSessionResult(
        text=result_text if result_text is not None else "\n".join(assistant_text),
        tool_calls=tool_calls,
        turns=turns,
        transcript_path=transcript_path,
        wall_clock_s=wall_clock_s,
    )


def _kill(proc: subprocess.Popen[str], flag: threading.Event) -> None:
    flag.set()
    try:
        proc.kill()
    except OSError:  # pragma: no cover -- already reaped
        pass


def _collect_sandbox_netlists(
    workdir: Path, stems: list[str], response_text: str
) -> dict[str, str]:
    """Gather the session's proposed netlist(s): the files it was asked to
    leave in its sandbox first, falling back to a labeled ```spice:<stem>
    fence in its final message (the single-turn provider's contract) for any
    stem with no file. Raises :class:`AgentInvocationError` -- an
    attempt-level failure, never a crash -- when a required stem is
    available neither way."""
    found: dict[str, str] = {}
    for stem in stems:
        try:
            body = (workdir / f"{stem}.spice").read_text()
        except OSError:
            continue
        if body.strip():
            found[stem] = body if body.endswith("\n") else body + "\n"
    missing = [stem for stem in stems if stem not in found]
    if missing:
        try:
            found.update(_extract_labeled_netlists(response_text, missing))
        except AgentInvocationError as exc:
            raise AgentInvocationError(
                f"agent session produced no netlist for: {', '.join(missing)} -- "
                f"expected a non-empty '<stem>.spice' in its working directory "
                f"({workdir}), or a labeled '```spice:<stem>' fence in its final "
                f"response ({exc})"
            ) from exc
    return found


def _attempt_sandbox(
    task: dict[str, Any], attempt_index: int, sandbox_root: Path | None
) -> Path:
    """A fresh, unshared directory for one attempt. Always a `mkdtemp`, even
    under an explicit ``sandbox_root``, so a re-run of the same task/attempt
    index can never inherit the previous run's half-written files and two
    concurrent attempts can never collide."""
    prefix = f"{task['id']}-attempt{attempt_index}-"
    if sandbox_root is not None:
        root = Path(sandbox_root)
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    return Path(tempfile.mkdtemp(prefix=f"design-agent-benchmark-{prefix}"))


def make_interactive_agent_provider(
    *,
    invoke_agent: InteractiveAgentInvoker = _default_invoke_interactive_agent,
    agent_timeout_s: float = DEFAULT_INTERACTIVE_TIMEOUT_S,
    tool_call_budget: int = DEFAULT_TOOL_CALL_BUDGET,
    sandbox_root: Path | None = None,
    allowed_tools: tuple[str, ...] = INTERACTIVE_ALLOWED_TOOLS,
) -> CandidateProvider:
    """Build the multi-turn, tool-using :data:`CandidateProvider` (issue
    #1739) -- the fuller counterpart to :func:`make_live_agent_provider`'s
    single-turn completion, not a replacement for it (``--provider
    live-agent`` stays selectable, and stays the cheaper single-shot
    regression signal for the same skill chain).

    Per attempt:

    1. Mint a private sandbox (:func:`_attempt_sandbox`) and seed it
       (:func:`_seed_agent_sandbox`) with the S4/S5/S6 skill files, the
       task's model library, a working copy of its `klt sim` request(s), and
       a ``bin/klt`` shim bound to this checkout.
    2. Run one bounded multi-turn session in it (``invoke_agent``; the
       default :func:`_default_invoke_interactive_agent` shells out to the
       `claude` CLI in streaming mode). The agent can run real `klt kb`
       queries for S4 and real `klt sim`/`klt sim --op-lint` sweeps for S5's
       Loop A, iterating against actual corner feedback.
    3. Collect whatever netlist(s) it left behind
       (:func:`_collect_sandbox_netlists`) and synthesize a `klt eval`
       descriptor pointing at them
       (:func:`_build_live_agent_descriptor`) -- rebuilt from the repo's
       **frozen reference request**, so a session that edited its sandbox
       copy of the testbench is still scored against the real contract.

    Sandboxes are never cleaned up automatically (same posture as the
    single-turn provider's scratch dirs): the netlist, the `klt sim`
    artifacts, and ``agent-transcript.jsonl`` are the evidence for what an
    attempt actually did, and an ephemeral CI runner reclaims them at job
    end anyway.
    """

    def provider(
        task: dict[str, Any], attempt_index: int, repo_root: Path
    ) -> tuple[str, str | None]:
        workdir = _attempt_sandbox(task, attempt_index, sandbox_root)
        layout = _seed_agent_sandbox(
            task,
            repo_root,
            workdir,
            timeout_s=agent_timeout_s,
            tool_call_budget=tool_call_budget,
            allowed_tools=allowed_tools,
        )
        session = invoke_agent(
            AgentSessionRequest(
                prompt=_build_interactive_agent_prompt(task, layout),
                workdir=workdir,
                timeout_s=agent_timeout_s,
                tool_call_budget=tool_call_budget,
                allowed_tools=tuple(allowed_tools),
            )
        )
        netlists_by_stem = _collect_sandbox_netlists(
            workdir, layout["stems"], session.text
        )
        eval_dir = workdir / ".eval"
        eval_dir.mkdir(parents=True, exist_ok=True)
        descriptor = _build_live_agent_descriptor(
            task, repo_root, netlists_by_stem, eval_dir
        )
        (workdir / SESSION_SUMMARY_FILENAME).write_text(
            json.dumps(
                {
                    "task_id": task["id"],
                    "attempt": attempt_index,
                    "tool_calls": session.tool_calls,
                    "turns": session.turns,
                    "wall_clock_s": session.wall_clock_s,
                    "netlists": sorted(netlists_by_stem),
                    "tool_call_budget": tool_call_budget,
                    "timeout_s": agent_timeout_s,
                },
                indent=2,
            )
            + "\n"
        )
        return json.dumps(descriptor), None

    return provider


#: The default-configured interactive provider -- usable directly by callers
#: that don't need CLI-flag-driven timeout/budget/sandbox wiring (`run`'s own
#: ``--provider interactive-agent`` path builds a fresh one via
#: :func:`make_interactive_agent_provider` so those flags take effect).
interactive_agent_candidate_provider: CandidateProvider = (
    make_interactive_agent_provider()
)


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


def _resolve_agent_timeout(provider: str, value: float | None) -> float:
    """The per-attempt wall-clock ceiling for ``provider``. ``--agent-timeout-s``
    defaults to ``None`` rather than a number so each agent provider can carry
    its own default: a single-turn completion's 600 s is far too tight for a
    multi-turn session whose individual Loop-A iterations are real corner
    sweeps, and a shared default would silently mean one or the other."""
    if value is not None:
        return value
    if provider == "interactive-agent":
        return DEFAULT_INTERACTIVE_TIMEOUT_S
    return DEFAULT_AGENT_TIMEOUT_S


def _resolve_provider(args: argparse.Namespace) -> CandidateProvider:
    """Build the `CandidateProvider` `run`'s ``--provider`` flag selected.
    The agent-backed providers are built fresh (rather than reusing the
    module-level singletons) so ``--agent-timeout-s`` and friends actually
    take effect."""
    timeout_s = _resolve_agent_timeout(args.provider, args.agent_timeout_s)
    if args.provider == "live-agent":
        return make_live_agent_provider(agent_timeout_s=timeout_s)
    if args.provider == "interactive-agent":
        return make_interactive_agent_provider(
            agent_timeout_s=timeout_s,
            tool_call_budget=args.agent_tool_budget,
            sandbox_root=(
                Path(args.agent_sandbox_root) if args.agent_sandbox_root else None
            ),
        )
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
        choices=("reference", "live-agent", "interactive-agent"),
        default="reference",
        help=(
            "candidate provider: 'reference' (deterministic answer-key "
            "stand-in, the default), 'live-agent' (one single-turn `claude` "
            "completion seeded with the S4->S5->S6 skill text, issue #1732), "
            "or 'interactive-agent' (a bounded multi-turn tool-using session "
            "per attempt, with live `klt kb`/`klt sim` access in a private "
            "per-attempt sandbox, issue #1739)"
        ),
    )
    run_parser.add_argument(
        "--agent-timeout-s",
        type=float,
        default=None,
        help=(
            "per-attempt wall-clock timeout for an agent-backed provider "
            f"(default: {DEFAULT_AGENT_TIMEOUT_S:g}s for --provider "
            f"live-agent, {DEFAULT_INTERACTIVE_TIMEOUT_S:g}s for --provider "
            "interactive-agent; ignored by --provider reference)"
        ),
    )
    run_parser.add_argument(
        "--agent-tool-budget",
        type=int,
        default=DEFAULT_TOOL_CALL_BUDGET,
        help=(
            "per-attempt tool-call ceiling for --provider interactive-agent; "
            "a session that exceeds it is killed and recorded as a failed "
            "attempt (ignored by the other providers)"
        ),
    )
    run_parser.add_argument(
        "--agent-sandbox-root",
        default=None,
        help=(
            "parent directory for --provider interactive-agent's per-attempt "
            "sandboxes (default: the system temp dir). Each attempt still "
            "gets its own fresh subdirectory; they are left in place as run "
            "evidence (netlist, `klt sim` artifacts, session transcript)"
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
