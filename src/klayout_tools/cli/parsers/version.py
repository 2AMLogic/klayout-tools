"""``klt version`` argument parser (moved out of ``parser.py`` as a pure
move: ``parser.py`` is at its file-size ratchet ceiling and may not grow).
Parser registration lives here; ``run()`` stays in ``version_cmd``."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import version_cmd


def add_version_parser(
    subparsers: argparse._SubParsersAction,
    _add_format_arg: Callable[..., Any],
) -> None:
    """Register the ``version`` verb (issue #1202).

    A verb rather than only a flag, because the ``--version`` flag has no
    ``--format`` of its own: a machine consumer gating on "is this build a
    release?" needs the structured payload, not a string to re-parse.
    """
    version_parser = subparsers.add_parser(
        "version",
        help="report the running build's identity (release vs post-tag build)",
        description=(
            "Report which klayout-tools build is running: its version, the "
            "git commit it was built from, and whether that commit is this "
            "version's release tag. Only a clean build made exactly at the "
            "release tag carries the bare `X.Y.Z`; any other build's "
            "distribution version is a distinct PEP 440 dev version "
            "(`X.Y.(Z+1).devN+g<sha>`, plus `.dirty` for uncommitted "
            "changes), so it can never satisfy a `klayout-tools==X.Y.Z` "
            "pin. Identifying the build never fails: an unrecoverable "
            "identity is reported as `X.Y.Z.dev0+unknown` with "
            "`is_release: null`, not as an error."
        ),
    )
    _add_format_arg(version_parser)
    version_parser.set_defaults(func=version_cmd.run)
