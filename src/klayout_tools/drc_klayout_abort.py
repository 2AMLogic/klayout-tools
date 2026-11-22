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
    r"^(?:[^\n]*?Memory Usage \([^\n()]*\)\s*:\s*)?Executing deck[ \t]+(?::[ \t]*)?"
    r"(?P<name>\S+)(?:[ \t]+from[ \t]+(?P<path>[^\n]+?))?[ \t]*$",
    re.MULTILINE,
)
_WORKER_FAILURE_RE = re.compile(
    r"^(?:[^\n]*?Memory Usage \([^\n()]*\)[ \t]*:[ \t]*)?"
    r"Deck ([\w.-]+) failed:[ \t]*(.*)$",
    re.MULTILINE,
)
_WORKER_SUMMARY_RE = re.compile(
    r"^(?:ERROR\b[^\n]*?)?DRC failed for decks:[ \t]*([\w.-]+(?:,[ \t]*[\w.-]+)*)",
    re.MULTILINE,
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


#: KLayout's own error prefix for a deck-script failure, as emitted by
#: ``klayout -b -r`` (verified against a real 0.28.16 run for issue #1941:
#: ``ERROR: In <deck>: undefined local variable or method '...'`` followed by
#: ``ERROR: NameError: ... in Executable::execute``). Anchored at column 0:
#: the backtrace lines KLayout prints after each ``ERROR:`` line are indented,
#: and a deck's own rule names/progress output (``Executing rule
#: ERROR_CHECK.1``) never starts a line with the bare prefix -- so the anchor
#: is what keeps this from misreading ordinary deck output as a failure.
_KLAYOUT_ERROR_LINE_RE = re.compile(r"^ERROR\b.*$", re.MULTILINE)


def _klayout_deck_error_lines(*streams: str | None) -> list[str]:
    """Every ``ERROR``-prefixed line in the given ``klayout`` output streams
    (issue #1941), in order, stdout before stderr.

    ``klayout -b -r`` writes its deck-script diagnostics to one or the other
    depending on build/platform, so both are scanned. An empty list means
    KLayout itself reported no error -- not that the deck necessarily ran to
    completion (see :func:`run_drc_klayout_engine`, which pairs this with the
    exit status).
    """
    lines: list[str] = []
    for stream in streams:
        if not stream:
            continue
        lines.extend(
            match.group(0).rstrip() for match in _KLAYOUT_ERROR_LINE_RE.finditer(stream)
        )
    return lines


class KlayoutDeckAborted(DrcError):
    """A native deck aborted part-way. ``vars(exc)`` is exactly the
    ``emit_error`` keyword set (``code``, ``details``), so ``klt drc`` can
    splat it into the JSON error envelope."""

    def __init__(self, message: str, details: dict[str, Any] | None) -> None:
        super().__init__(message, code=CODE_KLAYOUT_DECK_ABORTED)
        self.details = details


def deck_starts(stream: str) -> list[tuple[str, str | None]]:
    return [(m["name"], m["path"]) for m in _EXECUTING_DECK_RE.finditer(stream)]


def _worker_abort(text: str) -> dict[str, Any]:
    failures = dict(_WORKER_FAILURE_RE.findall(text))
    for summary in _WORKER_SUMMARY_RE.findall(text):
        for name in summary.split(","):
            failures.setdefault(name.strip(), "")
    if not failures:
        return {}
    abort: dict[str, Any] = {"decks": list(failures)}
    if len(failures) == 1:
        name, message = next(iter(failures.items()))
        abort["deck"] = name
        if message:
            abort["message"] = message
    return abort


def _abort_diagnostic(message: str, combined: str) -> dict[str, str]:
    diagnostic = {"message": message}
    symbol = _RUBY_UNDEFINED_SYMBOL_RE.search(message)
    if symbol:
        diagnostic["symbol"] = symbol.group("symbol")
    # Look for the location at or after the causal line, including the Ruby backtrace.
    start = combined.find(message)
    location = _RUBY_FILE_LINE_RE.search(combined[start:] if start >= 0 else message)
    if location:
        diagnostic["location"] = location.group("loc")
    return diagnostic


def deck_abort_info(
    stdout: str | None, stderr: str | None, error_lines: list[str] | None = None
) -> dict[str, Any]:
    """Structured abort cause of a failed native-deck run.

    Returns ``{"decks_executed": [...], "abort": {...}}`` with each key
    present only when it could be derived; ``{}`` when the output has no
    recognisable deck/error lines. Scans the *full* captured output (never
    the truncated error detail) and never raises.
    """
    combined = "\n".join(stream for stream in (stdout, stderr) if stream)
    decks = deck_starts(combined)
    info: dict[str, Any] = {}
    if decks:
        info["decks_executed"] = [name for name, _ in decks]
    if error_lines is None:
        error_lines = _klayout_deck_error_lines(combined)
    abort = _worker_abort(combined)
    if (
        not abort
        and decks
        and error_lines
        and "Starting DRC in isolated parallel mode" not in combined
    ):
        name, path = decks[-1]
        abort["deck"] = name
        if path:
            abort["deck_file"] = path
    message = abort.get("message") or next(iter(error_lines), "")
    if message:
        abort.update(_abort_diagnostic(message, combined))
    if abort.get("deck") == "mslot1" and abort.get("symbol") == "metal4_drawn":
        abort["hint"] = (
            "For the GF180 3LM stack, see docs/cli/drc.md#gf180-3lm-deck-selection "
            "for the metaltop_30k and all,-mslot selectors and their coverage."
        )
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
