"""``klt socket-check`` command: run the socket/template descriptor check
battery and serialise the report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/socket-check.md`` for the full table):
    0 - every check passed or was skipped, none failed
    1 - failed to run (bad layout file, missing/malformed --socket descriptor)
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one check failed
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..socket_check import SocketCheckError, run_socket_check
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_socket_check(args.file, args.socket, top=args.top)
    except SocketCheckError as exc:
        return emit_error("socket-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"socket: {report['socket']}")
    if report["socket_name"]:
        print(f"socket_name: {report['socket_name']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print()
    for check in report["checks"]:
        if check["status"] == "skipped":
            print(f"{check['name']}: skipped ({check['skip_reason']})")
        else:
            print(f"{check['name']}: {check['status']} ({check['violation_count']})")

    for check in report["checks"]:
        if not check["violations"]:
            continue
        print()
        print(f"{check['name']} violations:")
        for entry in check["violations"]:
            print(f"  {entry}")

    budgets = report["budgets"]
    if budgets:
        print()
        print("budgets (declared, not mechanically verified):")
        for budget in budgets:
            note = f" -- {budget['notes']}" if budget["notes"] else ""
            print(f"  {budget['name']}: {budget['value']} {budget['unit']}{note}")
