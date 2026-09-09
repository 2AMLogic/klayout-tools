"""``klt functional-verification`` command: serialise the cocotb regression
report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/functional-verification.md`` for the full table):
    0 - every test passed (``status: "pass"``); under ``--mutations``, the
        baseline passed and no valid proposal survived
    1 - failed to run (bad request, unresolvable RTL source or testbench
        module, coverage requested on an engine that has none, missing
        cocotb/simulator install, build/elaboration error, simulator crash,
        no ``results.xml`` produced) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``; under ``--mutations``, additionally: a
        malformed ``<proposals>`` document, a ``<proposals>.scope`` entry
        outside ``request.sources``, the baseline itself failing (the
        mutation-testing "baseline-must-pass gate"), or every proposal
        ending up ``"rejected"``
    3 - ran successfully, at least one test failed (``status: "fail"``);
        under ``--mutations``, the baseline passed but at least one valid
        proposal ``"survived"``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This is the same 0/1/2/3 trichotomy ``klt drc`` uses,
not ``klt sim``'s 3/4 split -- a cocotb regression has no "ran but part of
the batch is untrustworthy" outcome; either the build+run pipeline produced a
``results.xml`` to report from, or it did not. See
``docs/design/cocotb-verification-spike.md`` section 7.)

The ``status: "pass"`` -> exit 0 / ``status: "fail"`` -> exit 3 split is what
``klt eval``'s ``valid`` gate consumes (issue #387) -- an optimizer must
never read exit 1 (never ran) as exit 3 (ran, verdict was bad).

``--mutations`` (issue #1592, ``docs/design/mutation-testing-spike.md``
section 3) adds a ``mutation_testing`` block to the same response shape and
reuses this exact 0/1/2/3 trichotomy: exit 3 means "ran fine, found
[surviving mutants]" the same way it means "ran fine, found violations" for
``klt drc``.
"""

import argparse

from ..functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TESTS_FAILED = 3


def run(args: argparse.Namespace) -> int:
    mutations_path = getattr(args, "mutations", None)
    try:
        if mutations_path:
            report = run_functional_verification_with_mutations(
                args.request, mutations_path
            )
        else:
            report = run_functional_verification(args.request)
    except FunctionalVerificationError as exc:
        return emit_error("functional-verification", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    mutation_testing = report.get("mutation_testing")
    if mutation_testing is not None:
        # Baseline-must-pass (spike §3d) means `report["status"]` is always
        # "pass" by the time a `mutation_testing` block exists at all -- see
        # `run_functional_verification_with_mutations`'s docstring. Exit 3
        # is therefore driven by `survived_count`, matching `klt drc`'s
        # "ran fine, found violations" convention (spike "Exit codes").
        return (
            EXIT_TESTS_FAILED if mutation_testing["survived_count"] > 0 else EXIT_PASS
        )

    return EXIT_TESTS_FAILED if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    environment = report["environment"]
    engine_version = environment.get("engine_version") or ""
    print(f"engine: {report['engine']} {engine_version}".rstrip())
    print(f"hdl_toplevel: {report['hdl_toplevel']}")
    print(f"testbench: {report['testbench']}")
    print(f"status: {report['status']}")
    print(
        f"tests: {report['test_count']}  passed: {report['passed_count']}  "
        f"failed: {report['failed_count']}  skipped: {report['skipped_count']}"
    )

    print()
    for test in report["tests"]:
        sim_time = test.get("sim_time_ns")
        sim_time_text = "" if sim_time is None else f"  {sim_time:.1f} ns"
        print(f"[{test['status']}] {test['name']}{sim_time_text}")
        if test["status"] == "failed":
            error_type = test.get("error_type") or "failure"
            message = test.get("error_message") or ""
            print(f"    {error_type}: {message}".rstrip())

    coverage = report["coverage"]
    if coverage is not None:
        print()
        print("coverage:")
        for key in ("line_pct", "toggle_pct", "branch_pct", "expr_pct"):
            value = coverage.get(key)
            print(f"  {key}: {'n/a' if value is None else value}")
        print(f"  info_path: {coverage['info_path']}")

    print()
    print(f"results_xml: {environment['results_xml']}")
    random_seed = environment.get("random_seed")
    if random_seed is not None:
        print(f"random_seed: {random_seed}")
    # Printed only on an SDF-annotated run (issue #1002): a reader glancing
    # at a gate-level report has no other way to tell a delay-annotated
    # verdict from a zero-delay one, and the two mean very different things.
    sdf = environment.get("sdf")
    if sdf is not None:
        print(f"sdf: {sdf['file']}  corner: {sdf['corner']}")
        # Printed only when a benign diagnostic class was dropped (issue
        # #1102): `annotated: true` alone cannot distinguish "every check in
        # the SDF applied" from "every TIMINGCHECK section was dropped" --
        # this line makes that distinction visible without opening a
        # transcript.
        dropped = sdf.get("dropped") or {}
        if dropped:
            classes = ", ".join(
                f"{klass} (x{info['count']})" for klass, info in sorted(dropped.items())
            )
            print(f"sdf: partial annotation -- dropped: {classes}")

    mutation_testing = report.get("mutation_testing")
    if mutation_testing is not None:
        print()
        print(
            "mutations: {proposal_count} proposed  {valid_count} valid  "
            "{killed_count} killed  {survived_count} survived  "
            "{rejected_count} rejected  score: {score}".format(
                proposal_count=mutation_testing["proposal_count"],
                valid_count=mutation_testing["valid_count"],
                killed_count=mutation_testing["killed_count"],
                survived_count=mutation_testing["survived_count"],
                rejected_count=mutation_testing["rejected_count"],
                score=(
                    "n/a"
                    if mutation_testing["mutation_score"] is None
                    else f"{mutation_testing['mutation_score']:.2f}"
                ),
            )
        )
        for entry in mutation_testing["results"]:
            detail = ""
            if entry["status"] == "rejected":
                detail = f"  ({entry['reason']})"
            elif entry.get("first_killing_test"):
                detail = f"  (killed by {entry['first_killing_test']})"
            print(
                f"  [{entry['status']}] #{entry['index']} {entry['category']} "
                f"{entry['file']}:{entry['line']}{detail}"
            )
