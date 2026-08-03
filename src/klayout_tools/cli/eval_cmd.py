"""``klt eval`` command: serialise the scored-gate envelope as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/eval.md`` for the full table):
    0 - ran, valid: true (every declared gate passed)
    1 - failed to run at all (bad/malformed descriptor, unknown check name,
        an unresolved `--candidate` placeholder, or any named check's own
        library function raising its own error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, valid: false (one or more declared gates failed)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

An optimizer must never mistake exit 1 ("the tool itself failed to run")
for exit 3 ("the tool ran and reports the candidate is bad") -- that
distinction is the whole point of this verb (issue #387).
"""

from __future__ import annotations

import argparse

from ..eval import EvalError, run_eval
from .output import emit_error, emit_success

EXIT_VALID = 0
EXIT_INVALID = 3


def _parse_candidate(pairs: list[str] | None) -> dict[str, str]:
    candidate: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise EvalError(f"--candidate value '{pair}' is not in KEY=VALUE form")
        key, _, value = pair.partition("=")
        if not key:
            raise EvalError(f"--candidate value '{pair}' has an empty key")
        candidate[key] = value
    return candidate


def run(args: argparse.Namespace) -> int:
    try:
        candidate = _parse_candidate(args.candidate)
        report = run_eval(args.descriptor, candidate=candidate)
    except EvalError as exc:
        return emit_error("eval", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_VALID if report["valid"] else EXIT_INVALID


def _print_text(report: dict) -> None:
    print(f"valid: {report['valid']}")

    print("gates:")
    for gate in report["gates"]:
        exit_code = gate["exit_code"]
        exit_desc = "-" if exit_code is None else str(exit_code)
        print(
            f"  {gate['check']}: {gate['status']}  "
            f"count={gate['count']}  exit_code={exit_desc}"
        )

    objective = report["objective"]
    print(
        f"objective: {objective['name']}={objective['value']!r} "
        f"({objective['polarity']}) via {objective['check']}"
    )

    if report["candidate"]:
        print("candidate:")
        for key, value in sorted(report["candidate"].items()):
            print(f"  {key}={value}")
