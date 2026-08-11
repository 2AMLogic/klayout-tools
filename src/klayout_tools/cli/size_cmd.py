"""``klt size`` command: serialise a gm/Id sizing result as text or JSON --
either a single device's, or a coupled multi-device topology's (see
``docs/cli/size.md``'s "Coupled multi-device sizing"). The two response
shapes are distinguished by their top-level discriminator key
(``device`` vs. ``topology``), and rendered by :func:`_print_text` /
:func:`_print_topology_text` respectively; the exit-code mapping below is
shared by both.

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
    if "topology" in report:
        _print_topology_text(report)
        return

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
        print(f"objective: {corners.get('objective', 'sizing_corner')}")
        worst_case_id = corners.get("worst_case")
        print("corners:")
        for entry in corners.get("results") or corners.get("declared") or []:
            sizing_marker = "*" if entry.get("is_sizing") else " "
            worst_marker = "!" if entry.get("corner_id") == worst_case_id else " "
            entry_status = entry.get("status", "not evaluated")
            entry_margins = entry.get("margins")
            margin_str = (
                f"  gm_id_rel_error={entry_margins['gm_id_rel_error']:+.4g}"
                if entry_margins is not None
                else ""
            )
            print(
                f"  {sizing_marker}{worst_marker} {entry['corner_id']}: "
                f"{entry_status}{margin_str}"
            )

    method = report["method"]
    print()
    print(f"method: {method['name']}")
    print(f"rationale: {method['rationale']}")

    env = report["environment"]
    print()
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    print(f"models_lib: {env['models_lib']}")


def _print_topology_text(report: dict) -> None:
    topology = report["topology"]
    print(f"topology: {topology['kind']}  vcm={topology['vcm_v']:g}V")
    print(f"status: {report['status']}")

    target = report["target"]
    print(
        f"target: Id_tail={target['id_tail_a']:g}A  pair gm/Id="
        f"{target['pair_gm_id']:g}  mirror gm/Id={target['mirror_gm_id']:g}  "
        f"tail gm/Id={target['tail_gm_id']:g}"
    )

    devices = report.get("devices")
    if devices is not None:
        for role in ("tail", "pair", "mirror"):
            entry = devices[role]
            op = entry.get("operating_point")
            margins = entry.get("margins")
            print()
            print(f"{role}: {entry['status']}")
            if op is not None:
                extra = ""
                if role == "mirror" and op.get("w_output_um") is not None:
                    extra = (
                        f"  W_output={op['w_output_um']:g}um (ratio={op['ratio']:g})"
                    )
                print(
                    f"  W={op['w_um']:g}um  gm/Id={op['gm_id']:g}  "
                    f"Id={op['id_a']:g}A  inversion={op['inversion_level']}{extra}"
                )
            if margins is not None:
                print(
                    f"  margins: gm_id_rel_error={margins['gm_id_rel_error']:+.4g}  "
                    f"id_rel_error={margins['id_rel_error']:+.4g}"
                )

    method = report["method"]
    print()
    print(f"method: {method['name']}")
    print(f"rationale: {method['rationale']}")

    env = report["environment"]
    print()
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    print(f"models_lib: {env['models_lib']}")
