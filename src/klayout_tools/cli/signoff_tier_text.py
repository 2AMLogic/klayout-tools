"""Text renderers for a tier report's citations and summary dispositions.

Moved out of :mod:`.cli.signoff_cmd` to keep that module under its size
ratchet, following the same precedent as the ``signoff_*`` module splits:
the not-applicable, justified-gap, structural-row and composition renderers
live here. The tier summary and per-item listings distinguish these
dispositions from met items. Composed rows use ``[COMP ]`` and name the
sub-blocks that supply their evidence.
"""

from __future__ import annotations

from ..signoff_composition import STATUS_MET_BY_COMPOSITION
from ..signoff_evidence_pin import CONTENT_HASH_BASIS_EVIDENCE_FILE
from ..signoff_justified_gap import FINDING_JUSTIFIED_GAP_WITHOUT_ISSUE_LINK
from ..signoff_store_pin import CONTENT_HASH_BASIS_ARTIFACT_MANIFEST
from .color import Palette


def input_verified_statement(citation: dict) -> str | None:
    """Describe the hash source and whether the grader read its bytes."""
    verified = citation.get("input_verified")
    if verified is None and citation.get("content_hash") is None:
        return None
    basis = citation.get("content_hash_basis")
    if basis == CONTENT_HASH_BASIS_ARTIFACT_MANIFEST:
        return (
            "used the sha256 recorded for the member in the committed store manifest "
            "-- the artifact itself was not read"
        )
    if basis == CONTENT_HASH_BASIS_EVIDENCE_FILE:
        return "re-hashed the evidence file itself -- matches"
    if verified is True:
        return "re-hashed the artifact this envelope names -- matches"
    if verified is False:
        return (
            "CHANGED -- the artifact this envelope names no longer matches "
            "the content_hash it recorded"
        )
    return (
        "not re-hashed (content_hash compared against this envelope's own "
        "claim only -- the artifact itself was not read)"
    )


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


def _composed_suffix(report: dict) -> str:
    """``", N composed (parent boundary uncovered)"`` when the report has
    rows met by composition (issue #6927); empty otherwise, so a report
    that composes nothing renders exactly as before. The parenthetical is
    the point of the count: composition is visible evidence, never T1 --
    the parent's own boundary is exactly what those rows do not cover."""
    count = report.get("t1_composed_count")
    return f", {count} composed (parent boundary uncovered)" if count else ""


def status_marker(status: str, palette: Palette) -> tuple[str, str]:
    """The ``(marker, color)`` for an item ``status`` in the text report.

    Moved here from ``signoff_cmd.py`` (issue #6927) so the new
    ``met_by_composition`` status -- yellow, never the plain MET green --
    lives beside the other per-disposition renderers in this module.
    """
    if status == STATUS_MET_BY_COMPOSITION:
        return "COMP ", palette.yellow
    if status == "met":
        return "MET  ", palette.green
    if status == "not_applicable":
        return "N/A  ", palette.green
    return "UNMET", palette.red


def _print_composed_from(item: dict) -> None:
    """Print the sub-block evidence for a composed or partly composed row."""
    if item["status"] == STATUS_MET_BY_COMPOSITION:
        label = "composed from"
        entries = item.get("composed_from") or []
    elif item["status"] == "unmet":
        label = "sub-block"
        entries = (item.get("detail") or {}).get("composition", [])
    else:
        return
    for entry in entries:
        source = entry["source"] if entry["source"] is not None else "<inline>"
        note = (
            f", content_hash={entry['content_hash']}"
            if entry.get("content_hash")
            else ""
        )
        print(
            f"        {label}: {entry['block']} "
            f"(source={source}, status={entry['status']}{note})"
        )


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


def _print_partition_boundary(result: dict) -> None:
    """Print a `"mixed-signal"` manifest's declared partition boundary (issue
    #2278) under the `block:`/`kind:` header, or nothing when it declared
    none.

    Printed **once per declared partition**, not on each of that partition's
    eleven rows: unlike `coverage`/`body_bias` (per-citation facts, shown
    beside the citation they qualify), the boundary is one constant statement
    per partition, and restating it eleven times would bury the checklist it
    qualifies. The JSON carries it per row as well, where a consumer
    filtering `.items[]` needs each row to be self-contained.

    Absent entirely when nothing was declared -- "no statement" must not read
    as "the partitions were defined" -- exactly as an undeclared boundary is
    absent from the JSON rather than rendered `null`.
    """
    boundary = result.get("partition_boundary") or {}
    if not boundary:
        return
    print("partition boundary:")
    for partition, text in boundary.items():
        print(f"  {partition}: {text}")


def print_upstream_source(result: dict, label: str) -> None:
    """Print the manifest's upstream repository and commit when declared."""
    upstream = result.get("upstream_source")
    if upstream:
        print(f"{label}: {upstream['repository']} @ {upstream['commit']}")


def _print_structural_rows(item: dict) -> None:
    """Each structurally dispositioned row with its evidence (issue #6985):
    the claimed fact and the hash-bound artifact that evidences it. The
    hash pin proves the artifact is unchanged, not that the property holds
    -- ``klt`` records and freshness-pins it, it does not execute any
    check."""
    for row in (item.get("detail") or {}).get("structural_rows") or []:
        print(f"        structural: {row['id']} [{row['claim']}]")
        print(
            f"          artifact: {row['artifact']['file']} "
            f"(content_hash={row['artifact']['content_hash']})"
        )
