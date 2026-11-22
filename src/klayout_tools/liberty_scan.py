"""Liberty lexical scanning primitives (split out of ``liberty_merge``,
issue #6940).

Comment- and string-aware text walks shared by the Liberty header reader:
identifier/brace tokening, bracket matching, statement-terminator search
and next-significant-character skips. No merge semantics live here;
``liberty_merge`` stays the only caller above these helpers (the move is
verbatim, keeping ``liberty_merge`` under the repository's per-file line
limit).
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _next_significant(text: str, start: int) -> tuple[int, str]:
    """``(index, char)`` of the next char that is neither whitespace nor a
    comment; ``(len(text), "")`` at end of text."""
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            i = n if stop < 0 else stop + 2
        elif text.startswith("//", i):
            stop = text.find("\n", i)
            i = n if stop < 0 else stop
        else:
            return i, ch
    return n, ""


def _find_matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index of the ``close_ch`` matching the ``open_ch`` at ``start`` (strings
    and comments skipped), or ``len(text)`` when it never closes."""
    depth = 0
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            i = n if stop < 0 else stop + 2
        elif text.startswith("//", i):
            stop = text.find("\n", i)
            i = n if stop < 0 else stop
        elif ch == open_ch:
            depth += 1
            i += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    return n


def _find_terminator(text: str, start: int) -> int:
    """Index of the ``;`` ending the statement whose value begins at ``start``
    (strings and comments skipped), or ``len(text)`` when it never ends.

    Unlike :func:`_find_matching` this does not nest: a ``:`` inside the value
    is ordinary text, so the first unquoted ``;`` always ends the statement.
    """
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            i = n if stop < 0 else stop + 2
        elif text.startswith("//", i):
            stop = text.find("\n", i)
            i = n if stop < 0 else stop
        elif ch == ";":
            return i
        else:
            i += 1
    return n


def _tokens(text: str):
    """Yield ``(kind, start, end)`` for identifiers and braces, skipping
    strings and comments."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif text.startswith("/*", i):
            stop = text.find("*/", i + 2)
            i = n if stop < 0 else stop + 2
        elif text.startswith("//", i):
            stop = text.find("\n", i)
            i = n if stop < 0 else stop
        elif ch in "{}":
            yield ch, i, i + 1
            i += 1
        else:
            match = _IDENT_RE.match(text, i)
            if match:
                yield "ident", i, match.end()
                i = match.end()
            else:
                i += 1


def _starts_group(text: str, end: int) -> bool:
    """Whether the identifier ending here opens a named Liberty group."""
    pos, char = _next_significant(text, end)
    if char != "(":
        return False
    close = _find_matching(text, pos, "(", ")")
    return _next_significant(text, close + 1)[1] == "{"
