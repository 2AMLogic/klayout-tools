"""``klt version``: the running build's identity (issue #1202).

``klt --version`` prints the same one-line string this command's ``--format
text`` prints; ``--format json`` adds the machine-readable payload a consumer
gating on "am I on a release?" needs (``{version, package_version, git_commit,
git_tag, dirty, is_release}``). The payload itself is built by
:func:`klayout_tools.build_identity.version_report` so it travels with the
library, not the CLI layer (see ``docs/json-contract.md``, "Adding a new
command"). The CLI layer adds one field of its own on top of that payload:
``commands`` (issue #7324), the top-level verbs this build accepts -- built
from the live parser, which only the CLI layer may import (see
:func:`_list_commands`).

Always exits 0: identifying the running build cannot fail -- an unrecoverable
identity is reported as ``X.Y.Z.dev0+unknown`` with ``is_release: null``,
never as an error.
"""

from __future__ import annotations

import argparse
import sys

from ..build_identity import version_report
from .output import emit_success


def run(args: argparse.Namespace) -> int:
    report = {**version_report(), "commands": _list_commands()}
    emit_success(report, args.format, _print_text)
    return 0


def _list_commands() -> list[dict[str, str]]:
    """The top-level verbs this build accepts, for ``version``'s JSON (issue
    #7324).

    A downstream toolchain pin check that records which subcommands its
    runners invoke needs "does this install provide them?" answered from the
    JSON contract, not by scraping ``klt --help``: that rendering is coupled
    to argparse's formatting, and a formatting change yields an *empty or
    short* command set -- which reads as a pin violation rather than as a
    broken probe.

    The source of truth is the live parser's ``<command>`` subparsers action
    (the same ``_choices_actions`` walk ``mcp_tools`` uses), so the list
    cannot drift from what ``klt <cmd>`` actually accepts. One
    ``{"name", "summary"}`` object per verb, sorted by ``name`` -- objects
    rather than bare strings so a later ``deprecated`` flag is additive, not
    breaking. A verb hidden from ``klt --help`` (``help=argparse.SUPPRESS``)
    stays hidden here too, and nested subcommands (``klt pdk find`` ...) are
    deliberately not listed.

    ``create_parser`` is imported lazily: ``parser.py`` (via
    ``parsers/version.py``) imports this module at load time, so a module-level
    import would be circular. ``build_identity.version_report`` stays free of
    it entirely -- it is library-layer, reusable by a future MCP server, and
    must not depend on the CLI parser.
    """
    from .parser import create_parser

    subparsers = next(
        action
        for action in create_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    commands = [
        {"name": action.dest, "summary": action.help or ""}
        for action in subparsers._choices_actions
        if action.help != argparse.SUPPRESS
    ]
    commands.sort(key=lambda command: command["name"])
    return commands


def _print_text(report: dict) -> None:
    print(f"klt {report['version']}")
    if report["reinstall_spec"] is None:
        # stderr keeps stdout the one-line string `klt --version` shares.
        print(
            "not reinstallable: no clean, committed build to reinstall from",
            file=sys.stderr,
        )
