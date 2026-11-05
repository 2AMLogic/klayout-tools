"""Cause-line extraction for ngspice fatal-error diagnostics (issues #4623, #4924).

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
