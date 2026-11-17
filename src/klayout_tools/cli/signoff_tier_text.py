"""Text renderers for a tier report's summary dispositions.

Moved out of :mod:`.cli.signoff_cmd` to keep that module under its size
ratchet, following the same precedent as the ``signoff_*`` module splits:
the "not applicable" (issue #2865) and justified declared-gap (issue #5599)
renderers -- the count suffixes on the ``T1: ...`` summary line and the
per-item evidence listings -- live here. Both dispositions render beside an
item that is still ``unmet``; neither ever reads as ``met``.
"""

from __future__ import annotations

from ..signoff_justified_gap import FINDING_JUSTIFIED_GAP_WITHOUT_ISSUE_LINK


def _not_applicable_suffix(report: dict) -> str:
    """``", N not_applicable"`` when the report has items satisfied by a
    recorded "not applicable" (issue #2865); empty otherwise, so a report
    with none renders exactly as before."""
    count = report.get("t1_not_applicable_count")
    return f", {count} not_applicable" if count else ""


def _justified_gap_suffix(report: dict) -> str:
    """``", N justified gaps"`` when the report declares justified gaps
    (issue #5599); empty otherwise, so a report declaring none renders
    exactly as before -- "N met, M justified gaps, K unmet" readable at a
    glance."""
    count = report.get("t1_justified_gap_count")
    return f", {count} justified gaps" if count else ""


def _print_not_applicable(item: dict) -> None:
    """The evidence behind a ``not_applicable`` item (issue #2865): the
    ratified spec and the committed statement, each with its content hash,
    and the statement's per-row reasons."""
    block = item.get("not_applicable")
    if not block:
        return
    spec = block["spec"]
    statement = block["statement"]
    print(
        f"        not applicable: spec {spec['file']} "
        f"(status={spec['status']}, rows={spec['row_count']}, "
        f"content_hash={spec['content_hash']})"
    )
    print(
        f"        statement: {statement['file']} "
        f"(content_hash={statement['content_hash']})"
    )
    for row in statement["rows"]:
        print(f"          {row['id']} [{row['class']}]: {row['reason']}")


def _print_justified_gaps(item: dict) -> None:
    """Each justified declared gap with its evidence (issue #5599): the
    human reason, the hash-bound rationale artifact, and the issue that
    would close it -- with the missing-link finding inline when no issue is
    recorded (operator ruling on #5858, 2026-11-17)."""
    for gap in (item.get("detail") or {}).get("justified_gaps") or []:
        print(f"        justified gap: {gap['id']} [{gap['reason']}]")
        print(
            f"          rationale: {gap['rationale']['file']} "
            f"(content_hash={gap['rationale']['content_hash']})"
        )
        if gap["issue"]:
            print(f"          issue: {gap['issue']}")
        else:
            print(
                f"          finding: {FINDING_JUSTIFIED_GAP_WITHOUT_ISSUE_LINK} "
                "-- no issue names what would close this gap"
            )
