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

A ``reported`` yield campaign (issue #5490)
    A `klt yield` measurement that declares no ``target_yield`` has
    ``status: "reported"`` -- it made no claim, so nothing could fail -- and
    the ``pass``-only reader above credited it with nothing, so a row
    honestly declared ``ratified: false`` and cited with a real campaign
    read as unmeasured. Such a measurement now covers its row **when the row
    is unratified**, which is exactly the row for which declaring a
    ``target_yield`` would mean inventing a claim. See
    :func:`envelope_tagged_rows` for why it is confined to unratified rows
    and keyed on the citation's ``yield`` kind.

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


def envelope_tagged_rows(
    envelope: dict[str, Any],
    *,
    kind: str | None = None,
    unratified: frozenset[str] = frozenset(),
) -> tuple[bool, set[str]]:
    """``(has_tags, covering)`` for a ``klt sim`` envelope (issue #3778):
    whether any ``measurements[]`` entry declares a ``spec_row``, and the
    rows with a passing measurement that is tagged with them and has
    ``limits`` (a tag without limits grades nothing).

    ``kind``/``unratified`` carry the issue-#5490 case: on a ``yield``
    envelope a tagged, ``limits``-bearing measurement whose status is
    ``"reported"`` -- the token `klt yield` gives a measurement declaring no
    ``target_yield`` (docs/cli/yield.md), so nothing could fail -- also
    covers its row, but **only** when that row is declared
    ``ratified: false``. It is confined to unratified rows because such an
    item is *already* ``unmet``/``partial_coverage`` through
    ``unratified_rows``: only the ``detail`` lists move, and no item verdict
    changes. Crediting a ratified row from a no-claim campaign would instead
    loosen the #3778 gate.

    Keyed on the citation's ``yield`` kind rather than on ``limits`` alone so
    item 5 cannot move: `klt sim` grades an unlimited measurement ``"pass"``
    and never emits ``"reported"`` at all (``sim._evaluate_limits``), but a
    ``model-run`` envelope is hand-authored and could carry any token.
    """
    raw = envelope.get("measurements")
    entries = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    tagged = [
        m for m in entries if isinstance(m.get("spec_row"), str) and m["spec_row"]
    ]
    covering = {
        m["spec_row"]
        for m in tagged
        if m.get("limits")
        and (
            m.get("status") == "pass"
            or (
                kind == "yield"
                and m.get("status") == "reported"
                and m["spec_row"] in unratified
            )
        )
    }
    return bool(tagged), covering


def credited_rows(
    rows: list[str] | None,
    envelope: dict[str, Any] | None,
    *,
    kind: str | None = None,
    unratified: frozenset[str] = frozenset(),
) -> tuple[list[str] | None, bool]:
    """The rows a ``met`` member is credited with and whether they were
    ``derived`` from its envelope's ``spec_row`` tags (issue #3778). Untagged
    (or unavailable) envelopes keep the manifest-asserted ``rows``.
    ``kind``/``unratified`` are forwarded to :func:`envelope_tagged_rows` for
    its issue-#5490 ``reported`` case."""
    if not rows or envelope is None:
        return rows, False
    has_tags, covering = envelope_tagged_rows(
        envelope, kind=kind, unratified=unratified
    )
    if not has_tags:
        return rows, False
    return [r for r in rows if r in covering], True


def member_credited_rows(
    graded: list[Any],
    met_envelopes: dict[int, dict[str, Any]] | None,
    spec_rows: list[dict[str, Any]] | None,
) -> dict[int, tuple[list[str] | None, bool]]:
    """:func:`credited_rows` for every member of a graded multi-citation
    entry, keyed by ``id()`` of the member's citation spec -- the mapping
    :func:`multi_row_info` reads.

    Each member is credited against its own citation ``kind`` and the set of
    rows declared ``ratified: false``, which is what lets a ``reported``
    yield measurement cover an unratified row (issue #5490). That set is the
    same one :func:`multi_row_info` renders as ``unratified_rows``.
    """
    unratified = frozenset(
        row["id"]
        for row in (spec_rows or [])
        if row["gap"] is None and not row["ratified"]
    )
    return {
        id(spec): credited_rows(
            rows,
            (met_envelopes or {}).get(id(spec)),
            kind=(g[2] or {}).get("kind"),
            unratified=unratified,
        )
        for spec, rows, g in graded
    }


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
