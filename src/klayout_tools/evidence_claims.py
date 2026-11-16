"""``klt evidence claims``: check the *documents* that cite evidence records
(issue #5273).

:mod:`.evidence_lint` validates the records themselves. This module checks
READMEs, specs and signoff summaries against them: every cited record exists
(and its JSON twin), a citation of a superseded record is marked historical or
names the newer one, and registry rows tie a literal string in a document to a
JSON-pointer value in a record. Toolchain-free and fail-closed -- an
unreadable document, registry or record is an error, never a pass.

Legacy records (plain ``records/<id>.md`` plus a ``.json`` twin, predating
``evidence-record/1``) are resolved through the registry's ``mapping`` key.
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections.abc import Mapping
from typing import Any

from . import evidence_lint as el
from . import evidence_record as er
from ._jsonnum import is_number

F_CITE_MISSING = "cited_record_missing"
F_TWIN_MISSING = "cited_twin_missing"
F_SUPERSEDED = "cited_record_superseded"
F_LITERAL_MISSING = "claim_literal_not_in_doc"
F_MISMATCH = "claim_mismatch"
F_UNRESOLVED = "claim_unresolved"

CODE_BAD_INPUT = "bad_input"
CODE_NO_DOCS = "no_docs"

#: Marker governing its own paragraph; directly under a heading, that section.
HISTORICAL_MARKER = "<!-- klt:historical -->"

_NATIVE_CITE = re.compile(
    r"(?<![\w.\-])(?:[\w.\-]+/)*"
    r"(evidence/sim/[\w.\-]+/[\w.\-]+/[\w.\-]+\.(?:json|md))(?![\w\-])"
)
_HEADING = re.compile(r"^(#{1,6})\s")
_ROW_KEYS = {
    "doc",
    "literal",
    "record",
    "pointer",
    "decimals",
    "scale",
    "corner",
    "agg",
}


class EvidenceClaimsError(Exception):
    """The check could not run. ``code`` is the envelope ``error.code``."""

    def __init__(self, message: str, code: str = CODE_BAD_INPUT) -> None:
        super().__init__(message)
        self.code = code


def _finding(code: str, doc: str, record: str, message: str) -> dict[str, str]:
    return {"code": code, "doc": doc, "record": record, "message": message}


def _read_json(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise EvidenceClaimsError(f"cannot read {what} {path!r}: {exc}") from exc


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def _check_mapping(data: dict[str, Any]) -> list[Any]:
    mapping = data.get("mapping", {})
    if not isinstance(mapping, dict) or set(mapping) - {"records"}:
        raise EvidenceClaimsError("'mapping' must be an object with a 'records' list")
    records = mapping.get("records", [])
    if not isinstance(records, list):
        raise EvidenceClaimsError("'mapping.records' must be a list")
    for entry in records:
        ok = (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("chain"), str)
            and set(entry) <= {"path", "chain", "supersedes"}
            and all(isinstance(t, str) for t in entry.get("supersedes", []))
        )
        if not ok:
            raise EvidenceClaimsError(
                "each 'mapping.records' entry needs string 'path' and 'chain' "
                "and an optional 'supersedes' list of strings"
            )
    return records


def _check_claim_row(row: Any) -> None:
    if not isinstance(row, dict) or set(row) - _ROW_KEYS:
        raise EvidenceClaimsError(f"claims row has unknown keys: {row!r}")
    for key in ("literal", "record", "pointer"):
        if not isinstance(row.get(key), str):
            raise EvidenceClaimsError(f"claims row needs a string {key!r}: {row!r}")
    if row.get("agg") not in (None, "min", "max"):
        raise EvidenceClaimsError("claims 'agg' must be 'min' or 'max'")
    if "decimals" in row and (
        not isinstance(row["decimals"], int) or isinstance(row["decimals"], bool)
    ):
        raise EvidenceClaimsError("claims 'decimals' must be an integer")
    if "scale" in row and (
        not isinstance(row["scale"], (int, float)) or isinstance(row["scale"], bool)
    ):
        raise EvidenceClaimsError("claims 'scale' must be a number")


def load_registry(path: str) -> dict[str, Any]:
    """Validate and return ``{"mapping": [...], "claims": [...]}``."""
    data = _read_json(path, "--registry")
    if not isinstance(data, dict):
        raise EvidenceClaimsError("--registry must be a JSON object")
    unknown = set(data) - {"mapping", "claims"}
    if unknown:
        raise EvidenceClaimsError(f"--registry has unknown keys: {sorted(unknown)}")
    records = _check_mapping(data)
    claims = data.get("claims", [])
    if not isinstance(claims, list):
        raise EvidenceClaimsError("'claims' must be a list")
    for row in claims:
        _check_claim_row(row)
    return {"mapping": records, "claims": claims}


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #


def _expand_docs(root: str, patterns: list[str]) -> list[str]:
    docs: set[str] = set()
    for pattern in patterns:
        hits = [
            h
            for h in glob.glob(pattern, root_dir=root, recursive=True)
            if os.path.isfile(os.path.join(root, h))
        ]
        if not hits:
            raise EvidenceClaimsError(
                f"--docs pattern {pattern!r} matched no files under {root}",
                CODE_NO_DOCS,
            )
        docs.update(os.path.normpath(h) for h in hits)
    return sorted(docs)


def _historical_lines(lines: list[str]) -> set[int]:
    """0-based indices of lines governed by a historical marker."""
    governed: set[int] = set()
    for i, line in enumerate(lines):
        if HISTORICAL_MARKER not in line:
            continue
        lo = i
        while lo > 0 and lines[lo - 1].strip():
            lo -= 1
        hi = i
        while hi + 1 < len(lines) and lines[hi + 1].strip():
            hi += 1
        governed.update(range(lo, hi + 1))
        heading = _HEADING.match(lines[i - 1]) if i > 0 else None
        if heading:
            level = len(heading.group(1))
            j = i
            while j < len(lines):
                other = _HEADING.match(lines[j])
                if other and len(other.group(1)) <= level:
                    break
                governed.add(j)
                j += 1
    return governed


# --------------------------------------------------------------------------- #
# chains
# --------------------------------------------------------------------------- #


class _Chains:
    """Supersession model over native and mapped legacy records."""

    def __init__(self, root: str, mapped: list[dict[str, Any]]) -> None:
        self.root = root
        self.supersedes: dict[str, list[str]] = {}
        self.group: dict[str, str] = {}
        for path in el._discover(os.path.join(root, *er.DEFAULT_EVIDENCE_DIR)):
            rel = os.path.relpath(path, root)
            record = _read_json(path, "record")
            targets = record.get("supersedes") if isinstance(record, dict) else None
            self.supersedes[rel] = [
                os.path.normpath(t) for t in targets or [] if isinstance(t, str)
            ]
            self.group[rel] = os.path.dirname(rel)
        for entry in mapped:
            rel = os.path.normpath(entry["path"])
            self.supersedes[rel] = [
                os.path.normpath(t) for t in entry.get("supersedes", [])
            ]
            self.group[rel] = "mapping:" + entry["chain"]
        self._heads = el.current_heads(self.supersedes)

    def known(self, rel: str) -> bool:
        return rel in self.supersedes

    def newer(self, rel: str) -> list[str]:
        """Current heads of ``rel``'s chain when ``rel`` is not one (else [])."""
        heads = self._heads.get(rel, [rel])
        if heads == [rel]:
            return []
        directory = self.group.get(rel, "")
        head_file = os.path.join(self.root, directory, el.HEAD_FILE)
        if not directory.startswith("mapping:") and os.path.isfile(head_file):
            with open(head_file, encoding="utf-8") as handle:
                named = os.path.join(directory, handle.read().strip())
            if named == rel:
                return []
        return heads


def _check_exists(root: str, rel: str, doc: str) -> list[dict[str, str]]:
    if not os.path.isfile(os.path.join(root, rel)):
        return [
            _finding(F_CITE_MISSING, doc, rel, f"cited record {rel!r} does not exist")
        ]
    stem, ext = os.path.splitext(rel)
    if ext == ".md" and not os.path.isfile(os.path.join(root, stem + ".json")):
        return [
            _finding(
                F_TWIN_MISSING, doc, rel, f"JSON twin {stem + '.json'!r} is missing"
            )
        ]
    return []


def _citations(text: str, mapped_paths: list[str]) -> list[tuple[int, str]]:
    """``(0-based line, root-relative record path)`` for each citation."""
    found: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines()):
        for match in _NATIVE_CITE.finditer(line):
            found.append((i, os.path.normpath(match.group(1))))
        for path in mapped_paths:
            if path in line and not any(
                path.endswith(m.group(1)) for m in _NATIVE_CITE.finditer(line)
            ):
                found.append((i, os.path.normpath(path)))
    return found


# --------------------------------------------------------------------------- #
# registry rows
# --------------------------------------------------------------------------- #


def _pointer(value: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return value
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise KeyError(token)
    return value


def _row_value(row: Mapping[str, Any], record: Any) -> str:
    """The string the record's value renders to under the row's options."""
    target = record
    if "corner" in row:
        corners = (record.get("result") or {}).get("corners")
        match = [
            c
            for c in (corners if isinstance(corners, list) else [])
            if isinstance(c, dict) and c.get("corner_id") == row["corner"]
        ]
        if len(match) != 1:
            raise KeyError(f"corner {row['corner']!r} not found exactly once")
        target = match[0]
    value = _pointer(target, row["pointer"])
    if isinstance(value, list):
        if row.get("agg") is None:
            raise KeyError("pointer resolves to a list; set 'agg' to min or max")
        value = (min if row["agg"] == "min" else max)(value)
    if not is_number(value):
        return str(value)
    value = value * row.get("scale", 1)
    if "decimals" in row:
        return f"{value:.{row['decimals']}f}"
    return f"{value:g}"


def _record_json_path(rel: str) -> str:
    stem, ext = os.path.splitext(rel)
    return stem + ".json" if ext == ".md" else rel


def _check_rows(
    root: str, rows: list[dict[str, Any]], texts: dict[str, str]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        rel = os.path.normpath(row["record"])
        doc = os.path.normpath(row["doc"]) if "doc" in row else ""
        scope = {doc: texts[doc]} if doc in texts else texts
        if doc and doc not in texts:
            out.append(
                _finding(F_UNRESOLVED, doc, rel, f"row doc {doc!r} is not in --docs")
            )
            continue
        if not any(row["literal"] in text for text in scope.values()):
            out.append(
                _finding(
                    F_LITERAL_MISSING,
                    doc,
                    rel,
                    f"literal {row['literal']!r} does not appear in the document(s)",
                )
            )
        path = os.path.join(root, _record_json_path(rel))
        if not os.path.isfile(path):
            out.append(
                _finding(F_CITE_MISSING, doc, rel, f"record {rel!r} does not exist")
            )
            continue
        record = _read_json(path, "record")
        try:
            rendered = _row_value(row, record)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
            out.append(
                _finding(
                    F_UNRESOLVED,
                    doc,
                    rel,
                    f"pointer {row['pointer']!r} unresolved: {exc}",
                )
            )
            continue
        if rendered != row["literal"]:
            out.append(
                _finding(
                    F_MISMATCH,
                    doc,
                    rel,
                    f"literal {row['literal']!r} but {row['pointer']!r} renders "
                    f"to {rendered!r}",
                )
            )
    return out


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def claims(
    root: str, docs: list[str], registry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Check every document in ``docs`` (glob patterns relative to ``root``)."""
    root = os.path.realpath(root)
    registry = registry or {"mapping": [], "claims": []}
    doc_paths = _expand_docs(root, docs)
    texts: dict[str, str] = {}
    for rel in doc_paths:
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as handle:
                texts[rel] = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise EvidenceClaimsError(f"cannot read doc {rel!r}: {exc}") from exc

    chains = _Chains(root, registry["mapping"])
    mapped_paths = [e["path"] for e in registry["mapping"]]
    findings: list[dict[str, str]] = []
    citations = 0
    for rel_doc, text in texts.items():
        historical = _historical_lines(text.splitlines())
        for line, cited in _citations(text, mapped_paths):
            citations += 1
            missing = _check_exists(root, cited, rel_doc)
            findings += missing
            if missing or line in historical or not chains.known(cited):
                continue
            newer = chains.newer(cited)
            if newer:
                findings.append(
                    _finding(
                        F_SUPERSEDED,
                        rel_doc,
                        cited,
                        f"line {line + 1} cites {cited!r}, superseded by "
                        f"{newer}; cite the current record or mark it "
                        f"{HISTORICAL_MARKER}",
                    )
                )
    findings += _check_rows(root, registry["claims"], texts)

    findings.sort(key=lambda f: (f["doc"], f["record"], f["code"], f["message"]))
    return {
        "schema_version": 1,
        "status": "findings" if findings else "clean",
        "docs_checked": len(texts),
        "citations_checked": citations,
        "claims_checked": len(registry["claims"]),
        "findings": findings,
    }
