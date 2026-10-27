"""Merge several Liberty group files into one ``.lib`` (issue #4087).

Some upstream PDK trees split a standard-cell library's cells over several
complete ``library(){}`` files per corner (for example ``AO``/``OA``/
``INVBUF``/``SEQ``/``SIMPLE`` groups) that share one header. ``klt-pdk.json``
lets a ``liberty`` corner list those files; :func:`merge_liberty` produces the
single file every ``liberty_path`` consumer (Yosys, OpenROAD, OpenSTA) expects.

Merge rule: the header (text before the first ``cell (``) comes from the first
file and must match the rest after normalisation -- whitespace, comments and
the ``library (<name>)`` name are ignored, every other difference (units,
templates, operating conditions, ...) is an error; each file's cell
groups are appended in listed order; a duplicate cell name is an error. The
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
    the string form). Raises :class:`LibertyMergeError` on a header mismatch,
    a duplicate cell name, or a file that is not a ``library`` group.
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

    header, bodies = _collect(paths, texts)
    merged = header.rstrip() + "\n\n" + "\n".join(b.strip() for b in bodies) + "\n}\n"
    _write_atomic(out_path, merged)
    return out_path


def _collect(paths: list[str], texts: list[str]) -> tuple[str, list[str]]:
    """Validate headers/cell names across files; ``(header, bodies)``."""
    header = ""
    header_norm = ""
    first_path = paths[0]
    seen: dict[str, str] = {}
    bodies: list[str] = []
    for path, text in zip(paths, texts, strict=True):
        file_header, body, cells = _split(path, text)
        norm = _normalize_header(file_header)
        if path is first_path:
            header, header_norm = file_header, norm
        elif norm != header_norm:
            raise LibertyMergeError(
                f"Liberty headers differ: '{path}' does not match the header "
                f"of '{first_path}' (cannot merge cell groups)"
            )
        for cell in cells:
            if cell in seen:
                raise LibertyMergeError(
                    f"duplicate cell '{cell}' in '{path}' (already defined in "
                    f"'{seen[cell]}')"
                )
            seen[cell] = path
        bodies.append(body)
    return header, bodies


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
