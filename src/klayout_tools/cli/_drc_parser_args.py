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
