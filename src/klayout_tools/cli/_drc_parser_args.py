"""``klt drc`` KLayout-application flags (split out of ``parser.py``, issue
#4950, to keep that module under its file-size ceiling)."""

import argparse


def add_klayout_app_args(drc_parser: argparse.ArgumentParser) -> None:
    """Add ``--min-klayout-version`` and ``--klayout-binary``."""
    drc_parser.add_argument(
        "--min-klayout-version",
        dest="min_klayout_version",
        metavar="VERSION",
        default=None,
        help=(
            "minimum standalone KLayout application version (e.g. 0.30.12) "
            "the native deck needs (--engine klayout only; ignored for "
            "--engine curated). The `klayout` on PATH is probed with -v "
            "before the deck runs; an older, missing or unparseable "
            "version fails up front. Omitted: no check. Not bypassed by "
            "--allow-deck-errors/--allow-missing-host-tools, and passing "
            "does not prove the deck is compatible. "
            "See docs/cli/drc.md, 'Engine' -> 'klayout'."
        ),
    )
    drc_parser.add_argument(
        "--klayout-binary",
        dest="klayout_binary",
        metavar="PATH",
        default=None,
        help=(
            "standalone KLayout application to run the native deck with "
            "(--engine klayout/both only). Precedence: this flag, then "
            "$KLT_KLAYOUT_BINARY, then `klayout` on PATH. A named but "
            "non-runnable binary is an error, never a PATH fallback. "
            "Lets a throwaway pinned env supply a newer KLayout than the "
            "distro one. See docs/cli/drc.md, 'Engine' -> 'klayout'."
        ),
    )


def add_rule_listing_args(drc_input_group: argparse._MutuallyExclusiveGroup) -> None:
    """Add the two listing input modes, ``--list-rules`` (issue #2768) and
    ``--list-rule-categories`` (issue #5444).

    Both read no layout and reach no verdict, so argparse itself rejects
    combining either with ``file``/``--check``/each other; ``file`` is kept
    out of the group and handled by ``parser._require_one_input``.
    """
    drc_input_group.add_argument(
        "--list-rules",
        dest="list_rules",
        action="store_true",
        help=(
            "list the curated --deck's registered rules (id, description, "
            "check kind, layers, scope) and their total count instead of "
            "running DRC: no layout is read and the output is not a DRC "
            "verdict. Requires --deck; not supported with --engine klayout "
            "-- a PDK-native deck has no curated rule registry, so use "
            "--list-rule-categories for it. See docs/cli/drc.md, 'Rule "
            "listing and selection'"
        ),
    )
    drc_input_group.add_argument(
        "--list-rule-categories",
        dest="list_rule_categories",
        action="store_true",
        help=(
            "--engine klayout only (issue #5444): list the rule categories "
            "the native deck declares, instead of running DRC on a layout. "
            "The deck is run under the given --deck-var set against a "
            "tool-generated EMPTY layout, so the set is independent of any "
            "layout being signed off -- unlike copying an accepted run's "
            "own coverage.rule_categories, which is circular. --format "
            "text prints exactly the names, one per line, so stdout "
            "redirects straight into an --expect-rule-categories-file "
            "manifest (notes go to stderr). Categories a deck declares only "
            "inside a geometry-dependent branch are NOT listed; see the "
            "payload's `limitations`. Fails closed (exit 1) if the deck "
            "does not complete or declares nothing. Takes only --deck/"
            "--deck-file/--pdk/--pdk-root/--deck-var/--timeout-s/"
            "--klayout-binary/--min-klayout-version/"
            "--allow-missing-host-tools. See docs/cli/drc.md, 'Rule listing "
            "and selection'"
        ),
    )
