"""``klt lvs`` command: serialise the netlist-compare report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/lvs.md`` for the full table):
    0 - LVS clean, layout matches reference (``status: "match"``)
    1 - failed to run (bad request, unresolvable layout/reference input,
        unknown deck, unsupported engine, engine error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, mismatches found (``status: "mismatch"``)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This is the same 0/1/2/3 split as ``klt drc``, not ``klt sim``'s
3/4 split -- LVS is a clean/dirty verdict like DRC, with no third "ran but
untrustworthy" outcome; see docs/cli/lvs.md's "Exit codes" section.)
"""

import argparse

from ..lvs import LvsError, run_lvs
from .output import emit_error, emit_success

EXIT_MATCH = 0
EXIT_MISMATCH = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_lvs(args.request)
    except LvsError as exc:
        return emit_error("lvs", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_MISMATCH if report["status"] == "mismatch" else EXIT_MATCH


def _print_text(report: dict) -> None:
    print(f"layout: {report['layout']}")
    print(f"reference: {report['reference']}")
    print(f"top: {report['top']}")
    print(f"engine: {report['engine']}")
    # Issue #589: only printed when opted in, so the default human output is
    # unchanged -- but a verdict reached under a caller-supplied design
    # tolerance always says so, right next to `status`.
    if report.get("parameter_tolerance") is not None:
        print(f"parameter_tolerance: {report['parameter_tolerance']}")
    print(f"status: {report['status']}")
    print(f"mismatches: {report['mismatch_count']}")

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
