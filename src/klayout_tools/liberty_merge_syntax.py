"""Lexical scanning for Liberty header comparison and output pin limits."""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
_LIBRARY_NAME_RE = re.compile(r"\blibrary\s*\([^)]*\)")
_CELL_NAME_RE = re.compile(r"\(\s*\"?([^\")\s]+)\"?\s*\)")


def _scan_cells(text: str) -> tuple[int | None, list[str], int | None]:
    """``(first top-level cell offset, cell names, closing-brace offset)``."""
    first_cell: int | None = None
    cells: list[str] = []
    depth = 0
    for kind, pos, end in _tokens(text):
        if kind == "{":
            depth += 1
        elif kind == "}":
            depth -= 1
            if depth == 0:
                return first_cell, cells, pos
        elif depth == 1 and text[pos:end] == "cell":
            match = _CELL_NAME_RE.match(text, _skip_ws(text, end))
            if match:
                cells.append(match.group(1))
                if first_cell is None:
                    first_cell = pos
    return first_cell, cells, None


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


def _normalize_header(header: str) -> str:
    """Header text with comments, whitespace and the library name ignored."""
    parts: list[str] = []
    i, n = 0, len(header)
    while i < n:
        if header[i] == '"':
            j = i + 1
            while j < n and header[j] != '"':
                j += 2 if header[j] == "\\" else 1
            parts.append(header[i : j + 1])
            i = j + 1
        elif header.startswith("/*", i):
            stop = header.find("*/", i + 2)
            i = n if stop < 0 else stop + 2
            parts.append(" ")
        elif header.startswith("//", i):
            stop = header.find("\n", i)
            i = n if stop < 0 else stop
        else:
            parts.append(header[i])
            i += 1
    text = _LIBRARY_NAME_RE.sub("library ()", "".join(parts), count=1)
    return " ".join(text.split())


def _pin_tokens(text: str):
    """Yield braces, cell/pin names, and the two attributes used for slew limits."""
    for kind, start, end in _tokens(text):
        if kind != "ident":
            yield kind, ""
            continue
        word = text[start:end]
        if word not in ("cell", "pin", "direction", "max_transition"):
            continue
        offset, char = _next_significant(text, end)
        if word in ("cell", "pin") and char == "(":
            name = _CELL_NAME_RE.match(text, offset)
            if name and _next_significant(text, name.end())[1] == "{":
                yield word, name.group(1)
        elif word in ("direction", "max_transition") and char == ":":
            end_value = _find_terminator(text, offset + 1)
            yield word, _normalize_header(text[offset + 1 : end_value]).strip('"')


def _uncovered_output(text: str) -> tuple[str, str] | None:
    """First output with no pin-level max_transition in its own group."""
    cell = pin = ""
    depth = 0
    pin_depth = None
    attributes: dict[str, str] = {}
    for kind, value in _pin_tokens(text):
        if kind == "cell":
            cell = value
        elif kind == "pin":
            pin, pin_depth = value, depth + 1
            attributes.clear()
        elif kind == "{":
            depth += 1
        elif kind == "}":
            if depth == pin_depth:
                if (
                    attributes.get("direction") == "output"
                    and "max_transition" not in attributes
                ):
                    return cell, pin
                pin_depth = None
            depth -= 1
        elif depth == pin_depth:
            attributes[kind] = value
    return None
