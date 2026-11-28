"""Cause text for ngspice diagnostics: fatal-error cause lines (issues #4623,
#4924), the continuation block under an ``Error on line N`` header (issue
#7032), the netlist title-line cause (issue #5433), the ``measurement``
suffix for a ``.meas`` reason (issues #2925, #5415) and the benign
measure-substitute exemption for a completed run (issue #6401).

Split out of sim.py to keep it under its per-file size ceiling.
"""

from __future__ import annotations

import re
from typing import Any

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


def _with_fatal_error_cause(
    message: str, log_text: str, adjacent: tuple[str, ...] = ()
) -> str:
    """Append bounded cause lines from the part of the log belonging to this abort.

    Model-binding evidence comes first, followed by the echoed card and cause
    under a location header, then other warnings and errors in log order.
    """
    candidates = []
    own_line = message.strip()
    for line in log_text.splitlines():
        text = line.strip()
        if not text or text == own_line:
            continue
        if (
            "fatal error in ngspice" in text.lower()
            or "simulation interrupted" in text.lower()
        ):
            continue
        if _FATAL_CAUSE_LINE_RE.search(text) or _FATAL_CAUSE_SPECIFIC_RE.search(text):
            candidates.append(text)
    specific = [line for line in candidates if _FATAL_CAUSE_SPECIFIC_RE.search(line)]
    ranked = list(dict.fromkeys([*specific, *adjacent, *candidates]))
    causes = [line[:_FATAL_CAUSE_MAX_CHARS] for line in ranked[:_FATAL_CAUSE_MAX_LINES]]
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
    ``classify_diagnostics``'s list rather than appending it at the end, for
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


def _error_on_line_context(log_text: str, start: int) -> list[str]:
    """The bounded continuation block under an ``Error on line N`` header
    (issue #7032).

    ngspice aborts mid-deck by printing the location line, then the offending
    card and the cause under it::

        Error on line 1204 or its substitute:
          m.x1 d g s b nfet w=1u l=0.15u
          could not find a valid modelname
        Simulation interrupted due to error!

    Stop at the abort trailer or the following error header. Keep this block
    in log order so the caller can distinguish the first two continuation
    lines from any later model-binding evidence before the block ends.
    """
    context: list[str] = []
    for line in log_text[start:].splitlines()[1:]:
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if "fatal error in ngspice" in lowered or "simulation interrupted" in lowered:
            break
        if _ERROR_ON_LINE_RE.match(line) or _NETLIST_LINE_ERROR_RE.match(line):
            break
        context.append(text)
    return context


def _with_netlist_error_cause(message: str, log_text: str, start: int) -> str:
    """Append the next log line to an ``Error in netlist line no. N`` message.

    ngspice puts the cause (``Undefined parameter [expr]``) on the line after
    the location. An ``Error on line N`` message instead gains its bounded
    continuation block plus any earlier cause lines
    (:func:`_error_on_line_context`, :func:`_with_fatal_error_cause`). Any
    other message is returned unchanged.
    """
    if "fatal error in ngspice" in message.lower():
        return _with_fatal_error_cause(message, log_text)
    if message.lower().startswith("error on line"):
        block = _error_on_line_context(log_text, start)
        return _with_fatal_error_cause(
            message,
            "\n".join([log_text[:start], *block]),
            tuple(block[:_FATAL_CAUSE_MAX_LINES]),
        )
    if not message.lower().startswith("error in netlist line no."):
        return message
    following = log_text[start:].splitlines()[1:2]
    if following and following[0].strip():
        return f"{message} {following[0].strip()}"
    return message


# --------------------------------------------------------------------------- #
# Benign measure-substitute errors in a completed run (issue #6401)
# --------------------------------------------------------------------------- #


#: What ngspice prints once an analysis actually begins (or has produced
#: data); absent from a run that died in the deck parser. Lived in sim.py
#: until issue #6401's exemption helper needed it here too.
_ANALYSIS_STARTED_RE = re.compile(
    r"Doing analysis at TEMP|No\. of Data Rows|Initial Transient Solution",
    re.IGNORECASE,
)

#: The one ``ngspice_fatal`` alternative ngspice-47 also prints mid-analysis
#: for a benign cause: a ``.meas ... param='...'`` whose referenced measure
#: produced no value, where the block reads ``Error in netlist line no. N``
#: followed by ``Cannot compute substitute`` (issue #6401). Its genuine
#: reading -- the deck/library parse abort of #4603 -- never coexists with a
#: run that completed.
_NETLIST_LINE_ERROR_RE = re.compile(
    r"^[ \t]*Error in netlist line no\. \d+", re.IGNORECASE | re.MULTILINE
)

#: The ``ngspice_fatal`` alternatives that always mean an aborted run:
#: ``Error on line N`` (deck abort, #3879), ``Simulation interrupted due to
#: error`` and ``fatal error in ngspice``. Any one of these anywhere in the
#: log keeps the fatal reading even when the netlist-line form is present too.
_FATAL_ABORT_ALTERNATIVES_RE = re.compile(
    r"^[ \t]*Error on line \d+"
    r"|Simulation interrupted due to error"
    r"|fatal error in ngspice",
    re.IGNORECASE | re.MULTILINE,
)

#: ngspice's batch completion banner -- ``ngspice-47 done`` -- printed on
#: exit after a run ran to completion, and absent from one that died in the
#: deck parser or was interrupted mid-analysis.
_RUN_FINISHED_RE = re.compile(r"ngspice-\d+\s+done", re.IGNORECASE)


def _benign_netlist_line_fatal(log_text: str) -> bool:
    """Whether ``log_text``'s ``ngspice_fatal`` evidence is the benign
    measure-substitute block of a *completed* run (issue #6401) and so must
    not be graded fatal.

    True only when all of these hold: the ``Error in netlist line no. N``
    alternative is the log's only fatal evidence (no ``Error on line N``, no
    ``Simulation interrupted due to error``, no ``fatal error in ngspice``);
    the analysis started (:data:`_ANALYSIS_STARTED_RE`); and the run printed
    its completion banner (:data:`_RUN_FINISHED_RE`). Anything less keeps
    today's fatal reading, so a genuine abort -- with or without a
    benign-looking line -- still classifies ``ngspice_fatal``. The failed
    measures keep surfacing through the per-measurement no-value path, which
    this exemption never touches.
    """
    if not _NETLIST_LINE_ERROR_RE.search(log_text):
        return False
    if _FATAL_ABORT_ALTERNATIVES_RE.search(log_text):
        return False
    return bool(
        _ANALYSIS_STARTED_RE.search(log_text) and _RUN_FINISHED_RE.search(log_text)
    )


#: Codes that mean ngspice aborted while reading the deck (issue #3879). When
#: one of them is classified and the log shows no analysis banner, the run
#: never started: its per-measurement "produced no value" errors are
#: consequences and are collapsed (`sim._demote_abort_cascade`). Lived in
#: sim.py until issue #6401's :func:`_netlist_fatal_message` needed it here;
#: aliased there and imported from there by ``sim_batch``.
_NGSPICE_FATAL_CODES: frozenset[str] = frozenset(
    {"missing_include", "netlist", "model_bin_range", "ngspice_fatal"}
) | {NETLIST_TITLE_LINE_CODE}


def _netlist_fatal_message(
    message: str, log_text: str, start: int, classified: list[dict[str, Any]]
) -> str | None:
    """The ``ngspice_fatal`` message for ``log_text``, or ``None`` to drop the
    diagnostic entirely.

    ``None`` in two cases: a specific fatal code was already classified (the
    generic reading is only the fallback cause), or the whole evidence is the
    benign measure-substitute block of a completed run (issue #6401,
    :func:`_benign_netlist_line_fatal`). Otherwise the message gains its
    netline cause via :func:`_with_netlist_error_cause` as before.
    """
    if any(d["code"] in _NGSPICE_FATAL_CODES for d in classified):
        return None
    if _benign_netlist_line_fatal(log_text):
        return None
    return _with_netlist_error_cause(message, log_text, start)


# --------------------------------------------------------------------------- #
# Measurement-cause suffixing (issues #2925, #5415)
# --------------------------------------------------------------------------- #


#: ngspice's reason text when a ``.meas`` search (``WHEN``/``TRIG``/``TARG``/
#: ``FIND ... WHEN``) never found its crossing in the analysis interval, e.g.
#: ``Error: measure  x  when(WHEN) : out of interval`` (verified on ngspice-46).
#: The one engine reason that means "this figure does not exist at this point";
#: any other ``Error: measure`` reason is a malformed card and stays an error.
_MEAS_ABSENT_CAUSE_RE = re.compile(r"out of interval", re.IGNORECASE)

#: Appended to the default-path ``measurement`` diagnostic when ngspice's
#: reason is "out of interval" (issue #5415): the run completed and the target
#: was never reached in the analysis window -- not a simulator abort.
_WINDOW_MISS_HINT = (
    " -- the analysis completed but the target was never reached inside the "
    "analysis window (not a simulator failure): lengthen the window, or set "
    '`on_unreached: "null"` / `optional: true` on this measurement to grade '
    "it instead of erroring"
)


#: Where :func:`_cause_suffix` puts the engine's reason; the one marker
#: :func:`reports_window_miss` reads it back by.
_REPORTED_CAUSE_PREFIX = " -- ngspice reported: "


def _cause_suffix(cause: str) -> str:
    """The ``measurement`` diagnostic suffix for an ngspice ``.meas`` reason.

    Always names the reason. An "out of interval" reason (issue #5415) also
    explains that the analysis completed and the target was simply never
    reached inside the window -- not a simulator failure -- and names the two
    opt-ins (``on_unreached: "null"`` / ``optional: true``) that grade such a
    window miss instead of erroring.
    """
    suffix = f"{_REPORTED_CAUSE_PREFIX}{cause}"
    if _MEAS_ABSENT_CAUSE_RE.search(cause):
        suffix += _WINDOW_MISS_HINT
    return suffix


def reports_window_miss(message: str) -> bool:
    """True when a ``measurement`` diagnostic's ``message`` says ngspice
    evaluated the ``.meas`` card and reported "out of interval" (issue #7904).

    That is the engine itself stating the analysis ran to completion and the
    target was never reached -- the reader of a report (``klt pex``'s
    ``delta[].error``) can then tell it from a solver failure. Keyed on the
    engine's reason text that :func:`_cause_suffix` places after
    :data:`_REPORTED_CAUSE_PREFIX`, not on the hint wording that follows it, so
    a report assembled by a runner whose hint text differs still matches.
    """
    _, found, cause = message.partition(_REPORTED_CAUSE_PREFIX)
    return bool(found) and _MEAS_ABSENT_CAUSE_RE.search(cause) is not None
