"""``klt trajectory`` command: render an optimization trajectory JSONL log into
a milestone table + objective-vs-turn plot.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Why a standalone verb rather than extending ``klt report``:
``klt report`` renders one or more *single-object* JSON envelopes (a
``klt drc``/``lvs``/``layout-metrics`` result) into a combined report,
detecting each envelope's kind from its own top-level shape. A trajectory log
is a different input *format* entirely -- an append-only JSONL stream of many
records, not one envelope -- and this verb's job is analytic (milestone
detection across records) plus dual-artifact (a markdown table *and* an SVG
plot), neither of which fits ``report``'s "classify one envelope, render one
section" model. Folding a JSONL-log kind into ``report`` would overload it
with a second input grammar and a plot generator it otherwise has no reason
to own; a focused verb keeps both commands simple. The record schema still
mirrors the ``klt eval`` envelope's ``objective``/``gate_results`` shape
(#387), so the two stay vocabulary-compatible even though they are separate
verbs.

Exit codes (see ``docs/cli/trajectory.md`` for the full table):
    0 - trajectory rendered successfully
    1 - failed to run (missing/unreadable/malformed/empty log, or a log that
        mixes objectives) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. Like ``klt report``, this command has no "ran but found
regressions" exit code -- a trajectory's milestones are content it reports,
not a verdict it gates its own exit code on.)
"""

import argparse

from ..trajectory import TrajectoryError, build_trajectory
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        trajectory = build_trajectory(args.log, threshold=args.threshold)
    except TrajectoryError as exc:
        error_format = args.format if args.format == "json" else "text"
        return emit_error("trajectory", str(exc), error_format)

    # Optionally write the SVG plot to disk for README embedding, independent
    # of the chosen --format (a courtesy side effect, not part of the JSON
    # contract). A write failure is an application error, same exit code.
    if args.plot is not None:
        try:
            with open(args.plot, "w", encoding="utf-8") as handle:
                handle.write(trajectory["plot_svg"])
        except OSError as exc:
            error_format = args.format if args.format == "json" else "text"
            return emit_error(
                "trajectory",
                f"could not write plot to '{args.plot}': {exc}",
                error_format,
            )

    emit_success(trajectory, args.format, lambda payload: _print_text(payload, args))
    return 0


def _print_text(trajectory: dict, args: argparse.Namespace) -> None:
    # The markdown milestone table is the primary human-readable artifact.
    print(trajectory["markdown"])
    if args.plot is not None:
        print()
        print(f"plot written to {args.plot}")
