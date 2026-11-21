"""``klt sim-merge`` command: recombine per-request ``klt sim`` reports.

Output goes through the shared envelope helpers in :mod:`.output`; see
``docs/cli/sim.md`` ("Merging per-request reports") for the manifest format
and the exit-code table.

Exit codes:
    0 - merged; every corner passed (``status`` ``"pass"``/``"pass_partial"``)
    1 - could not run (unreadable manifest/report, invalid manifest)
    2 - argparse usage error
    3 - merged; at least one measurement failed a limit
    4 - merged; a corner errored, was graded inconclusive, or nothing was
        checked (the same meaning as for ``klt sim``)
    5 - refused (payload ``status: "refused"`` on stdout, ``refusal.code``
        names the class): a planned shard has no report
    6 - refused: two reports for the same planned shard
    7 - refused: a report that belongs to no planned shard
    8 - refused: a report whose corners differ from every planned shard's
    9 - refused: a planned request file is not the one the plan pinned
   10 - refused: shards disagree on measurements/netlist/schema (not one grid)
"""

import argparse

from ..sim_merge import MergeError, merge_reports
from .output import emit_error, emit_success
from .sim_cmd import EXIT_CORNER_ERRORED, EXIT_MEASUREMENT_FAILED, EXIT_PASS


def run(args: argparse.Namespace) -> int:
    try:
        report = merge_reports(args.manifest, args.reports)
    except MergeError as exc:
        if exc.code is None:
            return emit_error("sim-merge", str(exc), args.format)
        # A refusal is a verdict, not a tool failure: exits >= 3 mean "a
        # payload is on stdout" (docs/json-contract.md), so emit one.
        emit_success(
            {
                "schema_version": 1,
                "status": "refused",
                "refusal": {"code": exc.code, "message": str(exc)},
            },
            args.format,
            lambda payload: print(f"klt sim-merge: {payload['refusal']['message']}"),
        )
        return exc.exit_code
    emit_success(report, args.format, _print_text)
    if report["status"] in {"error", "not_checked", "inconclusive"}:
        return EXIT_CORNER_ERRORED
    if report["status"] == "fail":
        return EXIT_MEASUREMENT_FAILED
    return EXIT_PASS


def _print_text(report: dict) -> None:
    merge = report["merge"]
    print(
        f"klt sim-merge: {merge['shard_count']} shard(s), "
        f"{report['corner_count']} corner(s): {report['status']}"
    )
    print(
        f"  passed {report['passed']}  failed {report['failed']}  "
        f"errored {report['errored']}  inconclusive {report['inconclusive']}"
    )
    for entry in report["measurements"]:
        worst = entry["worst_case"]
        where = f" (worst: {worst['corner_id']})" if worst else ""
        print(f"  {entry['name']}: {entry['status']}{where}")
