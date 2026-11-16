"""``klt em`` command: run a field-solver request (issue #5616).

Output goes through the shared envelope helpers in :mod:`.output`.

Exit codes (see ``docs/cli/em.md``):
    0 - solve completed, every declared check passed (or none declared)
    1 - application error (bad request, missing ``geode`` binary, failed or
        timed-out solve, malformed engine report); error envelope, no report
    3 - solve completed but at least one check failed its limits
(2 is reserved for argparse usage errors.)
"""

import argparse

from ..em import EmError, run_em
from .output import emit_error, emit_success

EXIT_CHECK_FAILED = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_em(args.request)
    except EmError as exc:
        return emit_error("em", str(exc), args.format)
    emit_success(report, args.format, _print_text)
    return EXIT_CHECK_FAILED if report["status"] == "fail" else 0


def _print_text(report: dict) -> None:
    env = report["environment"]
    print(f"request: {report['request']}")
    print(f"status: {report['status']}")
    print(f"engine: {env['engine']} {env['engine_version'] or '(version unknown)'}")
    c = report["results"]["c"]
    names = c["terminals"]
    width = max(len(n) for n in names)
    print()
    print(" " * width + "  " + "  ".join(n.rjust(12) for n in names))
    for name, row in zip(names, c["matrix"], strict=True):
        print(f"{name.ljust(width)}  " + "  ".join(f"{v:12.6g}" for v in row))
    print("(farads)")
    for check in report["checks"]:
        print(
            f"check {check['name']}: {check['status']} "
            f"{check['value']:.6g} {check['unit']}"
        )
    for key, art in report["artifacts"].items():
        print(f"artifact {key}: {art['path']}")
