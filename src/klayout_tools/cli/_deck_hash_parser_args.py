"""``--expect-deck-hash`` for ``klt drc`` and ``klt lvs`` (issue #5806),
split out of ``parser.py`` to keep that module under its file-size ceiling
(the same pattern as ``_drc_parser_args.py``, issue #4950) and shared by both
verbs so their flags cannot drift apart. The compare itself lives in
:mod:`klayout_tools.deck_hash_assertion`; this module is argparse wiring
only."""

from __future__ import annotations

import argparse

from ..deck_hash_assertion import parse_expected


def expect_deck_hash_arg_type(value: str) -> str:
    """argparse ``type=`` for ``--expect-deck-hash``: a malformed value is a
    usage error (exit 2) before any deck runs, not a guaranteed mismatch
    after one."""
    return parse_expected(value, argparse.ArgumentTypeError)


def add_expect_deck_hash_arg(parser: argparse.ArgumentParser) -> None:
    """Register ``--expect-deck-hash`` on a ``klt drc``/``klt lvs`` subparser."""
    parser.add_argument(
        "--expect-deck-hash",
        dest="expect_deck_hash",
        type=expect_deck_hash_arg_type,
        metavar="HASH",
        default=None,
        help=(
            "opt-in deck-pin assertion (issue #5806): vouch for this run's "
            "deck bytes by giving the exact provenance.deck.content_hash "
            "value to expect (form 'sha256:<64 hex>'; take it from an "
            "earlier report's provenance.deck.content_hash). Compared "
            "against the run's own recorded hash after the run: a mismatch "
            "-- or a run that recorded no deck hash at all -- fails with "
            "the error envelope (exit 1) naming deck, expected and "
            "observed, so a deck bump becomes a loud, reviewed event "
            "instead of a silent re-score of locked expectations. A "
            "matching run is unchanged and the assertion is not recorded in "
            "the report. Not combinable with --check. See docs/cli/drc.md "
            "and docs/cli/lvs.md, '--expect-deck-hash'."
        ),
    )
