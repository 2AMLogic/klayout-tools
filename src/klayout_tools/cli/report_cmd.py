"""``klt report`` command: render one or more ``klt`` JSON envelopes into a
combined text/markdown report.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``. Unlike
every other verb, ``--format`` accepts a third value, ``github-summary``,
which renders GitHub Flavored Markdown suitable for piping into
``$GITHUB_STEP_SUMMARY`` — see ``docs/cli/report.md``. ``--format json``
still emits this command's own JSON envelope: rendering is this verb's job,
but it doesn't get to skip the JSON contract for its own machine-readable
output just because that job is "produce human-readable text."

Exit codes (see ``docs/cli/report.md`` for the full table):
    0 - report rendered successfully
    1 - failed to run (missing/unreadable/malformed envelope file, or an
        envelope with an unrecognized shape) — returned by ``emit_error``
        as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. Unlike ``klt drc``/``klt lvs``, this command has no third exit
code for "ran successfully but found violations" — the rendered envelopes'
own pass/fail status is content this command reports, not a verdict it
renders its own exit code around.)
"""

import argparse

from ..report import ReportError, build_report
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = build_report(args.files)
    except ReportError as exc:
        error_format = args.format if args.format == "json" else "text"
        return emit_error("report", str(exc), error_format)

    emit_success(report, args.format, lambda payload: _print(payload, args.format))
    return 0


def _print(report: dict, format: str) -> None:
    if format == "github-summary":
        print(report["markdown"], end="")
    else:
        print(report["text"], end="")
