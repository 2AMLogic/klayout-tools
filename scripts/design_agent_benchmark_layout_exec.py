"""Layout-flow benchmark executor (issue #3026, epic #2252 phase 3).

One executor runs all three arms (``draw-only``, ``gen-only``, ``gen+refine``)
of the frozen layout protocol (``benchmarks/design-agent/layout-protocol.json``)
over the frozen task corpus (``benchmarks/design-agent/layout-corpus``) through
the same isolation, budget, scoring and ledger path, so design strategy is
never confounded with harness differences. It implements the protocol; it
never redefines it -- arm definitions, tool classes and statuses are read from
the protocol document, and every record it writes is checked with
:func:`design_agent_benchmark_layout.validate_run` before it is appended.

Seams extended rather than replaced:

* the agent is behind :data:`LayoutProvider` (a callable over a
  :class:`LayoutSession`), the layout counterpart of ``CandidateProvider``;
  the live one drives the existing ``InteractiveAgentInvoker``
  (``design_agent_benchmark_interactive``), so tests stub the invoker or the
  provider and need no credentials;
* the per-arm tool surface is enforced by the same ``bin/klt`` sandbox shim the
  interactive provider already puts on ``PATH`` (``_write_sandbox_klt_shim``
  with a ``policy_path``): it refuses (exit 126, recorded) any verb outside the
  arm's allowed classes and records every call to a log that lives *outside*
  the agent's sandbox;
* scoring is :func:`design_agent_benchmark_layout.run_checks` /
  :func:`~design_agent_benchmark_layout.gate_verdict`, on a private copy of the
  artifact, against the repo-frozen netlist, outside the sandbox.

Output layout under ``--out-dir`` (append-only files are fsync'd per line)::

    runs.jsonl      one ``layout_run/1`` record per finished run
    journal.jsonl   one ``start`` line per run before it begins; a start with no
                    record is an interrupted run, reconciled on the next pass
    runs/<run_id>/  sandbox/ (the agent's, seeded with task inputs only),
                    tool-policy.json and tool-calls.jsonl (outside the
                    sandbox), artifact.gds (the harness's private copy),
                    score/ (scorer scratch)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import design_agent_benchmark_layout as layout
from design_agent_benchmark_interactive import (
    SANDBOX_BRIEF_FILENAME,
    AgentSessionRequest,
    InteractiveAgentInvoker,
    _default_invoke_interactive_agent,
    _write_sandbox_klt_shim,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNS_FILENAME = "runs.jsonl"
JOURNAL_FILENAME = "journal.jsonl"
TOOL_LOG_FILENAME = "tool-calls.jsonl"
POLICY_FILENAME = "tool-policy.json"
ARTIFACT_FILENAME = "artifact.gds"
#: The file an agent must leave in its sandbox: the layout to be scored.
DELIVERABLE = "layout.gds"
SEED_RELPATH = "seed/seed.gds"

#: Shim exit codes. 126 is the protocol's ``tool_recording`` refusal code; 125
#: marks a call past the tool-call budget.
EXIT_REFUSED = 126
EXIT_OVER_BUDGET = 125

#: Tool classes whose calls write geometry -- the artifact's only legal origin.
WRITE_CLASSES = ("draw", "generate")

#: What the live layout session may use (``claude --allowedTools`` syntax).
#: ``Bash(klt:*)`` is the only geometry path, and the shim polices it.
LAYOUT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(klt:*)",
    "Bash(cat:*)",
    "Bash(ls:*)",
)

#: Execution order inside one (task, agent, replicate): the seed arm first.
ARM_ORDER = ("draw-only", "gen-only", "gen+refine")

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_PROVIDERS = ("reference", "live-agent", "interactive-agent")
_RECORD_KEYS = ("seq", "phase", "verb", "class", "exit_code")


class LayoutExecutorError(Exception):
    """The harness (not the agent) could not run or record something. Raised
    instead of writing a record that would fail the protocol's own validator."""


# --------------------------------------------------------------------------
# Tool policy shim (runs inside the agent's `klt`)
# --------------------------------------------------------------------------


def _verb_of(argv: list[str]) -> str | None:
    """The subcommand: the first non-option argument. Option values are not
    skipped on purpose -- an ambiguous command line resolves to an unknown
    verb, which is refused (fail closed)."""
    return next((arg for arg in argv if not arg.startswith("-")), None)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sandbox_gds_hashes(sandbox: Path) -> list[str]:
    """Hashes of every stream under the sandbox (the shim's own ``bin`` aside)."""
    return sorted(
        {
            _sha256_file(path)
            for path in sandbox.rglob("*")
            if path.is_file()
            and path.suffix.lower() in (".gds", ".oas")
            and "bin" not in path.relative_to(sandbox).parts[:1]
        }
    )


def _default_klt_runner(argv: list[str]) -> int:
    from klayout_tools.cli import main as klt_main

    try:
        return int(klt_main(argv) or 0)
    except SystemExit as exc:  # argparse usage errors exit instead of returning
        return exc.code if isinstance(exc.code, int) else 2


def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one fsync'd JSON line, serialised across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def run_policed_tool(
    policy: dict[str, Any],
    argv: list[str],
    runner: Callable[[list[str]], int] = _default_klt_runner,
) -> int:
    """Apply the arm's tool policy to one ``klt`` invocation and record it.

    ``policy`` is the run's ``tool-policy.json``: ``phase``, ``allowed_classes``,
    ``class_of`` (verb -> class), ``log_path``, ``budget`` and ``sandbox``. A
    verb outside the allowed classes is refused with exit 126 *and recorded*; a
    verb in no class is refused and logged as ``unclassified`` (the run record's
    ``tool_calls`` only carries classified verbs); a call past the budget is
    refused with 125 and flagged. Everything else runs and is recorded with its
    real exit code. Calls with no verb (``--version``) pass through unrecorded.
    """
    verb = _verb_of(argv)
    if verb is None:
        return runner(argv)
    log = Path(policy["log_path"])
    calls = [r for r in _read_log(log) if r["event"] == "call"]
    cls = policy["class_of"].get(verb)
    if cls is None:
        _append_jsonl(log, {"event": "unclassified", "verb": verb})
        print(f"klt: verb {verb!r} is not available in this benchmark", flush=True)
        return EXIT_REFUSED
    if len(calls) >= policy["budget"]:
        _append_jsonl(log, {"event": "budget_exceeded", "verb": verb})
        print("klt: tool-call budget exhausted", flush=True)
        return EXIT_OVER_BUDGET
    record = {
        "event": "call",
        "seq": len(calls) + 1,
        "phase": policy["phase"],
        "verb": verb,
        "class": cls,
    }
    if cls not in policy["allowed_classes"]:
        print(
            f"klt: {verb!r} ({cls}) is not allowed in phase {policy['phase']!r}",
            flush=True,
        )
        _append_jsonl(log, {**record, "exit_code": EXIT_REFUSED})
        return EXIT_REFUSED
    code = runner(argv)
    outputs = (
        _sandbox_gds_hashes(Path(policy["sandbox"])) if cls in WRITE_CLASSES else []
    )
    _append_jsonl(log, {**record, "exit_code": code, "outputs": outputs})
    return code


def shim_main(argv: list[str], policy_path: str) -> int:
    """Entry point of the sandbox ``bin/klt`` shim when a policy is attached."""
    return run_policed_tool(json.loads(Path(policy_path).read_text()), argv)


def tool_policy(
    protocol: dict[str, Any],
    arm_id: str,
    *,
    log_path: Path,
    sandbox: Path,
    budget: int,
) -> dict[str, Any]:
    """The run's tool policy, derived from the protocol -- the single source of
    arm definitions and tool-class policy (nothing is hard-coded per arm)."""
    arm = layout._arm(protocol, arm_id)
    (phase,) = arm["phases"]
    return {
        "phase": phase["id"],
        "allowed_classes": list(phase["allowed_classes"]),
        "class_of": layout._class_of_verb(protocol),
        "log_path": str(log_path),
        "budget": budget,
        "sandbox": str(sandbox),
    }


# --------------------------------------------------------------------------
# Provider abstraction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """Who is being benchmarked: the ``agent`` object of the run record."""

    id: str
    provider: str
    model: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {"id": self.id, "provider": self.provider, "model": self.model}


@dataclass
class LayoutSession:
    """Everything a provider sees for one run: an isolated sandbox, the arm's
    policed ``klt``, and (``gen+refine`` only) the seed. Nothing about the
    reference layout, recipe or negative controls is reachable from here."""

    task: dict[str, Any]
    arm: str
    phase: str
    allowed_classes: tuple[str, ...]
    workdir: Path
    repo_root: Path
    policy: dict[str, Any]
    cell_name: str
    inputs: tuple[str, ...]
    seed_gds: Path | None
    timeout_s: float
    tool_call_budget: int
    runner: Callable[[list[str]], int] = field(default=_default_klt_runner, repr=False)

    @property
    def deliverable(self) -> Path:
        return self.workdir / DELIVERABLE

    def klt(self, argv: list[str]) -> int:
        """Run ``klt`` exactly as the sandbox shim would: policed and recorded,
        with the sandbox as cwd. The route a stub (or reference) provider
        takes; a live agent reaches the same function through ``bin/klt``."""
        cwd = Path.cwd()
        os.chdir(self.workdir)
        try:
            return run_policed_tool(self.policy, argv, self.runner)
        finally:
            os.chdir(cwd)


@dataclass
class LayoutSubmission:
    """What a provider hands back: where the layout is, and usage evidence."""

    gds: Path | None
    usage: dict[str, Any] | None = None
    notes: str | None = None


#: The layout counterpart of ``CandidateProvider``: run one session, return the
#: submission. Failures raise (``AgentInvocationError`` or anything else) and
#: are classified into a status; they never crash the sweep.
LayoutProvider = Callable[[LayoutSession], LayoutSubmission]


def build_layout_prompt(session: LayoutSession) -> str:
    """The opening prompt: task, arm tool surface, deliverable. Nothing here
    names the reference layout, the recipe, or any scorer file."""
    task = session.task
    verbs = sorted(
        verb
        for verb, cls in session.policy["class_of"].items()
        if cls in session.allowed_classes
    )
    seed = (
        f"\nA starting layout is at ./{SEED_RELPATH} (your own gen-only result for "
        "this task). Refine it; the result is scored against it, so it must be "
        "no larger than the seed and still pass DRC and LVS.\n"
        if session.seed_gds
        else ""
    )
    inputs = "\n".join(f"  - ./{name}" for name in session.inputs)
    return f"""You are producing an IC layout for the klayout-tools layout benchmark.

Task: {task["title"]} ({task["id"]}, PDK {task["pdk"]})
{task["description"]}
{seed}
Inputs (read-only reference for what the layout must implement):
{inputs}

Deliverable: a GDS at ./{DELIVERABLE} whose single top cell is named
{session.cell_name!r}. It is scored on DRC clean, LVS match against the task's
netlist, and drawn area (smaller is better) -- all re-run outside this sandbox.

Tools: only the `klt` verbs below, run as `klt <verb> ...` (see `klt <verb>
--help`). Any other verb is refused and ends the run as invalid. Do not try to
write geometry any other way.
  {", ".join(verbs)}
Budget: {session.tool_call_budget} klt calls, {session.timeout_s:.0f} s.
"""


def make_live_layout_provider(
    *,
    invoke_agent: InteractiveAgentInvoker = _default_invoke_interactive_agent,
    allowed_tools: tuple[str, ...] = LAYOUT_ALLOWED_TOOLS,
) -> LayoutProvider:
    """The multi-turn agent provider: one bounded session through the existing
    interactive invoker, in the policed sandbox the executor seeded."""

    def provider(session: LayoutSession) -> LayoutSubmission:
        prompt = build_layout_prompt(session)
        (session.workdir / SANDBOX_BRIEF_FILENAME).write_text(prompt)
        result = invoke_agent(
            AgentSessionRequest(
                prompt=prompt,
                workdir=session.workdir,
                timeout_s=session.timeout_s,
                tool_call_budget=session.tool_call_budget,
                allowed_tools=tuple(allowed_tools),
            )
        )
        usage: dict[str, Any] = dict(result.usage or {})
        usage.update({"tool_calls": result.tool_calls, "turns": result.turns})
        return LayoutSubmission(
            gds=session.deliverable if session.deliverable.is_file() else None,
            usage=usage,
        )

    return provider


def reference_layout_provider(session: LayoutSession) -> LayoutSubmission:
    """Deterministic, credential-free plumbing provider (the layout twin of
    ``reference_candidate_provider``). ``gen-only`` replays the task's frozen
    single-step ``klt gen`` recipe through the policed shim; ``gen+refine``
    resubmits its seed unchanged (round zero); ``draw-only`` has no reference
    solution and fails as an agent error rather than faking one."""
    from design_agent_benchmark import AgentInvocationError

    if session.arm == "gen+refine":
        assert session.seed_gds is not None
        shutil.copyfile(session.seed_gds, session.deliverable)
        return LayoutSubmission(gds=session.deliverable)
    if session.arm == "draw-only":
        raise AgentInvocationError("reference provider has no draw-only solution")
    recipe = json.loads(
        (session.repo_root / session.task["generator_recipe"]["path"]).read_text()
    )
    steps = recipe["steps"]
    if len(steps) != 1 or steps[0]["verb"] != "gen":
        raise AgentInvocationError(
            "reference provider replays single-step `klt gen` recipes only"
        )
    step = steps[0]
    code = session.klt(
        [
            "gen",
            step["generator"],
            "--params",
            json.dumps(step["params"]),
            "--cell-name",
            step["cell_name"],
            "--pdk",
            recipe["pdk"],
            "-o",
            str(session.deliverable),
            "--format",
            "json",
        ]
    )
    if code != 0:
        raise AgentInvocationError(f"klt gen exited {code}")
    return LayoutSubmission(gds=session.deliverable)


# --------------------------------------------------------------------------
# Scoring (one path for every arm)
# --------------------------------------------------------------------------


def artifact_area_um2(gds: Path, top: str) -> float:
    """Bounding-box area of the top cell in um^2 -- the protocol's primary
    score. Integer database-unit arithmetic, rounded for exact re-derivation."""
    import klayout.db as pya

    lay = pya.Layout()
    lay.read(str(gds))
    box = lay.cell(top).bbox()
    return round(box.width() * box.height() * lay.dbu * lay.dbu, 6)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(layout.canonical_json(value).encode("ascii")).hexdigest()


#: Members of a drc/lvs report that vary between runs of the same artifact
#: (timestamps, durations) or by machine (scratch / checkout paths).
_VOLATILE_KEYS = frozenset(
    {"generated_at", "timestamp", "duration_s", "wall_s", "elapsed_s"}
)
_PATH_KEYS = frozenset(
    {"file", "path", "layout", "output", "netlist", "workdir", "reference"}
)


def _stable_report(report: Any) -> Any:
    """A report minus members that change without changing the verdict, so
    ``report_sha256`` re-derives exactly from a re-score of the same artifact."""
    if isinstance(report, dict):
        return {
            key: _stable_report(value)
            for key, value in report.items()
            if key not in _VOLATILE_KEYS
            and not (key in _PATH_KEYS and isinstance(value, str) and "/" in value)
        }
    if isinstance(report, list):
        return [_stable_report(value) for value in report]
    return report


def _run_gate_checks(
    artifact: Path, netlist: Path, netlist_sha: str, top: str, workdir: Path
) -> dict[str, Any]:
    """:func:`layout.run_checks` on a private copy, except that a tool error on
    the artifact (e.g. LVS finding no ``top`` cell in a layout with no devices)
    is that check *failing* -- ``status: "error"`` -- rather than a harness
    crash, so a broken layout is an ``invalid_*`` outcome, not an agent error."""
    from klayout_tools.drc import DrcError, run_drc
    from klayout_tools.lvs import LvsError, run_lvs

    workdir.mkdir(parents=True, exist_ok=True)
    private = workdir / "scored.gds"
    shutil.copyfile(artifact, private)
    sha = _sha256_file(private)

    def scrub(exc: Exception) -> str:
        return str(exc).replace(str(private), "scored.gds")

    try:
        drc: dict[str, Any] = run_drc(str(private), layout.GATE_DECK)
    except DrcError as exc:
        drc = {
            "status": "error",
            "error": scrub(exc),
            "provenance": {"input": {"content_hash": f"sha256:{sha}"}},
        }
    try:
        lvs: dict[str, Any] = run_lvs(
            json.dumps(
                {
                    "schema": "klt.lvs.request/1",
                    "layout": {
                        "file": str(private),
                        "deck": layout.GATE_DECK,
                        "top": top,
                    },
                    "reference": {"netlist": str(netlist), "top": top},
                }
            )
        )
    except LvsError as exc:
        lvs = {
            "status": "error",
            "error": scrub(exc),
            "environment": {"layout_sha256": sha, "reference_sha256": netlist_sha},
        }
    return {"drc_report": drc, "lvs_report": lvs, "checked_sha256": sha}


def score_artifact(
    protocol: dict[str, Any],
    task: dict[str, Any],
    repo_root: Path,
    artifact: Path,
    workdir: Path,
) -> dict[str, Any]:
    """Re-derive the protocol's validity gate on ``artifact`` (a harness-owned
    copy) and, when it passes, the score. Returns ``status``, ``validity``,
    ``score`` and the gate ``verdict``; the agent's own checks are never read."""
    top = task["reference_layout"]["cell_name"]
    netlist = repo_root / task["frozen_netlist"]["path"]
    netlist_sha = task["frozen_netlist"]["sha256"]
    sha = _sha256_file(artifact)
    checks = _run_gate_checks(artifact, netlist, netlist_sha, top, workdir)
    drc, lvs = checks["drc_report"], checks["lvs_report"]
    verdict = layout.gate_verdict(sha, checks, netlist_sha)
    validity = {
        "drc": {
            "status": drc["status"],
            "gds_sha256": str(drc["provenance"]["input"]["content_hash"]).removeprefix(
                "sha256:"
            ),
            "report_sha256": _json_sha256(_stable_report(drc)),
        },
        "lvs": {
            "status": lvs["status"],
            "gds_sha256": lvs["environment"]["layout_sha256"],
            "report_sha256": _json_sha256(_stable_report(lvs)),
            "netlist_sha256": lvs["environment"]["reference_sha256"],
        },
    }
    if not verdict["provenance"]:
        status = "invalid_provenance"
    elif not verdict["drc"]:
        status = "invalid_drc"
    elif not verdict["lvs"]:
        status = "invalid_lvs"
    else:
        status = layout.SCORED_STATUS
    score = (
        {
            "area_um2": artifact_area_um2(artifact, top),
            "utilization": None,
            "sim_margin": None,
        }
        if status == layout.SCORED_STATUS
        else None
    )
    return {"status": status, "validity": validity, "score": score, "verdict": verdict}


# --------------------------------------------------------------------------
# Ledger files
# --------------------------------------------------------------------------


def load_runs(out_dir: Path) -> list[dict[str, Any]]:
    """Every record in ``runs.jsonl``. A torn *final* line (a write cut off by a
    kill) is ignored -- that run has no record and so is simply re-run -- while
    corruption anywhere else is an error: a ledger is never repaired silently."""
    path = out_dir / RUNS_FILENAME
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                continue
            raise LayoutExecutorError(
                f"{path}:{index + 1}: corrupt ledger line"
            ) from exc
    return records


def _run_id(task_id: str, arm: str, agent_id: str, replicate: int, attempt: int) -> str:
    return f"{task_id}.{arm}.{agent_id}.r{replicate}.a{attempt}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_tool_calls(log: Path) -> list[dict[str, Any]]:
    return [
        {key: entry[key] for key in _RECORD_KEYS}
        for entry in _read_log(log)
        if entry["event"] == "call"
    ]


def _versions(protocol: dict[str, Any]) -> dict[str, Any]:
    from klayout_tools import __version__

    return {"protocol": protocol["protocol_version"], "klt": __version__}


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


@dataclass
class _Plan:
    """The static identity of one run, known before it starts."""

    protocol: dict[str, Any]
    task: dict[str, Any]
    arm: str
    agent: AgentSpec
    replicate: int
    attempt: int
    seed_from: dict[str, str] | None
    run_id: str = ""
    seed_sha256: str = ""

    def __post_init__(self) -> None:
        self.run_id = _run_id(
            self.task["id"], self.arm, self.agent.id, self.replicate, self.attempt
        )
        self.seed_sha256 = layout.run_seed_sha256(
            self.protocol,
            task_id=self.task["id"],
            task_sha256=layout.document_sha256(self.task),
            arm=self.arm,
            agent_id=self.agent.id,
            replicate=self.replicate,
            seed_artifact_sha256=self.seed_from["gds_sha256"]
            if self.seed_from
            else None,
        )

    def base_record(self) -> dict[str, Any]:
        return {
            "schema": "klt.design_agent_benchmark.layout_run/1",
            "run_id": self.run_id,
            "protocol_sha256": layout.document_sha256(self.protocol),
            "task_id": self.task["id"],
            "task_sha256": layout.document_sha256(self.task),
            "arm": self.arm,
            "agent": self.agent.as_record(),
            "replicate": self.replicate,
            "seed_sha256": self.seed_sha256,
            "seed_from": self.seed_from,
        }


def _finish(
    plan: _Plan,
    *,
    status: str,
    tool_calls: list[dict[str, Any]],
    artifact: dict[str, Any] | None = None,
    validity: dict[str, Any] | None = None,
    score: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    notes: str | None = None,
    runtime_s: float = 0.0,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **plan.base_record(),
        "tool_calls": tool_calls,
        "status": status,
        "artifact": artifact,
        "validity": validity,
        "score": score,
        "usage": usage,
        "notes": notes,
        "runtime_s": round(runtime_s, 3),
        "versions": _versions(plan.protocol),
        "diagnostics": diagnostics or {},
    }


def _seed_sandbox(
    task: dict[str, Any],
    repo_root: Path,
    workdir: Path,
    policy_path: Path,
    seed: Path | None,
) -> tuple[str, ...]:
    """Seed the agent's sandbox with the task's *agent inputs only* (the frozen
    netlist), the policed ``bin/klt`` and, for ``gen+refine``, its own seed. The
    reference layout, recipe, negative controls and every other arm's output are
    never copied in."""
    workdir.mkdir(parents=True, exist_ok=True)
    _write_sandbox_klt_shim(workdir, repo_root, policy_path=policy_path)
    names = []
    for rel in task["agent_inputs"]:
        shutil.copyfile(repo_root / rel, workdir / Path(rel).name)
        names.append(Path(rel).name)
    if seed is not None:
        (workdir / SEED_RELPATH).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, workdir / SEED_RELPATH)
    return tuple(names)


def _classify_failure(exc: BaseException) -> str:
    text = str(exc).lower()
    if "timed out" in text:
        return "timeout"
    if "tool-call budget" in text:
        return "budget_exhausted"
    return "agent_error"


def _tool_surface_problem(log: list[dict[str, Any]]) -> str | None:
    refused = [
        e for e in log if e["event"] == "call" and e["exit_code"] == EXIT_REFUSED
    ]
    unclassified = [e["verb"] for e in log if e["event"] == "unclassified"]
    if refused or unclassified:
        names = sorted({e["verb"] for e in refused} | set(unclassified))
        return f"tool surface violation: refused verb(s) {', '.join(names)}"
    return None


def _artifact_origin_problem(
    artifact_sha: str, log: list[dict[str, Any]], seed_sha: str | None
) -> str | None:
    """The scored GDS must be an output of a recorded write-class call (or the
    unmodified seed): a stream written any other way is outside the recorded
    write path the protocol's tool-surface rule depends on."""
    produced = {h for e in log if e["event"] == "call" for h in e.get("outputs", [])}
    if artifact_sha in produced or artifact_sha == seed_sha:
        return None
    return "artifact was not produced by a recorded draw/generate tool call"


def _score_session(
    plan: _Plan,
    session: LayoutSession,
    submission: LayoutSubmission,
    run_dir: Path,
    repo_root: Path,
    seed_run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Everything after the agent returned: policy audit, then the one scorer."""
    log = _read_log(Path(session.policy["log_path"]))
    calls = _record_tool_calls(Path(session.policy["log_path"]))
    kwargs: dict[str, Any] = {"tool_calls": calls, "usage": submission.usage}
    diagnostics: dict[str, Any] = {"tool_log": TOOL_LOG_FILENAME}

    if any(e["event"] == "budget_exceeded" for e in log):
        return _finish(
            plan,
            status="budget_exhausted",
            notes="tool-call budget exhausted",
            diagnostics=diagnostics,
            **kwargs,
        )
    problem = _tool_surface_problem(log)
    if problem:
        return _finish(
            plan,
            status="invalid_tool_surface",
            notes=problem,
            diagnostics=diagnostics,
            **kwargs,
        )
    if submission.gds is None or not submission.gds.is_file():
        return _finish(
            plan,
            status="agent_error",
            notes=submission.notes or f"no ./{DELIVERABLE} was produced",
            diagnostics=diagnostics,
            **kwargs,
        )

    private = run_dir / ARTIFACT_FILENAME
    shutil.copyfile(submission.gds, private)
    sha = _sha256_file(private)
    seed_sha = seed_run["artifact"]["gds_sha256"] if seed_run else None
    problem = _artifact_origin_problem(sha, log, seed_sha)
    if problem:
        return _finish(
            plan,
            status="invalid_tool_surface",
            notes=problem,
            diagnostics=diagnostics,
            **kwargs,
        )
    top = plan.task["reference_layout"]["cell_name"]
    try:
        top_errors = layout.top_cell_errors(private, top)
        if top_errors:
            return _finish(
                plan,
                status="agent_error",
                notes="; ".join(top_errors),
                diagnostics=diagnostics,
                **kwargs,
            )
        scored = score_artifact(
            plan.protocol, plan.task, repo_root, private, run_dir / "score"
        )
    except Exception as exc:  # unreadable stream, deck failure: the agent's artifact
        return _finish(
            plan,
            status="agent_error",
            notes=f"artifact could not be scored: {exc}",
            diagnostics=diagnostics,
            **kwargs,
        )

    artifact = {"gds_sha256": sha, "cell_name": top, "selected_round": 1}
    status, validity, score = scored["status"], scored["validity"], scored["score"]
    if seed_run is not None and status == layout.SCORED_STATUS:
        # The seed is round zero of the refine phase: a refinement that scores
        # worse is superseded by the seed itself, never recorded as a regression.
        seed_area = seed_run["score"]["area_um2"]
        if score["area_um2"] > seed_area:
            diagnostics["refined_area_um2"] = score["area_um2"]
            diagnostics["refined_gds_sha256"] = sha
            artifact = {**seed_run["artifact"], "selected_round": 0}
            validity, score = seed_run["validity"], seed_run["score"]
            shutil.copyfile(
                run_dir.parent / seed_run["run_id"] / ARTIFACT_FILENAME, private
            )
    return _finish(
        plan,
        status=status,
        artifact=artifact,
        validity=validity,
        score=score,
        diagnostics={**diagnostics, "gate": scored["verdict"]},
        **kwargs,
    )


def _find_seed(
    runs: Iterable[dict[str, Any]], out_dir: Path, plan_key: tuple[str, str, int]
) -> dict[str, Any] | None:
    """The same agent's best ``valid`` gen-only run for (task, agent, replicate)
    whose stored artifact still hashes to the recorded value."""
    task_id, agent_id, replicate = plan_key
    candidates = [
        r
        for r in runs
        if r["arm"] == layout.SEED_ARM
        and r["status"] == layout.SCORED_STATUS
        and (r["task_id"], r["agent"]["id"], r["replicate"])
        == (task_id, agent_id, replicate)
    ]
    for run in sorted(candidates, key=lambda r: (r["score"]["area_um2"], r["run_id"])):
        stored = out_dir / "runs" / run["run_id"] / ARTIFACT_FILENAME
        if stored.is_file() and _sha256_file(stored) == run["artifact"]["gds_sha256"]:
            return run
    return None


def execute_run(
    *,
    protocol: dict[str, Any],
    task: dict[str, Any],
    arm: str,
    agent: AgentSpec,
    provider: LayoutProvider,
    replicate: int,
    attempt: int,
    out_dir: Path,
    repo_root: Path,
    seed_run: dict[str, Any] | None,
    timeout_s: float,
    tool_call_budget: int,
) -> dict[str, Any]:
    """Run one (task, agent, arm, replicate), append its record, return it."""
    seeded = layout._arm(protocol, arm)["seed"] is not None
    seed_from = (
        {"run_id": seed_run["run_id"], "gds_sha256": seed_run["artifact"]["gds_sha256"]}
        if seed_run
        else None
    )
    plan = _Plan(protocol, task, arm, agent, replicate, attempt, seed_from)
    _append_jsonl(
        out_dir / JOURNAL_FILENAME,
        {"event": "start", "ts": _now(), "identity": plan.base_record()},
    )

    def finish(record: dict[str, Any]) -> dict[str, Any]:
        result = layout.validate_run(record, protocol, task)
        if not result["valid"]:
            raise LayoutExecutorError(
                f"{plan.run_id}: refusing to record an invalid run: {result['errors']}"
            )
        _append_jsonl(out_dir / RUNS_FILENAME, record)
        _append_jsonl(
            out_dir / JOURNAL_FILENAME,
            {
                "event": "finish",
                "ts": _now(),
                "run_id": plan.run_id,
                "status": record["status"],
            },
        )
        return record

    if seeded and seed_run is None:
        return finish(
            _finish(
                plan,
                status=protocol_unseedable_status(protocol, arm),
                tool_calls=[],
                notes="no valid gen-only run with an intact artifact for this "
                "(task, agent, replicate)",
            )
        )

    run_dir = out_dir / "runs" / plan.run_id
    sandbox = run_dir / "sandbox"
    policy_path = run_dir / POLICY_FILENAME
    log_path = run_dir / TOOL_LOG_FILENAME
    policy = tool_policy(
        protocol, arm, log_path=log_path, sandbox=sandbox, budget=tool_call_budget
    )
    run_dir.mkdir(parents=True)
    policy_path.write_text(json.dumps(policy, indent=2))
    seed_src = (
        (out_dir / "runs" / seed_run["run_id"] / ARTIFACT_FILENAME)
        if seed_run
        else None
    )
    inputs = _seed_sandbox(task, repo_root, sandbox, policy_path, seed_src)
    session = LayoutSession(
        task=task,
        arm=arm,
        phase=policy["phase"],
        allowed_classes=tuple(policy["allowed_classes"]),
        workdir=sandbox,
        repo_root=repo_root,
        policy=policy,
        cell_name=task["reference_layout"]["cell_name"],
        inputs=inputs,
        seed_gds=(sandbox / SEED_RELPATH) if seed_run else None,
        timeout_s=timeout_s,
        tool_call_budget=tool_call_budget,
    )
    start = time.monotonic()
    try:
        try:
            submission = provider(session)
        except Exception as exc:  # a failed agent is a recorded outcome, not a crash
            record = _score_session(
                plan,
                session,
                LayoutSubmission(gds=None, notes=str(exc)),
                run_dir,
                repo_root,
                seed_run,
            )
            if record["status"] == "agent_error":
                record["status"] = _classify_failure(exc)
                record["notes"] = f"{type(exc).__name__}: {exc}"
            record["runtime_s"] = round(time.monotonic() - start, 3)
            return finish(record)
        record = _score_session(plan, session, submission, run_dir, repo_root, seed_run)
        record["runtime_s"] = round(time.monotonic() - start, 3)
        return finish(record)
    except BaseException as exc:
        if isinstance(exc, LayoutExecutorError):
            raise
        interrupted = _finish(
            plan,
            status="interrupted",
            tool_calls=_record_tool_calls(log_path),
            notes=f"{type(exc).__name__}: {exc}",
            runtime_s=time.monotonic() - start,
        )
        finish(interrupted)
        raise


def protocol_unseedable_status(protocol: dict[str, Any], arm: str) -> str:
    return str(layout._arm(protocol, arm)["seed"]["unseedable_status"])


# --------------------------------------------------------------------------
# The sweep (resumable)
# --------------------------------------------------------------------------


def reconcile_interrupted(
    out_dir: Path, protocol: dict[str, Any], tasks: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append an ``interrupted`` record for every journaled start that never
    finished (a killed sweep). Such a run stays auditable -- its tool log is kept
    -- and is never a score; the sweep then re-runs it under a new attempt."""
    journal = _read_log_lenient(out_dir / JOURNAL_FILENAME)
    done = {r["run_id"] for r in load_runs(out_dir)}
    written = []
    for entry in journal:
        if entry.get("event") != "start" or entry["identity"]["run_id"] in done:
            continue
        identity = entry["identity"]
        run_id = identity["run_id"]
        task = tasks.get(identity["task_id"])
        if task is None:
            raise LayoutExecutorError(f"{run_id}: journaled task is not in the corpus")
        record = {
            **identity,
            "tool_calls": _record_tool_calls(
                out_dir / "runs" / run_id / TOOL_LOG_FILENAME
            ),
            "status": "interrupted",
            "artifact": None,
            "validity": None,
            "score": None,
            "usage": None,
            "notes": "run was started but never finished (sweep interrupted)",
            "runtime_s": 0.0,
            "versions": _versions(protocol),
            "diagnostics": {},
        }
        result = layout.validate_run(record, protocol, task)
        if not result["valid"]:
            raise LayoutExecutorError(f"{run_id}: cannot reconcile: {result['errors']}")
        _append_jsonl(out_dir / RUNS_FILENAME, record)
        done.add(run_id)
        written.append(record)
    return written


def _read_log_lenient(path: Path) -> list[dict[str, Any]]:
    """Journal lines, skipping a torn final line (same rule as ``load_runs``)."""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def load_tasks(
    corpus_dir: Path, repo_root: Path, ids: list[str] | None
) -> dict[str, dict[str, Any]]:
    """The frozen corpus, each task schema-valid with every frozen file present
    and matching its recorded sha256 -- a task that fails is a harness error."""
    tasks: dict[str, dict[str, Any]] = {}
    for path in sorted(corpus_dir.glob("*/task.json")):
        task = json.loads(path.read_text())
        if ids and task["id"] not in ids:
            continue
        result = layout.validate_task(task, repo_root=repo_root)
        if not result["valid"]:
            raise LayoutExecutorError(f"{path}: {result['errors']}")
        tasks[task["id"]] = task
    missing = set(ids or ()) - set(tasks)
    if missing:
        raise LayoutExecutorError(f"unknown task id(s): {sorted(missing)}")
    return tasks


def run_layout_benchmark(
    *,
    protocol: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    agents: list[tuple[AgentSpec, LayoutProvider]],
    arms: Iterable[str] = ARM_ORDER,
    replicates: int = 1,
    out_dir: Path,
    repo_root: Path = REPO_ROOT,
    timeout_s: float | None = None,
    tool_call_budget: int | None = None,
) -> dict[str, Any]:
    """Run (or resume) the sweep. Finished runs -- every status except
    ``interrupted`` -- are never repeated, so a failed run stays a counted,
    unscored outcome instead of being retried until it scores."""
    for agent, _ in agents:
        if not _ID_PATTERN.match(agent.id) or agent.provider not in _PROVIDERS:
            raise LayoutExecutorError(f"invalid agent spec: {agent}")
    per_run = protocol["budget"]["per_run"]
    timeout_s = per_run["agent_timeout_s"] if timeout_s is None else timeout_s
    tool_call_budget = (
        per_run["tool_call_budget"] if tool_call_budget is None else tool_call_budget
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    reconciled = reconcile_interrupted(out_dir, protocol, tasks)
    ordered = [arm for arm in ARM_ORDER if arm in set(arms)]
    executed: list[dict[str, Any]] = []
    skipped = 0
    for task in tasks.values():
        for agent, provider in agents:
            for replicate in range(1, replicates + 1):
                for arm in ordered:
                    runs = load_runs(out_dir)
                    key = (task["id"], arm, agent.id, replicate)
                    same = [
                        r
                        for r in runs
                        if (r["task_id"], r["arm"], r["agent"]["id"], r["replicate"])
                        == key
                    ]
                    if any(r["status"] != "interrupted" for r in same):
                        skipped += 1
                        continue
                    seed_run = (
                        _find_seed(runs, out_dir, (task["id"], agent.id, replicate))
                        if layout._arm(protocol, arm)["seed"] is not None
                        else None
                    )
                    executed.append(
                        execute_run(
                            protocol=protocol,
                            task=task,
                            arm=arm,
                            agent=agent,
                            provider=provider,
                            replicate=replicate,
                            attempt=len(same) + 1,
                            out_dir=out_dir,
                            repo_root=repo_root,
                            seed_run=seed_run,
                            timeout_s=timeout_s,
                            tool_call_budget=tool_call_budget,
                        )
                    )
    all_runs = load_runs(out_dir)
    return {
        "out_dir": str(out_dir),
        "executed": [{"run_id": r["run_id"], "status": r["status"]} for r in executed],
        "skipped_finished": skipped,
        "reconciled_interrupted": [r["run_id"] for r in reconciled],
        "run_set": layout.validate_run_set(
            [r for r in all_runs if r["status"] != "interrupted"]
        ),
    }
