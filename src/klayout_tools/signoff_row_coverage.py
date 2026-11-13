"""Spec-row coverage for a multi-citation T1 item (issues #3778, #2908, #5490).

``signoff._grade_multi_citation`` aggregates the members of a list-form
evidence entry; this module answers the one question that aggregation needs
and that is worth reading on its own -- **which declared spec rows did the
cited envelopes actually cover, and on whose word**.

Three steps, in call order:

- :func:`_envelope_tagged_rows` reads one envelope's
  ``measurements[].spec_row`` tags (issue #3778) and returns the rows some
  tagged measurement actually grades;
- :func:`_credited_rows` turns that into the rows one ``met`` member is
  credited with, falling back to the manifest-asserted ``rows`` for an
  untagged envelope;
- :func:`_multi_row_info` rolls the credited rows of every member up into the
  ``failed_rows`` / ``uncovered_rows`` / ``declared_gaps`` /
  ``unratified_rows`` / ``row_sources`` block the item renders as
  ``detail`` (when unmet) or ``row_coverage`` (when met).

Split out of ``signoff.py`` unchanged in behaviour; the names keep their
leading underscore because they remain internal to the signoff package.
"""

from __future__ import annotations

from typing import Any


def _envelope_tagged_rows(
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
    ``ratified: false``. The row an unratified spec names is precisely the
    row for which declaring a ``target_yield`` would be inventing a claim,
    so requiring one to register coverage pushed manifest authors into the
    number-invention ``ratified: false`` (issue #2908) exists to avoid. It
    is confined to unratified rows because such an item is *already*
    ``unmet``/``partial_coverage`` through ``unratified_rows``: only the
    ``detail`` lists move, and no item verdict changes. Crediting a
    ratified row from a no-claim campaign would instead loosen the #3778
    gate.

    Keyed on the ``yield`` kind rather than on ``limits`` alone so item 5
    cannot move: `klt sim` grades an unlimited measurement ``"pass"`` and
    never emits ``"reported"`` at all (``sim._evaluate_limits``), but a
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


def _credited_rows(
    rows: list[str] | None,
    envelope: dict[str, Any] | None,
    *,
    kind: str | None = None,
    unratified: frozenset[str] = frozenset(),
) -> tuple[list[str] | None, bool]:
    """The rows a ``met`` member is credited with and whether they were
    ``derived`` from its envelope's ``spec_row`` tags (issue #3778). Untagged
    (or unavailable) envelopes keep the manifest-asserted ``rows``.
    ``kind``/``unratified`` are forwarded to :func:`_envelope_tagged_rows`
    for its issue-#5490 ``reported`` case."""
    if not rows or envelope is None:
        return rows, False
    has_tags, covering = _envelope_tagged_rows(
        envelope, kind=kind, unratified=unratified
    )
    if not has_tags:
        return rows, False
    return [r for r in rows if r in covering], True


def _multi_row_info(
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
        credited_rows, derived = credited.get(id(spec), (rows, False))
        for r in credited_rows or []:
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
