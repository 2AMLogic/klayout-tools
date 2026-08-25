"""``klt functional-verification`` command: serialise the cocotb regression
report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/functional-verification.md`` for the full table):
    0 - every test passed (``status: "pass"``)
    1 - failed to run (bad request, unresolvable RTL source or testbench
        module, coverage requested on an engine that has none, missing
        cocotb/simulator install, build/elaboration error, simulator crash,
        no ``results.xml`` produced) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one test failed (``status: "fail"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This is the same 0/1/2/3 trichotomy ``klt drc`` uses,
not ``klt sim``'s 3/4 split -- a cocotb regression has no "ran but part of
the batch is untrustworthy" outcome; either the build+run pipeline produced
a ``results.xml`` to report from, or it did not. See
``docs/design/cocotb-verification-spike.md`` section 7.)

The ``status: "pass"`` -> exit 0 / ``status: "fail"`` -> exit 3 split is what
``klt eval``'s ``valid`` gate consumes (issue #387) -- an optimizer must
never read exit 1 (never ran) as exit 3 (ran, verdict was bad).
"""

import argparse

from ..functional_verification import (
    FunctionalVerificationError,
    run_functional_verification,
)
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TESTS_FAILED = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_functional_verification(args.request)
    except FunctionalVerificationError as exc:
        return emit_error("functional-verification", str(exc), args.format)

    emit_success(report, args.format, _print_text)

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
