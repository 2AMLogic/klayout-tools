"""Argparse ``type=`` callables shared across ``klt`` subparsers (split out
of ``parser.py`` to keep that module under its file-size ceiling; the move
offsets issue #5806's wiring, following the pure-move precedents
``cli/parsers/extract.py`` and ``cli/parser_sim.py``)."""

from __future__ import annotations

import argparse

from ..extract_parasitics import parse_parasitic_variants


def _parasitic_variant_arg(value: str) -> str:
    """argparse ``type=`` for ``--parasitic-variant`` (issue #3056): a value
    outside the closed grammar is a usage error (exit 2)."""
    parse_parasitic_variants([value], argparse.ArgumentTypeError)
    return value
