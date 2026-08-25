"""``klt lvs`` command: serialise the netlist-compare report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/lvs.md`` for the full table):
    0 - LVS clean, layout matches reference (``status: "match"``), or under
        --check, the committed report still holds (``status: "match"``)
    1 - failed to run (bad request, unresolvable layout/reference input,
        unknown deck, unsupported engine, engine error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, mismatches found (``status: "mismatch"``), or
        under --check, drifted (``status: "drifted"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This is the same 0/1/2/3 split as ``klt drc``, not ``klt sim``'s
3/4 split -- LVS is a clean/dirty verdict like DRC, with no third "ran but
untrustworthy" outcome; see docs/cli/lvs.md's "Exit codes" section.)

``--check <report>`` (issue #1106) switches ``klt lvs`` from running a fresh
compare into *verifying a previously committed* ``--format json`` report
still reproduces -- see ``docs/cli/lvs.md``, "--check" -- and is mutually
exclusive with the positional ``request`` argument (inputs are read from the
committed report itself). Cheap mode (default): re-hash the layout/
reference netlists (and extraction deck, when one was used) named in the
report and compare against its own recorded hash values, no compare engine
re-run. Full mode (``--check <report> --rerun``): best-effort re-run of the
compare the report describes and diff verdict-bearing fields. Both reuse
``status: "match"`` / ``"drifted"`` and the same 0/3 exit-code split as a
normal run (see ``klayout_tools._report_verify``).
"""

import argparse

from ..lvs import LvsError, check_lvs_report, rerun_lvs_report, run_lvs
from .output import emit_error, emit_success

EXIT_MATCH = 0
EXIT_MISMATCH = 3
#: Aliases for the --check/--rerun outcome (issue #1106) -- same numeric
#: values as a normal run's 0/3 split (see this module's docstring), just
#: named for the "match"/"drifted" vocabulary those modes report under.
EXIT_DRIFTED = EXIT_MISMATCH


def run(args: argparse.Namespace) -> int:
    # `request` and `--check` are a required, mutually exclusive argparse
    # group (parser.py) -- omitting both, or giving both, is already a usage
    # error (exit 2) by the time `run()` is ever called.
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error("lvs", "--rerun requires --check <report>", args.format)

    try:
        report = run_lvs(args.request)
    except LvsError as exc:
        return emit_error("lvs", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_MISMATCH if report["status"] == "mismatch" else EXIT_MATCH


def _run_check(args: argparse.Namespace) -> int:
    try:
        if args.rerun:
            result = rerun_lvs_report(args.check)
        else:
            result = check_lvs_report(args.check)
    except LvsError as exc:
        return emit_error("lvs", str(exc), args.format)

    text_renderer = _print_rerun_text if args.rerun else _print_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


def _print_text(report: dict) -> None:
    print(f"layout: {report['layout']}")
    print(f"reference: {report['reference']}")
    print(f"top: {report['top']}")
    # Issue #1205: only printed when the two sides resolved to different top
    # circuits -- the ordinary compare's output is unchanged, but a negative
    # control (a `<cell>_shorted` layout compared against the intact
    # `<cell>`'s reference netlist) says which cell each side actually used.
    if report.get("reference_top") not in (None, report["top"]):
        print(f"reference top: {report['reference_top']}")
    print(f"engine: {report['engine']}")
    # Issue #589: only printed when opted in, so the default human output is
    # unchanged -- but a verdict reached under a caller-supplied design
    # tolerance always says so, right next to `status`.
    if report.get("parameter_tolerance") is not None:
        print(f"parameter_tolerance: {report['parameter_tolerance']}")
    print(f"status: {report['status']}")
    print(f"mismatches: {report['mismatch_count']}")
    # Issue #1132: printed unconditionally (like `mismatches` above), even
    # when 0, so a caller skimming text output can tell "0 mismatches" and
    # "N mismatches, 0 of them errors" apart from "no report at all".
    print(f"errors: {report['error_count']}")

    counts = report["counts"]
    for kind in ("nets", "devices", "pins"):
        c = counts[kind]
        print(
            f"  {kind}: layout={c['layout']}  reference={c['reference']}  "
            f"matched={c['matched']}"
        )

    category_counts = report["category_counts"]
    if category_counts:
        print()
        print("category_counts:")
        for category in sorted(category_counts):
            print(f"  {category}: {category_counts[category]}")

    mismatches = report["mismatches"]
    if not mismatches:
        return

    print()
    for entry in mismatches:
        print(
            f"[{entry['severity']}] {entry['category']}  ({entry['side']})  "
            f"{entry['description']}"
        )


def _print_check_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")
    # Issue #1373: engine-version drift is a non-fatal advisory, never folded
    # into the `[OK]`/`[DRIFTED]` hash-integrity list above -- it does not
    # affect `status`/exit code, so a scripted gate parsing that list alone
    # never sees it.
    for advisory in result.get("advisories", ()):
        print(
            f"  [DRIFT] {advisory['field']}: {advisory['report']} (report) "
            f"vs {advisory['current']} (current)"
        )


def _print_rerun_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    drift = result["drift"]
    if not drift:
        return
    print()
    print("drift:")
    for entry in drift:
        print(f"  {entry['field']}:")
        print(f"      committed: {entry['committed']!r}")
        print(f"      fresh:     {entry['fresh']!r}")
