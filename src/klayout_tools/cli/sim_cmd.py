"""``klt sim`` command: serialise the PVT corner-sweep report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/sim.md`` for the full table):
    0 - every corner passed
    1 - failed to run at all (bad request, unresolvable netlist/model
        library, unsupported engine) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one measurement failed a limit
    4 - at least one corner errored -- the sweep is incomplete/untrustworthy
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)

Exit codes 3/4 extend drc's precedent (0/1/2/3) rather than reusing 2, per
the spike's flagged open question -- see docs/cli/sim.md's "Exit codes"
section for the reasoning.
"""

import argparse

from ..sim import SimError, run_sim
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_MEASUREMENT_FAILED = 3
EXIT_CORNER_ERRORED = 4


def run(args: argparse.Namespace) -> int:
    try:
        report = run_sim(args.request, artifacts_dir=args.outdir)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "error":
        return EXIT_CORNER_ERRORED
    if report["status"] == "fail":
        return EXIT_MEASUREMENT_FAILED
    return EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"netlist: {report['netlist']}")
    print(f"status: {report['status']}")
    print(
        f"corners: {report['corner_count']}  "
        f"passed: {report['passed']}  "
        f"failed: {report['failed']}  "
        f"errored: {report['errored']}"
    )

    env = report["environment"]
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    print(f"models_lib: {env['models_lib']}")

    measurements = report["measurements"]
    if measurements:
        print()
        print("measurements:")
        for m in measurements:
            worst = m["worst_case"]
            if worst is not None:
                worst_desc = (
                    f"worst={worst['value']!r} @ {worst['corner_id']} "
                    f"(margin={worst['margin']!r})"
                )
            else:
                worst_desc = "worst=-"
            print(f"  {m['name']} [{m['status']}]  {worst_desc}")

    corners = report["corners"]
    if not corners:
        return

    print()
    for corner in corners:
        runtime = corner["runtime_s"]
        print(f"{corner['corner_id']}  [{corner['status']}]  runtime={runtime}s")
        for diag in corner["diagnostics"]:
            print(f"    diagnostic: {diag['code']} - {diag['message']}")
