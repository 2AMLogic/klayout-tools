"""``klt trajectory`` command: render an append-only JSONL optimization
trajectory log into a milestone table (+ an objective-vs-turn SVG plot).

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``. Like ``klt
report`` (and unlike every other verb), ``--format`` accepts a third value,
``github-summary``, which renders GitHub Flavored Markdown suitable for a
block repo's README or ``$GITHUB_STEP_SUMMARY``.

Why a standalone verb rather than one more ``klt report`` envelope kind
(the explicit design choice #388 left open): ``klt report`` renders *one
JSON object per input file*, detecting each file's kind from its own
top-level shape and emitting one section per file. A trajectory log is not
that — it is a JSONL *sequence* whose interesting content exists only
across records (best-so-far tracking, milestone detection), and whose
rendering is parameterised by knobs (``--threshold``/``--threshold-pct``
polarity-aware improvement thresholds, ``--plot``) that are meaningless for
every other envelope kind ``klt report`` accepts. Folding it in would give
``klt report`` flags that silently do nothing for four of its five input
kinds, and would require it to special-case "this file is many objects, not
one" ahead of its shape detector. The two verbs stay composable instead:
``klt trajectory --format json`` emits an ordinary klt envelope, and
``--format github-summary`` emits markdown that concatenates cleanly with
``klt report``'s.

Exit codes (see ``docs/cli/trajectory.md`` for the full table):
    0 - trajectory rendered successfully
    1 - failed to run (missing/unreadable log, a malformed or empty log, a
        record failing schema validation, or an unwritable --plot path) —
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. There is no third exit code: a trajectory with no milestones is
a successful render of a real result, not a failure.)
"""

import argparse

from ..trajectory import TrajectoryError, build_trajectory_report
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = build_trajectory_report(
            args.log,
            threshold=args.threshold,
            threshold_pct=args.threshold_pct,
            plot_path=args.plot,
        )
    except TrajectoryError as exc:
        error_format = args.format if args.format == "json" else "text"
        return emit_error("trajectory", str(exc), error_format)

    emit_success(report, args.format, lambda payload: _print(payload, args.format))
    return 0


def _print(report: dict, format: str) -> None:
    if format == "github-summary":
        print(report["markdown"], end="")
    else:
        print(report["text"], end="")
