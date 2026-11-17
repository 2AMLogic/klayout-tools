"""Merge-friendly attestation records for ``klt signoff --attest`` (issue #5945).

The classic attestation envelope rewrites aggregates on every change to any
covered file (``summary``/``check_count``/``passed_count``, the positional
``checks[]`` array, item 1's combined ``provenance.input.content_hash``), so
two PRs that touch *different* covered files still collide textually. **Record
format 2** (opt in with ``--attest ... --records FILE``) splits the committed
evidence in two:

* a **records file** -- NDJSON, one line per covered file, sorted by UTF-8
  path bytes: ``{"path": ..., "role": ..., "sha256": "<hex>"}`` (a derived
  artifact's line also carries ``"sources": {path: hex}``). A PR that changes a
  covered file changes exactly that file's line, so PRs on disjoint
  (non-adjacent) files merge with plain ``git merge``;
* the **judgment envelope** -- the ordinary generic envelope with the
  aggregates and per-file ``present`` checks removed: nothing a content edit
  rewrites. ``provenance.input.content_hash`` (what the manifest pins) is the
  hash of the *inventory*, the append-only judgment record (what was audited,
  by which commands, against which pins); ``provenance.input.records`` names
  the records file.

Counts and the summary are recomputed by every ``--attest``/``--check`` run
and shown in its live output, never committed. The gate is unchanged: a
covered file whose line was not renewed fails ``--check`` (a changed, missing
or extra line is drift) and, in the manifest grader, makes the citation stale.
Importing nothing from the rest of the package keeps it usable from both the
producer and the grader.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: ``attestation.record_format`` of an envelope whose per-file hashes live in a
#: records file.
RECORD_FORMAT = 2

_HEX = re.compile(r"[0-9a-f]{64}")
_COUNTS = re.compile(r": \d+/\d+ attestation checks")


def relative_path(records: str, root: str) -> str:
    """``records`` (a filesystem path) as a ``/``-separated path relative to
    the repository ``root``; refused (``ValueError``) outside it."""
    rel = os.path.relpath(os.path.realpath(os.path.abspath(records)), root)
    rel = rel.replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        raise ValueError(f"records file '{records}' is outside the repository")
    return rel


def input_binding(
    inventory_rel: str, inventory_hash: str, records_rel: str
) -> dict[str, Any]:
    """``provenance.input`` for record format 2: the pin is the inventory's
    hash, so it only changes when the judgment record does."""
    return {
        "path": {"path": inventory_rel, "scope": "repo"},
        "content_hash": inventory_hash,
        "records": {"path": {"path": records_rel, "scope": "repo"}},
    }


def files_from_checks(
    checks: Sequence[Mapping[str, Any]], derived: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The record lines for the passing ``present`` checks in ``checks``,
    sorted by path bytes. A derived artifact built from declared sources also
    records those sources' hashes (so renewing a source renews its line)."""
    hashes = {
        check["path"]: check["content_hash"].removeprefix("sha256:")
        for check in checks
        if check.get("check") == "present" and check.get("content_hash")
    }
    declared = {
        entry["path"]: entry["recorded"]["sources"]
        for entry in derived
        if "recorded" in entry
    }
    lines: dict[str, dict[str, Any]] = {}
    for check in checks:
        path = check.get("path")
        if check.get("check") != "present" or not isinstance(path, str):
            continue
        if path not in hashes or path in lines:
            continue
        line: dict[str, Any] = {
            "path": path,
            "role": check["role"],
            "sha256": hashes[path],
        }
        if path in declared:
            line["sources"] = {s: hashes[s] for s in declared[path] if s in hashes}
        lines[path] = line
    return [lines[p] for p in sorted(lines, key=lambda value: value.encode("utf-8"))]


def render_records(lines: Sequence[Mapping[str, Any]]) -> str:
    """The exact bytes of a records file: one compact, key-sorted JSON object
    per line, sorted by path bytes, trailing newline (empty for no lines)."""
    ordered = sorted(lines, key=lambda line: line["path"].encode("utf-8"))
    return "".join(
        json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n"
        for line in ordered
    )


def parse_records(text: str) -> list[dict[str, Any]] | None:
    """The lines of a records file, or ``None`` when it is malformed: not
    JSON objects, a missing/ill-typed ``path``/``role``/``sha256``, a
    non-hex digest, or a path listed twice. Blank lines are ignored."""
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(line, dict):
            return None
        path, role, digest = line.get("path"), line.get("role"), line.get("sha256")
        if not (isinstance(path, str) and path and isinstance(role, str) and role):
            return None
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            return None
        sources = line.get("sources", {})
        if not isinstance(sources, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _HEX.fullmatch(v)
            for k, v in sources.items()
        ):
            return None
        if path in seen:
            return None
        seen.add(path)
        lines.append(line)
    return lines


def write_records(lines: Sequence[Mapping[str, Any]], output: str) -> bool:
    """Write the records file **only if its bytes would change**; return
    whether it was written."""
    rendered = render_records(lines).encode("utf-8")
    try:
        with open(output, "rb") as handle:
            if handle.read() == rendered:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "wb") as handle:
        handle.write(rendered)
    return True


def committed_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """What is committed of a live record-format result: the envelope without
    the live-only keys, the counts (``summary``, ``check_count``,
    ``passed_count``) and the per-file ``present`` checks (their hashes live in
    the records file)."""
    view = {k: v for k, v in result.items() if k not in ("skipped_checks", "records")}
    attestation = {
        k: v
        for k, v in result["attestation"].items()
        if k not in ("check_count", "passed_count")
    }
    attestation["checks"] = [
        check for check in attestation["checks"] if check.get("check") != "present"
    ]
    view["attestation"] = attestation
    view["summary"] = _COUNTS.sub(": attestation checks", result["summary"])
    return view


def committed_records_ref(committed: Mapping[str, Any]) -> str | None:
    """The repo-relative records path a committed envelope names, ``None`` for
    a classic (record format 1) envelope."""
    attestation = committed.get("attestation")
    if not isinstance(attestation, Mapping) or "record_format" not in attestation:
        return None
    if attestation["record_format"] != RECORD_FORMAT:
        raise ValueError(
            f"committed envelope declares record_format "
            f"{attestation['record_format']!r}; this build reads {RECORD_FORMAT}"
        )
    input_block = (committed.get("provenance") or {}).get("input") or {}
    ref = (input_block.get("records") or {}).get("path")
    name = ref.get("path") if isinstance(ref, Mapping) else None
    if not isinstance(name, str) or not name:
        raise ValueError("committed record-format envelope names no records file")
    return name


def diff_records(
    committed_text: str | None,
    fresh_lines: Sequence[Mapping[str, Any]],
    records_rel: str,
) -> list[dict[str, Any]]:
    """``--check`` drift entries for the records file: one per changed, missing
    (committed but no longer covered) or extra (covered but not recorded) line,
    shaped like the envelope's own ``drift[]`` entries and naming the file via
    ``check``. A canonical-form difference alone is one ``format`` entry."""
    committed_lines = parse_records(committed_text) if committed_text else []
    if committed_lines is None or committed_text is None:
        return [
            {
                "field": f"records.{records_rel}",
                "committed": "unreadable" if committed_text else "missing",
                "fresh": "canonical records",
            }
        ]
    old = {line["path"]: line for line in committed_lines}
    new = {line["path"]: line for line in fresh_lines}
    drift: list[dict[str, Any]] = []
    for path in sorted(set(old) | set(new), key=lambda value: value.encode("utf-8")):
        before, after = old.get(path), new.get(path)
        for key in ("sha256", "role", "sources"):
            a = before.get(key) if before else None
            b = after.get(key) if after else None
            if a != b:
                drift.append(
                    {
                        "field": f"records.{path}.{key}",
                        "committed": a,
                        "fresh": b,
                        "check": {
                            "role": (after or before or {})["role"],
                            "path": path,
                        },
                    }
                )
    if not drift and committed_text != render_records(fresh_lines):
        drift.append(
            {
                "field": f"records.{records_rel}",
                "committed": "non-canonical bytes",
                "fresh": "sorted, compact, one line per file",
            }
        )
    return drift


def fold_records(
    envelope: Mapping[str, Any],
    spec: Mapping[str, Any],
    file_label: str | None,
    input_verified: bool | None,
    repo_root: Any,
    verify: Callable[..., list[dict[str, Any]]],
    resolve: Callable[..., list[str]],
) -> tuple[list[dict[str, Any]] | None, bool | None]:
    """Grader side: verify a record-format envelope's records file and fold
    the per-line statuses into ``input_verified`` (``(None, input_verified)``
    for an envelope with no ``provenance.input.records``).

    Every recorded file is re-hashed through ``verify`` (the grader's
    :func:`_verify_generic_input_set`, so ``stale`` / ``unverifiable`` mean
    exactly what they do for an item-1 input set). An unreadable, malformed or
    empty records file is ``unverifiable`` (``None``), never a pass; a stale
    line is ``False``. Whether the file set is *complete* is ``--check``'s
    job: the grader holds no inventory semantics."""
    provenance = envelope.get("provenance")
    input_block = provenance.get("input") if isinstance(provenance, Mapping) else None
    if not isinstance(input_block, Mapping) or "records" not in input_block:
        return None, input_verified
    ref = input_block["records"]
    value = ref.get("path") if isinstance(ref, Mapping) else None
    evidence_dir = os.path.dirname(os.path.abspath(file_label)) if file_label else None
    lines: list[dict[str, Any]] | None = None
    for candidate in resolve(value, spec, evidence_dir, repo_root):
        try:
            with open(candidate, encoding="utf-8") as handle:
                lines = parse_records(handle.read())
        except (OSError, UnicodeDecodeError):
            continue
        break
    if not lines:
        return None, None if input_verified is not False else False
    members = [
        ({"path": line["path"], "scope": "repo"}, f"sha256:{line['sha256']}")
        for line in lines
    ]
    results = verify(members, spec, file_label, repo_root)
    statuses = {member["status"] for member in results}
    if input_verified is False or "stale" in statuses:
        return results, False
    return results, input_verified if statuses == {"verified"} else None
