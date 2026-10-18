"""Layout-flow benchmark protocol validation (issue #3024, epic #2252).

Credential-free, simulation-free validation of the three-arm layout-flow
benchmark's frozen protocol (``benchmarks/design-agent/layout-protocol.json``),
its layout task documents, and its per-run records. Split out of
``design_agent_benchmark.py`` (as ``design_agent_benchmark_interactive.py``
was, issue #1766) so that file does not grow; ``design_agent_benchmark.py
validate-layout`` is the CLI entry point.

Three document types, each with a JSON Schema beside the existing task /
mutations / ledger schemas (``benchmarks/design-agent/schema/layout-*.schema.json``)
plus the cross-field rules a schema cannot express, enforced here:

* :func:`validate_protocol` -- arms only use declared tool classes, the
  unseeded arms cannot reach each other's tools, the seeded arm is seeded from
  ``gen-only``, the failure table scores only ``valid`` runs, and the budget
  policy matches the interactive provider's real constants.
* :func:`validate_task` -- schema, repo-relative frozen paths, and (given a
  repo root) that each named file exists and hashes to its recorded value.
* :func:`validate_run` / :func:`validate_run_set` -- every recorded tool call
  is allowed by its arm's phase (cross-arm contamination is rejected), the
  status agrees with the recorded validity checks and their hashes, an
  unscored run carries no score, the seed identity recomputes, and a
  ``gen+refine`` run is seeded from a ``valid`` ``gen-only`` run of the same
  (task, agent, replicate) and never scores worse than that seed.

The protocol, task and run validators above run nothing. The corpus gate at the
bottom (:func:`check_corpus`, issue #3025) calls ``klt``'s ``drc`` / ``lvs`` /
``gen`` / ``gen-compose`` library entry points in-process on the frozen corpus;
it needs no credentials, no network, and never ``ngspice`` or an agent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema
import referencing
from design_agent_benchmark_interactive import (
    DEFAULT_INTERACTIVE_TIMEOUT_S,
    DEFAULT_TOOL_CALL_BUDGET,
)
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = REPO_ROOT / "benchmarks" / "design-agent"
SCHEMA_DIR = BENCHMARK_DIR / "schema"
DEFAULT_PROTOCOL_PATH = BENCHMARK_DIR / "layout-protocol.json"
CORPUS_DIR = BENCHMARK_DIR / "layout-corpus"

PROTOCOL_SCHEMA_NAME = "layout-protocol.schema.json"
TASK_SCHEMA_NAME = "layout-task.schema.json"
RUN_SCHEMA_NAME = "layout-run.schema.json"

ARM_IDS = ("draw-only", "gen-only", "gen+refine")
SEED_ARM = "gen-only"
SEEDED_ARM = "gen+refine"
SCORED_STATUS = "valid"
#: Exit code the klt-shim records for a call it refused (protocol tool_recording).
REFUSED_EXIT_CODE = 126
#: Statuses that are legitimate without any recorded validity checks (the run
#: never reached the scorer).
_NO_VALIDITY_STATUSES = frozenset(
    {
        "invalid_tool_surface",
        "timeout",
        "budget_exhausted",
        "agent_error",
        "not_run_no_seed",
        "interrupted",
    }
)


class LayoutProtocolError(Exception):
    """An environment problem (missing schema file) -- never a document
    problem, which is reported in the returned ``errors`` list."""


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


def _load_schemas(schema_dir: Path) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir.glob("*.schema.json")):
        schemas[path.name] = json.loads(path.read_text())
    return schemas


def _validator(
    name: str, schema_dir: Path = SCHEMA_DIR
) -> jsonschema.protocols.Validator:
    """Validator for ``schema_dir/name``. Sibling schemas resolve through a
    registry keyed on their ``$id``, so ``layout-task`` can ``$ref`` into
    ``task.schema.json`` without a network fetch."""
    schemas = _load_schemas(schema_dir)
    if name not in schemas:
        raise LayoutProtocolError(f"schema file not found: {schema_dir / name}")
    registry: referencing.Registry = referencing.Registry()
    for document in schemas.values():
        resource = referencing.Resource.from_contents(
            document, default_specification=DRAFT202012
        )
        registry = registry.with_resource(document["$id"], resource)
    schema = schemas[name]
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, registry=registry)


def _schema_errors(
    validator: jsonschema.protocols.Validator, document: Any
) -> list[str]:
    errors = []
    for error in sorted(validator.iter_errors(document), key=str):
        location = "/".join(str(part) for part in error.path)
        errors.append(f"{location}: {error.message}" if location else error.message)
    return errors


# --------------------------------------------------------------------------
# Hashing and identity
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """The protocol's canonicalization: keys sorted, compact separators,
    ASCII-escaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def document_sha256(document: Any) -> str:
    """Fingerprint of a protocol or task document (``protocol_sha256`` /
    ``task_sha256``): sha256 of its canonical JSON."""
    return hashlib.sha256(canonical_json(document).encode("ascii")).hexdigest()


def run_seed_sha256(
    protocol: dict[str, Any],
    *,
    task_id: str,
    task_sha256: str,
    arm: str,
    agent_id: str,
    replicate: int,
    seed_artifact_sha256: str | None = None,
) -> str:
    """The seed identity of one run, per the protocol's ``seed_identity``.

    A seeded arm must pass ``seed_artifact_sha256``, so the same
    (task, agent, replicate) seeded from a different artifact is a different
    run; an unseeded arm must not."""
    spec = protocol["seed_identity"]
    values: dict[str, Any] = {
        "protocol_version": protocol["protocol_version"],
        "task_id": task_id,
        "task_sha256": task_sha256,
        "arm": arm,
        "agent_id": agent_id,
        "replicate": replicate,
    }
    payload = {field: values[field] for field in spec["fields"]}
    if seed_artifact_sha256 is not None:
        payload[spec["seeded_arm_extra_fields"][0]] = seed_artifact_sha256
    return hashlib.sha256(canonical_json(payload).encode("ascii")).hexdigest()


def load_run_records(path: Path) -> list[dict[str, Any]]:
    """Run records from a JSON array file, or from an executor ``runs.jsonl``
    ledger (one record per line, issue #3026)."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


def load_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LayoutProtocolError(f"{path}: failed to load protocol: {exc}") from exc


def _class_of_verb(protocol: dict[str, Any]) -> dict[str, str]:
    return {
        verb: cls
        for cls, verbs in protocol.get("tool_classes", {}).items()
        for verb in verbs
    }


def _disjoint_class_errors(classes: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for cls, verbs in classes.items():
        for verb in verbs:
            if verb in seen:
                errors.append(
                    f"tool_classes: verb {verb!r} is in both {seen[verb]!r} and "
                    f"{cls!r}; classes must be disjoint"
                )
            seen[verb] = cls
    return errors


def _arm_phase_errors(
    arms: dict[str, dict[str, Any]], classes: dict[str, list[str]]
) -> list[str]:
    errors: list[str] = []
    for arm_id, arm in arms.items():
        phase_ids = [phase["id"] for phase in arm["phases"]]
        if len(set(phase_ids)) != len(phase_ids):
            errors.append(f"arms/{arm_id}: duplicate phase ids {phase_ids}")
        for phase in arm["phases"]:
            for cls in phase["allowed_classes"]:
                if cls not in classes:
                    errors.append(
                        f"arms/{arm_id}/{phase['id']}: allowed class {cls!r} is not "
                        f"declared in tool_classes"
                    )
    return errors


def _arm_allowed_classes(arm: dict[str, Any]) -> set[str]:
    return {c for p in arm["phases"] for c in p["allowed_classes"]}


def _arm_class_policy_errors(arms: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if "generate" in _arm_allowed_classes(arms["draw-only"]):
        errors.append("arms/draw-only: must not allow the 'generate' class")
    if "draw" in _arm_allowed_classes(arms["gen-only"]):
        errors.append("arms/gen-only: must not allow the 'draw' class")
    if not {"generate", "draw"} <= _arm_allowed_classes(arms[SEEDED_ARM]):
        errors.append("arms/gen+refine: must allow both 'generate' and 'draw'")
    return errors


def _arm_seed_errors(protocol: dict[str, Any], arms: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for arm_id in ("draw-only", "gen-only"):
        if arms[arm_id]["seed"] is not None:
            errors.append(f"arms/{arm_id}: unseeded arm must have seed: null")
    seed = arms[SEEDED_ARM]["seed"]
    if seed is None:
        errors.append("arms/gen+refine: must be seeded (seed is null)")
    elif seed["source_arm"] != SEED_ARM:
        errors.append(f"arms/gen+refine: seed.source_arm must be {SEED_ARM!r}")

    decision = protocol["seeding_decision"]["decision"]
    if decision == "same_run_best_valid_gen_only" and (
        seed is None or seed["policy"] != "same_run_best_valid"
    ):
        errors.append(
            "seeding_decision: 'same_run_best_valid_gen_only' requires "
            "arms/gen+refine seed.policy 'same_run_best_valid'"
        )
    return errors


def _status_reference_errors(
    protocol: dict[str, Any], seed: dict[str, Any] | None
) -> list[str]:
    errors: list[str] = []
    statuses = {s["id"]: s for s in protocol["failure_semantics"]["statuses"]}
    run_statuses = _run_status_enum()
    if sorted(statuses) != sorted(run_statuses):
        errors.append(
            "failure_semantics: statuses must be exactly the run schema's "
            f"status enum {sorted(run_statuses)}"
        )
    for status_id, status in statuses.items():
        if status["scored"] != (status_id == SCORED_STATUS):
            errors.append(
                f"failure_semantics/{status_id}: only {SCORED_STATUS!r} may be scored"
            )
    if protocol["tool_recording"]["violation_status"] not in statuses:
        errors.append("tool_recording.violation_status: not a declared status")
    if protocol["budget"]["exhaustion_status"] not in statuses:
        errors.append("budget.exhaustion_status: not a declared status")
    if seed is not None and seed["unseedable_status"] not in statuses:
        errors.append("arms/gen+refine: seed.unseedable_status not a declared status")
    return errors


def _score_errors(score: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    fields = [f["id"] for f in score["fields"]]
    if score["primary"] not in fields:
        errors.append("score.primary: not listed in score.fields")
    for field in score["fields"]:
        if field["id"] == score["primary"] and not field["required"]:
            errors.append("score.primary: the primary score field must be required")
    return errors


def _declared_set_errors(protocol: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if sorted(protocol["tool_recording"]["record_fields"]) != sorted(
        ["seq", "phase", "verb", "class", "exit_code"]
    ):
        errors.append(
            "tool_recording.record_fields: must be the run schema's five fields"
        )
    eligibility = [rule["id"] for rule in protocol["task_eligibility"]["rules"]]
    if len(set(eligibility)) != len(eligibility):
        errors.append("task_eligibility: duplicate rule ids")
    if sorted(eligibility) != sorted(_ELIGIBILITY_RULES):
        errors.append(f"task_eligibility: expected rules {sorted(_ELIGIBILITY_RULES)}")
    if sorted(protocol["validity"]["required_checks"]) != ["drc", "lvs", "provenance"]:
        errors.append("validity.required_checks: must be drc, lvs and provenance")
    if sorted(protocol["seed_identity"]["fields"]) != sorted(_SEED_FIELDS):
        errors.append(f"seed_identity.fields: expected {sorted(_SEED_FIELDS)}")
    return errors


def _budget_errors(per_run: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if per_run["agent_timeout_s"] != DEFAULT_INTERACTIVE_TIMEOUT_S:
        errors.append(
            f"budget.per_run.agent_timeout_s: {per_run['agent_timeout_s']} "
            f"differs from "
            f"the interactive provider's DEFAULT_INTERACTIVE_TIMEOUT_S "
            f"({DEFAULT_INTERACTIVE_TIMEOUT_S})"
        )
    if per_run["tool_call_budget"] != DEFAULT_TOOL_CALL_BUDGET:
        errors.append(
            f"budget.per_run.tool_call_budget: {per_run['tool_call_budget']} "
            f"differs from "
            f"the interactive provider's DEFAULT_TOOL_CALL_BUDGET "
            f"({DEFAULT_TOOL_CALL_BUDGET})"
        )
    return errors


def _protocol_semantic_errors(protocol: dict[str, Any]) -> list[str]:
    classes: dict[str, list[str]] = protocol["tool_classes"]
    errors = _disjoint_class_errors(classes)

    arms = {arm["id"]: arm for arm in protocol["arms"]}
    if sorted(arms) != sorted(ARM_IDS):
        errors.append(f"arms: expected exactly {sorted(ARM_IDS)}, got {sorted(arms)}")
        return errors

    errors += _arm_phase_errors(arms, classes)
    errors += _arm_class_policy_errors(arms)
    errors += _arm_seed_errors(protocol, arms)
    errors += _status_reference_errors(protocol, arms[SEEDED_ARM]["seed"])
    errors += _declared_set_errors(protocol)
    errors += _score_errors(protocol["score"])
    errors += _budget_errors(protocol["budget"]["per_run"])
    return errors


_ELIGIBILITY_RULES = (
    "frozen_netlist",
    "reference_layout_valid",
    "generator_reachable",
    "negative_controls",
    "open_pdk",
)
_SEED_FIELDS = (
    "protocol_version",
    "task_id",
    "task_sha256",
    "arm",
    "agent_id",
    "replicate",
)


def _run_status_enum(schema_dir: Path = SCHEMA_DIR) -> list[str]:
    schema = json.loads((schema_dir / RUN_SCHEMA_NAME).read_text())
    return list(schema["properties"]["status"]["enum"])


def validate_protocol(protocol: Any, schema_dir: Path = SCHEMA_DIR) -> dict[str, Any]:
    """Schema-validate a layout protocol document, then enforce its
    cross-field rules. Returns ``{"valid", "errors", "protocol_sha256"}``;
    ``protocol_sha256`` is ``None`` when the document is not valid."""
    errors = _schema_errors(_validator(PROTOCOL_SCHEMA_NAME, schema_dir), protocol)
    if not errors:
        errors = _protocol_semantic_errors(protocol)
    return {
        "valid": not errors,
        "errors": errors,
        "protocol_sha256": None if errors else document_sha256(protocol),
    }


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------


def _frozen_paths(task: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(label, repo-relative path, expected sha256)`` for every frozen file."""
    entries = [
        (
            "frozen_netlist",
            task["frozen_netlist"]["path"],
            task["frozen_netlist"]["sha256"],
        ),
        (
            "reference_layout",
            task["reference_layout"]["path"],
            task["reference_layout"]["sha256"],
        ),
        (
            "generator_recipe",
            task["generator_recipe"]["path"],
            task["generator_recipe"]["sha256"],
        ),
    ]
    for index, control in enumerate(task["negative_controls"]):
        entries.append(
            (f"negative_controls/{index}", control["path"], control["sha256"])
        )
    return entries


def validate_task(
    task: Any,
    *,
    filename_stem: str | None = None,
    repo_root: Path | None = None,
    schema_dir: Path = SCHEMA_DIR,
) -> dict[str, Any]:
    """Schema-validate a layout task. With ``filename_stem`` the ``id`` must
    match it; with ``repo_root`` every frozen path must exist there and hash
    to its recorded sha256 (omitted for synthetic fixtures)."""
    errors = _schema_errors(_validator(TASK_SCHEMA_NAME, schema_dir), task)
    if not errors:
        if filename_stem is not None and task["id"] != filename_stem:
            errors.append(
                f"id {task['id']!r} does not match filename stem {filename_stem!r}"
            )
        seen_controls: set[str] = set()
        for control in task["negative_controls"]:
            if control["id"] in seen_controls:
                errors.append(f"negative_controls: duplicate id {control['id']!r}")
            seen_controls.add(control["id"])
        for label, rel, expected in _frozen_paths(task):
            if ".." in Path(rel).parts:
                errors.append(
                    f"{label}: must be a repository-relative path without '..': {rel}"
                )
            elif repo_root is not None:
                target = repo_root / rel
                if not target.is_file():
                    errors.append(f"{label}: referenced path does not exist: {rel}")
                elif _file_sha256(target) != expected:
                    errors.append(
                        f"{label}: sha256 of {rel} does not match the recorded value"
                    )
    return {
        "valid": not errors,
        "errors": errors,
        "task_sha256": None if errors else document_sha256(task),
    }


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def _arm(protocol: dict[str, Any], arm_id: str) -> dict[str, Any]:
    return next(arm for arm in protocol["arms"] if arm["id"] == arm_id)


def _tool_call_errors(protocol: dict[str, Any], run: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    arm = _arm(protocol, run["arm"])
    phases = {phase["id"]: phase for phase in arm["phases"]}
    class_of = _class_of_verb(protocol)
    last_seq = 0
    for call in run["tool_calls"]:
        where = f"tool_calls/seq {call['seq']}"
        if call["seq"] <= last_seq:
            errors.append(f"{where}: seq must be strictly increasing")
        last_seq = call["seq"]
        expected_class = class_of.get(call["verb"])
        if expected_class is None:
            errors.append(f"{where}: verb {call['verb']!r} is not in any tool class")
            continue
        if call["class"] != expected_class:
            errors.append(
                f"{where}: verb {call['verb']!r} recorded as class {call['class']!r}, "
                f"protocol says {expected_class!r}"
            )
        phase = phases.get(call["phase"])
        if phase is None:
            errors.append(
                f"{where}: phase {call['phase']!r} does not exist in arm "
                f"{run['arm']!r} (cross-arm contamination)"
            )
        elif (
            expected_class not in phase["allowed_classes"]
            # A refused call (exit 126) is the shim *recording* the violation
            # (protocol tool_recording): it is evidence for invalid_tool_surface,
            # not contamination of the scored artifact.
            and call["exit_code"] != REFUSED_EXIT_CODE
        ):
            errors.append(
                f"{where}: verb {call['verb']!r} (class {expected_class!r}) "
                f"is not allowed "
                f"in arm {run['arm']!r} phase {call['phase']!r} "
                f"(cross-arm contamination)"
            )
    return errors


def _valid_run_errors(
    protocol: dict[str, Any], run: dict[str, Any], task: dict[str, Any] | None
) -> list[str]:
    errors: list[str] = []
    gds = run["artifact"]["gds_sha256"]
    drc, lvs = run["validity"]["drc"], run["validity"]["lvs"]
    drc_pass = protocol["validity"]["drc"]["pass_status"]
    lvs_pass = protocol["validity"]["lvs"]["pass_status"]
    if drc["status"] != drc_pass:
        errors.append(
            f"status valid but drc status is {drc['status']!r}, not {drc_pass!r}"
        )
    if lvs["status"] != lvs_pass:
        errors.append(
            f"status valid but lvs status is {lvs['status']!r}, not {lvs_pass!r}"
        )
    for name, check in (("drc", drc), ("lvs", lvs)):
        if check["gds_sha256"] != gds:
            errors.append(
                f"provenance: {name} report was produced on a different GDS than "
                f"the scored artifact"
            )
    if "netlist_sha256" not in lvs:
        errors.append("lvs: netlist_sha256 is required on a valid run")
    elif task is not None and lvs["netlist_sha256"] != task["frozen_netlist"]["sha256"]:
        errors.append(
            "provenance: lvs compared against a netlist other than the "
            "task's frozen one"
        )
    return errors


def _lvs_status_errors(
    protocol: dict[str, Any], drc: dict[str, Any] | None, lvs: dict[str, Any] | None
) -> list[str]:
    errors: list[str] = []
    drc_pass = protocol["validity"]["drc"]["pass_status"]
    lvs_pass = protocol["validity"]["lvs"]["pass_status"]
    if drc is None or drc["status"] != drc_pass:
        errors.append("status invalid_lvs requires a clean drc check")
    if lvs is None or lvs["status"] == lvs_pass:
        errors.append("status invalid_lvs but no failing lvs check is recorded")
    return errors


def _provenance_status_errors(
    protocol: dict[str, Any],
    artifact: dict[str, Any],
    drc: dict[str, Any] | None,
    lvs: dict[str, Any] | None,
) -> list[str]:
    drc_pass = protocol["validity"]["drc"]["pass_status"]
    lvs_pass = protocol["validity"]["lvs"]["pass_status"]
    bound = all(
        check is not None and check["gds_sha256"] == artifact["gds_sha256"]
        for check in (drc, lvs)
    )
    passing = (
        drc is not None
        and lvs is not None
        and drc["status"] == drc_pass
        and lvs["status"] == lvs_pass
    )
    if bound and passing:
        return [
            "status invalid_provenance but drc/lvs pass and are bound to the artifact"
        ]
    return []


def _scorer_ran_errors(protocol: dict[str, Any], run: dict[str, Any]) -> list[str]:
    """invalid_drc / invalid_lvs / invalid_provenance: the scorer ran."""
    status, artifact, validity = run["status"], run["artifact"], run["validity"]
    if artifact is None or validity is None:
        return [f"status {status!r} requires an artifact and recorded validity checks"]
    drc, lvs = validity["drc"], validity["lvs"]
    drc_pass = protocol["validity"]["drc"]["pass_status"]
    errors: list[str] = []
    if status == "invalid_drc" and (drc is None or drc["status"] == drc_pass):
        errors.append("status invalid_drc but no failing drc check is recorded")
    if status == "invalid_lvs":
        errors += _lvs_status_errors(protocol, drc, lvs)
    if status == "invalid_provenance":
        errors += _provenance_status_errors(protocol, artifact, drc, lvs)
    return errors


def _validity_errors(
    protocol: dict[str, Any], run: dict[str, Any], task: dict[str, Any] | None
) -> list[str]:
    """Status vs. recorded checks vs. provenance. A ``valid`` run needs
    clean DRC, matching LVS, and every hash bound to the one scored GDS."""
    status = run["status"]
    if status == SCORED_STATUS:
        return _valid_run_errors(protocol, run, task)

    errors: list[str] = []
    if run["score"] is not None:
        errors.append(f"status {status!r} must not carry a score")
    if status in _NO_VALIDITY_STATUSES:
        if run["artifact"] is not None and status == "not_run_no_seed":
            errors.append("not_run_no_seed must not carry an artifact")
        return errors
    return errors + _scorer_ran_errors(protocol, run)


def _run_binding_errors(
    protocol: dict[str, Any], run: dict[str, Any], task: dict[str, Any] | None
) -> list[str]:
    errors: list[str] = []
    if run["protocol_sha256"] != document_sha256(protocol):
        errors.append("protocol_sha256 does not match the protocol document")
    if task is not None:
        if run["task_id"] != task["id"]:
            errors.append(
                f"task_id {run['task_id']!r} does not match task {task['id']!r}"
            )
        if run["task_sha256"] != document_sha256(task):
            errors.append("task_sha256 does not match the task document")
    return errors


def _seed_from_errors(run: dict[str, Any], arm: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    link = run["seed_from"]
    if arm["seed"] is None:
        if link is not None:
            errors.append(f"arm {run['arm']!r} is unseeded: seed_from must be null")
    elif link is None:
        if run["status"] != arm["seed"]["unseedable_status"]:
            errors.append(f"arm {run['arm']!r} is seeded: seed_from is required")
        elif run["tool_calls"]:
            errors.append("a run that was never seeded must record no tool calls")
    return errors


def _seed_identity_errors(protocol: dict[str, Any], run: dict[str, Any]) -> list[str]:
    seed_artifact = run["seed_from"]["gds_sha256"] if run["seed_from"] else None
    expected_seed = run_seed_sha256(
        protocol,
        task_id=run["task_id"],
        task_sha256=run["task_sha256"],
        arm=run["arm"],
        agent_id=run["agent"]["id"],
        replicate=run["replicate"],
        seed_artifact_sha256=seed_artifact,
    )
    if run["seed_sha256"] != expected_seed:
        return ["seed_sha256 does not match the recomputed seed identity"]
    return []


def _refusal_errors(run: dict[str, Any]) -> list[str]:
    if run["status"] != "invalid_tool_surface" and any(
        call["exit_code"] == REFUSED_EXIT_CODE for call in run["tool_calls"]
    ):
        return [
            "a tool call was refused (exit 126) but status is not invalid_tool_surface"
        ]
    return []


def validate_run(
    run: Any,
    protocol: dict[str, Any],
    task: dict[str, Any] | None = None,
    *,
    schema_dir: Path = SCHEMA_DIR,
) -> dict[str, Any]:
    """Validate one run record against the protocol (and, if given, the task
    it names). Cross-run rules (seed linkage) are :func:`validate_run_set`."""
    errors = _schema_errors(_validator(RUN_SCHEMA_NAME, schema_dir), run)
    if errors:
        return {"valid": False, "errors": errors}

    errors += _run_binding_errors(protocol, run, task)
    errors += _seed_from_errors(run, _arm(protocol, run["arm"]))
    errors += _seed_identity_errors(protocol, run)
    errors += _tool_call_errors(protocol, run)
    errors += _refusal_errors(run)
    errors += _validity_errors(protocol, run, task)
    return {"valid": not errors, "errors": errors}


def _seed_run_match_errors(
    where: str, run: dict[str, Any], seed_run: dict[str, Any], link: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    for key in ("task_id", "replicate"):
        if seed_run[key] != run[key]:
            errors.append(
                f"{where}: seed run {key} differs (seed must be the same run's)"
            )
    if seed_run["agent"]["id"] != run["agent"]["id"]:
        errors.append(
            f"{where}: seed run agent differs (seed must be the same agent's)"
        )
    if seed_run["artifact"]["gds_sha256"] != link["gds_sha256"]:
        errors.append(
            f"{where}: seed_from.gds_sha256 differs from the seed run's artifact"
        )
    return errors


def _seed_linkage_errors(
    run: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> list[str]:
    link = run["seed_from"]
    where = f"run {run['run_id']!r}"
    seed_run = by_id.get(link["run_id"])
    if seed_run is None:
        return [f"{where}: seed run {link['run_id']!r} is not in the run set"]
    errors: list[str] = []
    if seed_run["arm"] != SEED_ARM:
        errors.append(f"{where}: seed run is arm {seed_run['arm']!r}, not {SEED_ARM!r}")
    if seed_run["status"] != SCORED_STATUS:
        errors.append(f"{where}: seed run status is {seed_run['status']!r}, not valid")
        return errors
    errors += _seed_run_match_errors(where, run, seed_run, link)
    if run["status"] == SCORED_STATUS and (
        run["score"]["area_um2"] > seed_run["score"]["area_um2"]
    ):
        errors.append(
            f"{where}: scores worse than its seed; the seed is round zero of the "
            f"refine phase so the artifact must be the seed or better"
        )
    return errors


def validate_run_set(
    runs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Cross-run rules over a set of (individually valid) run records: unique
    ids, and every ``gen+refine`` run seeded from a ``valid`` ``gen-only`` run
    of the same (task, agent, replicate) whose artifact hash it records, and
    scoring no worse than that seed (the seed is round zero)."""
    runs = list(runs)
    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run["run_id"] in by_id:
            errors.append(f"duplicate run_id {run['run_id']!r}")
        by_id[run["run_id"]] = run
    for run in runs:
        if run["arm"] == SEEDED_ARM and run["seed_from"] is not None:
            errors += _seed_linkage_errors(run, by_id)
    return {"valid": not errors, "errors": errors}


# --------------------------------------------------------------------------
# Corpus gate (issue #3025)
# --------------------------------------------------------------------------

EXCLUSIONS_SCHEMA_NAME = "layout-exclusions.schema.json"
RECIPE_SCHEMA = "klt.design_agent_benchmark.layout_recipe/1"
CORPUS_REPORT_SCHEMA = "klt.design_agent_benchmark.layout_corpus_report/1"
GATE_DECK = "sky130"
CORPUS_TASKS_MIN = 4
CORPUS_TASKS_MAX = 6
_TASK_FILE = "task.json"
_EXCLUSIONS_FILE = "exclusions.json"


class RecipeError(Exception):
    """A generator recipe did not reach a layout (the recipe is the evidence)."""


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_recipe_step(
    step: dict[str, Any], variant: str, workdir: Path, seen: set[str], gds: Path
) -> dict[str, Any]:
    """Run one recipe step and return its generator/compose report."""
    from klayout_tools.gen import GenError, generate
    from klayout_tools.gen_compose import GenComposeError, compose

    step_id = step["id"]
    options = {"cell_name": step["cell_name"], "output": str(gds)}
    try:
        if step["verb"] == "gen":
            return generate(
                {
                    "schema": "klt.gen.request/1",
                    "generator": step["generator"],
                    "pdk": {"variant": variant},
                    "params": step["params"],
                    "options": options,
                }
            )
        if step["verb"] != "gen-compose":
            raise RecipeError(f"{step_id}: unknown verb {step['verb']!r}")
        request = json.loads(json.dumps(step["request"]))
        request["pdk"] = {"variant": variant}
        request["options"] = options
        for block in request["blocks"]:
            ref = block.pop("step")
            if ref not in seen:
                raise RecipeError(
                    f"{step_id}: block step {ref!r} is not an earlier step"
                )
            block["generator_report"] = str(workdir / f"{ref}.json")
        report = compose(request, request_dir=str(workdir))
        if report["unrouted_nets"]:
            raise RecipeError(
                f"{step_id}: gen-compose left nets unrouted: "
                f"{sorted(map(str, report['unrouted_nets']))}"
            )
        return report
    except (GenError, GenComposeError) as exc:
        raise RecipeError(f"{step_id}: {exc}") from exc


def run_recipe(recipe: dict[str, Any], workdir: Path) -> Path:
    """Execute a frozen generator recipe with ``klt``'s ``gen`` / ``gen-compose``
    library entry points and return the path of the layout the last step wrote.

    A recipe is ``{"schema", "pdk", "steps": [...]}``. A ``gen`` step carries
    ``generator``/``params``; a ``gen-compose`` step carries a ``request`` whose
    ``blocks[].step`` names an earlier step (rewritten to that step's generator
    report). Every step takes an explicit ``cell_name``. ``gen-compose`` with any
    ``unrouted_nets`` is a :class:`RecipeError` -- a recipe is only evidence of
    reachability if it produced a fully wired layout.
    """
    if recipe.get("schema") != RECIPE_SCHEMA:
        raise RecipeError(f"recipe schema must be {RECIPE_SCHEMA!r}")
    workdir.mkdir(parents=True, exist_ok=True)
    variant = recipe["pdk"]
    last: Path | None = None
    seen: set[str] = set()
    for step in recipe["steps"]:
        step_id = step["id"]
        if step_id in seen:
            raise RecipeError(f"duplicate step id {step_id!r}")
        gds = workdir / f"{step_id}.gds"
        report = _run_recipe_step(step, variant, workdir, seen, gds)
        (workdir / f"{step_id}.json").write_text(json.dumps(report, indent=2))
        seen.add(step_id)
        last = gds
    if last is None or not last.is_file():
        raise RecipeError("recipe produced no layout")
    return last


def run_checks(gds: Path, netlist: Path, top: str, workdir: Path) -> dict[str, Any]:
    """The scorer's own DRC + LVS of ``gds`` against the frozen ``netlist``, run
    on a private copy of the artifact (never the agent's file). Returns the two
    reports and the sha256 of the copy that was actually checked."""
    from klayout_tools.drc import run_drc
    from klayout_tools.lvs import run_lvs

    workdir.mkdir(parents=True, exist_ok=True)
    private = workdir / "scored.gds"
    shutil.copyfile(gds, private)
    drc_report = run_drc(str(private), GATE_DECK)
    lvs_report = run_lvs(
        json.dumps(
            {
                "schema": "klt.lvs.request/1",
                "layout": {"file": str(private), "deck": GATE_DECK, "top": top},
                "reference": {"netlist": str(netlist), "top": top},
            }
        )
    )
    return {
        "drc_report": drc_report,
        "lvs_report": lvs_report,
        "checked_sha256": _sha256_path(private),
    }


def provenance_errors(
    scored_sha256: str,
    drc_report: dict[str, Any],
    lvs_report: dict[str, Any],
    netlist_sha256: str,
) -> list[str]:
    """The protocol's provenance rule: the scored GDS is the artifact both
    reports checked, and the LVS reference is the task's frozen netlist."""
    errors = []
    drc_hash = str(drc_report["provenance"]["input"]["content_hash"]).removeprefix(
        "sha256:"
    )
    env = lvs_report["environment"]
    if drc_hash != scored_sha256:
        errors.append("scored GDS sha256 does not match the DRC report's checked input")
    if env["layout_sha256"] != scored_sha256:
        errors.append(
            "scored GDS sha256 does not match the LVS report's checked layout"
        )
    if env["reference_sha256"] != netlist_sha256:
        errors.append("LVS reference is not the task's frozen netlist")
    return errors


def gate_verdict(
    scored_sha256: str, checks: dict[str, Any], netlist_sha256: str
) -> dict[str, Any]:
    """Per-check booleans for the three required checks, plus ``failed``."""
    drc_ok = checks["drc_report"]["status"] == "clean"
    lvs_ok = checks["lvs_report"]["status"] == "match"
    prov_errors = provenance_errors(
        scored_sha256, checks["drc_report"], checks["lvs_report"], netlist_sha256
    )
    verdict = {"drc": drc_ok, "lvs": lvs_ok, "provenance": not prov_errors}
    return {
        **verdict,
        "failed": [name for name, ok in verdict.items() if not ok],
        "provenance_errors": prov_errors,
    }


def top_cell_errors(gds: Path, top: str) -> list[str]:
    """``top`` must be the stream's one top cell: a generator PCell sharing the
    requested name makes KLayout rename the wrapper (``name$1``), after which a
    check or a mutation aimed at ``top`` silently hits the wrong cell."""
    import klayout.db as pya

    lay = pya.Layout()
    lay.read(str(gds))
    tops = [cell.name for cell in lay.top_cells()]
    if tops != [top]:
        return [f"{gds.name}: expected the single top cell {top!r}, found {tops}"]
    return []


def _agent_input_errors(task: dict[str, Any]) -> list[str]:
    inputs = task.get("agent_inputs")
    if not inputs:
        return ["agent_inputs: missing (the corpus gate requires the shown inputs)"]
    errors = []
    scorer_only = {
        task["reference_layout"]["path"],
        task["generator_recipe"]["path"],
        *(control["path"] for control in task["negative_controls"]),
    }
    for path in inputs:
        if path in scorer_only:
            errors.append(f"agent_inputs: {path} is scorer-only and must not be shown")
        if Path(path).suffix.lower() in {".gds", ".oas", ".gdsii", ".oasis"}:
            errors.append(
                f"agent_inputs: {path} is a layout stream (no candidate layout ships)"
            )
    if task["frozen_netlist"]["path"] not in inputs:
        errors.append("agent_inputs: must include the frozen netlist")
    return errors


def _control_coverage_errors(task: dict[str, Any]) -> list[str]:
    seen = {control["expected_failure"] for control in task["negative_controls"]}
    return [
        f"negative_controls: no {check} control "
        "(each failing check needs a known-bad layout)"
        for check in ("drc", "lvs")
        if check not in seen
    ]


def _negative_control_results(
    task: dict[str, Any],
    repo_root: Path,
    netlist: Path,
    top: str,
    workdir: Path,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Run every negative control; append to ``errors`` any not isolating its check."""
    controls = []
    for control in task["negative_controls"]:
        path = repo_root / control["path"]
        verdict = gate_verdict(
            control["sha256"],
            run_checks(path, netlist, top, workdir / f"control-{control['id']}"),
            task["frozen_netlist"]["sha256"],
        )
        expected = control["expected_failure"]
        other = "lvs" if expected == "drc" else "drc"
        controls.append({"id": control["id"], "expected_failure": expected, **verdict})
        if verdict[expected]:
            errors.append(
                f"negative control {control['id']!r} is not rejected by {expected}"
            )
        if not verdict[other]:
            errors.append(
                f"negative control {control['id']!r} also fails {other}: "
                f"it must isolate the {expected} check"
            )
    return controls


def _provenance_control_results(
    task: dict[str, Any],
    repo_root: Path,
    ref_checks: dict[str, Any],
    netlist_sha: str,
    errors: list[str],
) -> dict[str, bool]:
    """Provenance controls: checks recorded for the reference must not vouch for
    another file (GDS swapped after the checks) or another netlist."""
    swapped = _sha256_path(repo_root / task["negative_controls"][0]["path"])
    swap = provenance_errors(
        swapped, ref_checks["drc_report"], ref_checks["lvs_report"], netlist_sha
    )
    wrong_netlist = provenance_errors(
        task["reference_layout"]["sha256"],
        ref_checks["drc_report"],
        ref_checks["lvs_report"],
        "0" * 64,
    )
    if not swap:
        errors.append("provenance: a GDS swapped after the checks was not rejected")
    if not wrong_netlist:
        errors.append("provenance: a non-frozen LVS reference was not rejected")
    return {
        "swapped_gds_rejected": bool(swap),
        "wrong_netlist_rejected": bool(wrong_netlist),
    }


def check_task_gate(
    task: dict[str, Any], repo_root: Path, workdir: Path
) -> dict[str, Any]:
    """Run every frozen-gate requirement for one corpus task (issue #3025).

    * the descriptor validates, its frozen files exist and hash as recorded, the
      eligibility record covers every rule and arm, and agent inputs carry no
      layout or scorer-only file;
    * the reference layout is DRC-clean, LVS-clean and provenance-bound;
    * the generator recipe, re-run, reaches a layout that passes the same gate;
    * each negative control is rejected by *its* check and passes the other one
      (so each check is shown to fire independently);
    * a recorded check set does not vouch for a different file or netlist
      (the provenance control: swap the GDS after the checks ran).
    """
    errors: list[str] = []
    base = validate_task(task, repo_root=repo_root)
    errors += base["errors"]
    result: dict[str, Any] = {
        "task_id": task.get("id"),
        "valid": False,
        "errors": errors,
    }
    if errors:
        return result
    errors += _agent_input_errors(task)
    errors += _control_coverage_errors(task)
    if "eligibility" not in task:
        errors.append(
            "eligibility: missing (record why the task is eligible for all arms)"
        )
    netlist = repo_root / task["frozen_netlist"]["path"]
    netlist_sha = task["frozen_netlist"]["sha256"]
    top = task["reference_layout"]["cell_name"]

    reference = repo_root / task["reference_layout"]["path"]
    for rel in [
        task["reference_layout"]["path"],
        *(control["path"] for control in task["negative_controls"]),
    ]:
        errors += top_cell_errors(repo_root / rel, top)
    ref_checks = run_checks(reference, netlist, top, workdir / "reference")
    ref_verdict = gate_verdict(
        task["reference_layout"]["sha256"], ref_checks, netlist_sha
    )
    result["reference"] = ref_verdict
    if ref_verdict["failed"]:
        errors.append(f"reference_layout: fails {ref_verdict['failed']}")

    try:
        built = run_recipe(
            json.loads((repo_root / task["generator_recipe"]["path"]).read_text()),
            workdir / "recipe",
        )
    except (RecipeError, KeyError) as exc:
        errors.append(f"generator_recipe: {exc}")
    else:
        built_sha = _sha256_path(built)
        built_verdict = gate_verdict(
            built_sha,
            run_checks(built, netlist, top, workdir / "recipe-check"),
            netlist_sha,
        )
        result["recipe"] = {
            **built_verdict,
            "matches_reference": built_sha == task["reference_layout"]["sha256"],
        }
        if built_verdict["failed"]:
            errors.append(f"generator_recipe: result fails {built_verdict['failed']}")

    result["negative_controls"] = _negative_control_results(
        task, repo_root, netlist, top, workdir, errors
    )
    result["provenance_controls"] = _provenance_control_results(
        task, repo_root, ref_checks, netlist_sha, errors
    )

    result["valid"] = not errors
    return result


def _exclusion_recipe_errors(
    item: dict[str, Any], repo_root: Path, workdir: Path
) -> list[str]:
    """Recipe rules for one exclusion: a ``generator_reachable`` exclusion needs a
    hash-bound recipe that still fails; no other reason may carry a recipe."""
    recipe = item.get("recipe")
    if item["reason"] != "generator_reachable":
        if recipe is None:
            return []
        return [f"exclusions/{item['id']}: recipe only applies to generator_reachable"]
    if recipe is None:
        return [f"exclusions/{item['id']}: generator_reachable needs a failing recipe"]
    path = repo_root / recipe["path"]
    if ".." in Path(recipe["path"]).parts or not path.is_file():
        return [f"exclusions/{item['id']}: recipe path missing: {recipe['path']}"]
    if _sha256_path(path) != recipe["sha256"]:
        return [f"exclusions/{item['id']}: recipe sha256 does not match"]
    try:
        run_recipe(json.loads(path.read_text()), workdir / f"exclusion-{item['id']}")
    except RecipeError:
        return []
    return [
        f"exclusions/{item['id']}: recipe now reaches a layout; "
        "the exclusion is stale -- promote it to a task"
    ]


def _exclusion_errors(
    document: Any, task_ids: set[str], repo_root: Path, workdir: Path, schema_dir: Path
) -> list[str]:
    errors = _schema_errors(_validator(EXCLUSIONS_SCHEMA_NAME, schema_dir), document)
    if errors:
        return errors
    seen: set[str] = set()
    for item in document["exclusions"]:
        if item["id"] in seen:
            errors.append(f"exclusions: duplicate id {item['id']!r}")
        seen.add(item["id"])
        if item["id"] in task_ids:
            errors.append(
                f"exclusions: {item['id']!r} is also a scored task (all arms or none)"
            )
        errors += _exclusion_recipe_errors(item, repo_root, workdir)
    return errors


def check_corpus(
    corpus_dir: Path = CORPUS_DIR,
    repo_root: Path = REPO_ROOT,
    *,
    schema_dir: Path = SCHEMA_DIR,
    only: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Gate the whole frozen corpus. ``only`` limits which task ids run the
    (slow) layout checks; the descriptor/count/exclusion rules always apply."""
    errors: list[str] = []
    task_dirs = sorted(p for p in corpus_dir.iterdir() if (p / _TASK_FILE).is_file())
    if not CORPUS_TASKS_MIN <= len(task_dirs) <= CORPUS_TASKS_MAX:
        errors.append(
            f"corpus has {len(task_dirs)} tasks; "
            f"expected {CORPUS_TASKS_MIN}-{CORPUS_TASKS_MAX}"
        )
    wanted = None if only is None else set(only)
    reports = []
    task_ids: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="layout-corpus-gate-") as tmp:
        scratch = Path(tmp)
        for task_dir in task_dirs:
            task = json.loads((task_dir / _TASK_FILE).read_text())
            task_ids.add(task_dir.name)
            if task.get("id") != task_dir.name:
                errors.append(
                    f"{task_dir.name}: id {task.get('id')!r} does not match directory"
                )
                continue
            if wanted is not None and task_dir.name not in wanted:
                continue
            report = check_task_gate(task, repo_root, scratch / task_dir.name)
            reports.append(report)
            errors += [f"{task_dir.name}: {e}" for e in report["errors"]]
        exclusions_path = corpus_dir / _EXCLUSIONS_FILE
        exclusions: list[dict[str, Any]] = []
        if not exclusions_path.is_file():
            errors.append(f"{_EXCLUSIONS_FILE}: missing")
        else:
            document = json.loads(exclusions_path.read_text())
            errors += _exclusion_errors(
                document, task_ids, repo_root, scratch, schema_dir
            )
            exclusions = (
                document.get("exclusions", []) if isinstance(document, dict) else []
            )
    return {
        "schema": CORPUS_REPORT_SCHEMA,
        "valid": not errors,
        "errors": errors,
        "tasks": reports,
        "exclusions": [{"id": e["id"], "reason": e["reason"]} for e in exclusions],
    }
