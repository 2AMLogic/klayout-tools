"""``klt extract`` command: serialise the extraction report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/extract.md`` for the full table):
    0 - extracted a netlist
    1 - failed to run (bad file, unknown deck, unresolvable PDK, engine
        error) — returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

There is deliberately no exit ``3`` here, unlike ``klt drc``: extraction has
no "ran successfully but found problems" outcome — it either produces a
netlist or it fails (``docs/design/lvs-extraction-spike.md`` § "Exit codes").
"""

import argparse

from ..extract import ExtractError, run_extract
from .output import emit_error, emit_success

EXIT_OK = 0


def run(args: argparse.Namespace) -> int:
    try:
        report = run_extract(
            args.file,
            deck_name=args.deck,
            output=args.output,
            top=args.top,
            pdk=args.pdk,
            pdk_root=args.pdk_root,
        )
    except ExtractError as exc:
        return emit_error("extract", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_OK


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"deck: {report['deck']}")
    print(f"top: {report['top']}")
    print(f"dbu_um: {report['dbu_um']}")
    pdk = report.get("pdk")
    if pdk:
        version = pdk.get("version") or "unknown"
        print(f"pdk: {pdk['variant']} ({version}) at {pdk['root']}")
    print(f"netlist: {report['netlist_path']}")
    print(f"sha256: {report['netlist_sha256']}")
    print(f"status: {report['status']}")
    print(
        f"devices: {report['device_count']}  "
        f"nets: {report['net_count']}  "
        f"pins: {report['pin_count']}"
    )

    device_counts = report["device_counts"]
    if device_counts:
        print()
        print("device_counts:")
        for class_name in sorted(device_counts):
            print(f"  {class_name}: {device_counts[class_name]}")

    devices = report["devices"]
    if devices:
        print()
        for entry in devices:
            terminals = " ".join(
                f"{name}={net if net is not None else '-'}"
                for name, net in entry["nets"].items()
            )
            params = " ".join(
                f"{key}={value:g}" for key, value in entry["params"].items()
            )
            print(f"{entry['name']}  {entry['class']}  {terminals}  {params}")

    warnings = report["warnings"]
    if warnings:
        print()
        for warning in warnings:
            print(f"warning: {warning}")
