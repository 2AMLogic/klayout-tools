"""``klt power-intent`` command: check isolation, state classification and
control sequencing of a power-gated gate-level netlist, as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/power-intent.md``):
    0 - no violation
    1 - failed to run (unreadable/unparsable inputs, bad intent file)
    3 - ran successfully, at least one violation
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..power_intent import PowerIntentError, reason_description, run_power_intent
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_power_intent(
            args.netlist, args.cell_models, args.intent, top=args.top
        )
    except PowerIntentError as exc:
        return emit_error("power-intent", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"netlist: {report['netlist']} (module {report['module']})")
    print(f"status: {report['status']}")
    print(", ".join(f"{k}: {v}" for k, v in report["summary"].items()))
    for check, body in report["checks"].items():
        print(f"{check}: {body['status']}")
    for v in report["violations"]:
        where = v.get("net") or v.get("instance") or ""
        detail = v.get("sink") or v.get("gated_net") or v.get("event") or ""
        if v.get("before"):
            detail = f"{v['before']} !< {v['after']}"
        print(f"  [{v['check']}] {where} {v['reason']} {detail}".rstrip())
        print(f"      {reason_description(v['reason'])}")
