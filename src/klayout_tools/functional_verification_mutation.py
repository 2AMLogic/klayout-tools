"""Mutation-testing subsystem for the cocotb functional-verification flow.

Split out of ``functional_verification.py`` (the #1909 SDF-split precedent):
the ``<proposals>`` document loader/validators, the isolated per-variant
build-and-test machinery, and the ``--mutations`` entry point
(:func:`run_functional_verification_with_mutations`). Names that stay in
``functional_verification.py`` are reached as ``_fv.<name>`` at call time, so
monkeypatching them on ``klayout_tools.functional_verification`` still takes
effect; every public name is re-exported from there.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue as _queue_module
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._vendor import mutation_variants as _mutation_variants

#: `<proposals>` top-level required fields (spike §3b). `schema` is not
#: required or validated -- user-authored input, the same posture every
#: other request-taking verb's own `schema` field already takes.
_MUTATION_PROPOSALS_REQUIRED_FIELDS = ("scope", "proposals")

#: `proposals[]` entry required fields (spike §3b). `detectability_argument`
#: is optional and never validated beyond "string or absent" -- free-text,
#: echoed back unmodified, never acted on programmatically.
_MUTATION_PROPOSAL_REQUIRED_FIELDS = (
    "index",
    "category",
    "file",
    "line",
    "original_code",
    "mutated_code",
)


@dataclass(frozen=True)
class _ProposalRecord:
    """One validated `<proposals>.proposals[]` entry -- satisfies the ported
    `_mutation_variants.MutationProposal` protocol (`index`/`file`/`line`/
    `original_code`/`mutated_code`) by construction, plus the two fields
    (`category`, `detectability_argument`) that protocol doesn't need but the
    response's `results[]` echoes back."""

    index: int
    category: str
    file: str
    line: int
    original_code: str
    mutated_code: str
    detectability_argument: str | None = None


def _load_mutation_proposals_json(path: str) -> Any:
    """Read ``path`` and decode it as JSON, raising
    :class:`FunctionalVerificationError` for every failure mode a caller
    needs to report -- the `<proposals>`-specific analogue of
    :func:`klayout_tools._paths._load_request_json`, kept separate only so
    the error text says "mutation proposals file", not "request file"."""
    if not os.path.exists(path):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file not found: {path}"
        )
    if os.path.isdir(path):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals path is not a file: {path}"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise _fv.FunctionalVerificationError(
            f"could not read mutation proposals file: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file is not valid JSON: {exc}"
        ) from exc


def load_mutation_proposals(path: str) -> tuple[list[str], list[_ProposalRecord]]:
    """Load and validate a ``<proposals>`` document (spike §3b): ``schema``
    (unvalidated), ``scope`` (array of RTL source paths, matched against
    `<request>.sources` by the caller -- see
    :func:`_validate_mutation_scope`), and ``proposals[]`` (booley's own
    byte-exact shape, §1a).

    Structural/type errors here are always whole-document failures (exit 1)
    -- a malformed `<proposals>` document can produce no trustworthy
    mutation run at all, the same "fail the whole request, don't run half a
    plan" posture ``options.sdf`` validation already takes in the base
    contract. Per-proposal *anchor* validation (does ``original_code``
    actually occur once on the declared line of the *pristine* source) is
    deliberately **not** done here -- that needs the request's own resolved
    ``sources``, and belongs to :func:`_resolve_mutation_proposals`, whose
    failures are individual ``"rejected"`` results, not whole-document ones.

    Returns ``(scope, proposals)``.
    """
    document = _load_mutation_proposals_json(path)
    if not isinstance(document, dict):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file must contain a JSON object: {path}"
        )
    for field in _MUTATION_PROPOSALS_REQUIRED_FIELDS:
        if field not in document:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals file is missing required field: {field}"
            )

    scope_raw = document["scope"]
    if not isinstance(scope_raw, list) or not all(
        isinstance(entry, str) and entry for entry in scope_raw
    ):
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'scope' must be an array of non-empty strings"
        )
    scope = list(scope_raw)

    proposals_raw = document["proposals"]
    if not isinstance(proposals_raw, list):
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'proposals' must be an array"
        )

    proposals: list[_ProposalRecord] = []
    for position, entry in enumerate(proposals_raw):
        if not isinstance(entry, dict):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}] must be a JSON object"
            )
        for field in _MUTATION_PROPOSAL_REQUIRED_FIELDS:
            if field not in entry:
                raise _fv.FunctionalVerificationError(
                    f"mutation proposals.proposals[{position}] is missing "
                    f"required field: {field}"
                )

        index = entry["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].index must be an integer"
            )
        category = entry["category"]
        if not isinstance(category, str) or not category:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].category must be "
                "a non-empty string"
            )
        file_field = entry["file"]
        if not isinstance(file_field, str) or not file_field:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].file must be a "
                "non-empty string"
            )
        line = entry["line"]
        if not isinstance(line, int) or isinstance(line, bool):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].line must be an integer"
            )
        original_code = entry["original_code"]
        if not isinstance(original_code, str):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].original_code "
                "must be a string"
            )
        mutated_code = entry["mutated_code"]
        if not isinstance(mutated_code, str):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].mutated_code "
                "must be a string"
            )
        detectability_argument = entry.get("detectability_argument")
        if detectability_argument is not None and not isinstance(
            detectability_argument, str
        ):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].detectability_argument "
                "must be a string when given"
            )

        proposals.append(
            _ProposalRecord(
                index=index,
                category=category,
                file=file_field,
                line=line,
                original_code=original_code,
                mutated_code=mutated_code,
                detectability_argument=detectability_argument,
            )
        )

    return scope, proposals


def _validate_proposal_indices(proposals: list[_ProposalRecord]) -> None:
    """Reject a duplicate or non-positive ``index`` before any build runs
    (spike §3b: "the same 'fail the whole request, don't run half a plan'
    posture ``options.sdf`` validation already takes"). Whole-document exit
    1, unlike a bad ``file``/anchor mismatch on one proposal, which is a
    per-proposal ``"rejected"`` result instead (see
    :func:`_resolve_mutation_proposals`)."""
    seen: set[int] = set()
    for proposal in proposals:
        if proposal.index <= 0 or proposal.index in seen:
            raise _fv.FunctionalVerificationError(
                "mutation proposals[].index must be unique and positive "
                f"(got {proposal.index!r})"
            )
        seen.add(proposal.index)


def _validate_mutation_scope(scope: list[str], request_sources: list[str]) -> None:
    """``<proposals>.scope`` must be a subset of `<request>.sources` (spike
    §3b) -- a proposal targeting a file the baseline build never compiles
    cannot be meaningfully tested. Whole-document exit 1, checked before the
    (expensive) baseline ever runs.
    """
    missing = [entry for entry in scope if entry not in request_sources]
    if missing:
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'scope' must be a subset of request.sources "
            f"-- not present in request.sources: {', '.join(missing)}"
        )


def _resolve_mutation_proposals(
    proposals: list[_ProposalRecord], scope: list[str], request_dir: str
) -> tuple[_mutation_variants.MutationVariantPlan, dict[int, str]]:
    """Anchor-validate every proposal against the *pristine* scope source
    (the ported seam, spike §1a), splitting them into a
    :class:`~klayout_tools._vendor.mutation_variants.MutationVariantPlan`
    of the ones that resolved cleanly and a ``{index: reason}`` map of the
    ones that did not.

    Deliberately does **not** call the ported module's own
    ``MutationVariantPlan.resolve()`` classmethod, which aborts the entire
    batch on the *first* proposal that fails ``file``-in-scope or anchor
    validation -- booley's own atomic-batch posture (§1a). This module's own
    contract is per-proposal (spike §3b/§3c): a bad ``file`` or an
    anchor mismatch on one proposal is that proposal's own ``"rejected"``
    result, not a whole-document failure -- "the same 'one bad entry doesn't
    poison the batch' posture ``klt drc``'s per-violation reporting already
    takes." So this function drives the ported module's own building blocks
    (``_read_originals``, ``_resolve_one``) directly, one proposal at a time,
    instead.
    """
    allowed = set(scope)
    try:
        originals = _mutation_variants.MutationVariantPlan._read_originals(
            Path(request_dir), allowed
        )
    except _mutation_variants.MutationVariantError as exc:
        # Every `scope` entry is already a `request.sources` member (see
        # `_validate_mutation_scope`), and every `request.sources` entry was
        # already confirmed readable by `_resolve_sources` -- so this should
        # not fire in practice. Kept as a whole-document exit 1 (not a
        # per-proposal rejection) rather than assumed unreachable: an
        # unreadable *scope* file affects every proposal naming it, not one.
        raise _fv.FunctionalVerificationError(str(exc)) from exc

    resolved: list[_mutation_variants.ResolvedMutation] = []
    rejected: dict[int, str] = {}
    for proposal in proposals:
        if proposal.file not in allowed:
            rejected[proposal.index] = (
                f"file '{proposal.file}' is outside the declared scope"
            )
            continue
        try:
            resolved.append(
                _mutation_variants.MutationVariantPlan._resolve_one(
                    proposal, originals[proposal.file]
                )
            )
        except _mutation_variants.MutationVariantError as exc:
            rejected[proposal.index] = str(exc)

    plan = _mutation_variants.MutationVariantPlan(
        Path(request_dir), originals, tuple(resolved)
    )
    return plan, rejected


# ---------------------------------------------------------------------------
# `--mutations`: isolated per-mutant build+test, bounded by a klt-level
# subprocess timeout (issue #1592, spike "Open questions")
# ---------------------------------------------------------------------------


def _build_and_test_variant(
    *,
    engine: str,
    sources: list[str],
    hdl_toplevel: str,
    build_dir: str,
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    env: dict[str, str],
    build_log: str,
    module: str,
    module_dir: str,
    testcase: str | list[str] | None,
    random_seed: int | None,
    test_dir: str,
    results_xml: str,
    test_log: str,
) -> None:
    """``_run_build`` then ``_run_test`` against whatever source bytes are on
    disk right now -- the isolated per-mutant build+test this module runs
    once per resolved proposal, inside :func:`plan.applied(index)
    <klayout_tools._vendor.mutation_variants.MutationVariantPlan.applied>`
    so the mutated bytes are only ever on disk for the duration of this call.

    Always runs inside the forked child process :func:`_run_isolated_variant`
    starts -- never called directly from the parent process -- so that a
    mutation that wedges the simulator (spike §2's mutant #4: an FSM that
    never reaches its terminal count) can be killed outright by the parent
    once :data:`DEFAULT_MUTATION_TIMEOUT_S` elapses, which is not possible
    for a blocking in-process call once cocotb's own ``Runner.test()`` has
    handed control to a synchronous ``subprocess.run()`` with no timeout
    parameter of its own (verified against cocotb 2.0.1's own
    ``cocotb_tools/runner.py``).
    """
    runner_module = _fv._import_runner()
    engine_runner = _fv._run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        build_dir=build_dir,
        build_args=build_args,
        defines=defines,
        includes=includes,
        timescale=timescale,
        parameters=parameters,
        log_path=build_log,
    )
    _fv._run_test(
        engine_runner,
        engine=engine,
        module=module,
        module_dir=module_dir,
        hdl_toplevel=hdl_toplevel,
        testcase=testcase,
        random_seed=random_seed,
        build_dir=build_dir,
        test_dir=test_dir,
        results_xml=results_xml,
        timescale=timescale,
        parameters=parameters,
        extra_env=env,
        log_path=test_log,
    )


def _mutation_subprocess_entry(kwargs: dict[str, Any], result_queue: Any) -> None:
    """Target of the forked child process :func:`_run_isolated_variant`
    starts. ``os.setpgrp()`` moves the child into its own process group
    *before* it can spawn cocotb's own simulator subprocess, so the parent
    can SIGKILL the whole group -- child plus simulator grandchild -- rather
    than orphaning the simulator process on a timeout kill.
    """
    try:
        os.setpgrp()
    except OSError:  # pragma: no cover - defensive; absent on non-POSIX
        pass
    try:
        _build_and_test_variant(**kwargs)
    except BaseException as exc:  # noqa: BLE001 - reported back, never raised across the process boundary
        try:
            result_queue.put(("error", f"{exc}"))
        except Exception:  # pragma: no cover - defensive: an unpicklable exc
            result_queue.put(("error", f"{type(exc).__name__}"))
        return
    result_queue.put(("ok", None))


def _kill_process_tree(pid: int | None) -> None:
    """SIGKILL ``pid``'s whole process group (see :func:`_mutation_subprocess_entry`),
    falling back to just ``pid`` if the group lookup itself fails. Never
    raises -- called only after a timeout has already been decided; a kill
    that fails because the process already exited on its own is not this
    function's problem to report."""
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_isolated_variant(
    *, timeout_s: float, **build_and_test_kwargs: Any
) -> tuple[bool, tuple[str, str | None]]:
    """Run :func:`_build_and_test_variant` in a forked child process bounded
    by ``timeout_s`` wall-clock seconds -- the ``klt``-level per-mutant
    subprocess timeout (issue #1592, spike "Open questions"), independent of
    and in addition to whatever cycle bound the testbench's own code has.

    Forking (rather than, say, a ``threading.Thread`` with a join timeout)
    is the load-bearing choice: a thread cannot forcibly terminate a
    blocking ``subprocess.run()`` several call frames down inside cocotb's
    own ``Runner.test()``, which has no timeout parameter to begin with
    (this module's own :func:`_build_and_test_variant` docstring). A forked
    child gives this function an OS process-group handle it can SIGKILL
    outright on a timeout -- see :func:`_kill_process_tree`.

    Returns ``(timed_out, (status, detail))``:

    - ``timed_out=True`` -- the child did not finish within ``timeout_s`` and
      was killed. Booley's own rule, ported verbatim (spike §1a): **"a
      timed-out mutant counts as detected because the mutation can wedge the
      design"** -- the caller classifies this as ``"killed"``, never
      ``"rejected"``.
    - ``timed_out=False, ("ok", None)`` -- the isolated build+test ran to
      completion (a failing *regression* is not an error here -- see
      :func:`_run_test`); the caller compares its ``results.xml`` against the
      baseline's own to classify ``"killed"``/``"survived"``.
    - ``timed_out=False, ("error", detail)`` -- the isolated build+test
      raised (a build/elaboration failure, a missing ``results.xml``, or any
      other exception) before producing a trustworthy verdict; the caller
      classifies this ``"rejected"``.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        # POSIX-only by construction (`os.setpgrp`/`os.killpg` above are
        # both POSIX-only too) -- a clear request-time error beats a cryptic
        # platform failure deep inside `multiprocessing`.
        raise _fv.FunctionalVerificationError(
            "--mutations requires OS process-fork support (POSIX) to bound "
            "each isolated per-mutant build+test with a klt-level subprocess "
            "timeout -- unavailable on this platform"
        )

    ctx = multiprocessing.get_context("fork")
    result_queue: Any = ctx.Queue()
    process = ctx.Process(
        target=_mutation_subprocess_entry,
        args=(build_and_test_kwargs, result_queue),
    )
    process.start()
    process.join(timeout_s)
    if process.is_alive():
        _kill_process_tree(process.pid)
        process.join(_fv._MUTATION_KILL_GRACE_S)
        return True, ("error", f"timed out after {timeout_s}s")

    try:
        return False, result_queue.get_nowait()
    except _queue_module.Empty:
        return False, (
            "error",
            "isolated build/test process exited without a result "
            f"(exit code {process.exitcode})",
        )


def _first_new_failure(
    baseline_tests: list[dict[str, Any]], mutant_tests: list[dict[str, Any]]
) -> str | None:
    """The first ``@cocotb.test()`` name in ``mutant_tests`` that failed
    where the baseline did not (spike §3c's ``first_killing_test``), or
    ``None`` if every mutant failure (if any) also failed on the baseline.

    The baseline-must-pass gate (:func:`run_functional_verification_with_mutations`)
    means ``baseline_tests`` never actually contains a ``"failed"`` entry by
    the time this is called -- so in practice this reduces to "the first
    mutant test that failed at all" -- but the lookup is written generally
    rather than assumed, since nothing about this function's own contract
    depends on that invariant holding.
    """
    baseline_status_by_name = {test["name"]: test["status"] for test in baseline_tests}
    for test in mutant_tests:
        baseline_status = baseline_status_by_name.get(test["name"])
        if test["status"] == "failed" and baseline_status != "failed":
            return test["name"]
    return None


def _write_mutant_combined_log(
    combined_path: str, build_log: str, test_log: str
) -> None:
    """Concatenate one mutant's build+test transcripts into the single
    ``log_path`` artifact the response references (spike §3c) -- `klt`'s own
    choice, not part of the ported seam: the baseline contract already keeps
    ``build_<engine>.log``/``test_<engine>.log`` as two separate files, but
    ``mutation_testing.results[].log_path`` is documented as one path per
    mutant, so this writes a single, clearly-labelled concatenation of both
    (the per-mutant ``build_<engine>.log``/``test_<engine>.log`` themselves
    are also kept on disk, unreferenced by the JSON, for anyone who wants
    them individually).

    Never raises -- a missing/unreadable per-step transcript degrades to
    "leave it out of the combined log", the same posture :func:`_log_tail`
    already takes toward a captured transcript it cannot read.
    """
    parts: list[str] = []
    for label, path in (("build", build_log), ("test", test_log)):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        parts.append(f"--- {label} log ({path}) ---\n{content}")
    try:
        with open(combined_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(parts))
    except OSError:  # pragma: no cover - defensive, mirrors _log_tail's own posture
        pass


def run_functional_verification_with_mutations(
    request: str, proposals_path: str, output_dir: str | None = None
) -> dict[str, Any]:
    """Run ``klt functional-verification --mutations`` end to end (issue
    #1592, spike section 3): the unmodified baseline run from
    :func:`run_functional_verification`, gated by the baseline-must-pass
    check (spike §3d), followed by one isolated, timeout-bounded build+test
    per resolved proposal in ``proposals_path`` (spike §1a's ported seam),
    classified ``killed``/``survived``/``rejected`` (spike §1a/§3c) into the
    additive ``mutation_testing`` response block (spike §3c).

    Raises :class:`FunctionalVerificationError` (CLI exit 1) for every
    whole-run failure mode: anything the base contract already raises for
    (bad request, missing sources, ...), a malformed ``<proposals>``
    document, a ``<proposals>.scope`` entry outside ``request.sources``,
    ``options.coverage``/``options.sdf`` combined with ``--mutations``
    (neither is designed yet -- see "Out of scope" in
    ``docs/cli/functional-verification.md``'s "`--mutations`" section), the
    baseline itself failing (spike §3d), or every proposal ending up
    ``"rejected"`` (``valid_count == 0`` with ``proposal_count > 0`` --
    mirrors the base contract's own "a regression that registered zero
    tests is exit 1, not a vacuous pass").

    A successful return is always a baseline "pass" (`status: "pass"`) --
    the returned dict is the ordinary :func:`run_functional_verification`
    response, unmodified, plus the additive ``mutation_testing`` block. The
    CLI (``cli/functional_verification_cmd.py``) derives exit 3 from
    ``mutation_testing.survived_count > 0``, exit 0 otherwise -- see this
    module's own docstring's "Exit codes" table replicated in
    ``docs/cli/functional-verification.md``.
    """
    # Cheap, whole-document validation first -- before the (expensive)
    # baseline ever runs. Order matters: a malformed proposals document or
    # an out-of-scope entry should never cost a full build+test cycle to
    # discover.
    scope, proposal_records = load_mutation_proposals(proposals_path)
    _validate_proposal_indices(proposal_records)

    request_doc, request_dir = _fv.load_request_arg(request)
    _fv._reject_verilog_top_mutations(request_doc.get("testbench"))
    raw_sources = request_doc.get("sources")
    _validate_mutation_scope(
        scope, raw_sources if isinstance(raw_sources, list) else []
    )

    engine = request_doc.get("engine", _fv.DEFAULT_ENGINE)
    if engine not in _fv.SUPPORTED_ENGINES:
        raise _fv.FunctionalVerificationError(
            f"unsupported engine '{engine}' "
            f"(supported: {', '.join(_fv.SUPPORTED_ENGINES)})"
        )
    (
        coverage_requested,
        timescale,
        random_seed,
        defines,
        user_build_args,
        includes,
        sdf,
        trace_requested,
    ) = _fv._resolve_options(request_doc.get("options"), engine, request_dir)
    if trace_requested:
        # Also not designed (issue #1845/Epic #1585 Phase 3 only wires
        # `options.trace` into the base contract): each isolated per-mutant
        # variant (:func:`_run_isolated_variant`) builds in its own
        # throwaway `build_dir` this function never surfaces, so there is no
        # single trace artifact a `mutation_testing` response block could
        # point at -- a request error is safer than silently tracing only
        # the baseline (or dropping the flag) with no signal either way.
        raise _fv.FunctionalVerificationError(
            "options.trace cannot currently be combined with --mutations -- "
            "not yet designed (Epic #1585 Phase 3 wires options.trace into "
            "the base contract only)"
        )
    if coverage_requested:
        # Explicitly out of scope (issue #1592, per the spike's "Open
        # questions"): the N+1-multiplied Verilator cost of coverage-per-
        # mutant has not been measured, so this is a clean request error
        # rather than a silently-ignored or silently-wrong combination.
        raise _fv.FunctionalVerificationError(
            "options.coverage cannot currently be combined with --mutations "
            "-- not yet designed (docs/design/mutation-testing-spike.md, "
            "'Open questions')"
        )
    _fv._reject_initial_state_with_mutations(
        request_doc.get("options"), engine, request_dir
    )
    if sdf is not None:
        # Also not designed (not explicitly listed in the issue's own "Out
        # of scope", but the same reasoning applies): options.sdf swaps in a
        # generated wrapper module as the elaboration root and adds extra
        # generated source files that this function's own isolated per-
        # mutant build does not replicate, so silently running it would
        # produce a build that diverges from the baseline's own in a way
        # nothing here would catch -- a request error is safer than a
        # silently-wrong mutant build.
        raise _fv.FunctionalVerificationError(
            "options.sdf cannot currently be combined with --mutations -- "
            "not yet designed (docs/design/mutation-testing-spike.md, "
            "'Open questions')"
        )

    sources = _fv._resolve_sources(request_doc["sources"], request_dir)
    hdl_toplevel = request_doc["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise _fv.FunctionalVerificationError(
            "request.hdl_toplevel must be a non-empty string"
        )
    module, module_dir, testcase = _fv._resolve_testbench(
        request_doc["testbench"], request_dir
    )
    parameters = _fv._resolve_parameters(request_doc.get("parameters"))
    env = _fv._resolve_env(request_doc.get("env"))
    _fv._resolve_requires_tools(request_doc.get("requires_tools"), env)
    build_args = list(user_build_args)
    source_defines = _fv._resolve_source_defines(
        request_doc.get("options"), sources, request_dir
    )

    output_dir = _fv._resolve_output_dir(request_dir, output_dir)
    mutants_dir = os.path.join(output_dir, "mutants")
    try:
        os.makedirs(mutants_dir, exist_ok=True)
    except OSError as exc:
        raise _fv.FunctionalVerificationError(
            f"could not create output directory '{mutants_dir}': {exc}"
        ) from exc

    # The baseline-must-pass gate (spike §3d): the exact same request the
    # caller already used for an ordinary (non-mutation) run, reused
    # unchanged -- see `docs/design/mutation-testing-spike.md` §3a.
    baseline = _fv.run_functional_verification(request, output_dir)
    if baseline["status"] != "pass":
        failing_names = ", ".join(
            test["name"] for test in baseline["tests"] if test["status"] == "failed"
        )
        raise _fv.FunctionalVerificationError(
            "the baseline run itself failed ({}/{} tests failed{}) -- "
            "mutation testing aborted before running any proposal: "
            "comparing mutant behavior against an already-broken baseline "
            "cannot distinguish 'the mutation broke it' from 'it was "
            "already broken' (docs/design/mutation-testing-spike.md "
            "section 3d)".format(
                baseline["failed_count"],
                baseline["test_count"],
                f": {failing_names}" if failing_names else "",
            )
        )
    baseline_tests = baseline["tests"]

    plan, doc_rejections = _resolve_mutation_proposals(
        proposal_records, scope, request_dir
    )

    results: list[dict[str, Any]] = []
    rejected_count = 0
    killed_count = 0
    survived_count = 0

    for record in proposal_records:
        index = record.index
        base_result = {
            "index": index,
            "category": record.category,
            "file": record.file,
            "line": record.line,
        }

        if index in doc_rejections:
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": doc_rejections[index],
                    "first_killing_test": None,
                    "log_path": _fv._path_envelope(None),
                }
            )
            rejected_count += 1
            continue

        mutant_dir = os.path.join(mutants_dir, f"mutant_{index}")
        try:
            os.makedirs(mutant_dir, exist_ok=True)
        except OSError as exc:
            raise _fv.FunctionalVerificationError(
                f"could not create mutant directory '{mutant_dir}': {exc}"
            ) from exc
        build_dir = os.path.join(mutant_dir, f"sim_build_{engine}")
        mutant_results_xml = os.path.join(mutant_dir, f"results_{engine}.xml")
        build_log = os.path.join(mutant_dir, f"build_{engine}.log")
        test_log = os.path.join(mutant_dir, f"test_{engine}.log")
        combined_log = os.path.join(mutants_dir, f"mutant_{index}.log")

        # Issue #3290: the wrappers `include the (mutated-in-place) source by
        # absolute path, so they are generated per mutant directory.
        variant_sources, scoped_include_dirs = _fv._write_scoped_define_wrappers(
            sources, source_defines, mutant_dir
        )

        with plan.applied(index):
            timed_out, (status_kind, detail) = _fv._run_isolated_variant(
                timeout_s=_fv.DEFAULT_MUTATION_TIMEOUT_S,
                engine=engine,
                sources=variant_sources,
                hdl_toplevel=hdl_toplevel,
                build_dir=build_dir,
                build_args=build_args,
                defines=defines,
                includes=[*includes, *scoped_include_dirs],
                timescale=timescale,
                parameters=parameters,
                env=env,
                build_log=build_log,
                module=module,
                module_dir=module_dir,
                testcase=testcase,
                random_seed=random_seed,
                test_dir=mutant_dir,
                results_xml=mutant_results_xml,
                test_log=test_log,
            )
        _write_mutant_combined_log(combined_log, build_log, test_log)

        if timed_out:
            # Booley's own rule, ported verbatim (spike §1a): "a timed-out
            # mutant counts as detected because the mutation can wedge the
            # design."
            results.append(
                {
                    **base_result,
                    "status": "killed",
                    "first_killing_test": None,
                    "log_path": _fv._path_envelope(combined_log),
                }
            )
            killed_count += 1
            continue

        if status_kind == "error":
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": detail,
                    "first_killing_test": None,
                    "log_path": _fv._path_envelope(combined_log),
                }
            )
            rejected_count += 1
            continue

        try:
            mutant_tests = _fv.parse_results_xml(mutant_results_xml)
        except _fv.FunctionalVerificationError as exc:
            results.append(
                {
                    **base_result,
                    "status": "rejected",
                    "reason": str(exc),
                    "first_killing_test": None,
                    "log_path": _fv._path_envelope(combined_log),
                }
            )
            rejected_count += 1
            continue

        first_killing_test = _first_new_failure(baseline_tests, mutant_tests)
        if first_killing_test is not None:
            results.append(
                {
                    **base_result,
                    "status": "killed",
                    "first_killing_test": first_killing_test,
                    "log_path": _fv._path_envelope(combined_log),
                }
            )
            killed_count += 1
        else:
            results.append(
                {
                    **base_result,
                    "status": "survived",
                    "first_killing_test": None,
                    "log_path": _fv._path_envelope(combined_log),
                }
            )
            survived_count += 1

    proposal_count = len(proposal_records)
    valid_count = proposal_count - rejected_count
    if valid_count == 0 and proposal_count > 0:
        # Mirrors the base contract's own "a regression that registered
        # zero tests is exit 1, not a vacuous pass": a mutation run that
        # tested nothing must never look like a clean `mutation_score: null`
        # success (spike "Exit codes" table).
        reasons = "; ".join(
            f"#{entry['index']}: {entry['reason']}"
            for entry in results
            if entry["status"] == "rejected"
        )
        raise _fv.FunctionalVerificationError(
            f"every mutation proposal was rejected ({proposal_count} "
            f"proposal(s), 0 valid) -- {reasons}"
        )

    mutation_score = (killed_count / valid_count) if valid_count else None

    baseline["mutation_testing"] = {
        "schema_version": _fv.MUTATION_SCHEMA_VERSION,
        "proposal_count": proposal_count,
        "valid_count": valid_count,
        "rejected_count": rejected_count,
        "killed_count": killed_count,
        "survived_count": survived_count,
        "mutation_score": mutation_score,
        "results": results,
    }
    return baseline


# Imported last so that whichever of the two modules is imported first, the
# other finds every name it needs already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
