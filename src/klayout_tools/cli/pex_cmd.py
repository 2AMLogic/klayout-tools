"""``klt pex`` command: serialise the schematic-vs-extracted delta report as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/pex.md`` for the full table):
    0 - every delta row passed
    1 - failed to run at all (bad layout/testbench, unresolvable deck/PDK,
        missing `.include` DUT reference, disagreeing schematic references,
        an extraction or simulation failure) -- returned by ``emit_error``
        as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, at least one delta row failed its measurement limit
    4 - at least one delta row errored (schematic or extracted side did not
        produce a trustworthy value)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. Exit codes 3/4 mirror `klt sim`'s own precedent -- see
docs/cli/sim.md's "Exit codes" section.)
"""

import argparse

from ..pex import PexError, run_pex
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_ROW_FAILED = 3
EXIT_ROW_ERRORED = 4


def run(args: argparse.Namespace) -> int:
    try:
        report = run_pex(
            args.layout,
            args.testbenches,
            args.deck,
            output=args.output,
            top=args.top,
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            artifacts_dir=args.outdir,
            backend=args.backend,
        )
    except PexError as exc:
        return emit_error("pex", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "error":
        return EXIT_ROW_ERRORED
    if report["status"] == "fail":
        return EXIT_ROW_FAILED
    return EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"layout: {report['layout']}")
    print(f"netlist: {report['netlist']}")
    print(f"reference_netlist: {report['reference_netlist']}")
    print(f"status: {report['status']}")
    print(
        f"corners: {report['corner_count']}  "
        f"rows: {len(report['delta'])}  "
        f"passed: {report['passed']}  "
        f"failed: {report['failed']}  "
        f"errored: {report['errored']}"
    )

    extraction = report["extraction"]
    print(
        f"extraction: deck={extraction['deck']}  "
        f"devices={extraction['device_count']}  nets={extraction['net_count']}"
    )

    testbenches = report["testbenches"]
    if testbenches:
        print()
        print("testbenches:")
        for tb in testbenches:
            print(
                f"  {tb['request']}  corners={tb['corner_count']}  "
                f"measurements={', '.join(tb['measurement_names'])}"
            )

    delta = report["delta"]
    if not delta:
        return

    print()
    print("delta:")
    for row in delta:
        delta_pct = row["delta_pct"]
        delta_desc = f"{delta_pct:+.3f}%" if delta_pct is not None else "-"
        print(
            f"  {row['spec_row']} @ {row['corner_id']}  [{row['status']}]  "
            f"schematic={row['schematic_value']!r}  "
            f"extracted={row['extracted_value']!r}  delta={delta_desc}"
        )
