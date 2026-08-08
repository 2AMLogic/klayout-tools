"""``klt deck`` command: resolve a built-in rule deck by content hash or by
``(name, version)`` against klt's own release history (issue #623).

One subcommand today (``resolve``), emitting through the shared envelope
helpers in :mod:`.output` -- see ``docs/json-contract.md``.

Exit codes:
    0 - the query matched exactly one known release
    1 - failed to run (invalid query, or a query that matched no known
        release) -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

from __future__ import annotations

import argparse

from ..decks.history import DeckHistoryError, resolve_deck
from .output import emit_error, emit_success


def run_resolve(args: argparse.Namespace) -> int:
    try:
        report = resolve_deck(
            content_hash=args.content_hash, deck=args.deck, version=args.version
        )
    except DeckHistoryError as exc:
        return emit_error("deck resolve", str(exc), args.format)

    emit_success(report, args.format, _print_resolve_text)
    return 0


def _print_resolve_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"git_tag: {report['git_tag']}")
    print(f"git_commit: {report['git_commit']}")
    print(f"package_version: {report['package_version']}")
