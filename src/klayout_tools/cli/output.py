"""Shared JSON/text output helper for ``klt`` subcommands.

Every ``*_cmd.py`` module emits through :func:`emit_success` /
:func:`emit_error` instead of hand-rolling ``json.dump``/``print`` — this is
the one place that knows the documented envelope shape (see
``docs/json-contract.md``).

Design notes (additive envelope, not a wrapping one):

- Success payloads stay **flat** at the top level (no ``{"result": {...}}``
  nesting) so existing, already-documented command shapes (e.g. ``klt
  layers``) are unaffected. Commands add their own ``schema_version`` field to
  the payload dict *before* calling :func:`emit_success` — this module does
  not inject it, since the version is owned by the library function that
  builds the payload (see ``layers.py``'s docstring on MCP reuse).
- ``--format json`` output on success goes to **stdout only**; on error, the
  JSON error object goes to **stderr**, and stdout is left empty. This means
  a caller never has to inspect stdout content to distinguish success from
  failure under ``--format json`` — check the exit code.
- ``--format text`` is a courtesy rendering, not the contract: success calls
  a command-supplied ``text_renderer`` callback, and errors print a plain
  ``klt <command>: <message>`` line to stderr, matching pre-existing
  behaviour.
- Exit code ``1`` is returned by :func:`emit_error` for application-level
  errors. Argparse-level usage errors (exit code ``2``) are raised by
  argparse itself before a command's ``run()`` executes, so they are out of
  scope for this helper by construction.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

#: Application-level error exit code (as opposed to argparse's usage-error 2).
ERROR_EXIT_CODE = 1


def emit_success(
    payload: dict,
    format: str,
    text_renderer: Callable[[dict], None],
) -> None:
    """Emit a successful command result in the requested ``format``.

    ``format == "json"`` writes ``payload`` as indented JSON to stdout (plus a
    trailing newline). ``format == "text"`` delegates to ``text_renderer``,
    which is responsible for printing whatever human-readable rendering the
    command defines; ``text_renderer`` is not part of the JSON contract.
    """
    if format == "json":
        json.dump(payload, sys.stdout, indent=2)
        print()
    else:
        text_renderer(payload)


def render_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    left_aligned: set[int],
) -> None:
    """Print an aligned, ``--`` text-table rendering of ``rows`` under ``headers``.

    Column widths are computed from the header and every row's cell in that
    column (so the table is always at least as wide as its header). Columns
    whose index is in ``left_aligned`` are left-justified (e.g. names, free
    text); every other column is right-justified (numeric-ish data). Prints
    nothing if ``rows`` is empty -- callers decide whether an empty table is
    worth a header-only print.

    This is a courtesy rendering for ``--format text``, not part of the JSON
    contract -- see this module's docstring.
    """
    if not rows:
        return

    widths = [
        max(len(headers[col]), max(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(
            row[col].ljust(widths[col])
            if col in left_aligned
            else row[col].rjust(widths[col])
            for col in range(len(headers))
        )

    print()
    print(fmt(headers))
    print("  ".join("-" * widths[col] for col in range(len(headers))))
    for row in rows:
        print(fmt(row))


def emit_error(command: str, message: str, format: str) -> int:
    """Emit an application-level error and return the exit code to use.

    ``format == "json"`` writes the documented error envelope to stderr:
    ``{"schema_version": 1, "error": {"command": ..., "message": ...}}``.
    ``format == "text"`` writes the pre-existing plain-text stderr line,
    ``klt <command>: <message>``.

    Always returns :data:`ERROR_EXIT_CODE` (``1``) so a command's ``run()``
    can simply ``return emit_error(...)``.
    """
    if format == "json":
        error_payload = {
            "schema_version": 1,
            "error": {"command": command, "message": message},
        }
        json.dump(error_payload, sys.stderr, indent=2)
        print(file=sys.stderr)
    else:
        print(f"klt {command}: {message}", file=sys.stderr)
    return ERROR_EXIT_CODE
