"""Helpers for the item-6 ``not_applicable`` form of :mod:`.signoff`: the
conservative statistical-row scan, the markdown-table spec parser and the
``reviewed_not_statistical`` row ids (issues #2766 size ratchet, #4793)."""

from __future__ import annotations

import re
from typing import Any

_NA_STATISTICAL_KEY_RE = re.compile(
    r"sigma|σ|yield|cpk|ppk|offset|mismatch|monte[_ -]?carlo|std[_ -]?dev",
    re.IGNORECASE,
)
_NA_STATISTICAL_TEXT_RE = re.compile(
    r"σ|\bsigma\b|\bcpk\b|\bppk\b|\byield\b|\bmonte[ _-]?carlo\b"
    r"|\bmismatch\b|\bmatching\b|\boffset\b|\baccuracy\b",
    re.IGNORECASE,
)


def _na_scan_dict(value: dict[str, Any], path: str, text: bool) -> str | None:
    """:func:`_na_scan_statistical` for a JSON object."""
    for key, child in value.items():
        key_path = f"{path}.{key}" if path else str(key)
        if str(key).lower() == "statistical":
            if child not in (None, False, 0, "", "false", "no"):
                return key_path
            continue
        keyed = _NA_STATISTICAL_KEY_RE.search(str(key)) and child not in (
            None,
            False,
            "",
            [],
            {},
        )
        if keyed:
            return key_path
        hit = _na_scan_statistical(child, key_path, text)
        if hit is not None:
            return hit
    return None


def _na_scan_statistical(value: Any, path: str, text: bool = True) -> str | None:
    """Conservative scan of one spec/statement row for anything statistical.

    Returns a ``path`` description of the first hit, or ``None``. A key named
    ``statistical`` hits only when truthy (``statistical: false`` is the
    honest declaration); a key naming sigma/yield/Cpk/offset/mismatch/Monte
    Carlo hits on any non-empty value; any string value mentioning one of
    those (or ``3σ``, accuracy, matching) hits. False positives refuse, which
    is the safe direction -- the author fixes the row's wording or cites real
    ``klt yield`` evidence. ``text=False`` skips the string-value pattern only
    (issue #4793: a reviewed row); key and ``statistical`` hits still fire.
    """
    if isinstance(value, dict):
        return _na_scan_dict(value, path, text)
    if isinstance(value, list):
        for index, child in enumerate(value):
            hit = _na_scan_statistical(child, f"{path}[{index}]", text)
            if hit is not None:
                return hit
        return None
    if text and isinstance(value, str) and _NA_STATISTICAL_TEXT_RE.search(value):
        return path
    return None


_NA_FORMAT_JSON = "json"
_NA_FORMAT_MARKDOWN = "markdown-table"
_NA_MD_STATUS_RE = re.compile(
    r"^\s*(?:[-*>]\s*)?[*_]*status[*_]*\s*:\s*[*_]*\s*([A-Za-z][\w-]*)",
    re.IGNORECASE,
)
_NA_MD_PIPE_RE = re.compile(r"(?<!\\)\|")


def _na_md_cells(line: str) -> list[str]:
    """Cells of one pipe-table line (``\\|`` is a literal pipe)."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [cell.replace("\\|", "|").strip() for cell in _NA_MD_PIPE_RE.split(body)]


def _na_md_is_alignment(line: str) -> bool:
    cells = _na_md_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-+:?", c) for c in cells)


def _na_parse_markdown(
    text: str, id_column: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Markdown spec -> ``({"status", "rows"}, None)`` or ``(None, problem)``.

    ``status`` is the first ``Status: <word>`` line; the rows are those of the
    first pipe table whose header has an ``id_column`` cell, each row a
    ``{header: cell}`` dict with ``id`` set from that column (issue #4793).
    """
    lines = text.splitlines()
    status = next((m.group(1) for m in map(_NA_MD_STATUS_RE.match, lines) if m), None)
    if status is None:
        return None, "no 'Status: <value>' line found"
    for index, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        header = _na_md_cells(line)
        if id_column not in header or not _na_md_is_alignment(lines[index + 1]):
            continue
        rows: list[dict[str, Any]] = []
        for body in lines[index + 2 :]:
            if not body.lstrip().startswith("|"):
                break
            cells = _na_md_cells(body)
            row = {h: c for h, c in zip(header, cells, strict=False) if h}
            row["id"] = (
                cells[header.index(id_column)]
                if len(cells) > header.index(id_column)
                else ""
            )
            rows.append(row)
        return {"status": status, "rows": rows}, None
    return None, f"no pipe table with an '{id_column}' header cell found"


def _na_reviewed_ids(statement_rows: Any) -> set[str]:
    """Ids of statement rows carrying ``reviewed_not_statistical: true``."""
    if not isinstance(statement_rows, list):
        return set()
    return {
        row["id"]
        for row in statement_rows
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and row.get("reviewed_not_statistical") is True
    }


def _na_spec_format(raw: dict[str, Any], ref: dict[str, Any]) -> str | None:
    """Validate a spec reference's ``format``/``id_column``; fill ``ref``.

    Returns a problem string, or ``None`` when the reference is fine.
    """
    fmt = raw.get("format", _NA_FORMAT_JSON)
    column = raw.get("id_column")
    if fmt not in (_NA_FORMAT_JSON, _NA_FORMAT_MARKDOWN):
        return f"'format' must be 'json' or '{_NA_FORMAT_MARKDOWN}'"
    if fmt == _NA_FORMAT_MARKDOWN:
        if not isinstance(column, str) or not column.strip():
            return "a markdown-table spec needs a string 'id_column'"
        ref["format"] = fmt
        ref["id_column"] = column.strip()
    elif column is not None:
        return "'id_column' applies only to format 'markdown-table'"
    return None
