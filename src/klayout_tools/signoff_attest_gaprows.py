"""Declared gap rows for ``klt signoff --attest`` (issues #4848, #5856).

Item 9's ``unbenched_rows`` and item 8's ``uncited_rows`` declare the same
thing: a spec row the inventory says is intentionally not yet covered -- no
testbench, no citation -- together with the issue that owns closing the gap.
Both surface as one *passing* check, so the coverage boundary stays visible
to the grader instead of either failing the attestation or hiding the row by
dropping it from the inventory.

The shape validator and the check builders are shared here so the two items
cannot drift (issue #5856 generalised item 9's helpers); an item's own
policy -- item 8 cross-checks each row against ``spec_rows`` and
``citations`` -- stays with the item. ``AttestationError`` is imported
lazily, the same way :mod:`klayout_tools.signoff_attest_item9` does it:
``signoff_attest`` imports this module's users.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._jsonnum import is_int


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def validated_gap_rows(
    inventory: Mapping[str, Any], key: str, adjective: str
) -> list[dict[str, Any]]:
    """``key`` (optional inventory field): ``[{"row": str, "issue": int}]`` --
    each entry a declared gap row plus the issue that owns closing it. The
    messages name ``key`` and ``adjective``, so delegating callers keep
    byte-identical error text."""
    raw = inventory.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _error(f"'{key}' must be a list of {{row, issue}}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        field = f"{key}[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"row", "issue"}:
            raise _error(f"'{field}' must be an object with 'row' and 'issue'")
        row, issue = entry["row"], entry["issue"]
        if not isinstance(row, str) or not row.strip():
            raise _error(f"'{field}.row' must be a non-empty string")
        if not is_int(issue) or issue <= 0:
            raise _error(f"'{field}.issue' must be a positive integer")
        if row in seen:
            raise _error(f"{adjective} row '{row}' is declared more than once")
        seen.add(row)
        rows.append({"row": row, "issue": issue})
    return rows


def gap_row_check(entry: Mapping[str, Any], check_name: str) -> dict[str, Any]:
    """One visible, **passing** check for a declared gap row: the declaration
    is the point -- the gap and its owner issue are surfaced, not failed."""
    return {
        "check": check_name,
        "role": "row",
        "row": entry["row"],
        "issue": entry["issue"],
        "passed": True,
    }


def gap_row_checks(
    rows: Sequence[Mapping[str, Any]], check_name: str
) -> list[dict[str, Any]]:
    """One visible, passing check per declared gap row (sorted by row id)."""
    return [
        gap_row_check(entry, check_name)
        for entry in sorted(rows, key=lambda e: e["row"])
    ]
