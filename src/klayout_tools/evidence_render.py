"""``klt evidence render``: fill marked census/count tables in docs from
evidence records (issue #5274).

A doc carries blocks::

    <!-- klt:table id=NAME -->
    ...generated markdown pipe table...
    <!-- /klt:table -->

and a JSON spec (one entry per ``id``) says which record values fill which
cells. Text outside blocks is never touched. Every failure is fail-closed:
all new doc text is computed first and nothing is written unless every block
succeeded (``--check`` never writes). Toolchain-free; records are read as JSON.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any

CODE_BAD_INPUT = "bad_input"

_OPEN_RE = re.compile(r"<!--\s*klt:table\s+id=([A-Za-z0-9_.-]+)\s*-->")
_CLOSE_RE = re.compile(r"<!--\s*/klt:table\s*-->")
_DECIMAL_RE = re.compile(r"^\{:\.(\d+)f\}$")


class EvidenceRenderError(Exception):
    """The render could not run. ``code`` is the envelope ``error.code``."""

    def __init__(self, message: str, code: str = CODE_BAD_INPUT) -> None:
        super().__init__(message)
        self.code = code


def _read_json(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise EvidenceRenderError(f"cannot read {what} {path}: {exc}") from exc


def load_spec(path: str) -> dict[str, dict[str, Any]]:
    """Load and shape-check the spec: ``{id: {"columns": [...], "rows": [...]}}``."""
    spec = _read_json(path, "spec")
    if not isinstance(spec, dict) or not spec:
        raise EvidenceRenderError(
            f"spec {path} must be a non-empty JSON object keyed by id"
        )
    for tid, entry in spec.items():
        where = f"spec id {tid!r}"
        if not isinstance(entry, dict):
            raise EvidenceRenderError(f"{where}: entry must be an object")
        cols = entry.get("columns")
        rows = entry.get("rows")
        if not isinstance(cols, list) or not cols:
            raise EvidenceRenderError(f"{where}: 'columns' must be a non-empty array")
        for col in cols:
            if not isinstance(col, dict) or not isinstance(col.get("header"), str):
                raise EvidenceRenderError(
                    f"{where}: each column needs a string 'header'"
                )
            _check_format(col.get("format", "str"), where)
        if not isinstance(rows, list):
            raise EvidenceRenderError(f"{where}: 'rows' must be an array")
        for row in rows:
            if not isinstance(row, list) or len(row) != len(cols):
                raise EvidenceRenderError(
                    f"{where}: every row must be an array with {len(cols)} cell(s)"
                )
            for cell in row:
                if isinstance(cell, dict):
                    if not isinstance(cell.get("record"), str) or not isinstance(
                        cell.get("pointer"), str
                    ):
                        raise EvidenceRenderError(
                            f"{where}: a record cell needs string 'record' "
                            "and 'pointer'"
                        )
                elif not isinstance(cell, (str, int, float)) or isinstance(cell, bool):
                    raise EvidenceRenderError(
                        f"{where}: literal cells must be string or number"
                    )
    return spec


def _check_format(fmt: Any, where: str) -> None:
    if fmt in ("str", "int") or (isinstance(fmt, str) and _DECIMAL_RE.match(fmt)):
        return
    raise EvidenceRenderError(
        f"{where}: unknown format {fmt!r} (allowed: 'str', 'int', '{{:.Nf}}')"
    )


def _pointer(doc: Any, pointer: str, where: str) -> Any:
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise EvidenceRenderError(
            f"{where}: JSON pointer {pointer!r} must start with '/'"
        )
    cur = doc
    for raw in pointer[1:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur = cur[int(tok)]
        else:
            raise EvidenceRenderError(f"{where}: pointer {pointer!r} does not resolve")
    return cur


def _format(value: Any, fmt: str, where: str) -> str:
    if fmt == "str":
        text = value if isinstance(value, str) else json.dumps(value)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceRenderError(
                f"{where}: format {fmt!r} needs a number, got {value!r}"
            )
        if fmt == "int":
            if value != int(value):
                raise EvidenceRenderError(f"{where}: {value!r} is not integral")
            text = str(int(value))
        else:
            text = f"{value:.{int(_DECIMAL_RE.match(fmt).group(1))}f}"  # type: ignore[union-attr]
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_table(
    tid: str, entry: dict[str, Any], root: str, cache: dict[str, Any]
) -> list[str]:
    """Render one spec entry to markdown pipe-table lines."""
    cols = entry["columns"]
    lines = [
        "| " + " | ".join(c["header"] for c in cols) + " |",
        "|" + "|".join(" --- " for _ in cols) + "|",
    ]
    root_abs = os.path.realpath(root)
    for ri, row in enumerate(entry["rows"]):
        cells = []
        for ci, cell in enumerate(row):
            where = f"table {tid!r} row {ri + 1} col {ci + 1}"
            fmt = cols[ci].get("format", "str")
            if isinstance(cell, dict):
                rec = os.path.realpath(os.path.join(root_abs, cell["record"]))
                if os.path.commonpath([root_abs, rec]) != root_abs:
                    raise EvidenceRenderError(
                        f"{where}: record {cell['record']!r} escapes root"
                    )
                if rec not in cache:
                    if not os.path.isfile(rec):
                        raise EvidenceRenderError(
                            f"{where}: missing record {cell['record']!r}"
                        )
                    cache[rec] = _read_json(rec, "record")
                value = _pointer(cache[rec], cell["pointer"], where)
            else:
                value = cell
            cells.append(_format(value, fmt, where))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _find_blocks(text: str, doc: str) -> list[tuple[str, int, int]]:
    """``[(id, body_start, body_end)]``; raises on unclosed/stray markers."""
    blocks: list[tuple[str, int, int]] = []
    pos = 0
    while True:
        o = _OPEN_RE.search(text, pos)
        c = _CLOSE_RE.search(text, pos)
        if o is None and c is None:
            return blocks
        if c is not None and (o is None or c.start() < o.start()):
            raise EvidenceRenderError(f"{doc}: closing marker without an open block")
        assert o is not None
        c2 = _CLOSE_RE.search(text, o.end())
        n = _OPEN_RE.search(text, o.end())
        if c2 is None or (n is not None and n.start() < c2.start()):
            raise EvidenceRenderError(f"{doc}: block {o.group(1)!r} is not closed")
        blocks.append((o.group(1), o.end(), c2.start()))
        pos = c2.end()


def render(
    root: str, docs: list[str], spec_path: str, check: bool = False
) -> dict[str, Any]:
    """Fill (or with ``check`` only compare) every marked block under ``docs``.

    Returns the payload. Writes only when ``check`` is false and every block
    rendered; any failure raises :class:`EvidenceRenderError` with nothing written.
    """
    if not os.path.isdir(root):
        raise EvidenceRenderError(f"root {root} is not a directory")
    spec = load_spec(spec_path)
    paths: list[str] = []
    for pattern in docs:
        hits = sorted(
            p
            for p in glob.glob(pattern, root_dir=root, recursive=True)
            if os.path.isfile(os.path.join(root, p))
        )
        if not hits:
            raise EvidenceRenderError(
                f"--docs {pattern!r} matched no files under {root}"
            )
        paths.extend(h for h in hits if h not in paths)

    cache: dict[str, Any] = {}
    seen: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []
    for rel in paths:
        full = os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise EvidenceRenderError(f"cannot read doc {rel}: {exc}") from exc
        nl = "\r\n" if "\r\n" in text else "\n"
        out: list[str] = []
        last = 0
        block_results = []
        for tid, start, end in _find_blocks(text, rel):
            if tid in seen:
                raise EvidenceRenderError(
                    f"duplicate block id {tid!r} ({seen[tid]} and {rel})"
                )
            seen[tid] = rel
            if tid not in spec:
                raise EvidenceRenderError(f"{rel}: block id {tid!r} has no spec entry")
            lines = build_table(tid, spec[tid], root, cache)
            body = nl + nl.join(lines) + nl
            out.append(text[last:start])
            out.append(body)
            last = end
            block_results.append(
                {"id": tid, "status": "current" if text[start:end] == body else "stale"}
            )
        out.append(text[last:])
        new_text = "".join(out)
        results.append({"doc": rel, "blocks": block_results})
        if new_text != text:
            pending.append((full, new_text))

    unused = sorted(set(spec) - set(seen))
    if unused:
        raise EvidenceRenderError(
            f"spec id(s) with no block in any doc: {', '.join(unused)}"
        )

    stale = [
        f"{r['doc']}#{b['id']}"
        for r in results
        for b in r["blocks"]
        if b["status"] == "stale"
    ]
    if not check:
        for full, new_text in pending:
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
        for r in results:
            for b in r["blocks"]:
                if b["status"] == "stale":
                    b["status"] = "updated"
    if check:
        status = "stale" if stale else "clean"
    else:
        status = "updated" if stale else "clean"
    return {
        "schema_version": 1,
        "status": status,
        "check": check,
        "docs": results,
        "stale": stale if check else [],
        "updated": [] if check else stale,
    }
