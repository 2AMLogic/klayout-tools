"""Deferred ``klt: warning:`` emission under ``--format json`` (issue #6935).

Under ``--format json`` an application error is the error envelope on stderr
with stdout left empty (``docs/json-contract.md``), so a consumer saves
stderr as the error document and parses it whole. A human-readable
``klt: warning:`` line printed ahead of that envelope -- e.g. the
engine-version mismatch advisory a negative-control run emits before raising
its error -- breaks that parse, through no change in the consumer's code.

The CLI therefore arms this module for the duration of a ``--format json``
invocation (:func:`klayout_tools.cli.main`): warnings raised while armed are
buffered instead of printed, and the shared output boundary
(:mod:`klayout_tools.cli.output`) decides their fate -- dropped when the
invocation ends in the JSON error envelope (:func:`discard`; stderr belongs
to that one document), flushed to stderr when it produces a report
(:func:`flush`; the advisory stays visible, and no envelope is written).
Either way the deferral is disarmed, so state never leaks across
invocations in one process.

Library callers are unchanged when nothing is armed: :func:`warn` prints
immediately, exactly like the ``print(..., file=sys.stderr)`` it replaces,
so MCP/library consumers keep seeing warnings as they happen.
"""

from __future__ import annotations

import sys

_deferred: list[str] | None = None


def warn(message: str) -> None:
    """Print one warning line to stderr -- or buffer it while a
    ``--format json`` invocation has the deferral armed (module docstring).
    ``message`` is the complete line, ``klt: warning:`` prefix included;
    armed or not, its wording is untouched."""
    if _deferred is not None:
        _deferred.append(message)
    else:
        print(message, file=sys.stderr)


def arm() -> None:
    """Start buffering warnings for this invocation, dropping any state a
    previous invocation left behind (the same-process reset issue #6935's
    tests pin: every invocation starts from a clean buffer)."""
    global _deferred
    _deferred = []


def flush() -> None:
    """Print any buffered warnings to stderr and disarm -- the
    report-producing outcome: the advisory is still delivered, and stderr
    carries no envelope it could pollute. A no-op when not armed."""
    global _deferred
    if _deferred:
        for message in _deferred:
            print(message, file=sys.stderr)
    _deferred = None


def discard() -> None:
    """Drop any buffered warnings and disarm -- the JSON application-error
    outcome: stderr holds the error envelope alone, so the document a
    consumer saves and parses is exactly one JSON value (issue #6935)."""
    global _deferred
    _deferred = None
