"""Member coverage counts for per-cell DRC/LVS evidence lists (issue #5031).

A deliverable that is a library of independently laid-out cells has one
``klt drc`` / ``klt lvs`` report per cell. T1 items 3 (DRC) and 4 (LVS)
accept a *list* of such reports (all must pass -- see
``signoff._grade_multi_citation``); this module only adds the additive
``members_covered`` / ``members_total`` fields so the report states how many
members were cited and how many passed, instead of a bare ``met`` that could
rest on a single member.

Single (non-list) entries never reach here, so their output is unchanged.
"""

from __future__ import annotations

from typing import Any

#: T1 items whose list form is a per-cell report list: DRC and LVS.
MEMBER_COUNT_ITEMS: frozenset[int] = frozenset({3, 4})


def member_coverage(raw_entry: Any, extra: dict[str, Any]) -> dict[str, int]:
    """``{"members_covered": n, "members_total": m}`` for a list-form entry.

    ``members_total`` is the number of cited members; ``members_covered`` is
    how many of them individually graded ``met`` (``0`` when the list was
    rejected outright as malformed, since no member was then graded).
    """
    graded = extra.get("citations_graded", [])
    return {
        "members_covered": sum(1 for g in graded if g.get("status") == "met"),
        "members_total": len(raw_entry) if isinstance(raw_entry, list) else 1,
    }


def _entry_problem(raw: Any) -> str:
    """Human-readable cause for an evidence entry
    :func:`_normalize_evidence_entry` rejects (issue #4562)."""
    if not isinstance(raw, (str, dict)):
        return "not a string or an object with 'file'/'command'"
    if isinstance(raw, dict) and not isinstance(raw.get("file"), str):
        command = raw.get("command")
        if not (
            isinstance(command, list)
            and command
            and all(isinstance(part, str) for part in command)
        ):
            return "object has neither a string 'file' nor a non-empty 'command' list"
    return "invalid 'pointer' or option field"
