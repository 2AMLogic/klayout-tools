"""``klt tap-distance`` command: assert every subject point lies within a
Euclidean distance of a qualifying tap and serialise the result as text or
JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/tap-distance.md`` for the full table):
    0 - pass
    1 - failed to run (bad file, malformed expression/spec, unknown --top)
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, a subject point is too far from every tap
    4 - ran successfully but the rule could not be evaluated (``unsupported``)
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

from __future__ import annotations

import argparse

from ..tap_distance import TapDistanceError, run_tap_distance
from ._parsing import load_region
from .output import emit_error, emit_success, render_table

EXIT_PASS = 0
EXIT_FAIL = 3
EXIT_UNSUPPORTED = 4


def run(args: argparse.Namespace) -> int:
    try:
        report = run_tap_distance(
            args.file,
            args.subject,
            args.taps,
            args.max_distance_um,
            island=args.island,
            tap_nets=args.tap_net,
            net_spec=args.net_spec,
            region_um=load_region(args.region, TapDistanceError),
            top=args.top,
        )
    except TapDistanceError as exc:
        return emit_error("tap-distance", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "unsupported":
        return EXIT_UNSUPPORTED
    return EXIT_FAIL if report["status"] == "fail" else EXIT_PASS


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"subject: {report['subject']}  taps: {report['taps']}")
    print(f"max_distance_um: {report['max_distance_um']}")
    print(f"status: {report['status']}")
    if report["status"] == "unsupported":
        print(f"reason: {report['reason']} -- {report['reason_detail']}")
        return
    measurement = report["measurement"]
    if measurement["measured_max_distance_um"] is not None:
        print(
            f"measured_max_distance_um: <= {measurement['measured_max_distance_um']}"
            f" (> {measurement['lower_bound_um']})"
        )
    print(f"violations: {report['violation_count']}")
    if not report["violations"]:
        return
    rows: list[tuple[str, ...]] = [
        (
            str(entry["kind"]),
            str(entry["island"]),
            f"({entry['bbox']['left']},{entry['bbox']['bottom']})-"
            f"({entry['bbox']['right']},{entry['bbox']['top']})",
            str(entry["description"]),
        )
        for entry in report["violations"]
    ]
    render_table(
        ("Kind", "Island", "BBox", "Description"), rows, left_aligned={0, 2, 3}
    )
