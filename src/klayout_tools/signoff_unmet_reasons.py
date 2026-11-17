"""Roll-up of why a tier report's T1 items are ``unmet`` (issue #5903).

``klt signoff --manifest`` exits ``3`` for any unmet T1 item, so exit status
cannot tell a block that is mid-progress (``no_evidence``) from one whose pin
went stale (``stale_evidence``). Every item already carries ``reason``; this
module only totals them so CI can gate with one ``jq``, without iterating
``items[]``. Moved out of :mod:`.signoff` to keep that module under its size
ratchet.
"""

from __future__ import annotations

from typing import Any

#: Report keys that are a pure function of ``items[]``; ``--check`` leaves
#: them out of the comparison so a committed report rendered before the field
#: existed does not read as ``drifted`` on an upgrade alone.
DERIVED_REPORT_PATHS: frozenset[tuple[str, ...]] = frozenset({("unmet_reason_counts",)})


def unmet_reason_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    """``{"unmet_reason_counts": {reason: n}}`` over the T1 items whose
    ``status`` is ``"unmet"``, or ``{}`` when there is none -- spread into the
    report so one with no unmet T1 item stays byte-identical to before.

    T2-T4 ladder rows (``tier != "T1"``) and ``not_applicable`` items are not
    counted. Keys are sorted so the output is deterministic.
    """
    counts: dict[str, int] = {}
    for item in items:
        if item.get("tier") == "T1" and item.get("status") == "unmet":
            reason = str(item.get("reason"))
            counts[reason] = counts.get(reason, 0) + 1
    return {"unmet_reason_counts": dict(sorted(counts.items()))} if counts else {}
