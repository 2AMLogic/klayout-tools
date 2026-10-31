"""``klt diff`` command: compare two layout streams for equal geometry.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``. Exit ``0``
means equivalent, ``3`` means the run succeeded and the layouts differ, ``1``
means an unreadable file or unknown ``--top``.
"""

import argparse

from ..layout_diff import LayoutDiffError, diff_report
from .output import emit_error, emit_success

#: Exit code for "ran fine, layouts differ" (matches ``klt drc`` violations).
EXIT_DIFFERENT = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = diff_report(
            args.file_a, args.file_b, top=args.top, max_items=args.max_items
        )
    except LayoutDiffError as exc:
        return emit_error("diff", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0 if report["equivalent"] else EXIT_DIFFERENT


def _print_text(report: dict) -> None:
    a, b = report["a"], report["b"]
    print(f"a: {a['file']}")
    print(f"b: {b['file']}")
    if report["equivalent"]:
        note = "" if not report["stream_identical"] else " (byte-identical)"
        print(f"status: equivalent{note}")
        if not report["stream_identical"]:
            print(
                "streams differ only in ignored detail (timestamps, element order, ...)"
            )
        return

    print("status: different")
    if not report["dbu_equal"]:
        print(f"dbu_um: a={a['dbu_um']} b={b['dbu_um']}")
    for label, key in (
        ("only in a", "cells_only_in_a"),
        ("only in b", "cells_only_in_b"),
    ):
        if report[key]:
            print(f"cells {label}: {', '.join(report[key])}")
    for diff in report["differences"]:
        print(f"cell {diff['cell']}:")
        for layer in diff["layers"]:
            print(
                f"  layer {layer['layer']}/{layer['datatype']}: "
                f"{layer['only_in_a_count']} only in a, "
                f"{layer['only_in_b_count']} only in b"
            )
        inst = diff["instances"]
        if inst["only_in_a_count"] or inst["only_in_b_count"]:
            print(
                f"  instances: {inst['only_in_a_count']} only in a, "
                f"{inst['only_in_b_count']} only in b"
            )
    if report["truncated"]:
        print(
            f"(output truncated at --max-items {report['max_items']}; see JSON counts)"
        )
