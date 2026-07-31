"""``klt layout-metrics`` command: emit a normalized ``layout.json`` per block.

Unlike the other ``klt`` verbs, this command's primary output is a file
(``<block>/output/layout.json``, or ``--output``), not just stdout — the
JSON envelope on stdout (``--format json``) mirrors exactly what was written,
so a caller never has to re-read the file to see what happened. ``--dry-run``
skips the write entirely.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.
"""

import argparse
import json

from ..layout_metrics import LayoutMetricsError, emit_layout_json, layout_metrics_report
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        if args.dry_run:
            report = layout_metrics_report(args.block, deck=args.deck)
            written = None
        else:
            written = emit_layout_json(
                args.block, deck=args.deck, output_path=args.output
            )
            report = json.loads(written.read_text())
    except LayoutMetricsError as exc:
        return emit_error("layout-metrics", str(exc), args.format)

    emit_success(report, args.format, lambda payload: _print_text(payload, written))
    return 0


def _print_text(report: dict, written) -> None:
    print(f"slug: {report['slug']}")
    print(f"name: {report['name']}")
    print(f"status: {report['status']}")
    if "layer_count" in report:
        print(f"layer_count: {report['layer_count']}")
    if "cell_count" in report:
        print(f"cell_count: {report['cell_count']}")
    if "instance_count" in report:
        print(f"instance_count: {report['instance_count']}")
    if "drc" in report:
        drc = report["drc"]
        print(
            f"drc: deck={drc['deck']} status={drc['status']} "
            f"violations={drc['violation_count']}"
        )
    if "renders" in report:
        print(f"renders: {', '.join(sorted(report['renders']))}")
    if written is not None:
        print(f"written: {written}")
    else:
        print("written: (dry run, not written)")
