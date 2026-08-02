"""``klt extract`` command: serialise the extraction report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/extract.md`` for the full table):
    0 - extraction succeeded, netlist written
    1 - failed to run (bad file, unknown deck, unresolvable PDK, missing/
        ambiguous top cell, engine error) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no "ran but found problems" outcome for extraction --
it either produces a netlist or it fails -- so, unlike ``klt drc``/``klt
lvs``, there is no exit code 3; see docs/cli/extract.md.)
"""

import argparse

from ..extract import ExtractError, run_extract
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_extract(
            args.file,
            args.deck,
            output=args.output,
            top=args.top,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            parasitics=args.parasitics,
        )
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"deck: {report['deck']}")
    print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"netlist_path: {report['netlist_path']}")
    print(f"netlist_sha256: {report['netlist_sha256']}")
    print(
        f"devices: {report['device_count']}  "
        f"nets: {report['net_count']}  "
        f"pins: {report['pin_count']}"
    )

    pdk = report["pdk"]
    if pdk is not None:
        print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")

    parasitics = report.get("parasitics")
    if parasitics is not None:
        print(
            f"parasitics: R={parasitics['r_count']} C={parasitics['c_count']}  "
            f"total_R={parasitics['total_resistance_ohm']} ohm  "
            f"total_C={parasitics['total_capacitance_ff']} fF"
        )

    device_counts = report["device_counts"]
    if device_counts:
        print()
        print("device_counts:")
        for class_name in sorted(device_counts):
            print(f"  {class_name}: {device_counts[class_name]}")

    ignored_layers = report.get("ignored_layers", [])
    if ignored_layers:
        print()
        print("ignored_layers (shapes not read by this deck's connectivity):")
        for entry in ignored_layers:
            print(f"  {entry['layer']}/{entry['datatype']}: {entry['shapes']} shape(s)")

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
