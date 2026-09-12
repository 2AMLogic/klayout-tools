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
    provider* (pluggable; defaults to :func:`reference_candidate_provider`,
    a deterministic stand-in that always hands back the task's own known-
    good reference solution -- see its docstring for what this milestone
    does and does not prove), score each attempt with ``klt eval``, and
    report pass@1/pass@k per tier plus overall, using the standard unbiased
    pass@k estimator from Chen et al. 2021 ("Evaluating Large Language
    Models Trained on Code").

Wiring a live S4->S6->S10 skill-driven candidate provider (an agent that
actually proposes a *different* netlist per attempt) is intentionally out
of scope for this module's first cut -- see README.md's "Known limitation"
section and the tracked follow-up issue.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
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
    Tracking *that* regression (the harness's actual purpose per issue
    #1719) requires a candidate provider that drives a live agent through
    those skills and returns each attempt's own proposed netlist/request as
    the ``candidate_arg`` substitution -- see README.md's "Known
    limitation" section for the tracked follow-up.
    """
    del attempt_index  # every attempt is identical for this provider
    descriptor_path = repo_root / task["reference"]["eval_descriptor"]
    return str(descriptor_path), None


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
    abort the whole sweep."""
    start = time.monotonic()
    try:
        descriptor_arg, candidate_arg = provider(task, attempt_index, repo_root)
        report = run_eval(descriptor_arg, candidate_arg)
        valid = bool(report.get("valid"))
        error = None
    except EvalError as exc:
        report = None
        valid = False
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
) -> dict[str, Any]:
    """Run ``n_attempts`` per task under every task in ``tasks_dir``,
    score with ``provider``, and report pass@k per tier and overall.

    Loads tasks directly (does not re-run :func:`validate_tasks`) -- a
    caller wanting the schema-validity check too should call that
    separately, matching this module's CLI's own two-subcommand split.
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


def _cmd_run(args: argparse.Namespace) -> int:
    result = run_benchmark(
        tasks_dir=Path(args.tasks_dir),
        repo_root=Path(args.repo_root),
        n_attempts=args.attempts,
        ks=args.k,
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
        f"design-agent-benchmark: overall pass@1={pass_at_1} "
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
    run_parser.set_defaults(func=_cmd_run)

    parsed = parser.parse_args(argv)
    try:
        return parsed.func(parsed)
    except BenchmarkError as exc:
        print(f"design-agent-benchmark: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
