"""``klt em`` argument parser (kept out of ``parser.py``: it is at its
file-size ratchet ceiling and may not grow). Parser registration lives
here; ``run()`` stays in ``em_cmd``. See docs/cli/em.md."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import em_cmd


def add_em_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register Geode solves for capacitance, steady heat, and Joule self-heating."""
    em_parser = subparsers.add_parser(
        "em",
        help="Geode capacitance, steady heat and Joule self-heating",
        description=(
            "Run a Geode field solve. Supply a mesh for capacitance, steady heat "
            "or Joule self-heating. Capacitance also accepts a layout region "
            "and PDK stackup using Gmsh. "
            "See docs/cli/em.md."
        ),
    )
    em_parser.add_argument("request", help="path to the klt em request JSON")
    _add_format_arg(em_parser)
    em_parser.set_defaults(func=em_cmd.run)
