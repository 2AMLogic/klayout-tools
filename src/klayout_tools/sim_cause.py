"""Cause-line extraction for ngspice fatal-error diagnostics (issues #4623, #4924,
#5433).

Split out of sim.py to keep it under its per-file size ceiling.
"""

from __future__ import annotations

import re

#: A log line that names the cause behind a bare ``fatal error in ngspice``
#: (issue #4623): a missing model, an undefined parameter, or any ``Error`` /
#: ``warning`` line.
_FATAL_CAUSE_LINE_RE = re.compile(
    r"can't find model|undefined parameter|^\s*(?:error|warning)\b", re.IGNORECASE
)
#: Model-binding failures (e.g. an OSDI preload that registers no module the
#: ``.model`` cards use). These win a cause slot over generic ``warning`` lines.
_FATAL_CAUSE_SPECIFIC_RE = re.compile(
    r"unknown model type|unable to find definition of model", re.IGNORECASE
)
_FATAL_CAUSE_MAX_LINES = 2
_FATAL_CAUSE_MAX_CHARS = 200


def _with_fatal_error_cause(message: str, log_text: str) -> str:
    """Append the first few cause lines to a bare ``fatal error in ngspice``.

    That line carries no cause; ngspice prints it on other log lines. The
    result is bounded to ``_FATAL_CAUSE_MAX_LINES`` lines of at most
    ``_FATAL_CAUSE_MAX_CHARS`` chars each. A log with no cause line leaves the
    message unchanged.
    """
    candidates: list[str] = []
    own_line = message.strip()
    for line in log_text.splitlines():
        text = line.strip()
        if not text or text == own_line or text in candidates:
            continue
        if "fatal error in ngspice" in text.lower():
            continue
        if "simulation interrupted" in text.lower():
            continue
        if _FATAL_CAUSE_LINE_RE.search(text) or _FATAL_CAUSE_SPECIFIC_RE.search(text):
            candidates.append(text[:_FATAL_CAUSE_MAX_CHARS])
    # Stable partition: model-binding lines first, then log order.
    causes = (
        [c for c in candidates if _FATAL_CAUSE_SPECIFIC_RE.search(c)]
        + [c for c in candidates if not _FATAL_CAUSE_SPECIFIC_RE.search(c)]
    )[:_FATAL_CAUSE_MAX_LINES]
    if not causes:
        return message
    return f"{message} [cause: {' | '.join(causes)}]"


#: The ``diagnostics[].code`` for a SPICE title line on the request's own
#: netlist (issue #5433). ``klt sim`` composes its own corner deck and pulls
#: the request's ``netlist`` in with ``.include``, where SPICE has no
#: title-line semantics -- every line of the included file is circuit
#: content, so a deck written to the standalone convention (title on line 1)
#: has that title parsed as a device card. ngspice then aborts with a message
#: naming neither the file nor the convention (an empty model name, or a
#: "line 1" that is the composed deck's line 1, not the caller's).
NETLIST_TITLE_LINE_CODE = "netlist_title_line"

#: ngspice's location line for a deck-parse abort, in both spellings observed
#: in the wild: ``Error on line 1 or its substitute:`` (ngspice 47) and
#: ``Error on line 42 :`` (older builds). The offending source line is echoed
#: on the next non-blank log line.
_ERROR_ON_LINE_RE = re.compile(
    r"^[ \t]*Error on line \d+", re.IGNORECASE | re.MULTILINE
)

#: First characters that make a netlist's first line unambiguously *not* a
#: SPICE title line: a comment (``*``), a dot card (``.``) or a continuation
#: (``+``). ngspice never parses one of those as a device, so the issue #5433
#: failure cannot be what happened.
_NON_TITLE_PREFIXES = ("*", ".", "+")

#: The ``netlist_title_line`` message. Deliberately readable under *both*
#: readings of the same evidence: a prose title mis-parsed as a card (the
#: issue #5433 case) and a genuine device card on line 1 that ngspice
#: rejected on its own merits -- the echo match cannot tell those apart, and
#: claiming only the first would mislead the second.
_TITLE_LINE_MESSAGE = (
    "the netlist's first line was parsed as circuit content and ngspice "
    "rejected it -- {path}:{lineno} reads `{text}`. `klt sim` composes its "
    "own corner deck and pulls the request's netlist in with `.include`, "
    "where SPICE has no title-line semantics: every line of the included "
    "file is circuit content, so a conventional title on line 1 is parsed as "
    "a device card. If that line is a title, comment it out (prefix it with "
    "`*`); if it is meant as a device card, the error is in the card itself"
)


def _normalize_echo(text: str) -> str:
    """``text`` folded the way ngspice echoes a source line back: lower-cased,
    outer whitespace stripped, internal whitespace runs collapsed to one space
    (all three verified against ngspice 47, issue #5433)."""
    return " ".join(text.lower().split())


def _first_netlist_line(netlist_path: str) -> tuple[int, str] | None:
    """``(1-based line number, stripped text)`` of the first non-blank line of
    ``netlist_path``.

    ``None`` -- never an exception -- when the path is empty, unreadable, or
    holds no non-blank line, so a caller that cannot read the netlist where it
    classifies (an off-host corner, say) falls through to the unchanged
    classification.
    """
    if not netlist_path:
        return None
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                if line.strip():
                    return lineno, line.strip()
    except OSError:
        return None
    return None


def _netlist_title_line(log_text: str, netlist_path: str) -> tuple[int, str] | None:
    """``(line number, text)`` when ``log_text``'s deck-parse abort is ngspice
    rejecting the *first* line of ``netlist_path`` as circuit content -- the
    issue #5433 failure -- else ``None``.

    ngspice prints ``Error on line N ...`` and echoes the offending source
    line on the next non-blank line. A match requires that echo to equal the
    netlist's own first non-blank line under :func:`_normalize_echo`, and that
    line not to be a comment/dot/continuation card. Anything else -- an abort
    elsewhere in the deck, a log with no echo, an unreadable netlist -- returns
    ``None``, leaving classification exactly as it was.

    ``sim._title_line_cause`` turns a hit into the **seed** of
    ``_classify_diagnostics``'s list rather than appending it at the end, for
    two reasons: the cause then reads as ``diagnostics[0]``, and the generic
    ``ngspice_fatal`` reading of the same abort is dropped by that function's
    existing "a specific fatal code already matched" rule without needing a
    rule of its own. ``model_bin_range`` is not covered by that rule (it is
    itself a specific code), so ``sim._BIN_RANGE_SUPERSEDERS`` names it.
    """
    first = _first_netlist_line(netlist_path)
    if first is None or first[1].startswith(_NON_TITLE_PREFIXES):
        return None
    for match in _ERROR_ON_LINE_RE.finditer(log_text):
        # `[1:]` drops the remainder of the location line itself; only the
        # first non-blank line after it is ngspice's echo.
        for line in log_text[match.end() :].splitlines()[1:]:
            if not line.strip():
                continue
            if _normalize_echo(line) == _normalize_echo(first[1]):
                return first
            break
    return None


def _title_line_message(netlist_path: str, lineno: int, text: str) -> str:
    """The :data:`NETLIST_TITLE_LINE_CODE` message for ``netlist_path``'s own
    first line (issue #5433), naming the file, the line number and the fix."""
    return _TITLE_LINE_MESSAGE.format(
        path=netlist_path, lineno=lineno, text=text[:_FATAL_CAUSE_MAX_CHARS]
    )


def _with_netlist_error_cause(message: str, log_text: str, start: int) -> str:
    """Append the next log line to an ``Error in netlist line no. N`` message.

    ngspice puts the cause (``Undefined parameter [expr]``) on the line after
    the location. Any other message is returned unchanged.
    """
    if "fatal error in ngspice" in message.lower():
        return _with_fatal_error_cause(message, log_text)
    if message.lower().startswith("error on line"):
        return _with_fatal_error_cause(message, log_text)
    if not message.lower().startswith("error in netlist line no."):
        return message
    following = log_text[start:].splitlines()[1:2]
    if following and following[0].strip():
        return f"{message} {following[0].strip()}"
    return message
