"""Merge several Liberty group files into one ``.lib`` (issue #4087).

Some upstream PDK trees split a standard-cell library's cells over several
complete ``library(){}`` files per corner (for example ``AO``/``OA``/
``INVBUF``/``SEQ``/``SIMPLE`` groups) that share one header. ``klt-pdk.json``
lets a ``liberty`` corner list those files; :func:`merge_liberty` produces the
single file every ``liberty_path`` consumer (Yosys, OpenROAD, OpenSTA) expects.

Merge rule: the header (text before the first ``cell (``) comes from the first
file and must agree with the rest field by field -- whitespace, lexical
comments, the ``library (<name>)`` name and the free-text ``date``,
``revision`` and ``comment`` attributes are ignored; any other difference
(units, operating conditions, ``default_*``, ...) is an error naming the
conflicting field and both values. Top-level ``lu_table_template`` and
``power_lut_template`` groups are the exception: they are unioned by name
(same name with different content is an error) and the ones the first file
lacks are appended after its header. Each file's cell groups are appended in
listed order; a duplicate cell name is an error. The
result is written content-addressed under a klt cache directory -- never into
the PDK tree -- so repeated runs reuse it and its path (and hash) is stable.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

#: Environment override for the cache root (otherwise ``$XDG_CACHE_HOME`` or
#: ``~/.cache``, plus ``klt/liberty-merged``).
CACHE_ENV = "KLT_CACHE_DIR"

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$]*")
_LIBRARY_NAME_RE = re.compile(r"\blibrary\s*\([^)]*\)")
_CELL_NAME_RE = re.compile(r"\(\s*\"?([^\")\s]+)\"?\s*\)")
_TEMPLATE_KINDS = ("lu_table_template", "power_lut_template")

#: Header attributes ignored when comparing files for a merge: free text with
#: no electrical content (a per-file timestamp, a version string, a vendor
#: note). This is a closed set -- every other header field must agree.
_IGNORED_HEADER_ATTRS = frozenset({"date", "revision", "comment"})

#: Key carrying header text that is neither an attribute nor a group, so
#: unrecognised residue still participates in the comparison (fail closed).
_RESIDUE_KEY = "<other header text>"


class LibertyMergeError(Exception):
    """Liberty files cannot be merged (header mismatch, duplicate cell, ...)."""


def cache_dir() -> str:
    """Directory merged Liberty files are written to."""
    base = os.environ.get(CACHE_ENV)
    if base:
        return os.path.join(os.path.expanduser(base), "liberty-merged")
    xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(xdg, "klt", "liberty-merged")


def merge_liberty(paths: list[str]) -> str:
    """Merge ``paths`` into one cached ``.lib`` and return its path.

    A single path is returned unchanged (the list form of one element equals
    the string form). Raises :class:`LibertyMergeError` on a header field
    conflict, a duplicate cell name, or a file that is not a ``library``
    group.
    """
    if not paths:
        raise LibertyMergeError("no Liberty files to merge")
    if len(paths) == 1:
        return paths[0]

    texts = [_read(path) for path in paths]
    digest = hashlib.sha256()
    for text in texts:
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
    out_path = os.path.join(cache_dir(), f"merged-{digest.hexdigest()[:24]}.lib")
    if os.path.isfile(out_path):
        # Content-addressed: still validate nothing, inputs are unchanged.
        return out_path

    header, extra_templates, bodies = _collect(paths, texts)
    head = header.rstrip()
    if extra_templates:
        head += "\n" + "\n".join(extra_templates)
    merged = head + "\n\n" + "\n".join(b.strip() for b in bodies) + "\n}\n"
    _write_atomic(out_path, merged)
    return out_path


def _collect(paths: list[str], texts: list[str]) -> tuple[str, list[str], list[str]]:
    """Validate headers/cell names across files; ``(header, extra templates,
    bodies)``. Extra templates are those the first header does not define."""
    header = ""
    header_fields: dict[str, str] = {}
    first_path = paths[0]
    seen: dict[str, str] = {}
    templates: dict[str, tuple[str, str, str]] = {}  # name -> (norm, raw, path)
    extra: list[str] = []
    bodies: list[str] = []
    for path, text in zip(paths, texts, strict=True):
        file_header, body, cells = _split(path, text)
        stripped, file_templates = _extract_templates(file_header)
        fields = _header_fields(stripped)
        if path is first_path:
            header, header_fields = file_header, fields
        else:
            _compare_headers(first_path, header_fields, path, fields)
        for name, raw in file_templates:
            tnorm = _normalize_header(raw)
            known = templates.get(name)
            if known is None:
                templates[name] = (tnorm, raw, path)
                if path is not first_path:
                    extra.append(raw)
            elif known[0] != tnorm:
                raise LibertyMergeError(
                    f"Liberty template '{name}' differs between '{known[2]}' "
                    f"and '{path}' (cannot merge cell groups)"
                )
        for cell in cells:
            if cell in seen:
                raise LibertyMergeError(
                    f"duplicate cell '{cell}' in '{path}' (already defined in "
                    f"'{seen[cell]}')"
                )
            seen[cell] = path
        bodies.append(body)
    return header, extra, bodies


def _header_fields(header: str) -> dict[str, str]:
    """Depth-1 fields of a template-stripped header, keyed by name.

    ``attr : value ;`` statements map to their normalised value (quoted
    strings kept verbatim), ``name (args) { ... }`` groups to their
    normalised body under a ``name(args)`` key, ``name (args) ;`` complex
    attributes to their normalised args. Attributes in
    :data:`_IGNORED_HEADER_ATTRS` are dropped; anything unrecognised is
    carried under :data:`_RESIDUE_KEY` so it still takes part in the
    comparison.
    """
    fields: dict[str, str] = {}
    cursor = depth = 0
    for kind, pos, end in _tokens(header):
        if pos < cursor:
            continue  # inside a statement already consumed below
        if kind == "{":
            depth += 1
            if depth == 1:
                cursor = end  # the 'library (<name>) {' opener
            continue
        if kind == "}":
            depth -= 1
            continue
        if depth != 1:
            continue
        if cursor < pos:
            _add_residue(header[cursor:pos], fields)
        cursor = _take_field(header, pos, end, cursor, fields)
    _add_residue(header[cursor:], fields)
    return fields


def _take_field(
    header: str, pos: int, end: int, cursor: int, fields: dict[str, str]
) -> int:
    """Consume the depth-1 statement starting at the identifier ``pos`` and
    return the new cursor (the old one when unrecognised, so the text rides
    in the next residue flush instead of being silently skipped)."""
    name = header[pos:end]
    nxt, ch = _next_significant(header, end)
    n = len(header)
    if ch == ":":
        semi = _find_matching(header, nxt, ":", ";")
        if name not in _IGNORED_HEADER_ATTRS:
            fields[name] = _normalize_header(header[nxt + 1 : min(semi, n)])
        return min(semi + 1, n)
    if ch == "(":
        close = _find_matching(header, nxt, "(", ")")
        args = _normalize_header(header[nxt + 1 : close])
        p2, ch2 = _next_significant(header, close + 1)
        if ch2 == "{":
            bend = _find_matching(header, p2, "{", "}")
            fields[f"{name}({args})"] = _normalize_header(header[p2 + 1 : min(bend, n)])
            return min(bend + 1, n)
        fields[f"{name}({args})"] = args  # complex attribute 'name (v1, v2) ;'
        return min(p2 + 1, n)
    if ch == "{":
        bend = _find_matching(header, nxt, "{", "}")
        fields[name] = _normalize_header(header[nxt + 1 : min(bend, n)])
        return min(bend + 1, n)
    return cursor


def _add_residue(text: str, fields: dict[str, str]) -> None:
    """Carry unrecognised header text into the field map (fail closed)."""
    norm = _normalize_header(text)
    if not norm:
        return
    prior = fields.get(_RESIDUE_KEY)
    fields[_RESIDUE_KEY] = f"{prior} {norm}" if prior else norm


def _compare_headers(
    first_path: str, first: dict[str, str], path: str, other: dict[str, str]
) -> None:
    """Raise :class:`LibertyMergeError` on the first header field that
    differs; a field present in one header and absent from the other is
    reported as missing rather than as a value difference."""
    for key, value in first.items():
        if key not in other:
            raise LibertyMergeError(
                f"cannot merge: '{key}' present in '{first_path}', missing in '{path}'"
            )
        if other[key] != value:
            raise LibertyMergeError(
                f"cannot merge: '{key}' differs: {_clip(value)} in "
                f"'{first_path}', {_clip(other[key])} in '{path}'"
            )
    for key in other:
        if key not in first:
            raise LibertyMergeError(
                f"cannot merge: '{key}' missing in '{first_path}', present in '{path}'"
            )


def _clip(value: str, limit: int = 80) -> str:
    """Value truncated so an error message stays readable."""
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _extract_templates(header: str) -> tuple[str, list[tuple[str, str]]]:
    """``(header without template groups, [(name, group text), ...])``."""
    found: list[tuple[str, str]] = []
    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    name = ""
    for kind, pos, end in _tokens(header):
        if kind == "{":
            depth += 1
        elif kind == "}":
            depth -= 1
            if depth == 1 and start is not None:
                found.append((name, header[start : pos + 1]))
                spans.append((start, pos + 1))
                start = None
        elif depth == 1 and start is None and header[pos:end] in _TEMPLATE_KINDS:
            match = _CELL_NAME_RE.match(header, _skip_ws(header, end))
            if match:
                start, name = pos, match.group(1)
    stripped, last = [], 0
    for lo, hi in spans:
        stripped.append(header[last:lo])
        last = hi
    stripped.append(header[last:])
    return " ".join(stripped), found


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


def _write_atomic(out_path: str, merged: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(merged)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise LibertyMergeError(f"cannot read Liberty file '{path}': {exc}") from exc


def _split(path: str, text: str) -> tuple[str, str, list[str]]:
    """``(header, body, cell names)`` for one ``library(){}`` file.

    ``body`` runs from the first top-level ``cell (`` to just before the
    library's closing brace.
    """
    first_ident = next((text[p:e] for k, p, e in _tokens(text) if k == "ident"), None)
    first_cell, cells, close = _scan_cells(text)
    if close is None or first_ident != "library":
        raise LibertyMergeError(f"'{path}' is not a complete Liberty library group")
    if first_cell is None:
        raise LibertyMergeError(f"'{path}' defines no cells")
    return text[:first_cell], text[first_cell:close], cells


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
