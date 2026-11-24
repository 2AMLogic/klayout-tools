"""``klt sim-plan`` command: split a ``klt sim`` request into mergeable shards.

Output goes through the shared envelope helpers in :mod:`.output`; see
``docs/cli/sim.md`` ("Planning shards") for the file layout.

Exit codes:
    0 - planned; shard requests and the manifest were written
    1 - could not plan (unreadable request, Monte Carlo / deck-list request,
        axis with fewer than two values, a shard emptied by ``exclude``)
    2 - argparse usage error
"""

import argparse

from ..sim_plan import PlanError, write_plan
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = write_plan(args.request, args.axis)
    except PlanError as exc:
        return emit_error("sim-plan", str(exc), args.format)
    except OSError as exc:
        return emit_error("sim-plan", f"could not write plan: {exc}", args.format)
    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"klt sim-plan: {report['shard_count']} shard(s) by {report['axis']}")
    for shard in report["shards"]:
        print(
            f"  {shard['id']}: {shard['request']} ({shard['corner_count']} corner(s))"
        )
    print(f"  manifest: {report['manifest']}")
