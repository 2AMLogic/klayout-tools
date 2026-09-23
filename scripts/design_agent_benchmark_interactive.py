#!/usr/bin/env python3
"""Interactive (multi-turn, tool-using) agent provider for the design-agent
benchmark harness (issue #1739).

Split out of ``design_agent_benchmark.py`` (issue #1766) as a self-contained
subsystem: everything needed to seed a per-attempt sandbox, drive one
bounded multi-turn `claude` CLI session inside it with real `klt kb`/`klt
sim` tool access, and collect the netlist(s) it produced --
:class:`AgentSessionRequest`, :class:`AgentSessionResult`,
:func:`_write_sandbox_klt_shim`, :func:`_seed_agent_sandbox`,
:func:`_build_interactive_agent_prompt`, :func:`_iter_stream_events`,
:func:`_default_invoke_interactive_agent`, :func:`_kill`,
:func:`_collect_sandbox_netlists`, :func:`_attempt_sandbox`,
:func:`make_interactive_agent_provider`, and
:data:`interactive_agent_candidate_provider`. This mirrors the shape of the
``sim.py``/``sim_remote.py`` split (#1764/#1765): a cohesive,
low-coupling subsystem relocated verbatim out of a file that had grown past
the point one module should hold both the harness's own orchestration and
this provider's sandbox/session machinery.

``run_attempt``/``run_task_attempts`` are *not* part of this split -- they
are generic attempt-scoring code shared by every provider (the reference
provider, the single-turn live-agent provider, and this one alike) and stay
in ``design_agent_benchmark.py``.

Dependency surface: this module calls out to a handful of names defined in
``design_agent_benchmark.py`` (``CandidateProvider``, ``AGENT_CLI_ENV``,
``SKILL_CHAIN_PATHS``, ``AgentInvocationError``, ``_reference_request_refs``,
``_reference_testbenches``, ``_device_model_contract``,
``_extract_labeled_netlists``, ``_build_live_agent_descriptor``), imported
*inside* the handful of functions that use them (the same deferred-import
discipline ``sim_remote.py`` uses for its own back-references to ``sim.py``)
rather than at module scope. This is not just style here: unlike
``sim.py``/``sim_remote.py`` (always imported by dotted package name),
``design_agent_benchmark.py`` also runs standalone as ``python
scripts/design_agent_benchmark.py`` (this repo's benchmark CI workflow), in
which case it loads as ``__main__`` rather than as a module named
``design_agent_benchmark`` -- a module-scope back-import here would try to
resolve the literal name ``design_agent_benchmark`` before
``design_agent_benchmark.py``'s own re-export of this module's names (which
must itself run *after* this module's names are defined) has finished,
which is a real circular-import failure, not just an ordering nit.
``CandidateProvider`` is a type-only reference (annotations are deferred via
``from __future__ import annotations``); it is imported under
``TYPE_CHECKING`` only, never at runtime.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from design_agent_benchmark import CandidateProvider

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
    scored.

    ``usage`` is whatever token counts the CLI's own ``result`` event
    reported (``input_tokens``/``output_tokens``/cache buckets, normalized by
    ``design_agent_benchmark._normalize_token_usage``), or ``None`` for a CLI
    version/stub whose envelope carries none -- optional and additive, issue
    #2294, so a caller-supplied stub constructing this dataclass positionally
    or by keyword keeps working unchanged."""

    text: str
    tool_calls: int = 0
    turns: int = 0
    transcript_path: Path | None = None
    wall_clock_s: float = 0.0
    usage: dict[str, int] | None = None


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
    from design_agent_benchmark import (
        SKILL_CHAIN_PATHS,
        AgentInvocationError,
        _reference_request_refs,
    )

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

    When ``task`` carries ``ROUND_HISTORY_CONTEXT_KEY`` (round mode, issue
    #2253), a "previous rounds" section is inserted before the block spec --
    see ``design_agent_benchmark._format_round_history``.
    """
    from design_agent_benchmark import (
        ROUND_HISTORY_CONTEXT_KEY,
        _device_model_contract,
        _format_round_history,
        _reference_testbenches,
    )

    repo_root = Path(layout["repo_root"])
    testbenches = _reference_testbenches(task, repo_root)
    device_model_contract = _device_model_contract(task, repo_root, testbenches)
    round_history_section = _format_round_history(task.get(ROUND_HISTORY_CONTEXT_KEY))

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

{round_history_section}=== Block to design ===

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
    from design_agent_benchmark import (
        AGENT_CLI_ENV,
        AgentInvocationError,
        _normalize_token_usage,
    )

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
    token_usage: dict[str, int] | None = None
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
                        # The same `result` event the text comes off of also
                        # carries the session's own token accounting (issue
                        # #2294). Absent on an older CLI version -- recorded
                        # as "no usage known", never an error.
                        token_usage = (
                            _normalize_token_usage(event.get("usage")) or token_usage
                        )
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
        usage=token_usage,
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
    from design_agent_benchmark import AgentInvocationError, _extract_labeled_netlists

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
        from design_agent_benchmark import (
            _build_live_agent_descriptor,
            _write_session_summary,
        )

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
        _write_session_summary(
            workdir,
            {
                "task_id": task["id"],
                "attempt": attempt_index,
                "tool_calls": session.tool_calls,
                "turns": session.turns,
                # Whatever token counts the session's own CLI envelope
                # reported (issue #2294) -- `null` for a CLI/stub that
                # reports none, which `_usage_from_scratch_dir` reads back as
                # "tool_calls/turns only", exactly as before.
                "usage": session.usage,
                "wall_clock_s": session.wall_clock_s,
                "netlists": sorted(netlists_by_stem),
                "tool_call_budget": tool_call_budget,
                "timeout_s": agent_timeout_s,
            },
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
