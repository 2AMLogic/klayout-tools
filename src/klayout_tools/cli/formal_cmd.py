"""``klt formal`` command: serialise the formal property-check report as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/formal.md``):
    0 - properties hold (``status: "pass"``)
    1 - failed to run at all (bad request, unreadable source, missing
        ``sby``/solver, an sby/Yosys elaboration error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran, a property was violated or a cover was unreachable
        (``status: "fail"``)
    4 - ran but no verdict -- timeout or unproven induction
        (``status: "unknown"``); never ``0``
(2 is reserved for argparse usage errors.) The 0/1/3/4 split is ``klt
equiv``'s.
"""

import argparse

from ..formal import FormalError, run_formal
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3
EXIT_UNKNOWN = 4

_EXIT_BY_STATUS = {
    "pass": EXIT_PASS,
    "fail": EXIT_FAIL,
    "unknown": EXIT_UNKNOWN,
}


def run(args: argparse.Namespace) -> int:
    try:
        report = run_formal(args.request, timeout_s=args.timeout_s)
    except FormalError as exc:
        return emit_error("formal", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return _EXIT_BY_STATUS[report["status"]]


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} (solver: {report['solver']})")
    print(f"top: {report['top']}  mode: {report['mode']}  depth: {report['depth']}")
    if report["netlist_path"]:
        print(f"netlist: {report['netlist_path']}")
    print(f"status: {report['status']}")
    print(f"depth_reached: {report['depth_reached']}")
    if report["trace_path"]:
        print(f"trace: {report['trace_path']}")
    print(f"timeout_s: {report['timeout_s']}  elapsed_s: {report['elapsed_s']}")

    for diag in report["diagnostics"]:
        print(f"  diagnostic: {diag['code']} - {diag['message']}")

    artifacts = report["artifacts"]
    print()
    print(f"sby: {artifacts['sby_path']}")
    if artifacts["log_path"]:
        print(f"log: {artifacts['log_path']}")
