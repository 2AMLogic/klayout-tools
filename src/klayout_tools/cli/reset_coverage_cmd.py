"""``klt reset-coverage`` command: report the state elements of a gate-level
netlist that an asynchronous reset does not reach, as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/reset-coverage.md``):
    0 - every state element is reset-covered or waived, no stale waiver
    1 - failed to run (unreadable/unparsable inputs, bad waiver file)
    3 - ran successfully, an unwaived unreset element or a stale waiver
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..reset_coverage import ResetCoverageError, run_reset_coverage
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_reset_coverage(
            args.netlist,
            args.cell_models,
            args.reset_port,
            reset_polarity=args.reset_polarity,
            top=args.top,
            waiver_path=args.waivers,
        )
    except ResetCoverageError as exc:
        return emit_error("reset-coverage", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"netlist: {report['netlist']} (module {report['module']})")
    print(f"reset port: {report['reset_port']} (active {report['reset_polarity']})")
    print(f"status: {report['status']}")
    summary = report["summary"]
    print(", ".join(f"{k}: {v}" for k, v in summary.items()))
    for element in report["elements"]:
        if element["status"] != "unreset":
            continue
        tag = " [waived]" if element["waived"] else ""
        print(f"  {element['instance']} ({element['cell']}): {element['reason']}{tag}")
    for waiver in report["stale_waivers"]:
        print(f"  stale waiver: {waiver['instance']}")
