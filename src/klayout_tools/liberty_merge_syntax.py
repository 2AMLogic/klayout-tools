"""Lexical scanning for Liberty header comparison and output pin limits."""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
_LIBRARY_NAME_RE = re.compile(r"\blibrary\s*\([^)]*\)")
_CELL_NAME_RE = re.compile(r"\(\s*\"?([^\")\s]+)\"?\s*\)")
#: Characters that may open a quoted string or a comment (both skipped).
_SKIPPABLE = '"/'


def _scan_cells(text: str) -> tuple[int | None, list[str], int | None, int]:
    """Locate cells and count top-level operating-condition declarations."""
    conditions = 0
    first_cell: int | None = None
    cells: list[str] = []
    depth = 0
    for kind, pos, end in _tokens(text):
        if kind == "{":
            depth += 1
        elif kind == "}":
            depth -= 1
            if depth == 0:
                return first_cell, cells, pos, conditions
        elif depth == 1 and text[pos:end] == "operating_conditions":
            conditions += _starts_group(text, end)
        elif depth == 1 and text[pos:end] == "cell":
            match = _CELL_NAME_RE.match(text, _skip_ws(text, end))
            if match:
                cells.append(match.group(1))
                if first_cell is None:
                    first_cell = pos
    return first_cell, cells, None, conditions


def _starts_group(text: str, end: int) -> bool:
    """Whether the identifier ending here opens a named Liberty group."""
    return _group_opener(text, end) is not None


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_comment(text: str, i: int) -> int | None:
    """Index just past the comment opening at ``i``, or ``None`` when none
    does; an unclosed comment runs to the end of ``text``."""
    if text.startswith("/*", i):
        stop = text.find("*/", i + 2)
        return len(text) if stop < 0 else stop + 2
    if text.startswith("//", i):
        stop = text.find("\n", i)
        return len(text) if stop < 0 else stop
    return None


def _skip_string_or_comment(text: str, i: int) -> int | None:
    """Index just past the quoted string (backslash escapes honoured) or the
    comment opening at ``i``, or ``None`` when neither does; an unterminated
    one runs to the end of ``text``."""
    if text[i] == '"':
        close = i + 1
        n = len(text)
        while close < n and text[close] != '"':
            close += 2 if text[close] == "\\" else 1
        return close + 1
    return _skip_comment(text, i)


def _next_significant(text: str, start: int) -> tuple[int, str]:
    """``(index, char)`` of the next char that is neither whitespace nor a
    comment; ``(len(text), "")`` at end of text."""
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "/" and (stop := _skip_comment(text, i)) is not None:
            i = stop
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
        if ch in _SKIPPABLE and (skip := _skip_string_or_comment(text, i)) is not None:
            i = skip
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
        if ch in _SKIPPABLE and (skip := _skip_string_or_comment(text, i)) is not None:
            i = skip
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
        if ch in _SKIPPABLE and (skip := _skip_string_or_comment(text, i)) is not None:
            i = skip
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
        ch = header[i]
        if ch == '"' and (close := _skip_string_or_comment(header, i)) is not None:
            parts.append(header[i:close])
            i = close
        elif ch == "/" and (stop := _skip_comment(header, i)) is not None:
            i = stop
            parts.append(" ")
        else:
            parts.append(ch)
            i += 1
    text = _LIBRARY_NAME_RE.sub("library ()", "".join(parts), count=1)
    return " ".join(text.split())


def _unquote(value: str) -> str:
    """``value`` without one pair of surrounding double quotes."""
    return value[1:-1] if value[:1] == '"' and value[-1:] == '"' else value


def _colon_attr(text: str, ident_end: int) -> tuple[int, int] | None:
    """``(colon, terminator)`` of the ``: value ;`` statement following the
    identifier ending at ``ident_end`` -- the value is
    ``text[colon + 1 : terminator]`` -- or ``None`` when none follows."""
    colon, char = _next_significant(text, ident_end)
    if char != ":":
        return None
    return colon, _find_terminator(text, colon + 1)


def _group_opener(text: str, ident_end: int) -> tuple[int, int, int] | None:
    """``(open paren, close paren, open brace)`` of the ``name (args) {``
    group following the name ending at ``ident_end``, or ``None`` when none
    follows."""
    opening, char = _next_significant(text, ident_end)
    if char != "(":
        return None
    closing = _find_matching(text, opening, "(", ")")
    brace, char = _next_significant(text, closing + 1)
    return (opening, closing, brace) if char == "{" else None


def _depth1_groups(
    header: str, kinds: tuple[str, ...]
) -> list[tuple[str, str, int, int]]:
    """``(kind, name, start, stop)`` for every ``kind (name) { ... }`` group at
    library level whose ``kind`` is in ``kinds``, in source order: ``name`` is
    the normalised, unquoted argument list and ``header[start : stop]`` the
    group's raw text."""
    found: list[tuple[str, str, int, int]] = []
    depth = resume = 0
    for kind, pos, end in _tokens(header):
        if pos < resume:
            continue  # inside a group already consumed below
        if kind == "{":
            depth += 1
        elif kind == "}":
            depth -= 1
        elif depth == 1 and header[pos:end] in kinds:
            if opener := _group_opener(header, end):
                opening, closing, brace = opener
                stop = _find_matching(header, brace, "{", "}")
                name = _unquote(_normalize_header(header[opening + 1 : closing]))
                found.append((header[pos:end], name, pos, stop + 1))
                resume = stop + 1
    return found


def _find_attr_reference(text: str, attrs: set[str], names: set[str]) -> str | None:
    """The first of ``names`` that an ``attr : name ;`` statement in ``text``
    refers to, for any ``attr`` in ``attrs``, or ``None``."""
    for kind, start, end in _tokens(text):
        if kind != "ident" or text[start:end] not in attrs:
            continue
        if attr := _colon_attr(text, end):
            value = _normalize_header(text[attr[0] + 1 : attr[1]])
            if (name := _unquote(value)) in names:
                return name
    return None


def _pin_tokens(text: str):
    """Yield braces, cell/pin names, and the two attributes used for slew limits."""
    for kind, start, end in _tokens(text):
        if kind != "ident":
            yield kind, ""
            continue
        word = text[start:end]
        if word not in ("cell", "pin", "direction", "max_transition"):
            continue
        if word in ("cell", "pin"):
            opening, char = _next_significant(text, end)
            if char == "(" and (name := _CELL_NAME_RE.match(text, opening)):
                if _next_significant(text, name.end())[1] == "{":
                    yield word, name.group(1)
        elif attr := _colon_attr(text, end):
            yield word, _unquote(_normalize_header(text[attr[0] + 1 : attr[1]]))


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


_TEMPLATE_KINDS = ("lu_table_template", "power_lut_template")
_AXIS = re.compile(r"(variable|index)_([1-9][0-9]*)\Z")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def _direct_words(body: str):
    """Identifier spans at the current group level, excluding nested blocks."""
    depth = 0
    for kind, start, end in _tokens(body):
        if kind == "{":
            depth += 1
        elif kind == "}":
            depth -= 1
        elif depth == 0:
            yield body[start:end], end


def _index_value(text: str, end: int) -> tuple[int, int] | None:
    """Contents of a direct index attribute containing Liberty numeric values."""
    opening, ch = _next_significant(text, end)
    if ch != "(":
        return None
    closing = _find_matching(text, opening, "(", ")")
    if _next_significant(text, closing + 1)[1] != ";":
        return None
    value = _normalize_header(text[opening + 1 : closing].replace("\\\n", " "))
    numbers = re.findall(r'[^\s,"]+', value)
    if not numbers or not all(_NUMBER.fullmatch(n) for n in numbers):
        return None
    return opening + 1, closing


def _template_shape(raw: str) -> tuple[str, set[str]]:
    """Mask direct index values, preserving every other part of the definition."""
    opening = next(start for kind, start, _ in _tokens(raw) if kind == "{")
    # _extract_templates already located the closing brace of this group.
    body = raw[opening + 1 : -1]
    pieces = [raw[: opening + 1]]
    axes: set[str] = set()
    last = 0
    for word, end in _direct_words(body):
        axis = _AXIS.fullmatch(word)
        if not axis:
            continue
        if axis[1] == "variable" and _next_significant(body, end)[1] == ":":
            axes.add("index_" + axis[2])
        elif axis[1] == "index" and (span := _index_value(body, end)):
            axes.add(word)
            pieces.append(body[last : span[0]])
            last = span[1]
    pieces.append(body[last:] + "}")
    return _normalize_header("".join(pieces)), axes


def _candidate_tables(text: str, templates: dict[str, set[str]]):
    """Yield the bodies of groups whose sole argument names a conflicting template."""
    for kind, start, end in _tokens(text):
        if kind != "ident":
            continue
        opener = _group_opener(text, end)
        if opener is None:
            continue
        opening, closing, brace = opener
        name = _unquote(_normalize_header(text[opening + 1 : closing]))
        if name in templates and text[start:end] not in _TEMPLATE_KINDS:
            yield (
                text[start:end],
                name,
                text[brace + 1 : _find_matching(text, brace, "{", "}")],
            )


def _local_table_axes(kind: str, body: str, required: set[str]) -> set[str] | None:
    """Find direct numeric overrides on a lookup table."""
    axes: set[str] = set()
    is_table = kind in {
        "cell_rise",
        "cell_fall",
        "rise_transition",
        "fall_transition",
        "rise_constraint",
        "fall_constraint",
        "rise_power",
        "fall_power",
        "vector",
    }
    for word, end in _direct_words(body):
        if word == "values" and _next_significant(body, end)[1] == "(":
            is_table = True
        elif word in required:
            is_table = True
            if _index_value(body, end):
                axes.add(word)
    return axes if is_table else None


def inherited_table_axis(
    text: str, templates: dict[str, set[str]]
) -> tuple[str, str] | None:
    """First table that would rely on an ambiguous merged template axis."""
    for kind, name, body in _candidate_tables(text, templates):
        local = _local_table_axes(kind, body, templates[name])
        if local is None:
            continue
        missing = templates[name] - local
        if missing:
            return name, min(missing)
    return None
