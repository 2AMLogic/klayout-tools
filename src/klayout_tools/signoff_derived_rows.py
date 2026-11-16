"""Where T1 item 5's spec-row coverage came from, and the manifest option
that requires it be machine-checked.

Two issues live here, moved out of :mod:`.signoff` to keep that module under
its size ratchet:

``derived`` vs ``asserted`` (issue #3778)
    A ``klt sim`` measurement may declare the spec row it grades
    (``measurements[].spec_row``). When a citation carries ``rows`` *and* its
    envelope tags any measurement, the citation is credited only with the
    rows some tagged, ``limits``-bearing, passing measurement grades --
    coverage *derived* from the artifact. A citation whose envelope has no
    tags at all keeps the manifest's ``rows`` on the manifest's word alone --
    coverage *asserted*. :func:`multi_row_info` reports the split per row
    under ``row_sources``.

``require_derived_rows`` (issue #5462)
    ``row_sources`` is report-only by itself, so an item whose coverage is
    entirely asserted grades exactly like a fully tagged one. A manifest that
    has finished tagging its envelopes can set the top-level boolean
    ``"require_derived_rows": true`` to ratchet: an asserted row then stops
    counting as coverage, and the item grades ``unmet`` /
    ``partial_coverage`` with the rows listed under ``detail.asserted_rows``.
    Off by default, so existing manifests grade unchanged.
"""

from __future__ import annotations

from typing import Any

#: The one T1 item whose row coverage ``require_derived_rows`` ratchets. The
#: manifest key is top-level, but item 6 reuses the same list form and is
#: deliberately left alone -- see the issue's scope.
DERIVED_ROWS_ITEM_ID = 5


def read_require_derived_rows(manifest: dict[str, Any]) -> tuple[bool, bool]:
    """``(require, invalid)`` for a block manifest's optional top-level
    ``require_derived_rows``.

    Absent, ``null`` or ``false`` is ``(False, False)`` -- grading is exactly
    what it was before the key existed, and no new report key is emitted. A
    non-boolean value is ``(False, True)``, which degrades item 5 alone to
    ``invalid_evidence``; like a malformed ``spec_rows`` it never raises, so
    one bad key cannot fail the whole report.
    """
    raw = manifest.get("require_derived_rows")
    if raw is None:
        return False, False
    if not isinstance(raw, bool):
        return False, True
    return raw, False


def asserted_rows(row_info: dict[str, Any]) -> list[str]:
    """The covered rows :func:`multi_row_info` credited to the manifest's
    word alone, sorted. Empty when nothing was declared or every covered row
    was derived."""
    sources = row_info.get("row_sources") or {}
    return sorted(row for row, source in sources.items() if source == "asserted")


def envelope_tagged_rows(envelope: dict[str, Any]) -> tuple[bool, set[str]]:
    """``(has_tags, covering)`` for a ``klt sim`` envelope (issue #3778):
    whether any ``measurements[]`` entry declares a ``spec_row``, and the
    rows with a passing measurement that is tagged with them and has
    ``limits`` (a tag without limits grades nothing)."""
    raw = envelope.get("measurements")
    entries = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    tagged = [
        m for m in entries if isinstance(m.get("spec_row"), str) and m["spec_row"]
    ]
    covering = {
        m["spec_row"] for m in tagged if m.get("status") == "pass" and m.get("limits")
    }
    return bool(tagged), covering


def credited_rows(
    rows: list[str] | None, envelope: dict[str, Any] | None
) -> tuple[list[str] | None, bool]:
    """The rows a ``met`` member is credited with and whether they were
    ``derived`` from its envelope's ``spec_row`` tags (issue #3778). Untagged
    (or unavailable) envelopes keep the manifest-asserted ``rows``."""
    if not rows or envelope is None:
        return rows, False
    has_tags, covering = envelope_tagged_rows(envelope)
    if not has_tags:
        return rows, False
    return [r for r in rows if r in covering], True


def multi_row_info(
    graded: list[Any],
    failed: list[Any],
    spec_rows: list[dict[str, Any]] | None,
    credited: dict[int, tuple[list[str] | None, bool]] | None = None,
) -> dict[str, Any]:
    """``failed_rows`` / ``uncovered_rows`` / ``declared_gaps`` /
    ``unratified_rows`` for the declared ``spec_rows`` (empty when none were
    declared). ``unratified_rows`` lists every non-gap row declared
    ``ratified: false``, covered or not."""
    if spec_rows is None:
        return {}
    credited = credited or {}
    covered: set[str] = set()
    row_sources: dict[str, str] = {}
    for spec, rows, g in graded:
        if g[0] != "met":
            continue
        member_rows, derived = credited.get(id(spec), (rows, False))
        for r in member_rows or []:
            covered.add(r)
            # A row covered by any derived citation is machine-checked.
            if derived or r not in row_sources:
                row_sources[r] = "derived" if derived else "asserted"
    failed_rows = sorted({r for _, rows, _ in failed for r in rows or []} - covered)
    return {
        "failed_rows": failed_rows,
        "uncovered_rows": [
            row["id"]
            for row in spec_rows
            if row["gap"] is None
            and row["id"] not in covered
            and row["id"] not in failed_rows
        ],
        "declared_gaps": [
            {"id": row["id"], "gap": row["gap"]}
            for row in spec_rows
            if row["gap"] is not None
        ],
        "unratified_rows": [
            row["id"] for row in spec_rows if row["gap"] is None and not row["ratified"]
        ],
        "row_sources": {r: row_sources[r] for r in sorted(row_sources)},
    }
