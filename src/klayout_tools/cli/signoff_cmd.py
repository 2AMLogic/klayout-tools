"""``klt signoff`` command: aggregate klt drc/lvs/extract/sim JSON envelopes
into one pass/fail verdict.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/signoff.md`` for the full table):
    0 - every check passed and every input's provenance agreed
    1 - failed to run (missing/unreadable/malformed envelope file, or an
        envelope with an unrecognized shape) -- returned by ``emit_error``
        as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, provenance was consistent, but at least one check
        failed
    4 - refused: two or more inputs' provenance blocks disagree (see
        docs/cli/signoff.md's "Provenance consistency" section) -- no
        pass/fail verdict is produced
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)
"""

import argparse

from ..signoff import SignoffError, build_signoff
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_FAIL = 3
EXIT_REFUSED = 4

_EXIT_CODES = {"pass": EXIT_PASS, "fail": EXIT_FAIL, "refused": EXIT_REFUSED}


def run(args: argparse.Namespace) -> int:
    try:
        result = build_signoff(args.files)
    except SignoffError as exc:
        return emit_error("signoff", str(exc), args.format)

    emit_success(result, args.format, _print_text)

    return _EXIT_CODES[result["status"]]


def _print_text(result: dict) -> None:
    print(f"status: {result['status']}")
    print(f"checks: {result['passed_count']}/{result['check_count']} passed")

    consistency = result["provenance_consistency"]
    if not consistency["ok"]:
        print()
        print("provenance mismatches (refusing to aggregate):")
        for mismatch in consistency["mismatches"]:
            print(f"  {mismatch['field']}:")
            for entry in mismatch["values"]:
                print(f"    {entry['source']}: {entry['value']}")

    print()
    for check in result["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(
            f"[{marker}] {check['kind']:<8} {check['source']}  status={check['status']}"
        )
