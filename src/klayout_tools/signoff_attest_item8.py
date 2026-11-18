"""Item 8 (characterization report) row-level pieces of ``--attest``
(issue #5856): the accepted inventory keys, the ``uncited_rows`` gap
declaration, and the per-spec-row checks. Split out of
:mod:`klayout_tools.signoff_attest` (file-size ratchet), mirroring
:mod:`klayout_tools.signoff_attest_item9`; the shared gap-row shape lives in
:mod:`klayout_tools.signoff_attest_gaprows`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import signoff_attest_gaprows as gaprows

#: Item 8's accepted inventory keys (``signoff_attest._ITEM_KEYS[8]``).
ITEM8_KEYS = frozenset(
    {
        "report",
        "spec_rows",
        "citations",
        "envelope_checks",
        "corners",
        "metrics",
        "uncited_rows",
    }
)


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def validated_uncited_rows(
    inventory: Mapping[str, Any],
    spec_rows: Sequence[str],
    citations: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    """Item 8's ``uncited_rows``: ``[{"row": str, "issue": int}]``
    (optional), the counterpart of item 9's ``unbenched_rows`` -- a declared
    spec row that intentionally carries no citation yet, with the issue that
    owns closing the gap. The row must be declared in ``spec_rows`` and must
    have no citation: a gap declaration that coexists with evidence, or
    names an unknown row, is a malformed inventory (exit ``1``), not a
    failed check."""
    rows = gaprows.validated_gap_rows(inventory, "uncited_rows", "uncited")
    declared = set(spec_rows)
    for entry in rows:
        row = entry["row"]
        if row not in declared:
            raise _error(
                f"'uncited_rows' names row '{row}', which 'spec_rows' does not declare"
            )
        if citations.get(row):
            raise _error(
                f"'uncited_rows' names row '{row}', which 'citations' cites -- a "
                "declared gap must not coexist with evidence"
            )
    return rows


def row_checks(
    normalized: Mapping[str, Any], present: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Item 8's per-spec-row checks, sorted by row id: a ``citation`` check
    passing iff the row cites at least one present envelope -- except a row
    declared in ``uncited_rows``, which emits its passing ``uncited_row``
    check instead of a failing ``citation`` (issue #5856): the gap and its
    owner issue stay visible to the grader rather than the row disappearing
    from ``spec_rows``."""
    uncited = {entry["row"]: entry for entry in normalized["uncited_rows"]}
    checks: list[dict[str, Any]] = []
    for row in sorted(normalized["spec_rows"]):
        if row in uncited:
            checks.append(gaprows.gap_row_check(uncited[row], "uncited_row"))
            continue
        paths = normalized["citations"][row]
        checks.append(
            {
                "check": "citation",
                "role": "spec_row",
                "row": row,
                "envelopes": paths,
                "passed": bool(paths) and all(present[p]["passed"] for p in paths),
            }
        )
    return checks
