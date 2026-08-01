"""``klt gen-compose`` command: place already-generated ``klt gen`` blocks.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/gen-compose.md`` for the full table):
    0 - composition succeeded -- gds_path was written and the report is on
        stdout
    1 - application error: unresolvable PDK, malformed request, an
        unsupported placement.strategy, a blocks[]/connectivity[] reference
        to a nonexistent id/port, or a write failure -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    2 - usage error (missing request path, bad --format value) -- from
        argparse
"""

import argparse
import json

from ..gen_compose import GenComposeError, compose
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        with open(args.request, encoding="utf-8") as handle:
            request = json.load(handle)
    except OSError as exc:
        return emit_error(
            "gen-compose",
            f"could not read request '{args.request}': {exc}",
            args.format,
        )
    except json.JSONDecodeError as exc:
        return emit_error(
            "gen-compose",
            f"request '{args.request}' is not valid JSON: {exc}",
            args.format,
        )

    try:
        report = compose(request)
    except GenComposeError as exc:
        return emit_error("gen-compose", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"cell_name: {report['cell_name']}")
    print(f"gds_path: {report['gds_path']}")
    pdk = report["pdk"]
    print(f"pdk: {pdk['variant']} ({pdk['version'] or '-'})")
    bbox = report["bbox_um"]
    print(f"bbox_um: ({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})")

    print()
    print("blocks:")
    for block in report["blocks"]:
        offset = block["offset_um"]
        bbox = block["bbox_um"]
        print(
            f"  {block['id']} ({block['generator']})  "
            f"offset=({offset['x']}, {offset['y']})  "
            f"bbox=({bbox['x0']}, {bbox['y0']}) - ({bbox['x1']}, {bbox['y1']})"
        )

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
