"""Cause text for ngspice diagnostics: fatal-error cause lines (issues #4623,
#4924) and the ``measurement`` suffix for a ``.meas`` reason (issues #2925,
#5415).

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


def _cause_suffix(cause: str) -> str:
    """The ``measurement`` diagnostic suffix for an ngspice ``.meas`` reason.

    Always names the reason. An "out of interval" reason (issue #5415) also
    explains that the analysis completed and the target was simply never
    reached inside the window -- not a simulator failure -- and names the two
    opt-ins (``on_unreached: "null"`` / ``optional: true``) that grade such a
    window miss instead of erroring.
    """
    suffix = f" -- ngspice reported: {cause}"
    if _MEAS_ABSENT_CAUSE_RE.search(cause):
        suffix += _WINDOW_MISS_HINT
    return suffix
