"""``klt size`` command: serialise a single-device gm/Id sizing result as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/size.md`` for the full table):
    0 - the confirmed operating point meets the gm/Id target within
        tolerance
    1 - the request could not even be attempted (bad request, unresolvable
        model library, unsupported engine/device kind) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - the search ran but the target could not be met (infeasible within
        the given width bounds, or outside the stated tolerance)
    4 - the ngspice evaluator itself errored (launch failure, timeout, or
        no usable operating-point data)

Reuses the exact exit-code trichotomy ``klt sim`` established (0/1/3/4,
skipping 2 which argparse itself owns) rather than inventing a fourth
scheme -- see ``docs/cli/sim.md``'s "Exit codes" section for the reasoning
this command shares.
"""

import argparse

from ..size import SizeError, run_size
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TARGET_UNMET = 3
EXIT_EVALUATOR_ERRORED = 4


def run(args: argparse.Namespace) -> int:
    try:
        report = run_size(
            args.request,
            artifacts_dir=args.outdir,
            timeout_s=args.timeout_s,
        )
    except SizeError as exc:
        return emit_error("size", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "error":
        return EXIT_EVALUATOR_ERRORED
    if report["status"] == "fail":
        return EXIT_TARGET_UNMET
    return EXIT_PASS


def _print_text(report: dict) -> None:
    device = report["device"]
    print(f"device: {device['kind']} {device['model']} (L={device['l_um']}um)")
    print(f"status: {report['status']}")

    target = report["target"]
    print(f"target: gm/Id={target['gm_id']:g}  Id={target['id_a']:g}A")

    op = report.get("operating_point")
    if op is not None:
        print(
            f"sized: W={op['w_um']:g}um  gm/Id={op['gm_id']:g}  "
            f"Id={op['id_a']:g}A  inversion={op['inversion_level']}"
        )
        if op.get("vgs_v") is not None:
            print(f"  Vgs={op['vgs_v']:g}V  Vth={op['vth_v']!r}  Vov={op['vov_v']!r}")

    margins = report.get("margins")
    if margins is not None:
        print(
            f"margins: gm_id_rel_error={margins['gm_id_rel_error']:+.4g}  "
            f"id_rel_error={margins['id_rel_error']:+.4g}"
        )

    corners = report.get("corners")
    if corners is not None and len(corners.get("declared") or []) > 1:
        print()
        print("corners:")
        for entry in corners.get("results") or corners.get("declared") or []:
            marker = "*" if entry.get("is_sizing") else " "
            entry_status = entry.get("status", "not evaluated")
            print(f"  {marker} {entry['corner_id']}: {entry_status}")

    method = report["method"]
    print()
    print(f"method: {method['name']}")
    print(f"rationale: {method['rationale']}")

    env = report["environment"]
    print()
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    print(f"models_lib: {env['models_lib']}")
