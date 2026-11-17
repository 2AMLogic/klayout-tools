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
from collections.abc import Callable
from typing import Any

from ..functional_verification import (
    FunctionalVerificationError,
    _log_tail,
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TESTS_FAILED = 3


def add_functional_verification_parser(
    subparsers: argparse._SubParsersAction,
    _add_pdk_args: Callable[..., Any],
    _add_format_arg: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the ``functional-verification`` subparser (moved out of
    ``parser.py`` for the size ratchet, the ``add_sim_parser`` pattern --
    issue #5782, which is also what adds ``--pdk``/``--pdk-root``)."""
    parser = subparsers.add_parser(
        "functional-verification",
        help="run a cocotb regression against Icarus/Verilator",
        description=(
            "Run a cocotb testbench against RTL sources through Icarus "
            "Verilog (default) or Verilator, reporting a per-test "
            "passed/failed/skipped breakdown plus optional Verilator "
            "coverage -- see docs/design/cocotb-verification-spike.md "
            "section 7 for the request/response contract and "
            "docs/cli/functional-verification.md for the CLI surface. Phase "
            "3 of Epic #391, and the hard gate behind `klt eval`'s `valid` "
            "field (#387). Invoked exclusively through cocotb 2.0's "
            "first-party Python `Runner` API, never a generated Makefile; "
            "the verdict is always derived from the run's own `results.xml`, "
            "never from a simulator's exit code. Requires cocotb (`pip "
            "install cocotb`) plus an `iverilog` or `verilator` binary on "
            '`$PATH`. `options.coverage` requires `engine: "verilator"`. '
            "Takes a request document (like `klt lvs`/`klt sim`), not "
            "positional RTL file args."
        ),
    )
    parser.add_argument(
        "request",
        help=(
            "klt functional-verification request: a path to a JSON file, '-' "
            "to read the request from stdin, or an inline JSON object string"
        ),
    )
    parser.add_argument(
        "--mutations",
        metavar="PROPOSALS_PATH",
        help=(
            "run mutation testing (issue #1592): a path to a <proposals> "
            "JSON document (schema "
            "klt.functional_verification.mutation_proposals/1) naming "
            "byte-exact source mutations to apply, one at a time, to an "
            "isolated build+test of the same <request>. Aborts with exit 1 "
            "if the request's own baseline run fails first -- see "
            "docs/cli/functional-verification.md, '--mutations'"
        ),
    )
    parser.add_argument(
        "-o",
        "--outdir",
        default=None,
        help=(
            "write run artifacts (results XML, logs, simulator build "
            "directory, coverage, trace, mutants/) here instead of "
            "`.klt/functional-verification/` next to the request file; "
            "created if missing, relative paths resolve against the cwd. "
            "Request-relative source paths still resolve against the "
            "request's own directory"
        ),
    )
    _add_pdk_args(
        parser,
        pdk_help=(
            "PDK variant to resolve a request.pdk block's cell models from "
            "(e.g. sky130A); overrides request.pdk.variant (issue #5782)"
        ),
    )
    _add_format_arg(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    mutations_path = getattr(args, "mutations", None)
    outdir = getattr(args, "outdir", None)
    pdk_variant = getattr(args, "pdk", None)
    pdk_root = getattr(args, "pdk_root", None)
    try:
        if mutations_path:
            report = run_functional_verification_with_mutations(
                args.request,
                mutations_path,
                output_dir=outdir,
                pdk_variant=pdk_variant,
                pdk_root=pdk_root,
            )
        else:
            report = run_functional_verification(
                args.request,
                output_dir=outdir,
                pdk_variant=pdk_variant,
                pdk_root=pdk_root,
            )
    except FunctionalVerificationError as exc:
        return emit_error(
            "functional-verification",
            str(exc),
            args.format,
            code=getattr(exc, "code", None),
            details=getattr(exc, "details", None),
        )

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


def _print_initial_state(initial_state: dict | None) -> None:
    """One line, only when `options.initial_state` was requested (issue #2998)."""
    if initial_state is None:
        return
    seeds = ",".join(str(seed) for seed in initial_state["seeds"]) or "-"
    print(
        f"initial_state: {initial_state['mode']}  seeds: {seeds}  "
        f"state_elements: {initial_state['state_elements']}  "
        f"clock: {initial_state['clock']}"
    )


def _local_path(report: dict, key: str) -> str | None:
    """The resolved absolute path for ``environment.<key>``. The JSON carries
    only a repo-relative ``{path, scope}`` envelope (#2224), which is null for
    an external outdir, so the text view reads the path the run kept aside."""
    local_paths = getattr(report, "local_paths", None)
    if local_paths is not None:
        return local_paths.get(key)
    return (report["environment"].get(key) or {}).get("path")


def _print_log_lines(report: dict) -> None:
    test_log = _local_path(report, "test_log")
    status = report["status"]
    if test_log is None:
        return
    print(f"test_log: {test_log}")
    if status == "fail":
        print(f"test_log tail: {_log_tail(test_log, lines=40)}")


def _print_results_xml(report: dict) -> None:
    # Null on the Verilog-bench path (issue #4083): no cocotb run, no XML.
    results_xml = _local_path(report, "results_xml")
    if results_xml is not None:
        print(f"results_xml: {results_xml}")


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

    # Printed only when `options.trace` was requested (Epic #1585 Phase 3,
    # issue #1845) -- the artifact `klt wave build`'s own `trace` field
    # accepts unchanged (docs/cli/wave.md).
    trace = report.get("trace")
    if trace is not None:
        print()
        print(
            f"trace: {trace['path']}  format: {trace['format']}  "
            f"size_bytes: {trace['size_bytes']}"
        )

    print()
    _print_results_xml(report)
    _print_log_lines(report)
    random_seed = environment.get("random_seed")
    if random_seed is not None:
        print(f"random_seed: {random_seed}")
    _print_initial_state(environment.get("initial_state"))
    # Printed only on an SDF-annotated run (issue #1002): a reader glancing
    # at a gate-level report has no other way to tell a delay-annotated
    # verdict from a zero-delay one, and the two mean very different things.
    sdf = environment.get("sdf")
    if sdf is not None:
        # Issue #2364: the entry population is part of what a glance must
        # convey -- "interconnect" means every IOPATH delay was deliberately
        # set aside (net-delay back-annotation only), which changes what the
        # verdict below means. Omitted on the default "all" mode so the line
        # stays byte-identical to before the field existed.
        entries_note = (
            "" if sdf.get("entries", "all") == "all" else f"  entries: {sdf['entries']}"
        )
        print(f"sdf: {sdf['file']}  corner: {sdf['corner']}{entries_note}")
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
