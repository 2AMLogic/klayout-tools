"""``klt eval`` command: serialise the candidate-scoring envelope as text or
JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/eval.md`` for the full table):
    0 - ran, valid: true
    1 - failed to run at all (bad descriptor/candidate, unresolvable
        candidate path, unknown check, an underlying subcommand's own
        "failed to run" error, or a ``--trajectory-log`` write failure) --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, valid: false (at least one gate failed)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This mirrors ``klt drc``'s 0/1/2/3 split -- an
optimizer must never mistake exit 1 (crash) for exit 3 (a real, if bad,
score) -- see ``docs/cli/eval.md``'s "Exit codes" section.)

``--trajectory-log`` (issue #437, Epic #391 Phase 5): when given (together
with ``--turn``/``--candidate-ref``), the computed envelope is also appended
to an optimization trajectory log (#388) as one record -- the "wire this
result into #388's trajectory log" half of #437's own scope, exposed at the
CLI boundary so a shell-driven optimizer loop can score *and* log a turn in
one ``klt eval`` invocation, exactly as it already scores in one invocation.
This is a side effect on top of the documented JSON contract (the response
envelope's own shape is unchanged), matching ``klt trajectory --plot``'s
existing "write a file as a courtesy, independent of --format" precedent.
"""

import argparse

from ..eval import EvalError, run_eval
from ..trajectory import TrajectoryError, append_record, record_from_eval
from .output import emit_error, emit_success

EXIT_VALID = 0
EXIT_INVALID = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_eval(args.descriptor, args.candidate)
    except EvalError as exc:
        return emit_error("eval", str(exc), args.format)

    logged = False
    if args.trajectory_log is not None:
        if args.turn is None or args.candidate_ref is None:
            return emit_error(
                "eval",
                "--trajectory-log requires both --turn and --candidate-ref",
                args.format,
            )
        try:
            record = record_from_eval(
                report,
                turn=args.turn,
                candidate_ref=args.candidate_ref,
                description=args.description,
                wall_clock_s=args.wall_clock_s,
            )
            append_record(args.trajectory_log, record)
        except TrajectoryError as exc:
            return emit_error("eval", str(exc), args.format)
        logged = True

    emit_success(
        report, args.format, lambda payload: _print_text(payload, args, logged)
    )

    return EXIT_VALID if report["valid"] else EXIT_INVALID


def _print_text(report: dict, args: argparse.Namespace, logged: bool) -> None:
    print(f"valid: {report['valid']}")

    print()
    print("gates:")
    for gate in report["gates"]:
        count = f"  count={gate['count']}" if "count" in gate else ""
        print(
            f"  {gate['name']} [{gate['check']}]  status={gate['status']}  "
            f"exit_code={gate['exit_code']}{count}"
        )

    objective = report["objective"]
    print()
    print(
        f"objective: {objective['name']} = {objective['value']!r} "
        f"({objective['polarity']})"
    )

    metrics = report["metrics"]
    if metrics:
        print()
        print("metrics:")
        for name in sorted(metrics):
            print(f"  {name}: {metrics[name]!r}")

    if logged:
        print()
        print(
            f"trajectory: appended turn {args.turn} "
            f"({args.candidate_ref!r}) to {args.trajectory_log}"
        )
