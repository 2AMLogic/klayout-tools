"""Structured abort cause for a failed ``--engine klayout`` native-deck run
(issue #5091; split from ``drc_klayout_engine.py`` for the file-size ratchet).

Surfaced as ``error.abort`` / ``error.decks_executed`` on the
``klayout_deck_aborted`` error envelope and under ``engine_deck_errors`` when
``--allow-deck-errors`` tolerates the run.
"""

from __future__ import annotations

import re
from typing import Any

from .drc import DrcError

#: A native-deck run aborted part-way; see :class:`KlayoutDeckAborted`.
CODE_KLAYOUT_DECK_ABORTED = "klayout_deck_aborted"

_EXECUTING_DECK_RE = re.compile(
    r"^Executing deck\s+(?P<name>\S+)(?:\s+from\s+(?P<path>\S+))?\s*$", re.MULTILINE
)
_RUBY_UNDEFINED_SYMBOL_RE = re.compile(
    r"undefined (?:local variable or )?method [`'‘\"]?(?P<symbol>[^`'’\"\s]+)"
)
_RUBY_FILE_LINE_RE = re.compile(r"(?P<loc>[^\s:'\"`]+\.[A-Za-z]+:\d+)")


#: Cap on how many lines of klayout's own output a deck-abort error message
#: carries (issue #2333). The whole captured stdout/stderr is surfaced below
#: this size -- the point of the change is that a *non*-``ERROR``-prefixed
#: line (``sh: pmap: command not found``) is usually the causal one -- but a
#: deck that prints a per-rule progress line for thousands of rules should
#: not turn a single error into a multi-megabyte message. When the cap trips,
#: the **tail** is what survives (the abort is at the end of the run) and any
#: ``ERROR`` line from the omitted head is repeated so the detection signal is
#: never lost to truncation.
_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES = 500


def _klayout_deck_error_detail(
    stdout: str | None, stderr: str | None, error_lines: list[str]
) -> str:
    """The klayout output to quote in a deck-abort :class:`DrcError` (issue
    #2333), stdout before stderr.

    Deliberately **not** ``error_lines`` alone. ``_klayout_deck_error_lines``
    keeps only lines starting with ``ERROR`` -- correct for *detecting* a
    failed deck (a rule named ``ERROR_CHECK.1`` must not be misread as one),
    but wrong for *explaining* it: the line that says why is routinely not
    ``ERROR``-prefixed. The motivating case is a PDK driver that shells out
    to a host utility the machine does not have, where klayout's captured
    output is::

        sh: pmap: command not found
        ERROR: In main.drc: undefined method 'strip' for nil
        ERROR: NoMethodError: undefined method 'strip' for nil in ...

    Quoting only the ``ERROR`` lines drops the one line that identifies the
    real cause and leaves a nil-dereference that reads like a broken rule
    deck. So the full captured output is surfaced (subject to
    :data:`_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES`), and ``error_lines`` is
    used only as the fallback for the impossible-in-practice case of an empty
    capture.
    """
    chunks = [
        stream.strip() for stream in (stdout, stderr) if stream and stream.strip()
    ]
    combined = "\n".join(chunks)
    if not combined:
        return "\n".join(error_lines)

    lines = combined.splitlines()
    if len(lines) <= _KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES:
        return combined

    kept = lines[-_KLAYOUT_DECK_ERROR_DETAIL_MAX_LINES:]
    kept_set = {line.rstrip() for line in kept}
    dropped_errors = [line for line in error_lines if line.rstrip() not in kept_set]
    head = [
        f"... ({len(lines) - len(kept)} earlier lines omitted; "
        "klt kept the tail, where the abort is)"
    ]
    if dropped_errors:
        head.extend(dropped_errors)
        head.append("... (end of ERROR lines recovered from the omitted section)")
    return "\n".join(head + kept)


class KlayoutDeckAborted(DrcError):
    """A native deck aborted part-way. ``vars(exc)`` is exactly the
    ``emit_error`` keyword set (``code``, ``details``), so ``klt drc`` can
    splat it into the JSON error envelope."""

    def __init__(self, message: str, details: dict[str, Any] | None) -> None:
        super().__init__(message, code=CODE_KLAYOUT_DECK_ABORTED)
        self.details = details


def deck_abort_info(
    stdout: str | None, stderr: str | None, error_lines: list[str]
) -> dict[str, Any]:
    """Structured abort cause of a failed native-deck run.

    Returns ``{"decks_executed": [...], "abort": {...}}`` with each key
    present only when it could be derived; ``{}`` when the output has no
    recognisable deck/error lines. Scans the *full* captured output (never
    the truncated error detail) and never raises.
    """
    combined = "\n".join(stream for stream in (stdout, stderr) if stream)
    decks = [
        (m.group("name"), m.group("path"))
        for m in _EXECUTING_DECK_RE.finditer(combined)
    ]
    info: dict[str, Any] = {}
    if decks:
        info["decks_executed"] = [name for name, _ in decks]
    if not error_lines and not decks:
        return info
    abort: dict[str, Any] = {}
    if decks and error_lines:
        name, path = decks[-1]
        abort["deck"] = name
        if path:
            abort["deck_file"] = path
    if error_lines:
        abort["message"] = error_lines[0]
        symbol = _RUBY_UNDEFINED_SYMBOL_RE.search(error_lines[0])
        if symbol:
            abort["symbol"] = symbol.group("symbol")
        # The Ruby backtrace line (``file:line:in ...``) usually follows the
        # first ERROR line; fall back to the ERROR line itself.
        start = combined.find(error_lines[0])
        tail = combined[start:] if start >= 0 else error_lines[0]
        location = _RUBY_FILE_LINE_RE.search(tail)
        if location:
            abort["location"] = location.group("loc")
    if abort:
        info["abort"] = abort
    return info


def deck_aborted_error(completed: Any, error_lines: list[str], detail: str) -> DrcError:
    """The ``klayout_deck_aborted`` error for a failed ``completed`` run."""
    return KlayoutDeckAborted(
        "klayout reported an error while running the deck script "
        f"(exit status {completed.returncode}) -- the deck likely "
        "aborted part-way through, so the report file it did write "
        "covers only the rules that ran before the abort, and its "
        "verdict cannot be trusted. Pass --allow-deck-errors to "
        "accept a partial report anyway. klayout's own output:\n" + detail,
        deck_abort_info(completed.stdout, completed.stderr, error_lines) or None,
    )
