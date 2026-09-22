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
    Schema-validate every task under a tasks directory, confirm each task's
    own reference solution passes its own ``eval_descriptor`` (the "task
    cannot be unsatisfiable" acceptance check from issue #1719), and -- for
    every task shipping a ``<id>.mutations.json`` beside it -- confirm that
    each declared mutant of its reference netlist is *rejected* by that same
    gate (the "task cannot be vacuously satisfiable" discrimination check,
    issue #2262; see :func:`check_mutation_gates`).

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
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from klayout_tools._paths import _resolve_relative  # noqa: E402
from klayout_tools._provenance import sha256_file  # noqa: E402
from klayout_tools._vendor import mutation_variants as _mutation_variants  # noqa: E402
from klayout_tools.eval import EvalError, _load_json_arg, run_eval  # noqa: E402
from klayout_tools.sim import SimError, _resolve_models_lib  # noqa: E402

DEFAULT_TASKS_DIR = REPO_ROOT / "benchmarks" / "design-agent" / "tasks"
DEFAULT_SCHEMA_PATH = (
    REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "task.schema.json"
)

#: Filename suffix of a task's optional mutation-gate document (issue #2262):
#: ``<tasks_dir>/<task-id>.mutations.json``, validated against
#: :data:`DEFAULT_MUTATIONS_SCHEMA_PATH`. Deliberately lives *beside* the task
#: rather than under a separate directory, so a task and its own
#: discrimination gate move together -- and is excluded from
#: :func:`_task_paths` so it is never mistaken for a task descriptor.
MUTATIONS_SUFFIX = ".mutations.json"

DEFAULT_MUTATIONS_SCHEMA_PATH = (
    REPO_ROOT / "benchmarks" / "design-agent" / "schema" / "mutations.schema.json"
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
    """Every task descriptor under ``tasks_dir``, in filename order.

    Skips ``*.mutations.json`` (issue #2262): a task's mutation-gate
    document lives beside the task it belongs to, so a plain ``*.json`` glob
    would otherwise hand one to :func:`load_task` and schema-fail it as a
    malformed task."""
    if not tasks_dir.is_dir():
        raise BenchmarkError(f"tasks directory not found: {tasks_dir}")
    return sorted(
        path
        for path in tasks_dir.glob("*.json")
        if not path.name.endswith(MUTATIONS_SUFFIX)
    )


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

    Also populates the cross-step reference-solution cache (issue #1783,
    :func:`_write_reference_cache`) for every task it evaluates. The `run`
    subcommand's deterministic :func:`reference_candidate_provider` path
    reads this cache before re-simulating (see :func:`run_attempt`), so a
    `run` step that shares a checkout with a `validate` step that already
    ran (``.github/workflows/design-agent-benchmark.yml`` runs both as
    steps of the same job) does not pay for the same real ``ngspice``
    corner sweep twice. A cache write failure is swallowed -- it is purely
    a CI cost optimization, never a reason to fail this check.
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
        _write_reference_cache(
            repo_root,
            task["id"],
            _reference_solution_fingerprint(task, repo_root),
            valid=valid,
            error=error,
            eval_report=report,
        )
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
# Per-task mutation gates (issue #2262)
#
# `check_reference_solutions` above proves a task is *satisfiable* (its own
# reference solution passes its own gate). It says nothing about whether the
# gate is *discriminating*: a threshold that accepts everything passes that
# check just as happily. This section closes that gap the way AHRR
# (https://github.com/ZijD/AHRR, ICCAD'26, MIT -- methodology reference
# only, nothing reproduced from it) does for RTL: every task may ship a
# `<id>.mutations.json` declaring deliberately-wrong variants of its own
# reference netlist, and `validate` requires the task's own
# `reference.eval_descriptor` gate to REJECT every one of them.
#
# Three details carried over deliberately (issue #2254):
#   1. A mutant's `find` anchor must match exactly one site in the netlist it
#      targets, or validation errors out -- never a silently-unapplied
#      mutant.
#   2. A mutant that survives must be declared equivalent, with a written
#      reason AND a code anchor; a declaration whose anchor no longer occurs
#      reverts to SURVIVED, and one with an explicitly null anchor is
#      reported UNVERIFIED. Neither silences a survivor.
#   3. A task shipping a mutations file must declare at least one mutant --
#      `targeted[]` is `minItems: 1` in the schema, so no task passes this
#      gate vacuously.
#
# The find/replace-and-apply step itself reuses the byte-exact mutation seam
# already vendored for `klt functional-verification --mutations` (issue
# #1592, `klayout_tools._vendor.mutation_variants`) rather than growing a
# second applier -- see `_apply_mutant_edits`.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _NetlistEdit:
    """One byte-exact netlist edit, shaped to satisfy the vendored
    ``mutation_variants.MutationProposal`` protocol
    (``index``/``file``/``line``/``original_code``/``mutated_code``) so the
    already-tested anchoring machinery can resolve it -- the benchmark-side
    analogue of ``functional_verification._ProposalRecord``.

    ``line`` is *derived* (see :func:`_apply_mutant_edits`) rather than
    declared in the mutations document: a mutations file anchors on text that
    must be unique in the whole netlist, which is a stronger condition than
    the vendored protocol's "unique on the declared line", and asking a
    mutant author to keep a line number in sync with an edited netlist would
    only add a way for the file to go stale.
    """

    index: int
    file: str
    line: int
    original_code: str
    mutated_code: str


def _exact_text_list(value: Any, field: str, label: str) -> list[str]:
    """Normalize a schema ``exactText`` value (a string, or a list of them)
    into a list. Raises :class:`BenchmarkError` for anything else -- the
    schema already rejects those shapes, so this is the belt-and-braces path
    for a caller that skipped validation."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise BenchmarkError(f"{label}: {field} must be a string or a list of strings")


def _mutant_edits(mutant: dict[str, Any], label: str) -> list[tuple[str, str]]:
    """Pair up a targeted mutant's ``find``/``replace`` spans. Both may be a
    bare string (one edit) or equal-length lists (one conceptual mutant that
    takes several coordinated edits, e.g. "remove both cascode devices")."""
    finds = _exact_text_list(mutant.get("find"), "find", label)
    replaces = _exact_text_list(mutant.get("replace"), "replace", label)
    if len(finds) != len(replaces):
        raise BenchmarkError(
            f"{label}: find/replace must have the same number of entries "
            f"(got {len(finds)} find, {len(replaces)} replace)"
        )
    return list(zip(finds, replaces, strict=True))


def _apply_mutant_edits(
    netlist_path: Path, edits: list[tuple[str, str]], label: str
) -> None:
    """Apply one mutant's every ``find``/``replace`` span to ``netlist_path``
    in place, byte-exactly, raising :class:`BenchmarkError` if any anchor is
    not unique.

    Drives the vendored mutation seam's own building blocks
    (``_all_offsets``/``_line_at`` to locate and uniqueness-check the anchor,
    ``MutationVariantPlan._resolve_one`` to resolve it to an exact byte span,
    ``_replace_exact`` to splice it) exactly as
    ``functional_verification._resolve_mutation_proposals`` does, rather than
    re-deriving byte-exact replacement here. Two deliberate differences from
    that caller, both because a *netlist* mutant is a different unit than an
    RTL proposal:

    * **Whole-file anchor uniqueness.** ``_resolve_one`` requires the anchor
      to be unique on a declared line; issue #2254's requirement 1 is
      stronger -- unique in the whole netlist -- so that is checked first and
      the line is then derived from the single match.
    * **All of one mutant's spans are applied together.** ``_resolve_one``
      resolves against pristine bytes, so the resolved spans are spliced in
      descending offset order (later spans first), leaving every
      not-yet-applied span's offsets still valid. Overlapping spans are
      rejected rather than silently applied in some order.

    A deleted device is expressed by commenting its card out, never by an
    empty ``replace``: the vendored ``_resolve_one`` requires a non-empty,
    different replacement (and the schema mirrors that), and a commented-out
    card is both netlist-equivalent to deletion for ngspice and visible
    evidence in the mutated file of what the mutant removed.
    """
    source = netlist_path.read_bytes()
    resolved: list[_mutation_variants.ResolvedMutation] = []
    for position, (find, replace) in enumerate(edits, start=1):
        needle = find.encode("utf-8")
        offsets = _mutation_variants._all_offsets(source, needle)
        if len(offsets) != 1:
            raise BenchmarkError(
                f"{label}: find[{position}] matches {len(offsets)} sites in "
                f"{netlist_path.name} (must match exactly one): {find!r}"
            )
        edit = _NetlistEdit(
            index=position,
            file=netlist_path.name,
            line=_mutation_variants._line_at(source, offsets[0]),
            original_code=find,
            mutated_code=replace,
        )
        try:
            resolved.append(
                _mutation_variants.MutationVariantPlan._resolve_one(edit, source)
            )
        except _mutation_variants.MutationVariantError as exc:
            raise BenchmarkError(f"{label}: find[{position}]: {exc}") from exc

    ordered = sorted(resolved, key=lambda mutation: mutation.start)
    for previous, following in zip(ordered, ordered[1:], strict=False):
        if following.start < previous.end:
            raise BenchmarkError(
                f"{label}: two of this mutant's find spans overlap in "
                f"{netlist_path.name}; they must edit disjoint text"
            )

    mutated = source
    for mutation in reversed(ordered):
        mutated = _mutation_variants._replace_exact(mutated, mutation)
    netlist_path.write_bytes(mutated)


def mutations_path_for(task_path: Path) -> Path:
    """Where ``task_path``'s mutation-gate document lives (whether or not it
    exists): ``<task-id>.mutations.json`` beside the task."""
    return task_path.with_name(task_path.stem + MUTATIONS_SUFFIX)


def load_mutations(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{path}: failed to load mutations: {exc}") from exc
    if not isinstance(document, dict):
        raise BenchmarkError(f"{path}: mutations document must be a JSON object")
    return document


def _mutations_validator(
    schema_path: Path = DEFAULT_MUTATIONS_SCHEMA_PATH,
) -> jsonschema.protocols.Validator:
    """Build the mutations-document validator, using the same
    ``validator_for``/``check_schema`` convention :func:`validate_tasks` uses
    for ``task.schema.json``."""
    if not schema_path.is_file():
        raise BenchmarkError(f"mutations schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text())
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def _resolve_mutant_netlist(
    mutant_netlist: str, task: dict[str, Any], label: str
) -> str:
    """Which of ``task``'s own ``reference.netlists`` entries a mutant's
    ``netlist`` field names -- accepting either the full repository-relative
    path as written there or just its filename. Raises
    :class:`BenchmarkError` when it names none of them, or when a bare
    filename is ambiguous across two entries: a mutant that cannot be tied to
    exactly one declared reference netlist is a broken declaration, never
    something to guess at."""
    declared = [
        entry
        for entry in (task.get("reference") or {}).get("netlists") or []
        if isinstance(entry, str)
    ]
    matches = [entry for entry in declared if entry == mutant_netlist]
    if not matches:
        matches = [entry for entry in declared if Path(entry).name == mutant_netlist]
    if len(matches) != 1:
        raise BenchmarkError(
            f"{label}: netlist {mutant_netlist!r} matches {len(matches)} of the "
            f"task's reference.netlists entries (must match exactly one): "
            f"{declared}"
        )
    return matches[0]


def _mutations_task_field_errors(document: dict[str, Any], path: Path) -> list[str]:
    """Cross-check a mutations document's own ``task`` field against both its
    filename stem and the task it was loaded beside -- split out of
    :func:`_mutations_errors` to keep that function's branching within the
    repo's complexity baseline."""
    errors: list[str] = []
    expected_task = path.name[: -len(MUTATIONS_SUFFIX)]
    if document["task"] != expected_task:
        errors.append(
            f"task {document['task']!r} does not match filename stem {expected_task!r}"
        )
    return errors


def _targeted_mutant_errors(
    document: dict[str, Any], task: dict[str, Any]
) -> tuple[list[str], set[str]]:
    """Validate every ``targeted[]`` mutant's name uniqueness, find/replace
    shape, and netlist reference, returning ``(errors, declared_names)``.
    Split out of :func:`_mutations_errors` to keep that function's branching
    within the repo's complexity baseline."""
    errors: list[str] = []
    names: set[str] = set()
    for mutant in document["targeted"]:
        label = f"targeted[{mutant['name']}]"
        if mutant["name"] in names:
            errors.append(f"{label}: duplicate mutant name")
        names.add(mutant["name"])
        try:
            _mutant_edits(mutant, label)
        except BenchmarkError as exc:
            errors.append(str(exc))
        try:
            _resolve_mutant_netlist(mutant["netlist"], task, label)
        except BenchmarkError as exc:
            errors.append(str(exc))
    return errors, names


def _equivalent_declaration_errors(
    document: dict[str, Any], names: set[str]
) -> list[str]:
    """Validate every ``equivalent[]`` declaration's name uniqueness and that
    it names a real ``targeted[]`` mutant. Split out of
    :func:`_mutations_errors` to keep that function's branching within the
    repo's complexity baseline."""
    errors: list[str] = []
    declared: set[str] = set()
    for declaration in document.get("equivalent") or []:
        name = declaration["name"]
        if name in declared:
            errors.append(f"equivalent[{name}]: duplicate equivalence declaration")
        declared.add(name)
        if name not in names:
            errors.append(
                f"equivalent[{name}]: names no targeted mutant "
                f"(declared: {sorted(names)})"
            )
    return errors


def _mutations_errors(
    path: Path,
    task: dict[str, Any],
    validator: jsonschema.protocols.Validator,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Schema- and cross-reference-validate one mutations document against
    the task it belongs to, returning ``(document, errors)``. Never raises
    for a document-level problem -- mirrors :func:`_task_errors`'s posture so
    one malformed mutations file reports as that task's own errors rather
    than aborting the whole check."""
    try:
        document = load_mutations(path)
    except BenchmarkError as exc:
        return None, [str(exc)]

    errors: list[str] = []
    for error in sorted(validator.iter_errors(document), key=str):
        location = "/".join(str(part) for part in error.path)
        errors.append(f"{location}: {error.message}" if location else error.message)
    if errors:
        # Cross-reference checks below assume the schema-declared shape.
        return document, errors

    errors.extend(_mutations_task_field_errors(document, path))
    if document["task"] != task.get("id"):
        errors.append(
            f"task {document['task']!r} does not match the task's own id "
            f"{task.get('id')!r}"
        )

    targeted_errors, names = _targeted_mutant_errors(document, task)
    errors.extend(targeted_errors)
    errors.extend(_equivalent_declaration_errors(document, names))

    return document, errors


def _mutant_scratch_repo(
    task: dict[str, Any], repo_root: Path, scratch_root: Path
) -> Path:
    """Copy ``task``'s whole reference directory into a scratch repository
    root at the same repository-relative location, so a mutated netlist can
    be scored through the task's own (unmodified) ``eval_descriptor``/`klt
    sim` request files without touching the checkout. The same scratch shape
    ``tests/test_design_agent_benchmark.py`` already used for its
    hand-written mutation parametrize list before this check existed."""
    reference_rel = Path(task["reference"]["eval_descriptor"]).parent
    scratch_repo = scratch_root / "repo"
    destination = scratch_repo / reference_rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_root / reference_rel, destination)
    return scratch_repo


def _equivalence_status(
    declaration: dict[str, Any], mutated_netlist_text: str
) -> tuple[str, str]:
    """Classify a surviving mutant that carries an ``equivalent[]``
    declaration, returning ``(status, note)``:

    * ``"unverified"`` -- the declaration has an explicitly null ``code``
      anchor, so there is nothing to re-verify it against.
    * ``"survived"`` -- the anchor no longer occurs in the mutated netlist,
      so the reference was edited out from under the declaration and the
      equivalence argument no longer demonstrably applies.
    * ``"declared-equivalent"`` -- anchor still present; the survival is
      accounted for.
    """
    code = declaration.get("code")
    if not isinstance(code, str) or not code:
        return "unverified", "equivalence declared without a code anchor"
    if code not in mutated_netlist_text:
        return "survived", (
            "equivalence declaration is stale: its code anchor no longer "
            "occurs in the mutated netlist"
        )
    return "declared-equivalent", declaration.get("reason", "")


def _run_targeted_mutant(
    mutant: dict[str, Any],
    task: dict[str, Any],
    repo_root: Path,
    declarations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Apply one targeted mutant to a scratch copy of its task's reference
    solution, score it through the task's own ``reference.eval_descriptor``
    (:func:`klayout_tools.eval.run_eval` -- the same library entry point
    :func:`check_reference_solutions` calls), and classify the outcome.

    Deliberately calls ``run_eval`` directly rather than
    :func:`check_reference_solutions`: that function also writes the
    cross-step reference-solution cache (issue #1783), and a mutant's result
    must never land there -- a later `run` step would then score the *mutant*
    as if it were the reference.
    """
    label = f"{task['id']} targeted[{mutant['name']}]"
    record: dict[str, Any] = {
        "name": mutant["name"],
        "netlist": mutant["netlist"],
        "why": mutant["why"],
        "status": "unapplied",
        "eval_valid": None,
        "error": None,
        "note": None,
        "equivalence_declared": mutant["name"] in declarations,
    }
    with tempfile.TemporaryDirectory(
        prefix=f"design-agent-mutant-{task['id']}-{mutant['name']}-"
    ) as scratch:
        try:
            scratch_repo = _mutant_scratch_repo(task, repo_root, Path(scratch))
            netlist_rel = _resolve_mutant_netlist(mutant["netlist"], task, label)
            netlist_path = scratch_repo / netlist_rel
            if not netlist_path.is_file():
                raise BenchmarkError(
                    f"{label}: netlist {netlist_rel} is not inside the task's "
                    "reference directory, so it cannot be mutated in isolation"
                )
            _apply_mutant_edits(netlist_path, _mutant_edits(mutant, label), label)
        except BenchmarkError as exc:
            record["error"] = str(exc)
            return record

        descriptor_path = scratch_repo / task["reference"]["eval_descriptor"]
        try:
            report = run_eval(str(descriptor_path))
        except EvalError as exc:
            # The mutant could not be scored at all (ngspice refused the
            # deck, a measurement had nothing to read). Reported in its own
            # bucket rather than as a kill: it is certainly not a survivor,
            # but it also did not demonstrate the gate discriminating.
            record["status"] = "unbuildable"
            record["error"] = str(exc)
            return record

        record["eval_valid"] = bool(report.get("valid"))
        if not record["eval_valid"]:
            record["status"] = "killed"
            return record

        declaration = declarations.get(mutant["name"])
        if declaration is None:
            record["status"] = "survived"
            record["note"] = (
                "the task's own reference gate accepted this mutant -- either "
                "the gate does not discriminate what this mutant changes, or "
                "the mutant is equivalent and must say so in equivalent[]"
            )
            return record
        status, note = _equivalence_status(declaration, netlist_path.read_text())
        record["status"] = status
        record["note"] = note
        return record


def check_mutation_gates(
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    repo_root: Path = REPO_ROOT,
    schema_path: Path = DEFAULT_MUTATIONS_SCHEMA_PATH,
) -> dict[str, Any]:
    """For every task under ``tasks_dir`` that ships a ``<id>.mutations.json``
    beside it, apply each declared ``targeted[]`` mutant to a scratch copy of
    the reference netlist it names and require the task's own
    ``reference.eval_descriptor`` gate to **reject** it (issue #2262).

    Reports killed / survived / unbuildable / declared-equivalent per task,
    and is ``valid: false`` when any task has

    * a malformed or internally-inconsistent mutations document (including a
      ``find`` anchor that does not match exactly one site, and -- via the
      schema's own ``minItems: 1`` -- an empty ``targeted[]``),
    * a **survivor**: a mutant its own task's gate accepted, with no
      equivalence declaration, or with one whose code anchor has gone stale,
    * an **unverified** equivalence declaration (no code anchor).

    A task with no mutations file contributes nothing -- neither a pass nor a
    failure. Growing coverage to the tasks that have none today is issue
    #2263; this function is the mechanism that makes such coverage
    enforceable.

    Never raises :class:`BenchmarkError` for a per-task problem (mirrors
    :func:`validate_tasks`), only for an environment one: a missing tasks
    directory or mutations schema file.
    """
    validator = _mutations_validator(schema_path)

    results = []
    for task_path in _task_paths(tasks_dir):
        mutations_path = mutations_path_for(task_path)
        if not mutations_path.is_file():
            continue
        task = load_task(task_path)
        document, errors = _mutations_errors(mutations_path, task, validator)

        mutants: list[dict[str, Any]] = []
        if document is not None and not errors:
            declarations = {
                declaration["name"]: declaration
                for declaration in document.get("equivalent") or []
            }
            for mutant in document["targeted"]:
                record = _run_targeted_mutant(mutant, task, repo_root, declarations)
                mutants.append(record)
                if record["status"] == "unapplied":
                    errors.append(record["error"])

        counts = {
            status: sum(1 for record in mutants if record["status"] == status)
            for status in (
                "killed",
                "survived",
                "unbuildable",
                "declared-equivalent",
                "unverified",
                "unapplied",
            )
        }
        valid = (
            not errors
            and bool(mutants)
            and counts["survived"] == 0
            and counts["unverified"] == 0
        )
        results.append(
            {
                "id": task["id"],
                "tier": task.get("tier"),
                "valid": valid,
                "errors": errors,
                "total": len(mutants),
                "killed": counts["killed"],
                "survived": counts["survived"],
                "unbuildable": counts["unbuildable"],
                "declared_equivalent": counts["declared-equivalent"],
                "unverified": counts["unverified"],
                "mutants": mutants,
            }
        )

    return {
        "schema_version": 1,
        "valid": all(result["valid"] for result in results),
        "task_count": len(results),
        "mutant_count": sum(result["total"] for result in results),
        "survived_count": sum(result["survived"] for result in results),
        "tasks": results,
    }


# --------------------------------------------------------------------------
# Cross-step reference-solution cache (issue #1783)
#
# `validate` and `run` each independently pay the full "run this task's
# reference solution through a real, sky130A-backed ngspice corner sweep"
# cost when invoked as separate steps of the same CI job (see
# `.github/workflows/design-agent-benchmark.yml`) -- `validate`'s
# `check_reference_solutions` calls `run_eval` per task, and `run`'s
# deterministic `reference_candidate_provider` calls the exact same
# `run_eval` per task, in a *separate* process. This cache lets the second
# call reuse the first call's already-paid-for result instead of
# re-simulating, as long as nothing about the reference solution changed in
# between (fingerprint mismatch => cache miss => re-simulate, never a
# silently-stale reuse -- mirrors `klayout_tools.sim`'s own `--resume`
# checkpoint-fingerprint convention, see `sim._checkpoint_fingerprint`).
# --------------------------------------------------------------------------


def _reference_descriptor_check_entries(descriptor: dict[str, Any]) -> list[Any]:
    """Every ``gates``/``objective``/``metrics`` entry in an ``eval``
    descriptor -- the three places a descriptor can carry a check's
    ``args`` (see ``klayout_tools.eval``'s module docstring for the
    descriptor shape)."""
    entries: list[Any] = list(descriptor.get("gates") or [])
    objective = descriptor.get("objective")
    if isinstance(objective, dict):
        entries.append(objective)
    entries.extend(descriptor.get("metrics") or [])
    return entries


def _reference_solution_dependency_files(
    task: dict[str, Any], repo_root: Path
) -> list[Path]:
    """Every on-disk file that determines whether ``task``'s reference
    solution passes its own ``eval_descriptor`` gate: the descriptor file
    itself, every ``sim``-check request JSON file it names (a string
    ``args.request`` across ``gates``/``objective``/``metrics``), and --
    for each of those -- the netlist and resolved model-library file it in
    turn names. Content-hashing this exact file set (see
    :func:`_reference_solution_fingerprint`) is what lets an edit to any of
    them (netlist, model deck, corner list, analysis, measurements,
    timeout -- every one of those fields lives inside one of these files)
    invalidate a cached reference-solution result.

    Best-effort and never raises: a descriptor/request that cannot be
    parsed, or a model library that cannot be resolved, simply contributes
    no further files past the point of failure -- :func:`sha256_file`
    hashes a missing file as ``None``, so the overall fingerprint still
    changes if a file up to that point changes, and a reference solution
    this function cannot fully walk is exactly the case where a cache
    should be most conservative (see the fingerprint's own docstring).
    """
    descriptor_path = repo_root / task["reference"]["eval_descriptor"]
    files = [descriptor_path]
    try:
        descriptor = json.loads(descriptor_path.read_text())
    except (OSError, json.JSONDecodeError):
        return files
    if not isinstance(descriptor, dict):
        return files

    request_names: set[str] = set()
    for entry in _reference_descriptor_check_entries(descriptor):
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if not isinstance(args, dict):
            continue
        request = args.get("request")
        if (
            isinstance(request, str)
            and request != "-"
            and not request.lstrip().startswith("{")
        ):
            request_names.add(request)

    for name in sorted(request_names):
        request_path = descriptor_path.parent / name
        files.append(request_path)
        try:
            request_doc = json.loads(request_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(request_doc, dict):
            continue
        request_dir = str(request_path.parent)

        netlist = request_doc.get("netlist")
        if isinstance(netlist, str):
            files.append(Path(_resolve_relative(netlist, request_dir)))

        models = request_doc.get("models")
        if isinstance(models, dict):
            try:
                files.append(Path(_resolve_models_lib(models, request_dir)))
            except SimError:
                pass

    return files


def _reference_solution_fingerprint(task: dict[str, Any], repo_root: Path) -> str:
    """SHA-256 fingerprint of every file
    :func:`_reference_solution_dependency_files` finds for ``task`` -- the
    basis for deciding whether a cached reference-solution result still
    applies. Content-hashes file bytes (not paths), so an edited-in-place
    file at the same path invalidates the cache, exactly like
    ``sim._checkpoint_fingerprint``."""
    files = _reference_solution_dependency_files(task, repo_root)
    payload = {
        "schema": 1,
        "files": {str(path): sha256_file(str(path)) for path in files},
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _reference_cache_dir(repo_root: Path) -> Path:
    """Where the cross-step reference-solution cache lives: one JSON file
    per task under ``<repo_root>/.klt/design-agent-benchmark-cache/``.
    ``.klt/`` is already this repo's own scratch-directory convention
    (gitignored -- see ``.gitignore``), and `validate`/`run` already share
    a checkout when run as steps of the same CI job
    (``.github/workflows/design-agent-benchmark.yml``), so a plain on-disk
    cache needs no extra artifact-passing wiring between them."""
    return repo_root / ".klt" / "design-agent-benchmark-cache"


def _reference_cache_path(repo_root: Path, task_id: str) -> Path:
    return _reference_cache_dir(repo_root) / f"{task_id}.json"


def _write_reference_cache(
    repo_root: Path,
    task_id: str,
    fingerprint: str,
    *,
    valid: bool,
    error: str | None,
    eval_report: dict[str, Any] | None,
) -> None:
    """Persist ``check_reference_solutions``'s own `klt eval` result for
    ``task_id``, fingerprinted per :func:`_reference_solution_fingerprint`,
    so :func:`run_attempt`'s reference-provider path can skip re-simulating
    the identical reference solution in a later process within the same
    job. Best-effort: a write failure (read-only filesystem, disk full) is
    swallowed rather than failing the caller -- the next reader just gets a
    cache miss, exactly like today's uncached behaviour. Writes via a
    temp-file-plus-``replace`` so a concurrent reader never observes a
    partially-written file, mirroring ``sim._Checkpoint``'s own atomic
    write.
    """
    path = _reference_cache_path(repo_root, task_id)
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "valid": valid,
        "error": error,
        "eval_report": eval_report,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(path)
    except OSError:
        pass


def _load_reference_cache(
    repo_root: Path, task_id: str, fingerprint: str
) -> dict[str, Any] | None:
    """Read back a cache entry :func:`_write_reference_cache` wrote for
    ``task_id``, or ``None`` on a miss -- a missing file, unreadable or
    malformed JSON, or (critically) a fingerprint that no longer matches
    ``fingerprint`` (the reference solution changed since the cache was
    written -- a stale cache is never silently reused, per issue #1783's
    own acceptance criteria). Never raises: mirrors
    ``sim._load_checkpoint``'s never-raises, fingerprint-gated shape."""
    path = _reference_cache_path(repo_root, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    return data


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


#: Marks :func:`reference_candidate_provider` as safe for
#: :func:`run_task_attempts`'s attempt-caching shortcut (issue #1781): a
#: provider carrying ``is_deterministic = True`` is documented (like this
#: one) to return byte-identical output regardless of ``attempt_index``, so
#: rerunning `klt eval` against it ``n_attempts`` times can never change the
#: scored outcome -- only multiply cost. A plain function attribute (rather
#: than a new provider-registry abstraction) so the opt-in stays visible
#: right next to the provider it describes, and so a future deterministic
#: provider can opt in the same way without touching `run_task_attempts`
#: itself. Deliberately **not** set on `live_agent_candidate_provider` /
#: `interactive_agent_candidate_provider` (issue #1732/#1739): those drive a
#: real agent invocation per attempt and are genuinely non-deterministic --
#: every attempt must still run for real.
reference_candidate_provider.is_deterministic = True  # type: ignore[attr-defined]


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


#: The `task` dict key round mode (:func:`run_task_round`, issue #2253)
#: attaches the last :data:`ROUND_HISTORY_WINDOW` ledger entries under,
#: before handing the (copied) task dict to a :data:`CandidateProvider`.
#: Deliberately a private, underscore-prefixed key -- it is never part of
#: `benchmarks/design-agent/schema/task.schema.json`'s own shape (that
#: schema's `additionalProperties: false` only ever validates a task as
#: loaded from disk, never this runtime-only copy) -- so every existing
#: provider, and both prompt builders below, can check for it with a plain
#: `task.get(...)` without any of them needing a signature change. This is
#: what lets round mode reuse `reference_candidate_provider`,
#: `make_live_agent_provider`, and `make_interactive_agent_provider`
#: completely unmodified: the deterministic reference provider ignores the
#: key entirely (proving the "stays deterministic" plumbing test), while the
#: two prompt builders below render it into a "previous rounds" section.
ROUND_HISTORY_CONTEXT_KEY = "_design_agent_benchmark_round_history"

#: How many trailing ledger entries a round's prompt carries -- "each
#: round's prompt carries the previous three rounds' evaluation results"
#: (issue #2253's Proposal).
ROUND_HISTORY_WINDOW = 3


def _format_round_history(history: list[dict[str, Any]] | None) -> str:
    """Render up to the last :data:`ROUND_HISTORY_WINDOW` ledger entries
    (:func:`run_task_round`'s own return shape) as a prompt section -- ``""``
    when ``history`` is empty/``None`` (round 1, or non-round `--attempts`
    mode, whose prompt is therefore byte-identical to before this section
    existed).

    Deliberately renders only the ledger's own small evaluation-result
    fields (``round``/``valid``/``score``/``notes``) -- never a submission's
    netlist body or file path -- so a later round's context is the
    *evaluation record*, not a live, writable path into an earlier round's
    own sandbox. This is what keeps "previous rounds are readable but
    inert" true regardless of what filesystem access a future extension
    might grant: the prompt itself never hands out anything an agent could
    edit to retroactively change how a past round was scored.
    """
    if not history:
        return ""
    lines = [
        "=== Previous rounds' evaluation results (most recent last -- you "
        "are iterating on this design, not starting fresh) ===",
        "",
    ]
    for entry in history[-ROUND_HISTORY_WINDOW:]:
        lines.append(
            f"- round {entry.get('round')}: valid={entry.get('valid')}, "
            f"score={entry.get('score')}, notes={entry.get('notes')!r}"
        )
    lines.append(
        "\nThis harness keeps its own frozen copy of every round's "
        "submission -- editing a previous round's files, if you can see "
        "them, changes nothing about how that round was already scored. "
        "You may also read `../ledger.jsonl` directly: it is the exact "
        "record you are scored on.\n"
    )
    return "\n".join(lines) + "\n"


def _build_live_agent_prompt(
    task: dict[str, Any], repo_root: Path
) -> tuple[str, list[str]]:
    """Build the single-turn prompt :func:`_default_invoke_agent` sends, and
    the ordered list of netlist stems the agent must return one labeled
    fence per (:func:`_extract_labeled_netlists`). Never includes the
    reference solution's own netlist body -- only the block spec (S3's
    output) and the testbench contract (`klt sim` request documents, minus
    their `netlist` field's content) the agent's own candidate must satisfy.

    When ``task`` carries :data:`ROUND_HISTORY_CONTEXT_KEY` (round mode,
    issue #2253), a "previous rounds" section (:func:`_format_round_history`)
    is inserted before the block spec; absent that key, the prompt is
    unchanged from before round mode existed.
    """
    stems = [Path(p).stem for p in task["reference"]["netlists"]]
    testbenches = _reference_testbenches(task, repo_root)
    skill_chain_text = _load_skill_chain_text(repo_root)
    device_model_contract = _device_model_contract(task, repo_root, testbenches)
    round_history_section = _format_round_history(task.get(ROUND_HISTORY_CONTEXT_KEY))

    fence_block = "\n".join(
        f'```spice:{stem}\n<your netlist body for "{stem}">\n```' for stem in stems
    )

    prompt = f"""You are acting as the design agent for klayout-tools' staged
analog design pipeline (docs/design/design-pipeline.md), driving stages
S4 (topology selection) -> S5 (sizing) -> S6 (netlist authoring) for the
block below, exactly as this repo's own skills instruct.

=== S4/S5/S6 skill procedures (read these before answering) ===

{skill_chain_text}

{round_history_section}=== Block to design ===

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
        # Only rewrite a *repo-local* relative `models.lib` (the pre-#1736
        # generic-model convention) into an absolute path here -- when
        # `models.pdk`/`models.pdk_root` is set (every shipped task's own
        # convention as of #1736), `lib` is deliberately relative to the
        # *resolved PDK variant directory*, per docs/cli/sim.md's
        # `_resolve_models_lib`, never to this reference directory; rewriting
        # it here would silently point `klt sim` at a nonexistent path under
        # `reference_dir` instead of letting it resolve via $PDK_ROOT.
        if (
            isinstance(models, dict)
            and isinstance(models.get("lib"), str)
            and not models.get("pdk")
            and not models.get("pdk_root")
        ):
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

#: This module's own code below only calls `make_interactive_agent_provider`
#: (`--provider interactive-agent`'s `_resolve_provider` case) and the two
#: bound constants `DEFAULT_INTERACTIVE_TIMEOUT_S`/`DEFAULT_TOOL_CALL_BUDGET`
#: (`_resolve_agent_timeout`/`run`'s own argparse defaults) -- the remaining
#: names below moved to `design_agent_benchmark_interactive.py` (split out,
#: issue #1766) are not called directly from this module's own code any
#: more. They are re-exported (self-aliased so linters recognise the
#: re-export as intentional) so `design_agent_benchmark.<name>` keeps
#: working for every external caller/test that reached them before this
#: split.
#:
#: `design_agent_benchmark_interactive` itself imports the handful of names
#: it needs back from *this* module (`CandidateProvider`,
#: `AgentInvocationError`, and the other pre-existing names its section
#: depended on) lazily, inside the functions that use them, rather than at
#: its own module scope -- see that module's docstring for why: this script
#: also runs standalone as `python scripts/design_agent_benchmark.py` (this
#: repo's benchmark CI workflow), in which case it loads as `__main__`
#: rather than as a module literally named `design_agent_benchmark`, and a
#: module-scope back-import in that split module would otherwise resolve
#: the *other* name at load time -- a real circular-import failure, not
#: just an import-order nit. This import is kept at the interactive
#: section's original location (rather than hoisted to the top-of-file
#: import block) purely so the diff that introduced this split reads as a
#: clean 1:1 "section replaced by an import" swap.
from design_agent_benchmark_interactive import (  # noqa: E402
    DEFAULT_INTERACTIVE_TIMEOUT_S,
    DEFAULT_TOOL_CALL_BUDGET,
    make_interactive_agent_provider,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    INTERACTIVE_ALLOWED_TOOLS as INTERACTIVE_ALLOWED_TOOLS,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    INTERACTIVE_PERMISSION_MODE as INTERACTIVE_PERMISSION_MODE,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    SANDBOX_BRIEF_FILENAME as SANDBOX_BRIEF_FILENAME,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    SESSION_STDERR_FILENAME as SESSION_STDERR_FILENAME,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    SESSION_SUMMARY_FILENAME as SESSION_SUMMARY_FILENAME,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    TRANSCRIPT_FILENAME as TRANSCRIPT_FILENAME,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    AgentSessionRequest as AgentSessionRequest,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    AgentSessionResult as AgentSessionResult,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    InteractiveAgentInvoker as InteractiveAgentInvoker,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _attempt_sandbox as _attempt_sandbox,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _build_interactive_agent_prompt as _build_interactive_agent_prompt,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _collect_sandbox_netlists as _collect_sandbox_netlists,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _default_invoke_interactive_agent as _default_invoke_interactive_agent,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _iter_stream_events as _iter_stream_events,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _kill as _kill,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _seed_agent_sandbox as _seed_agent_sandbox,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    _write_sandbox_klt_shim as _write_sandbox_klt_shim,
)
from design_agent_benchmark_interactive import (  # noqa: E402
    interactive_agent_candidate_provider as interactive_agent_candidate_provider,
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
    *any* provider, present or future, per issue #1732's Test Plan.

    When ``provider`` is the deterministic :func:`reference_candidate_provider`
    (identity check -- every attempt hands back the same fixed reference
    solution, so a per-attempt cache lookup is meaningful; the agent-backed
    providers each produce a genuinely new candidate per attempt, so no
    cache lookup applies to them), the cross-step reference-solution cache
    (issue #1783) is consulted before calling `klt eval`: a fingerprint
    match (see :func:`_reference_solution_fingerprint`) means some earlier
    process already paid for this exact reference solution's corner sweep
    (typically `validate`'s :func:`check_reference_solutions`, run as an
    earlier step of the same CI job) and the cached report is reused
    verbatim instead of re-simulating; a miss (no cache yet, or the
    reference solution changed since it was written) falls through to the
    normal `run_eval` call, exactly like today's uncached behaviour."""
    start = time.monotonic()
    report: dict[str, Any] | None = None
    valid = False
    error: str | None = None
    try:
        descriptor_arg, candidate_arg = provider(task, attempt_index, repo_root)
    except Exception as exc:  # noqa: BLE001 -- see docstring: any provider failure is a scored attempt failure, never a crash
        error = f"candidate provider failed: {exc}"
    else:
        cached = None
        if provider is reference_candidate_provider:
            fingerprint = _reference_solution_fingerprint(task, repo_root)
            cached = _load_reference_cache(repo_root, task["id"], fingerprint)
        if cached is not None:
            report = cached.get("eval_report")
            valid = bool(cached.get("valid"))
            error = cached.get("error")
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
    """Run ``n_attempts`` attempts for ``task`` through ``provider``.

    **Deterministic-provider caching (issue #1781)**: when ``provider`` is
    marked ``is_deterministic = True`` (see
    :func:`reference_candidate_provider`), every attempt is documented to
    return byte-identical output regardless of ``attempt_index`` -- so
    running `klt eval` against it more than once per task adds zero pass@k
    signal, only cost. This runs attempt 0 for real and replicates that one
    real result across the remaining ``n_attempts - 1`` slots rather than
    re-invoking the provider/`klt eval` chain again: each replicated
    attempt keeps its own ``attempt`` index (so :func:`summarize_task`'s
    ``n``/``c`` counts, and therefore pass@k, are identical to what a real
    ``n_attempts``-deep rerun against an unmodified deterministic provider
    would have produced) but is tagged ``"cached": True`` with
    ``wall_clock_s: 0.0``, so :func:`summarize_task`'s wall-clock total
    reflects the one real run's actual cost rather than a tautological
    n-times multiple of it.

    A provider that does not opt in (the default; every non-deterministic
    provider, e.g. ``live-agent``/``interactive-agent``, issues #1732/#1739)
    runs every attempt for real, unchanged from before this caching was
    added.
    """
    if n_attempts <= 0:
        return []
    if not getattr(provider, "is_deterministic", False):
        return [run_attempt(task, i, provider, repo_root) for i in range(n_attempts)]

    first = run_attempt(task, 0, provider, repo_root)
    first["cached"] = False
    attempts = [first]
    for i in range(1, n_attempts):
        cached = dict(first)
        cached["attempt"] = i
        cached["wall_clock_s"] = 0.0
        cached["cached"] = True
        attempts.append(cached)
    return attempts


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
    # `attempts > 0` guard: a task run with zero attempts has solved == 0 ==
    # attempts, which would otherwise count as "solved every attempt". Zero
    # attempts became reachable with `--attempts 0 --rounds R` (issue #2253:
    # round mode only, no pass@k) -- reporting those tasks as solved would
    # be a false pass rate. Unreachable, and therefore a no-op, for any
    # `--attempts >= 1` run.
    solved_count = sum(
        1 for t in task_summaries if t["attempts"] > 0 and t["solved"] == t["attempts"]
    )
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
    n_rounds: int = 0,
    rounds_root: Path | None = None,
    round_provider_factory: Callable[[Path], CandidateProvider] | None = None,
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

    ``n_rounds`` (issue #2253, default ``0``) is orthogonal to
    ``n_attempts``/pass@k: when positive, every task is *additionally* run
    through :func:`run_task_rounds` and each task summary gains a purely
    additive ``"rounds"`` key (:func:`summarize_rounds`) -- pass@k's own
    fields are computed exactly as before and are never touched. ``0`` (the
    default) skips round mode entirely, so a caller that never passes these
    three new parameters gets byte-for-byte the same report this function
    has always returned.

    ``round_provider_factory``, when given, is called once per task with
    that task's own round root and must return the :data:`CandidateProvider`
    round mode should drive -- this is how an agent-backed provider gets a
    sandbox root *inside* the task's round directory (so its own
    ``../ledger.jsonl`` resolves to that task's ledger), while ``provider``
    keeps driving `--attempts` mode with whatever sandbox root the caller
    chose for it. Omitted, round mode simply reuses ``provider``.
    """
    ks = sorted(set(ks))
    start = time.monotonic()

    effective_rounds_root = rounds_root
    if n_rounds > 0 and effective_rounds_root is None:
        effective_rounds_root = Path(
            tempfile.mkdtemp(prefix="design-agent-benchmark-rounds-")
        )

    task_summaries = []
    for path in _task_paths(tasks_dir):
        task = load_task(path)
        attempts = run_task_attempts(task, n_attempts, provider, repo_root)
        summary = summarize_task(task, attempts, ks)
        if n_rounds > 0:
            task_round_root = effective_rounds_root / task["id"]
            task_round_root.mkdir(parents=True, exist_ok=True)
            round_provider = (
                round_provider_factory(task_round_root)
                if round_provider_factory is not None
                else provider
            )
            round_entries = run_task_rounds(
                task,
                n_rounds,
                round_provider,
                repo_root,
                rounds_root=effective_rounds_root,
                scratch_root=task_round_root,
            )
            summary["rounds"] = summarize_rounds(round_entries)
            summary["rounds"]["ledger_path"] = str(task_round_root / LEDGER_FILENAME)
        task_summaries.append(summary)

    by_tier: dict[str, list[dict[str, Any]]] = {tier: [] for tier in TIERS}
    for summary in task_summaries:
        by_tier.setdefault(summary["tier"], []).append(summary)

    result = {
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
    if n_rounds > 0:
        result["n_rounds"] = n_rounds
    return result


# --------------------------------------------------------------------------
# Round mode: multi-round refinement with a per-cell ledger.jsonl (#2253)
#
# `--attempts` (above) runs k *independent* attempts per task and scores
# pass@k -- "can the agent produce a valid design at all". Round mode
# instead runs R *sequential* rounds per task, feeding each round the
# outcome of the last few, and scores the *trajectory*: does the design get
# better as the agent iterates against feedback, the way the real pipeline
# loops (design-sizing Loop A, design-drc-lvs Loop B) actually run. Modeled
# on the AHRR artifact's ledger.jsonl shape (github.com/ZijD/AHRR, ICCAD'26,
# MIT -- methodology reference only, no code reuse): one fsync'd JSON line
# per round, `best()` is the highest-scoring *valid* round (an invalid round
# always scores `None`, never a partial credit), and every reported number
# regenerates from the ledger alone.
# --------------------------------------------------------------------------

#: Contract identifier for one `ledger.jsonl` line (issue #2253). Documented
#: alongside `benchmarks/design-agent/schema/task.schema.json` in
#: `benchmarks/design-agent/schema/ledger.schema.json`, following this
#: repo's own `klt.<verb>.<doc>/<n>` schema-identifier convention (e.g.
#: `klt.drc.request/1`).
LEDGER_SCHEMA = "klt.design_agent_benchmark.ledger/1"

#: Filename for a task's own append-only round ledger, written directly
#: under `<rounds_root>/<task_id>/` -- sibling to each round's own scratch
#: sandbox, so `../ledger.jsonl` (the path the round-history prompt section
#: tells an interactive-agent session it may read) resolves correctly.
LEDGER_FILENAME = "ledger.jsonl"


def _looks_like_timeout(exc: BaseException) -> bool:
    """Best-effort, machine-distinguishable ``timed_out`` detection: every
    timeout this module's own agent invokers raise
    (:class:`AgentInvocationError` from :func:`_default_invoke_agent`'s
    ``subprocess.TimeoutExpired`` handler, and from
    ``_default_invoke_interactive_agent``'s watchdog) phrases its message as
    "... timed out after ...". A future provider that raises its own
    exception type for a timeout only needs to keep that phrasing for this
    to keep working -- no exception-type registry to maintain here."""
    return "timed out" in str(exc).lower()


def _freeze_directory_readonly(path: Path) -> None:
    """Best-effort: recursively make ``path`` (a just-scored round's own
    scratch sandbox) unwritable -- files ``0o444``, directories ``0o555``
    (readable/listable, never writable again) -- so a later round's prompt
    can truthfully say "previous rounds' directories are seeded read-only"
    (issue #2253's acceptance criteria) regardless of what a future
    extension might grant an agent read access to. Never raises: a filename
    this process cannot chmod (already read-only, unusual ownership) is left
    exactly as read-only as it already was, which is the safe direction to
    fail in.
    """
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o444)
            except OSError:
                pass
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), 0o555)
            except OSError:
                pass
    try:
        os.chmod(path, 0o555)
    except OSError:
        pass


def _usage_from_scratch_dir(scratch_dir: Path | None) -> dict[str, Any] | None:
    """Best-effort per-round tool-call usage, read back from whatever the
    provider itself already wrote into its own scratch directory -- never
    plumbed through a new :data:`CandidateProvider` return value, so every
    existing provider's 2-tuple contract stays exactly as it is.

    Today this only ever resolves for ``--provider interactive-agent``,
    which already writes ``agent-session.json``
    (:data:`SESSION_SUMMARY_FILENAME`, ``tool_calls``/``turns``) into its own
    per-attempt sandbox (:func:`make_interactive_agent_provider`). Real
    *token* accounting is not surfaced by any shipped provider yet -- see
    ``benchmarks/design-agent/README.md``'s "Known limitations" for the
    tracked follow-up; ``usage`` is ``None`` for every other provider, and
    for a scratch directory that carries no such file.
    """
    if scratch_dir is None:
        return None
    try:
        data = json.loads((scratch_dir / SESSION_SUMMARY_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    usage = {key: data[key] for key in ("tool_calls", "turns") if key in data}
    return usage or None


def _submission_dependency_files(descriptor: dict[str, Any]) -> list[Path]:
    """Every **absolute** file path a submitted `klt eval` descriptor's
    ``sim`` gates/objective/metrics reference, walked two levels deep (the
    named ``request`` document, then its own ``netlist``/``models.lib``
    fields) -- the file set whose *bytes* :func:`_submission_sha256`
    content-hashes as "the submission".

    Deliberately skips a relative-path reference rather than trying to
    resolve it: the deterministic :func:`reference_candidate_provider`'s own
    descriptor is exactly this case (its ``request`` paths are relative to
    its ``benchmarks/design-agent/reference/<id>/`` directory) and needs no
    content-hash-based tamper evidence -- it is a repository-committed file
    no provider ever writes to. Every agent-backed provider
    (:func:`make_live_agent_provider`, :func:`make_interactive_agent_provider`)
    already writes *absolute* paths into the descriptor it returns (see
    :func:`_build_live_agent_descriptor`), which is the only case where "what
    did the agent actually submit" is a real question -- so this covers it
    without needing to know the reference provider's own directory
    convention.
    """
    files: list[Path] = []
    for entry in _reference_descriptor_check_entries(descriptor):
        if not isinstance(entry, dict) or entry.get("check") != "sim":
            continue
        args = entry.get("args")
        if not isinstance(args, dict):
            continue
        request = args.get("request")
        if not isinstance(request, str) or not os.path.isabs(request):
            continue
        request_path = Path(request)
        files.append(request_path)
        try:
            request_doc = json.loads(request_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(request_doc, dict):
            continue
        netlist = request_doc.get("netlist")
        if isinstance(netlist, str) and os.path.isabs(netlist):
            files.append(Path(netlist))
        models = request_doc.get("models")
        if isinstance(models, dict) and isinstance(models.get("lib"), str):
            lib = models["lib"]
            if os.path.isabs(lib):
                files.append(Path(lib))
    return files


def _submission_sha256(descriptor_arg: str, candidate_arg: str | None) -> str:
    """Content hash of this round's submission -- see
    :func:`_submission_dependency_files` for what "content" means here.
    Falls back to hashing the ``(descriptor_arg, candidate_arg)`` argument
    strings themselves when no absolute-path dependency file is found (the
    deterministic reference provider's own case: its fixed descriptor file
    path is itself stable across rounds, so the fallback hash is exactly as
    stable) -- never raises.
    """
    try:
        descriptor, _base_dir = _load_json_arg(descriptor_arg, "descriptor")
    except EvalError:
        descriptor = None
    files = (
        _submission_dependency_files(descriptor) if isinstance(descriptor, dict) else []
    )
    if files:
        payload: dict[str, Any] = {
            "schema": 1,
            "files": {
                str(path): sha256_file(str(path))
                for path in sorted(set(files), key=str)
            },
        }
    else:
        payload = {
            "schema": 1,
            "descriptor_arg": descriptor_arg,
            "candidate_arg": candidate_arg,
        }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


#: Subdirectory of a task's own round root holding the harness's private,
#: per-round *copy* of every submission it scored (:func:`_snapshot_submission`).
#: Never handed to a provider: no agent-backed provider is ever constructed
#: with a scratch/sandbox root inside it, so nothing an agent runs can write
#: here, which is what makes "scoring never reads a path the agent can still
#: write" (issue #2253) literally true rather than merely timing-dependent.
SUBMISSIONS_DIRNAME = "submissions"


def _snapshot_submission(descriptor_arg: str, dest_dir: Path) -> tuple[str, list[Path]]:
    """Copy every agent-written file this round's `klt eval` descriptor
    references into ``dest_dir`` (harness-owned, never inside any provider's
    sandbox) and return ``(rewritten_descriptor_arg, copied_files)`` pointing
    at the copies -- "the harness keeps its own copy of each round's
    submission; scoring never reads a path the agent can still write"
    (issue #2253's acceptance criteria).

    Only *absolute* ``sim``-check ``request`` references are snapshotted, for
    exactly the reason :func:`_submission_dependency_files` gives: an
    agent-backed provider always writes absolute paths into its own scratch
    directory (:func:`_build_live_agent_descriptor`), while the deterministic
    :func:`reference_candidate_provider` hands back its repository-committed
    descriptor with *relative* request paths that no provider can write to
    anyway. A descriptor with nothing to snapshot is returned unmodified
    (and the reference provider's round therefore stays byte-identical to
    what `--attempts` mode would have scored).

    Only the two fields that can name an agent-written file are rewritten --
    the descriptor's ``args.request`` and the request document's own
    ``netlist``. A ``models.lib`` is deliberately left exactly as it is: it
    either resolves against ``$PDK_ROOT`` (every shipped task's
    ``models.pdk`` convention) or was already absolutised by the provider,
    and in neither case is it a file an agent can write.

    Never raises -- an unreadable/unparseable request is left pointing at
    its original path rather than failing the round, since `klt eval` will
    report that same problem far more usefully than this function could.
    """
    try:
        descriptor, _base_dir = _load_json_arg(descriptor_arg, "descriptor")
    except EvalError:
        return descriptor_arg, []
    if not isinstance(descriptor, dict):
        return descriptor_arg, []

    snapshot = copy.deepcopy(descriptor)
    copies: dict[str, Path] = {}
    request_copies: dict[str, str] = {}
    for args in _sim_args_with_absolute_request(snapshot):
        request = args["request"]
        snapshotted = request_copies.get(request) or _snapshot_sim_request(
            request, dest_dir, copies
        )
        if snapshotted is not None:
            request_copies[request] = snapshotted
            args["request"] = snapshotted

    if not copies:
        return descriptor_arg, []
    return json.dumps(snapshot), sorted(copies.values(), key=str)


def _sim_args_with_absolute_request(
    descriptor: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Every ``args`` dict in ``descriptor`` that names a ``sim`` check's
    request by *absolute* path -- yielded live (not copied), so a caller can
    rewrite the reference in place."""
    for entry in _reference_descriptor_check_entries(descriptor):
        if not isinstance(entry, dict) or entry.get("check") != "sim":
            continue
        args = entry.get("args")
        if not isinstance(args, dict):
            continue
        request = args.get("request")
        if isinstance(request, str) and os.path.isabs(request):
            yield args


def _copy_into_snapshot(src: Path, dest_dir: Path, copies: dict[str, Path]) -> Path:
    """Copy ``src`` into ``dest_dir`` once, recording it in ``copies`` (keyed
    by source path, so a file named by two requests is copied a single
    time). The ``<n>-`` filename prefix keeps two same-named files from
    different directories from colliding."""
    key = str(src)
    if key not in copies:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{len(copies)}-{src.name}"
        shutil.copyfile(src, dest)
        copies[key] = dest
    return copies[key]


def _snapshot_sim_request(
    request: str, dest_dir: Path, copies: dict[str, Path]
) -> str | None:
    """Copy one `klt sim` request document -- and the candidate netlist it
    names -- into ``dest_dir``, returning the copy's path (or ``None`` when
    the request cannot be read, in which case :func:`_snapshot_submission`
    leaves the original reference alone)."""
    src = Path(request)
    try:
        doc = json.loads(src.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    netlist = doc.get("netlist")
    if isinstance(netlist, str):
        netlist_src = Path(netlist)
        if not netlist_src.is_absolute():
            netlist_src = src.parent / netlist_src
        try:
            doc["netlist"] = str(_copy_into_snapshot(netlist_src, dest_dir, copies))
        except OSError:
            return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{len(copies)}-{src.name}"
    dest.write_text(json.dumps(doc, indent=2) + "\n")
    copies[str(src)] = dest
    return str(dest)


#: Name of the extra ``metrics`` entry :func:`_round_score_descriptor`
#: synthesizes to read a `sim` objective's worst-case *margin* alongside its
#: worst-case *value* -- see that function's docstring.
_ROUND_SCORE_MARGIN_METRIC_NAME = "__design_agent_benchmark_round_score_margin__"


def _round_score_descriptor(descriptor_arg: str) -> tuple[str, str | None]:
    """Best-effort: rewrite ``descriptor_arg`` (this round's already-built
    `klt eval` descriptor, exactly as any :data:`CandidateProvider` returns
    it) to *additionally* request the objective's own worst-case **margin**
    sibling metric, when the objective reads a ``sim`` measurement's
    worst-case *value* -- the ``measurements.<i>.worst_case.value`` dotted
    path every shipped design-agent-benchmark task descriptor's own
    ``objective.metric`` uses (see e.g.
    ``benchmarks/design-agent/reference/five-transistor-ota/eval_descriptor.json``).
    Returns ``(new_descriptor_arg, margin_metric_name)``.

    This is what makes a round's continuous ``score`` a **spec margin**
    (worst-corner headroom against the task's own declared limits --
    `klt sim`'s own already-computed ``measurements[].worst_case.margin``,
    the same per-corner headroom concept `klt size`'s
    ``worst_case_margin`` objective searches against) rather than a raw,
    unnormalized objective value -- without this module re-deriving a
    threshold comparison of its own. The margin metric shares the
    objective's exact ``check``/``args``, so `run_eval`'s own per-call cache
    (`eval.run_eval`'s ``_run_check``) serves it from the same `klt sim`
    invocation the objective already pays for -- adding this metric never
    costs an extra simulation.

    Returns ``(descriptor_arg, None)`` unmodified when the objective's shape
    does not match that convention (a non-``sim`` objective, or a metric
    path not ending in ``.value``) -- the caller then falls back to scoring
    from the plain, polarity-oriented objective value instead
    (:func:`_round_score`).
    """
    try:
        descriptor, _base_dir = _load_json_arg(descriptor_arg, "descriptor")
    except EvalError:
        return descriptor_arg, None
    if not isinstance(descriptor, dict):
        return descriptor_arg, None
    objective = descriptor.get("objective")
    if not isinstance(objective, dict) or objective.get("check") != "sim":
        return descriptor_arg, None
    metric_path = objective.get("metric")
    if not isinstance(metric_path, str) or not metric_path.endswith(".value"):
        return descriptor_arg, None

    augmented = copy.deepcopy(descriptor)
    metrics = list(augmented.get("metrics") or [])
    metrics.append(
        {
            "name": _ROUND_SCORE_MARGIN_METRIC_NAME,
            "check": "sim",
            "args": copy.deepcopy(objective.get("args") or {}),
            "metric": metric_path[: -len(".value")] + ".margin",
        }
    )
    augmented["metrics"] = metrics
    return json.dumps(augmented), _ROUND_SCORE_MARGIN_METRIC_NAME


def _round_score(
    report: dict[str, Any], margin_metric_name: str | None
) -> float | None:
    """This round's continuous, rankable score, derived only from an
    already-``valid`` report -- callers must not call this for an invalid
    round (:func:`run_task_round` always sets ``score: None`` on an invalid
    round directly, per the "an invalid round has ``score: null``, never a
    partial score" acceptance criterion).

    Prefers the worst-case margin :func:`_round_score_descriptor` may have
    synthesized (higher is always better, regardless of the objective's own
    ``polarity``, since a margin is already signed so that positive means
    "passing with headroom"). Falls back to the plain objective value,
    oriented so higher is always better (negated for a ``"minimize"``
    objective) -- used whenever the margin convention does not apply, or the
    margin metric could not be extracted for a task-specific reason (e.g. an
    unextractable corner)."""
    if margin_metric_name is not None:
        margin_value = (report.get("metrics") or {}).get(margin_metric_name)
        if isinstance(margin_value, int | float):
            return float(margin_value)
    objective = report.get("objective") or {}
    value = objective.get("value")
    if isinstance(value, int | float):
        return (
            float(value) if objective.get("polarity") == "maximize" else -float(value)
        )
    return None


def run_task_round(
    task: dict[str, Any],
    round_index: int,
    provider: CandidateProvider,
    repo_root: Path,
    history: list[dict[str, Any]],
    *,
    scratch_root: Path | None = None,
    submission_dir: Path | None = None,
) -> dict[str, Any]:
    """Run round ``round_index`` (1-based) for ``task`` through ``provider``,
    and return one ``klt.design_agent_benchmark.ledger/1`` entry.

    ``history`` is this task's ledger entries so far (oldest first); the
    last :data:`ROUND_HISTORY_WINDOW` are attached to a *copy* of ``task``
    under :data:`ROUND_HISTORY_CONTEXT_KEY` before calling ``provider`` --
    every existing :data:`CandidateProvider` (reference, live-agent,
    interactive-agent) is called completely unmodified; the two prompt
    builders render the key when present, the deterministic reference
    provider ignores it entirely (the "stays deterministic" acceptance
    criterion).

    ``scratch_root``, when given, must be the exact directory an agent-
    backed provider was itself constructed to write its per-round sandbox
    under (``make_live_agent_provider(scratch_root=...)`` /
    ``make_interactive_agent_provider(sandbox_root=...)``) -- this function
    diffs that directory's *subdirectories* before/after the ``provider``
    call to discover the one the call created (if any), which is then
    chmod'd read-only (:func:`_freeze_directory_readonly`) once the round is
    scored, so a later round that can see it finds it inert: "previous
    rounds' directories are seeded read-only". A ``None``/mismatched
    ``scratch_root`` (the deterministic reference provider, or a caller that
    omits it) simply skips this step -- there is nothing to freeze.

    ``submission_dir``, when given, is a harness-owned directory *outside*
    any provider's sandbox into which this round's submitted files are
    copied (:func:`_snapshot_submission`) before scoring; `klt eval` is then
    run against those copies, never the provider's own writable paths --
    "the harness keeps its own copy of each round's submission; scoring
    never reads a path the agent can still write". Omitting it scores the
    provider's own descriptor directly (what the unit tests and the
    repository-committed reference descriptor do, neither of which is
    agent-writable).

    Never raises: a provider failure, an `AgentInvocationError` (including a
    timeout), or an `EvalError` from `klt eval` are all recorded as an
    invalid round with a named ``notes`` reason, exactly like
    :func:`run_attempt`'s identical posture for `--attempts` mode.
    """
    round_start = time.monotonic()
    round_task = dict(task)
    trimmed_history = history[-ROUND_HISTORY_WINDOW:]
    if trimmed_history:
        round_task[ROUND_HISTORY_CONTEXT_KEY] = trimmed_history

    agent_start = time.monotonic()
    descriptor_arg, candidate_arg, scratch_dir, error, timed_out = (
        _invoke_round_provider(
            round_task, round_index, provider, repo_root, scratch_root
        )
    )
    agent_wall_s = time.monotonic() - agent_start

    # Take the harness's own copy of what was submitted *before* anything is
    # scored, and score that copy -- see `_snapshot_submission`.
    snapshot_files: list[Path] = []
    scored_arg = descriptor_arg
    report: dict[str, Any] | None = None
    valid = False
    margin_metric_name: str | None = None
    if error is None:
        # `error is None` already implies a non-None descriptor_arg -- that is
        # exactly what `_invoke_round_provider`'s last guard establishes.
        scored_arg, snapshot_files = _snapshot_for_scoring(
            descriptor_arg, submission_dir
        )
        report, margin_metric_name, error = _evaluate_round(scored_arg, candidate_arg)
        valid = report is not None and bool(report.get("valid"))

    score: float | None = None
    notes: str | None = error
    if error is None and report is not None:
        score = _round_score(report, margin_metric_name) if valid else None
        notes = None if valid else _invalid_round_notes(report)

    usage = _usage_from_scratch_dir(scratch_dir)
    _freeze_round_artifacts(scratch_dir, submission_dir if snapshot_files else None)

    return _round_ledger_entry(
        task_id=task["id"],
        round_index=round_index,
        submission_sha256=(
            _submission_sha256(descriptor_arg, candidate_arg)
            if descriptor_arg is not None
            else None
        ),
        seed_sha256=_round_seed_sha256(task["id"], trimmed_history),
        agent_wall_s=agent_wall_s,
        round_wall_s=time.monotonic() - round_start,
        timed_out=timed_out,
        usage=usage,
        report=report,
        valid=valid,
        score=score,
        notes=notes,
    )


def _child_directories(root: Path | None) -> set[Path]:
    """``root``'s immediate subdirectories (never its files -- the ledger and
    the harness's own ``submissions/`` tree live beside a provider's sandbox
    and must not be mistaken for one), or the empty set when ``root`` is
    ``None``/not a directory."""
    if root is None or not root.is_dir():
        return set()
    return {child for child in root.iterdir() if child.is_dir()}


def _invoke_round_provider(
    round_task: dict[str, Any],
    round_index: int,
    provider: CandidateProvider,
    repo_root: Path,
    scratch_root: Path | None,
) -> tuple[str | None, str | None, Path | None, str | None, bool]:
    """Call ``provider`` for one round, returning ``(descriptor_arg,
    candidate_arg, scratch_dir, error, timed_out)``.

    ``scratch_dir`` is the single subdirectory the call newly created under
    ``scratch_root`` (its own sandbox), or ``None`` when it created none or
    more than one -- there is then nothing this round can unambiguously
    attribute to the provider. Never raises: any provider failure comes back
    as a named ``error`` string, exactly like :func:`run_attempt`'s posture.
    """
    try:
        before = _child_directories(scratch_root)
        descriptor_arg, candidate_arg = provider(round_task, round_index, repo_root)
    except Exception as exc:  # noqa: BLE001 -- see run_attempt's identical rationale
        return (
            None,
            None,
            None,
            f"provider produced no submission: {exc}",
            _looks_like_timeout(exc),
        )
    new_dirs = _child_directories(scratch_root) - before
    scratch_dir = next(iter(new_dirs)) if len(new_dirs) == 1 else None
    if descriptor_arg is None:
        # A provider that returns without raising, but hands back no
        # descriptor at all, breaks the `CandidateProvider` contract -- score
        # it as an invalid round with its own named reason rather than
        # letting a `None` reach `klt eval` and violate "never raises".
        return None, None, scratch_dir, "provider returned no eval descriptor", False
    return descriptor_arg, candidate_arg, scratch_dir, None, False


def _snapshot_for_scoring(
    descriptor_arg: str | None, submission_dir: Path | None
) -> tuple[str | None, list[Path]]:
    """:func:`_snapshot_submission` when the caller gave this round a
    harness-owned submission directory; otherwise the provider's own
    descriptor, unchanged (nothing to copy, nothing copied)."""
    if submission_dir is None or descriptor_arg is None:
        return descriptor_arg, []
    return _snapshot_submission(descriptor_arg, submission_dir)


def _evaluate_round(
    scored_arg: str, candidate_arg: str | None
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Score one round's (already snapshotted) descriptor, returning
    ``(report, margin_metric_name, error)``.

    The worst-case-margin augmentation (:func:`_round_score_descriptor`) must
    never be what turns an otherwise-valid submission into a scored failure,
    so an `EvalError` from the augmented descriptor retries once against the
    unaugmented one before the round is recorded as invalid."""
    scored_descriptor_arg, margin_metric_name = _round_score_descriptor(scored_arg)
    try:
        return run_eval(scored_descriptor_arg, candidate_arg), margin_metric_name, None
    except EvalError:
        pass
    try:
        return run_eval(scored_arg, candidate_arg), None, None
    except EvalError as exc:
        return None, None, f"invalid submission: {exc}"


def _invalid_round_notes(report: dict[str, Any]) -> str:
    """The machine-distinguishable ``notes`` reason for a round that ran to
    completion but failed at least one of its own gates -- names the gates,
    so two invalid rounds are distinguishable in the ledger."""
    failing = [
        gate.get("name", gate.get("check"))
        for gate in report.get("gates") or []
        if gate.get("status") != "pass"
    ]
    return f"invalid submission: gate(s) failed: {', '.join(failing) or 'unknown'}"


def _round_seed_sha256(task_id: str, trimmed_history: list[dict[str, Any]]) -> str:
    """Hash of this round's input context: the task id plus the trailing
    ledger entries fed into its prompt."""
    return hashlib.sha256(
        json.dumps(
            {"task_id": task_id, "history": trimmed_history},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _freeze_round_artifacts(
    scratch_dir: Path | None, submission_dir: Path | None
) -> None:
    """Make this round's provider sandbox and the harness's own copy of its
    submission unwritable, now that the round has been scored -- a later
    round that can see either finds nothing it can edit to change how this
    one was already graded."""
    for path in (scratch_dir, submission_dir):
        if path is not None:
            _freeze_directory_readonly(path)


def _round_ledger_entry(
    *,
    task_id: str,
    round_index: int,
    submission_sha256: str | None,
    seed_sha256: str,
    agent_wall_s: float,
    round_wall_s: float,
    timed_out: bool,
    usage: dict[str, Any] | None,
    report: dict[str, Any] | None,
    valid: bool,
    score: float | None,
    notes: str | None,
) -> dict[str, Any]:
    """Assemble one ``klt.design_agent_benchmark.ledger/1`` line -- the single
    place this module's ledger shape is written, so
    `benchmarks/design-agent/schema/ledger.schema.json` has exactly one
    implementation to stay in step with.

    ``cached`` is always ``False`` here: this function is only ever called
    for a round that really ran the provider and `klt eval`. The replicated
    entries :func:`_replicated_round_entry` derives from a real round are
    the only ones that carry ``cached: true`` (issue #2295)."""
    return {
        "schema": LEDGER_SCHEMA,
        "task_id": task_id,
        "round": round_index,
        "cached": False,
        "submission_sha256": submission_sha256,
        "seed_sha256": seed_sha256,
        "agent_wall_s": agent_wall_s,
        "round_wall_s": round_wall_s,
        "timed_out": timed_out,
        "usage": usage,
        "functional": (
            {"valid": valid, "gates": report.get("gates")}
            if report is not None
            else None
        ),
        "ppa": (
            {"objective": report.get("objective"), "metrics": report.get("metrics")}
            if report is not None
            else None
        ),
        "valid": valid,
        "score": score,
        "notes": notes,
    }


def append_ledger_entry(ledger_path: Path, entry: dict[str, Any]) -> None:
    """Append one fsync'd JSON line to ``ledger_path`` -- "one fsync'd JSON
    line per round" (issue #2253) so a killed sweep leaves a durable record
    of every round that completed before the kill, not just whatever the
    OS's own write buffering happened to have flushed."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True) + "\n"
    with open(ledger_path, "a") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def best_round(entries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The highest-scoring **valid** round in ``entries`` -- ``None`` when no
    round is valid. Every invalid round's own ``score`` is always ``None``
    (:func:`run_task_round` never assigns a partial score to one), so this
    only ever compares among rounds that are both ``valid`` and carry a
    numeric ``score``; ties keep the earliest round (Python's ``max`` never
    replaces its current pick with an equal one)."""
    valid_entries = [
        e for e in entries if e.get("valid") and e.get("score") is not None
    ]
    if not valid_entries:
        return None
    return max(valid_entries, key=lambda e: e["score"])


def summarize_rounds(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-task round-mode summary: round count, the best valid round
    (:func:`best_round`), and a per-round progress series (round/valid/score
    only -- the full ledger entry is available via ``ledger_path``, added by
    :func:`run_benchmark`)."""
    return {
        "count": len(entries),
        "best": best_round(entries),
        "series": [
            {"round": e["round"], "valid": e["valid"], "score": e["score"]}
            for e in entries
        ],
    }


def _replicated_round_entry(
    first: dict[str, Any],
    round_index: int,
    task_id: str,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Round ``round_index``'s ledger entry, replicated from the one real
    round ``first`` a deterministic provider already produced (issue #2295 --
    the round-mode counterpart of :func:`run_task_attempts`'s own
    deterministic-provider shortcut).

    Everything that describes *what was submitted and how it scored*
    (``submission_sha256``, ``functional``, ``ppa``, ``valid``, ``score``,
    ``notes``, ``usage``, ``timed_out``) is copied verbatim -- that is the
    whole premise of the shortcut: a provider marked ``is_deterministic =
    True`` would have resubmitted byte-identical bytes, which `klt eval`
    would have scored identically. Everything that describes *this round's
    own execution* is corrected rather than copied:

    - ``round`` is renumbered, so :func:`summarize_rounds`'s series and
      :func:`best_round`'s tie-breaking see the same round numbering a real
      rerun would have produced.
    - ``agent_wall_s``/``round_wall_s`` are zeroed, so a ledger's wall-clock
      total reflects the one real round's actual cost rather than a
      tautological ``R``-times multiple of it (exactly what
      :func:`run_task_attempts` does to ``wall_clock_s``).
    - ``cached`` is ``True``, so a replicated round is never mistaken for a
      real one by any consumer of the ledger.
    - ``seed_sha256`` is **recomputed** from the history this round would
      have been handed (``history``, trimmed to
      :data:`ROUND_HISTORY_WINDOW`), rather than reusing round 1's. The
      field's documented meaning is "hash of this round's input context",
      and the ledger is self-describing: a reader can re-derive any line's
      ``seed_sha256`` from the lines above it. Reusing round 1's value would
      break that check for every replicated line; recomputing keeps it,
      costs one sha256 over at most three already-in-memory entries, and is
      exactly the value a real rerun would have recorded for round 2. (From
      round 3 on it necessarily differs from a real rerun, because the
      history being hashed now contains replicated entries -- zeroed wall
      times, ``cached: true``. That divergence is inherent to the shortcut
      and is why ``cached`` is on the line at all. It is harmless here
      because a deterministic provider ignores the history entirely.)
    """
    entry = copy.deepcopy(first)
    entry["round"] = round_index
    entry["agent_wall_s"] = 0.0
    entry["round_wall_s"] = 0.0
    entry["cached"] = True
    entry["seed_sha256"] = _round_seed_sha256(task_id, history[-ROUND_HISTORY_WINDOW:])
    return entry


def run_task_rounds(
    task: dict[str, Any],
    n_rounds: int,
    provider: CandidateProvider,
    repo_root: Path,
    *,
    rounds_root: Path | None = None,
    scratch_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Run ``n_rounds`` sequential rounds for ``task``
    (:func:`run_task_round`), appending each round's ledger entry to
    ``<rounds_root>/<task id>/ledger.jsonl`` (fsync'd,
    :func:`append_ledger_entry`) as it completes. ``rounds_root`` defaults to
    a fresh, per-task temp directory when omitted (mirrors every other
    scratch-directory default in this module).

    ``scratch_root`` must be the same directory the ``provider`` was itself
    constructed to write its per-round sandboxes under; pass the task's own
    round root (which is what :func:`run_benchmark` does, by building the
    provider per task) so a sandbox sits *beside* the ledger and the
    ``../ledger.jsonl`` path a round's prompt advertises really does resolve
    to this task's ledger.

    Each round's submitted files are copied into
    ``<task root>/submissions/round-<N>/`` and scored from there
    (:func:`_snapshot_submission`) -- that directory tree is created up
    front so it is never mistaken for a provider-created sandbox by
    :func:`run_task_round`'s own before/after subdirectory diff.

    Once every round has run, the ledger file itself is chmod'd read-only
    (best-effort) -- "the ledger is published read-only ... next to the
    results" (issue #2253's Reference shape).

    **Deterministic-provider caching (issue #2295)**: when ``provider`` is
    marked ``is_deterministic = True`` (only
    :func:`reference_candidate_provider` today), it is documented to return
    byte-identical output for every round *and* to ignore the round-history
    context entirely -- so rounds 2..R would re-simulate the exact same
    submission for zero additional refinement signal, only cost. Round 1 is
    therefore run for real and its ledger entry replicated across the
    remaining rounds (:func:`_replicated_round_entry`), mirroring the
    identical shortcut :func:`run_task_attempts` already applies in
    ``--attempts`` mode. Every replicated round is still appended to the
    ledger as its own line, so the ledger still has exactly ``n_rounds``
    lines and :func:`summarize_rounds`/:func:`best_round` behave identically
    to an uncached run; only ``submissions/round-1/`` exists on disk, since
    rounds 2..R submitted nothing new to snapshot (their
    ``submission_sha256`` names round 1's bytes, by definition of
    determinism).

    A provider that does not opt in (the default -- every agent-backed
    provider, issues #1732/#1739, whose whole point in round mode is that
    later rounds differ) runs every round for real, unchanged.
    """
    if n_rounds <= 0:
        return []
    task_root = (
        (rounds_root / task["id"])
        if rounds_root is not None
        else Path(
            tempfile.mkdtemp(prefix=f"design-agent-benchmark-rounds-{task['id']}-")
        )
    )
    ledger_path = task_root / LEDGER_FILENAME
    submissions_root = task_root / SUBMISSIONS_DIRNAME
    submissions_root.mkdir(parents=True, exist_ok=True)

    deterministic = bool(getattr(provider, "is_deterministic", False))
    entries: list[dict[str, Any]] = []
    for round_index in range(1, n_rounds + 1):
        if deterministic and entries:
            entry = _replicated_round_entry(
                entries[0], round_index, task["id"], entries
            )
        else:
            entry = run_task_round(
                task,
                round_index,
                provider,
                repo_root,
                entries,
                scratch_root=scratch_root,
                submission_dir=submissions_root / f"round-{round_index}",
            )
        append_ledger_entry(ledger_path, entry)
        entries.append(entry)

    try:
        os.chmod(ledger_path, 0o444)
    except OSError:
        pass
    return entries


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    schema_result = validate_tasks(
        Path(args.tasks_dir), Path(args.schema), Path(args.repo_root)
    )
    run_slow_checks = schema_result["valid"] and not args.schema_only
    ref_result = (
        check_reference_solutions(Path(args.tasks_dir), Path(args.repo_root))
        if run_slow_checks
        else None
    )
    mutation_result = (
        check_mutation_gates(
            Path(args.tasks_dir),
            Path(args.repo_root),
            Path(args.mutations_schema),
        )
        if run_slow_checks and not args.skip_mutation_gates
        else None
    )
    payload = {
        "schema": schema_result,
        "reference_solutions": ref_result,
        "mutation_gates": mutation_result,
    }
    print(json.dumps(payload, indent=2))
    ok = (
        schema_result["valid"]
        and (ref_result is None or ref_result["valid"])
        and (mutation_result is None or mutation_result["valid"])
    )
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


def _resolve_provider(
    args: argparse.Namespace, *, scratch_root_override: Path | None = None
) -> CandidateProvider:
    """Build the `CandidateProvider` `run`'s ``--provider`` flag selected.
    The agent-backed providers are built fresh (rather than reusing the
    module-level singletons) so ``--agent-timeout-s`` and friends actually
    take effect.

    ``scratch_root_override``, when given, takes priority over
    ``--agent-sandbox-root`` -- round mode (``--rounds``, issue #2253) calls
    this once per task with that task's own round root, so each round's
    sandbox lands beside that task's ``ledger.jsonl`` (making the
    ``../ledger.jsonl`` its prompt advertises resolve) and
    :func:`run_task_round` has a directory to watch for the sandbox each
    round creates. `--attempts` mode keeps using ``--agent-sandbox-root``
    unchanged: the two modes get separately-built providers.
    """
    timeout_s = _resolve_agent_timeout(args.provider, args.agent_timeout_s)
    if args.provider == "live-agent":
        return make_live_agent_provider(
            agent_timeout_s=timeout_s, scratch_root=scratch_root_override
        )
    if args.provider == "interactive-agent":
        sandbox_root = scratch_root_override or (
            Path(args.agent_sandbox_root) if args.agent_sandbox_root else None
        )
        return make_interactive_agent_provider(
            agent_timeout_s=timeout_s,
            tool_call_budget=args.agent_tool_budget,
            sandbox_root=sandbox_root,
        )
    return reference_candidate_provider


def _cmd_run(args: argparse.Namespace) -> int:
    rounds_root = (
        Path(args.rounds_root)
        if args.rounds_root
        else (
            Path(tempfile.mkdtemp(prefix="design-agent-benchmark-rounds-"))
            if args.rounds > 0
            else None
        )
    )
    result = run_benchmark(
        tasks_dir=Path(args.tasks_dir),
        repo_root=Path(args.repo_root),
        n_attempts=args.attempts,
        ks=args.k,
        provider=_resolve_provider(args),
        provider_name=args.provider,
        n_rounds=args.rounds,
        rounds_root=rounds_root,
        round_provider_factory=(
            (lambda root: _resolve_provider(args, scratch_root_override=root))
            if args.rounds > 0
            else None
        ),
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
    if args.rounds > 0:
        round_bits = ", ".join(
            f"{t['id']}=best:{(t['rounds']['best'] or {}).get('score')}"
            for t in result["tasks"]
        )
        print(
            f"design-agent-benchmark ({result['provider']}): "
            f"{args.rounds} rounds/task, ledgers under {rounds_root} ({round_bits})",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help=(
            "schema-validate the task set, check reference solutions, and "
            "enforce every task's declared mutation gate"
        ),
    )
    validate_parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR))
    validate_parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH))
    validate_parser.add_argument(
        "--mutations-schema", default=str(DEFAULT_MUTATIONS_SCHEMA_PATH)
    )
    validate_parser.add_argument("--repo-root", default=str(REPO_ROOT))
    validate_parser.add_argument(
        "--schema-only",
        action="store_true",
        help=(
            "skip the (slower) reference-solution eval run, and with it the "
            "mutation gates"
        ),
    )
    validate_parser.add_argument(
        "--skip-mutation-gates",
        action="store_true",
        help=(
            "skip the per-task mutation gates (issue #2262) while still "
            "running the reference-solution check -- for a local run that "
            "only cares whether the reference solutions still pass"
        ),
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
            "evidence (netlist, `klt sim` artifacts, session transcript). "
            "Applies to --attempts mode only -- --rounds puts each round's "
            "sandbox under its own task directory in --rounds-root, beside "
            "that task's ledger.jsonl"
        ),
    )
    run_parser.add_argument(
        "--rounds",
        type=int,
        default=0,
        help=(
            "run N sequential refinement rounds per task, in addition to "
            "--attempts' pass@k (orthogonal, issue #2253): each round's "
            "prompt carries the last 3 rounds' evaluation results, and every "
            "round is appended to a per-task ledger.jsonl under "
            "--rounds-root. 0 (the default) skips round mode entirely -- "
            "--attempts' pass@k output is byte-for-byte unchanged"
        ),
    )
    run_parser.add_argument(
        "--rounds-root",
        default=None,
        help=(
            "parent directory for --rounds' per-task <task-id>/ directory: "
            "its ledger.jsonl, each round's own sandbox, and the harness's "
            "private submissions/round-N/ copy of what it scored (default: "
            "the system temp dir). Each round's sandbox is chmod'd read-only "
            "once it has been scored, and the ledger.jsonl itself is chmod'd "
            "read-only once every round has run"
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
