"""``klt deck`` commands: identify a built-in rule deck, and resolve one back
to the release that shipped it.

- ``resolve`` (issue #623) -- turn a pinned ``provenance.deck.content_hash``
  (or a ``(name, version)`` pair) into the klt release that shipped it, via
  the generated release-history table.
- ``hash`` (issue #1202) -- report the ``content_hash`` *this* build resolves
  for a deck, with no layout file and no check run. Answers "which deck
  revision will this build use?" before doing any work; ``resolve`` answers
  the complementary "which release shipped that revision?".

Both emit through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes:
    0 - the query succeeded
    1 - failed to run (invalid query, a query that matched no known release,
        or an unknown deck name) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

from __future__ import annotations

import argparse

from .._provenance import UnknownProvenanceDeckError, deck_identity
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


def run_hash(args: argparse.Namespace) -> int:
    try:
        block = deck_identity(args.deck)
    except UnknownProvenanceDeckError as exc:
        return emit_error("deck hash", str(exc), args.format)

    if block["content_hash"] is None:
        return emit_error(
            "deck hash",
            f"deck '{args.deck}' is registered but its source file could not "
            "be read, so no content hash can be computed",
            args.format,
        )

    # Flat payload, mirroring `deck resolve`'s shape: `deck`/`content_hash`
    # are named exactly as the provenance block names them, so a consumer
    # comparing against a committed report's `provenance.deck` is comparing
    # like with like.
    payload = {
        "schema_version": 1,
        "deck": block["name"],
        "content_hash": block["content_hash"],
        "released": block["released"],
    }
    emit_success(payload, args.format, _print_hash_text)
    return 0


def _print_hash_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"released: {_released_text(report['released'])}")


def _released_text(released: bool | None) -> str:
    """Render the tri-state ``released`` signal for humans -- ``unknown`` is
    deliberately distinct from ``no`` (see ``docs/json-contract.md``)."""
    if released is None:
        return "unknown"
    return "yes" if released else "no"


def _print_resolve_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"git_tag: {report['git_tag']}")
    print(f"git_commit: {report['git_commit']}")
    print(f"package_version: {report['package_version']}")
