"""Disclosed foundry-cell DRC exceptions for T1 item 3 (issue #4530).

``docs/design-evidence-tiers.md`` item 3 lets a ``klt drc`` report that is
not ``clean`` still back the item when every violation sits inside an
unmodified stock foundry cell, under three conditions: the violations
reproduce on the bare cell, the exception is pinned to the cell by content
hash with named rules and counts, and the claim discloses it.

A manifest evidence entry opts in with ``foundry_cell_exceptions``, a list of::

    {"cell": str, "cell_hash": "sha256:...", "rules": {rule_id: count},
     "bare_cell_report": path, "bare_cell_layout": path, "layout": path?}

Attribution limit: ``klt drc`` only attributes a violation to a cell
(``source_cell``) on engines with per-instance attribution; the RDB engine
reports ``source_cell: null``. Grading therefore compares the block's
per-rule counts to the summed exception counts (and, where a violation does
carry a ``source_cell``, requires it to be an excepted cell or lie beneath
one, i.e. an excepted cell appears in its ``source_path``). Paths resolve
against the working directory, like every other evidence path.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from ._provenance import cell_geometry_digest, sha256_file

#: Failure tokens, listed in ``failure_detail.foundry_cell_exceptions.lapsed``.
LAPSE_MALFORMED = "malformed_exception"
LAPSE_LAYOUT_UNREADABLE = "layout_unreadable"
LAPSE_CELL_HASH_MISMATCH = "cell_hash_mismatch"
LAPSE_BARE_LAYOUT_HASH_MISMATCH = "bare_cell_layout_hash_mismatch"
LAPSE_BARE_REPORT_UNREADABLE = "bare_cell_report_unreadable"
LAPSE_BARE_REPORT_STALE = "bare_cell_report_stale"
LAPSE_BARE_COUNT_MISMATCH = "bare_cell_counts_differ"
LAPSE_COUNT_MISMATCH = "block_counts_differ"
LAPSE_OUTSIDE_CELL = "violation_outside_excepted_cell"


def normalize_exceptions(raw: Any) -> list[dict[str, Any]] | None:
    """Validate the manifest's ``foundry_cell_exceptions`` value.

    ``[]`` when absent; ``None`` (a malformed entry) when present but not a
    list of well-formed records, so an opt-in never fails open."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        rules = item.get("rules")
        if not (
            isinstance(item.get("cell"), str)
            and isinstance(item.get("cell_hash"), str)
            and isinstance(item.get("bare_cell_report"), str)
            and isinstance(item.get("bare_cell_layout"), str)
            and isinstance(rules, dict)
            and rules
            and all(
                isinstance(k, str) and type(v) is int and v > 0
                for k, v in rules.items()
            )
            and isinstance(item.get("layout", ""), str)
        ):
            return None
        out.append(dict(item))
    return out


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _block_layout(envelope: dict[str, Any], exc: dict[str, Any]) -> str | None:
    if exc.get("layout"):
        return str(exc["layout"])
    file = envelope.get("file")
    if isinstance(file, dict):
        file = file.get("path")
    return file if isinstance(file, str) else None


def _check_one(envelope: dict[str, Any], exc: dict[str, Any]) -> list[str]:
    lapses: list[str] = []
    layout = _block_layout(envelope, exc)
    if cell_geometry_digest(layout, exc["cell"]) != exc["cell_hash"]:
        lapses.append(
            LAPSE_LAYOUT_UNREADABLE
            if not layout or not os.path.isfile(layout)
            else LAPSE_CELL_HASH_MISMATCH
        )
    if cell_geometry_digest(exc["bare_cell_layout"], exc["cell"]) != exc["cell_hash"]:
        lapses.append(LAPSE_BARE_LAYOUT_HASH_MISMATCH)
    bare = _read_json(exc["bare_cell_report"])
    if not isinstance(bare, dict) or bare.get("status") != "violations":
        lapses.append(LAPSE_BARE_REPORT_UNREADABLE)
        return lapses
    input_hash = ((bare.get("provenance") or {}).get("input") or {}).get("content_hash")
    actual = sha256_file(exc["bare_cell_layout"])
    if input_hash is None or actual is None or input_hash.split(":")[-1] != actual:
        lapses.append(LAPSE_BARE_REPORT_STALE)
    if bare.get("rule_counts") != exc["rules"]:
        lapses.append(LAPSE_BARE_COUNT_MISMATCH)
    return lapses


def evaluate(
    envelope: dict[str, Any], exceptions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Grade a ``klt drc`` ``violations`` envelope against ``exceptions``.

    Returns ``(applied, [])`` when every violation is covered, else
    ``(None, lapse_tokens)`` -- the exception lapses."""
    lapses: list[str] = []
    for exc in exceptions:
        lapses.extend(_check_one(envelope, exc))
    covered: Counter[str] = Counter()
    for exc in exceptions:
        covered.update(exc["rules"])
    if dict(covered) != envelope.get("rule_counts"):
        lapses.append(LAPSE_COUNT_MISMATCH)
    cells = {exc["cell"] for exc in exceptions}
    for violation in envelope.get("violations") or []:
        source = violation.get("source_cell") if isinstance(violation, dict) else None
        if source is None:
            continue
        path = violation.get("source_path")
        chain = {source, *(path if isinstance(path, list) else [])}
        if not any(isinstance(c, str) and c in cells for c in chain):
            lapses.append(LAPSE_OUTSIDE_CELL)
            break
    if lapses:
        return None, sorted(set(lapses))
    return [
        {k: exc[k] for k in ("cell", "cell_hash", "rules", "bare_cell_report")}
        for exc in exceptions
    ], []
