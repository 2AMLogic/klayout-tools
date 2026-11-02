"""Record a subparser's own input flags on the parsed namespace.

Split out of :mod:`klayout_tools.cli.parser` (file-size ratchet, issue #4758).
"""

from __future__ import annotations

import argparse

#: ``dest`` values excluded from :func:`_record_request_argv_flags`' table:
#: ``help`` is not an input, ``format`` only shapes output, and
#: ``check``/``rerun`` select the separate re-verification mode (already
#: mutually exclusive with the positional input slot at the argparse level).
_REQUEST_ARGV_FLAG_EXCLUDE = ("help", "format", "check", "rerun")


def _record_request_argv_flags(
    parser: argparse.ArgumentParser,
    *,
    exclude: tuple[str, ...] = _REQUEST_ARGV_FLAG_EXCLUDE,
) -> None:
    """Record this subparser's own input flags on the parsed namespace, so
    ``cli/_request_document.reject_argv_flags`` can enforce the stated
    "a request document is mutually exclusive with this command's own input
    flags" precedence rule (issue #1867).

    Derived from the subparser's own actions rather than a hand-maintained
    list, so a flag added to ``klt drc``/``klt extract`` later is covered
    without anyone remembering to update a second table. Stored as
    ``{dest: (option_string, default)}`` -- the option string is taken from
    the action rather than reconstructed from ``dest``, since several
    repeatable flags deliberately differ (``--critical-net`` accumulates into
    ``critical_nets``, ``--matched-group`` into ``matched_groups``).

    Call once, *after* every ``add_argument`` on the subparser.
    """
    flags = {
        action.dest: (action.option_strings[-1], action.default)
        for action in parser._actions
        if action.option_strings and action.dest not in exclude
    }
    parser.set_defaults(request_argv_flags=flags)
