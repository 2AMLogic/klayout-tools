"""``klt sim --check <report>`` dispatch (issue #5821).

Kept out of :mod:`klayout_tools.cli.sim_cmd` (at its size ceiling): the parser
points ``func`` here, which handles ``--check`` and defers everything else.
"""

from __future__ import annotations

import argparse

from ..sim_errors import SimError
from ..sim_report_check import check_sim_report
from . import sim_cmd
from .output import EXIT_USAGE_ERROR, emit_error, emit_success, render_check_text

EXIT_MATCH = 0
EXIT_DRIFTED = 3


def _render(result: dict) -> None:
    render_check_text(result)
    for check in result["checks"]:
        if check.get("error"):
            print(f"      error: {check['error']}")
    for item in result.get("skipped", []):
        print(f"  [SKIPPED] {item['field']}: {item['reason']}")
    print(f"diagnosis: {result['diagnosis']}")
    if result.get("note"):
        print(f"note: {result['note']}")


def run(args: argparse.Namespace) -> int:
    if args.check is None:
        if args.root is not None:
            return emit_error(
                "sim",
                "--root only applies with --check",
                args.format,
                exit_code=EXIT_USAGE_ERROR,
            )
        return sim_cmd.run(args)
    if args.request is not None or getattr(args, "batch_set", None) is not None:
        return emit_error(
            "sim",
            "--check takes its report as its own argument and "
            "cannot be combined with a request or --batch-set",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        result = check_sim_report(args.check, root=args.root)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format)
    emit_success(result, args.format, _render)
    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED
