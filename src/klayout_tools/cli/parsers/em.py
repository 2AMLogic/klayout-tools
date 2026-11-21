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
    """Register the ``em`` verb (issue #5616, Epic #708 brief 1.A): wraps
    the ``geode`` binary for quasi-static capacitance. See docs/cli/em.md."""
    em_parser = subparsers.add_parser(
        "em",
        help="field-solver wrapper (geode-fem quasi-static capacitance)",
        description=(
            "Run a field-solver request: engine geode_fem, solve_mode "
            "quasi_static, outputs [c]. Supply a mesh or a layout region and "
            "PDK stackup. Layout meshing requires Gmsh. Both paths require geode. "
            "See docs/cli/em.md."
        ),
    )
    em_parser.add_argument("request", help="path to the klt em request JSON")
    _add_format_arg(em_parser)
    em_parser.set_defaults(func=em_cmd.run)
