"""``klt drc`` command: serialise the DRC report as text or JSON.

Exit codes (see ``docs/cli/drc.md`` for the full table):
    0 - ran clean, no violations
    1 - failed to run (bad file, unknown deck, engine error)
    3 - ran successfully, violations found
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

import argparse
import json
import sys

from ..drc import DrcError, run_drc

EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_VIOLATIONS = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_drc(args.file, args.deck)
    except DrcError as exc:
        print(f"klt drc: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        _print_text(report)

    return EXIT_VIOLATIONS if report["status"] == "violations" else EXIT_CLEAN


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"deck: {report['deck']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    rule_counts = report["rule_counts"]
    if rule_counts:
        print()
        print("rule_counts:")
        for rule_id in sorted(rule_counts):
            print(f"  {rule_id}: {rule_counts[rule_id]}")

    violations = report["violations"]
    if not violations:
        return

    print()
    for entry in violations:
        bbox = entry["bbox"]
        print(
            f"{entry['rule']}  {entry['cell']}  {entry['layer']}  "
            f"({bbox['left']},{bbox['bottom']})-({bbox['right']},{bbox['top']})  "
            f"{entry['description']}"
        )
