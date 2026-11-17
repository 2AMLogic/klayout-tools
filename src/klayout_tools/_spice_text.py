"""Netlist text to logical cards -- the line-level pre-pass every
netlist-reading ``klt`` verb shares (issue #5779).

``logical_lines`` drops ``*`` comment lines and inline ``;``/``$`` comments,
then folds ``+`` continuation lines with
:func:`_paths.fold_spice_continuations` and drops an orphan ``+``-prefixed
line (a continuation with no entry above it -- never a card of its own).
It is deliberately lexical, not a complete SPICE parser: ``.include``/
``.lib`` are not followed.

Companion modules: this one runs *before* :func:`_spice_tokens.split_spice_tokens`
(which splits one card into tokens) and :func:`_spice_number.parse` (which
reads one value token). ``sim.py``'s ``_logical_netlist_lines`` and the
device-multiplier lint are the first two consumers of the shared copy.
"""

from __future__ import annotations

import re

from ._paths import fold_spice_continuations

#: Inline comment: a ``;`` anywhere, or a ``$`` preceded by whitespace
#: (ngspice reserves a bare ``$`` inside words).
_INLINE_COMMENT_RE = re.compile(r";|\s\$")


def logical_lines(text: str) -> list[str]:
    """Netlist lines with ``+`` continuations joined and comments removed.

    Full-line ``*`` comments and inline ``;``/``$`` comments are dropped;
    an orphan ``+`` continuation (nothing above it to fold onto) is dropped
    too, matching the behavior ``sim.py`` has always had.
    """
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        line = _INLINE_COMMENT_RE.split(line, maxsplit=1)[0].strip()
        if line:
            kept.append(line)
    return [line for line in fold_spice_continuations(kept) if not line.startswith("+")]
