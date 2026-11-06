"""Mutation-testing entry point for the cocotb functional-verification flow.

Split out of ``functional_verification.py`` (the #1909 SDF-split precedent):
the ``--mutations`` driver (:func:`run_functional_verification_with_mutations`).
The proposal loader lives in ``functional_verification_mutation_proposals`` and
the isolated per-variant machinery in ``functional_verification_mutation_variant``.
Names that stay in ``functional_verification.py`` are reached as ``_fv.<name>``
at call time, so monkeypatching them on ``klayout_tools.functional_verification``
still takes effect; every public name is re-exported from there.
"""

from __future__ import annotations

import os
from typing import Any

from .functional_verification_mutation_proposals import (
    _resolve_mutation_proposals,
    _validate_mutation_scope,
    _validate_proposal_indices,
    load_mutation_proposals,
)
from .functional_verification_mutation_variant import (
    _first_new_failure,
)


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


# Imported last so that whichever module is imported first, the others find
# every name they need already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
