"""``klt eval`` command: serialise the candidate-scoring envelope as text or
JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/eval.md`` for the full table):
    0 - ran, valid: true
    1 - failed to run at all (bad descriptor/candidate, unresolvable
        candidate path, unknown check, an underlying subcommand's own
        "failed to run" error) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, valid: false (at least one gate failed)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. This mirrors ``klt drc``/``klt lvs``'s 0/1/2/3 split -- an
optimizer must never mistake exit 1 (crash) for exit 3 (a real, if bad,
score) -- see ``docs/cli/eval.md``'s "Exit codes" section.)
"""

import argparse

from ..eval import EvalError, run_eval
from .output import emit_error, emit_success

EXIT_VALID = 0
EXIT_INVALID = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_eval(args.descriptor, args.candidate)
    except EvalError as exc:
        return emit_error("eval", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_VALID if report["valid"] else EXIT_INVALID


def _print_text(report: dict) -> None:
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
