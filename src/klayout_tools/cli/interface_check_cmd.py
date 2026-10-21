"""``klt interface-check`` command: check that two blocks' labelled pins mate
at a declared placement and serialise the per-pin report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes (see ``docs/cli/interface-check.md``):
    0 - every matched pin is ``ok``
    1 - failed to run (unreadable stream, bad --place/--pins/layer, no pin matched)
    3 - ran successfully, at least one pin is ``misaligned`` or ``no_partner``
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..interface_check import (
    InterfaceCheckError,
    parse_layer,
    parse_place,
    run_interface_check,
)
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3


def run(args: argparse.Namespace) -> int:
    try:
        place = parse_place(args.place)
        layer_a = (
            parse_layer(args.pin_layer_a, "--pin-layer-a") if args.pin_layer_a else None
        )
        layer_b = (
            parse_layer(args.pin_layer_b, "--pin-layer-b") if args.pin_layer_b else None
        )
        report = run_interface_check(
            args.a,
            args.b,
            place,
            args.pins,
            pin_layer_a=layer_a,
            pin_layer_b=layer_b,
            tol_um=args.tol_um,
            top_a=args.top_a,
            top_b=args.top_b,
        )
    except InterfaceCheckError as exc:
        return emit_error("interface-check", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"a: {report['a']}")
    print(f"b: {report['b']}")
    print(f"place_um: {report['place_um'][0]}, {report['place_um'][1]}")
    print(f"tolerance_um: {report['tolerance_um']}")
    print(f"status: {report['status']}")
    print(
        f"pins: {report['pin_count']} (ok {report['ok_count']}, "
        f"misaligned {report['misaligned_count']}, "
        f"no_partner {report['no_partner_count']})"
    )
    print()
    for pin in report["pins"]:
        detail = ""
        if pin["offset_um"] is not None:
            dx, dy = pin["offset_um"]
            detail = f" offset=({dx}, {dy}) gap={pin['gap_um']}"
        elif pin["reason"]:
            detail = f" ({pin['reason']})"
        print(f"  {pin['name']}: {pin['status']}{detail}")
