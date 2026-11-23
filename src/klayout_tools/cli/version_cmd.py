"""``klt version``: the running build's identity (issue #1202).

``klt --version`` prints the same one-line string this command's ``--format
text`` prints; ``--format json`` adds the machine-readable payload a consumer
gating on "am I on a release?" needs (``{version, package_version, git_commit,
git_tag, dirty, is_release}``). The payload itself is built by
:func:`klayout_tools.build_identity.version_report` so it travels with the
library, not the CLI layer (see ``docs/json-contract.md``, "Adding a new
command"). The CLI layer adds fields of its own on top of that payload:

* ``commands`` (issue #7324), the top-level verbs this build accepts -- built
  from the live parser, which only the CLI layer may import (see
  :func:`_list_commands`);
* ``required_commands`` (issue #7331), present only with ``--require``: the
  verbs the caller asked for and the ones this build lacks (see
  :func:`_require_gate`).

Identifying the running build cannot fail -- an unrecoverable identity is
reported as ``X.Y.Z.dev0+unknown`` with ``is_release: null``, never as an
error -- so without ``--require`` the exit is always 0. ``--require`` is a
gate on top of that report: it still emits the normal payload, then exits
:data:`EXIT_MISSING_COMMANDS` when a required verb is missing.
"""

from __future__ import annotations

import argparse
import sys

from ..build_identity import version_report
from .output import EXIT_USAGE_ERROR, emit_error, emit_success

#: `klt version --require` exit code when one or more required top-level verbs
#: are missing -- the build was identified and its report emitted, the answer
#: is just "no" (the convention `klt pdk cells --supply` and `klt drc` finding
#: violations use). Distinct from 1 (application error) and 2 (usage error,
#: including a `--require` that names no command). See docs/cli/version.md.
EXIT_MISSING_COMMANDS = 3

_REQUIRE_NAMES_NOTHING = "--require names no command"


def run(args: argparse.Namespace) -> int:
    report = {**version_report(), "commands": _list_commands()}
    if args.require is None:
        emit_success(report, args.format, _print_text)
        return 0
    return _require_gate(report, args)


def _require_gate(report: dict, args: argparse.Namespace) -> int:
    """``--require`` (issue #7331): emit the report with ``required_commands``
    added and return 0, or :data:`EXIT_MISSING_COMMANDS` once a stderr line
    has named each missing verb.

    An unknown name is the signal, not a usage error -- "this install lacks
    that verb" is exactly what the flag exists to report -- so the report is
    emitted first and the exit code follows. Only a ``--require`` that names
    nothing at all is rejected (exit 2): a gate that checks nothing must not
    pass silently.
    """
    required = _required_names(args.require)
    if not required:
        return emit_error(
            "version", _REQUIRE_NAMES_NOTHING, args.format, exit_code=EXIT_USAGE_ERROR
        )
    missing = _missing_commands(required, report["commands"])
    report["required_commands"] = {"required": required, "missing": missing}
    emit_success(report, args.format, _print_text)
    if missing:
        _warn_missing(missing, report["reinstall_spec"])
        return EXIT_MISSING_COMMANDS
    return 0


def _required_names(values: list[str]) -> list[str]:
    """``--require`` values as unique verb names in input order: each value is
    one name or a comma-separated list, whitespace around a name is ignored,
    and a duplicate keeps its first position."""
    names: dict[str, None] = {}
    for value in values:
        for token in value.split(","):
            if token.strip():
                names.setdefault(token.strip(), None)
    return list(names)


def _missing_commands(required: list[str], commands: list[dict[str, str]]) -> list[str]:
    """The required names this build does not provide, in ``required`` order.

    A name is looked up in ``commands``, so only a listed top-level verb
    satisfies it: a nested subcommand (``pdk find``) or a verb hidden from
    ``klt --help`` counts as missing.
    """
    provided = {command["name"] for command in commands}
    return [name for name in required if name not in provided]


def _warn_missing(missing: list[str], reinstall_spec: str | None) -> None:
    """One stderr line naming the missing commands, followed by the line that
    re-obtains the running build when it is reinstallable (a build with
    ``reinstall_spec: null`` gets no second line)."""
    print(f"klt: missing required command(s): {', '.join(missing)}", file=sys.stderr)
    if reinstall_spec is not None:
        print(
            "klt: this build can be reinstalled with: "
            f'uvx --from "{reinstall_spec}" klt ...',
            file=sys.stderr,
        )


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
