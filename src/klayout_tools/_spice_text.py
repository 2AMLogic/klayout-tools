"""The SPICE line pre-processing every netlist-reading ``klt`` verb shares.

Reading a SPICE netlist lexically -- which `klt pex`, `klt sim`,
`klt characterize`, `klt op-sanity` and the flatten/normalize passes all do,
none of them a full SPICE parser -- starts with the same two steps, in this
order:

1. :func:`strip_spice_inline_comment` -- drop a ``;``/``$`` inline comment;
2. :func:`fold_spice_continuations` -- fold ``+`` continuation lines onto the
   card they continue.

Both began as per-module copies. The fold was consolidated first (issues
#1741, #3703); the comment strip followed once six copies had drifted into
four different rules about what a ``$`` means (issue #5417), only one of
which stated the rule it implemented. One documented rule beats six
near-misses: a ``$`` glued to the next token is a ``$PDK_ROOT``-style
variable reference, and four of those copies silently truncated
``.include $PDK_ROOT/...`` to a bare ``.include``.

What is deliberately *not* shared: what each caller does about full-line
``*`` comments and orphan ``+`` continuations. Those differ per caller for
real reasons, recorded in :func:`fold_spice_continuations`.
"""

from __future__ import annotations

import re

#: ngspice's inline-comment rule, as one pattern shared by every netlist
#: reader in the package (issue #5417). Two markers:
#:
#: * ``;`` anywhere on the line;
#: * ``$`` that *begins a token* (start of line, or preceded by whitespace)
#:   **and** ends one (followed by whitespace or end of line).
#:
#: Both halves of the ``$`` condition are load-bearing against real netlists:
#:
#: * a ``$`` glued to the next token is a shell-style variable reference, not
#:   a comment -- ``.include $PDK_ROOT/...`` and ``.lib $SKY130/... tt`` must
#:   survive intact, and so must KLayout's anonymous-net names (``.subckt
#:   cell $1 $2``, see ``extract.py``'s ``"$5"``-style placeholder note);
#: * a ``$`` glued to the *previous* token is part of that token's name --
#:   KLayout writes cards as ``X$1``/``M$1``/``C$1`` (issue #2972).
_SPICE_INLINE_COMMENT = re.compile(r"(?:^|\s)\$(?=\s|$)|;")


def strip_spice_inline_comment(line: str) -> str:
    """``line`` with a trailing SPICE inline comment removed, right-stripped.

    Implements ngspice's rule (:data:`_SPICE_INLINE_COMMENT`): everything
    from the first ``;``, or from the first token-delimited ``$``, is
    dropped. A line that is nothing but a comment returns ``""``; a line
    with no comment marker comes back only right-stripped.

    This is deliberately *line*-level and lexical -- it knows nothing about
    quoting or ``{...}``/``(...)`` nesting, so a ``;`` inside a quoted
    expression would still cut the line. No netlist this package reads puts
    one there, and keeping the rule a single regex is what lets every caller
    share it instead of re-deriving a near-miss of their own.

    Callers differ in what they do about *full-line* ``*`` comments and
    orphan ``+`` continuations; those stay per-caller choices (see
    :func:`fold_spice_continuations`), and only the inline rule is shared.
    """
    return _SPICE_INLINE_COMMENT.split(line, maxsplit=1)[0].rstrip()


def fold_spice_continuations(lines: list[str]) -> list[str]:
    """Fold SPICE ``+`` continuation lines onto the entry they continue.

    ``lines`` is whatever a caller's own pre-pass has already produced (raw
    lines, comment-stripped lines, or comment-stripped-and-filtered lines --
    this function does not care). For each entry, if it starts with ``+``
    (after left-stripping) *and* there is already a previous entry to fold
    onto, that previous entry is replaced with itself plus a single space
    plus the continuation's own content (the ``+`` and any leading/trailing
    whitespace around it dropped); otherwise the entry is appended as-is.

    A ``+``-prefixed entry with nothing preceding it to fold onto (the first
    entry in ``lines`` starts with ``+``, or every entry before it was
    itself folded away) is appended verbatim, ``+`` prefix intact, rather
    than raising or being silently dropped -- callers that want an orphan
    continuation line dropped (``op_sanity.py``, ``sim.py``) filter such
    unfolded ``+``-prefixed entries out of this function's return value
    themselves; callers that want it kept as a literal line (``pex.py``,
    ``netlist_normalize.py``) use the return value as-is.
    """
    folded: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("+") and folded:
            folded[-1] = f"{folded[-1]} {stripped[1:].strip()}"
        else:
            folded.append(line)
    return folded
