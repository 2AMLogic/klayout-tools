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

Row-scoped credit from a failing envelope (issue #6027)
    A ``check_failed`` ``sim`` citation grades ``unmet`` as a unit, but its
    envelope's per-row truth survives the failure: rows its tagged,
    ``limits``-bearing, passing measurements grade now count as ``derived``
    coverage (and leave ``failed_rows``) instead of being mis-reported as
    failed. Only derived rows are credited -- a failed citation never covers
    a row on the manifest's word alone, and an envelope that a stale-pin,
    binding, kind or provenance refusal would reject is not captured at all
    (:func:`_maybe_record_failed_sim` decides).

The envelope-capture plumbing (:func:`_record_envelope` /
:func:`_remember_envelope`) lives here too, moved out of :mod:`.signoff` for
the same ceiling: it exists to feed the derived-row check.
"""

from __future__ import annotations

from typing import Any

from .coverage import coverage_refusal_reason
from .signoff_evidence_pin import (
    PIN_STALE,
    PIN_UNVERIFIABLE,
    content_hash_pin_state,
)

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
    """The rows a member is credited with and whether they were ``derived``
    from its envelope's ``spec_row`` tags (issue #3778). Untagged (or
    unavailable) envelopes keep the manifest-asserted ``rows``. Since issue
    #6027 the envelope may also come from a ``check_failed`` ``sim`` grading;
    :func:`multi_row_info` credits the result only when it is derived.
    """
    if not rows or envelope is None:
        return rows, False
    has_tags, covering = envelope_tagged_rows(envelope)
    if not has_tags:
        return rows, False
    return [r for r in rows if r in covering], True


def _record_envelope(
    sink: list[dict[str, Any]] | None, envelope: dict[str, Any]
) -> None:
    """Append ``envelope`` to ``sink`` when one was given (issue #3778).

    Called on the ``met`` return and -- issue #6027 -- through
    :func:`_maybe_record_failed_sim` on a ``check_failed`` ``sim`` grading
    that survived every trust gate, the only non-passing verdict whose
    envelope still carries trustworthy per-row truth. The stale-pin,
    binding, kind and provenance refusals record nothing: those envelopes
    are not evidence for any row.
    """
    if sink is not None:
        sink.append(envelope)


#: Mirrors ``signoff._REASON_CHECK_FAILED`` -- not importable from here,
#: because :mod:`.signoff` imports this module.
_CHECK_FAILED = "check_failed"


def _maybe_record_failed_sim(
    sink: list[dict[str, Any]] | None,
    unmet: tuple[str, str, Any, Any] | None,
    check_kind: str,
    envelope: dict[str, Any],
    spec: dict[str, Any],
    resolution: dict[str, Any],
    allowed_kinds: set[str] | None,
) -> None:
    """Capture a failing ``sim`` envelope for the derived-row credit
    (issue #6027) -- but only when its per-row truth is trustworthy
    evidence. A no-op for every other verdict and kind.

    The verdict itself is never touched: the trust gates are ordered
    *after* the pass/fail check on purpose, so a run that failed is
    reported as the failure it is, with a stale pin or a vacuous verdict
    disclosed beside it (:func:`signoff._grade_check_outcome`). The
    *capture* however waits on those gates: a refused envelope is not
    evidence for any row, and records nothing.

    Only the gates that can fire for ``sim`` are consulted. The ``sta``
    annotation/constraint refusals self-gate on ``sta``, the generic
    binding check on ``generic``, the post-layout requirement is item 7's
    and item 7 accepts no ``sim`` citation, and the runset/yield gates are
    kind-matched -- leaving the kind acceptance (a citation the item
    rejects as ``wrong_kind`` must not credit rows), the vacuous-verdict
    gate -- :func:`~.coverage.coverage_refusal_reason`, exactly what
    ``sim`` sees of ``signoff._coverage_refusal`` -- and the
    ``content_hash`` pin state: the same trio the ``met`` path applies to
    a ``sim`` envelope.
    """
    if unmet is None or unmet[1] != _CHECK_FAILED or check_kind != "sim":
        return
    if allowed_kinds is not None and check_kind not in allowed_kinds:
        return
    if coverage_refusal_reason(envelope) is not None:
        return
    pin = content_hash_pin_state(spec.get("content_hash"), resolution["content_hash"])
    if pin in (PIN_UNVERIFIABLE, PIN_STALE):
        return
    _record_envelope(sink, envelope)


def _remember_envelope(
    met_envelopes: dict[int, dict[str, Any]],
    spec: dict[str, Any],
    sink: list[dict[str, Any]],
) -> None:
    """Key the envelope a grading captured -- ``met``, or a ``check_failed``
    ``sim`` one (issue #6027) -- by ``spec``'s identity, for the
    derived-row credit :func:`multi_row_info` computes.

    The caller must invoke this only *after* the member's own
    ``wrong_kind`` downgrades: a citation the item rejects as the wrong
    kind is not evidence for any row, and :func:`multi_row_info` credits
    derived rows from every non-``met`` member that has an entry here."""
    if sink:
        met_envelopes[id(spec)] = sink[0]


def multi_row_info(
    graded: list[Any],
    failed: list[Any],
    spec_rows: list[dict[str, Any]] | None,
    credited: dict[int, tuple[list[str] | None, bool]] | None = None,
) -> dict[str, Any]:
    """``failed_rows`` / ``uncovered_rows`` / ``declared_gaps`` /
    ``unratified_rows`` for the declared ``spec_rows`` (empty when none were
    declared). ``unratified_rows`` lists every non-gap row declared
    ``ratified: false``, covered or not.

    A member grades ``met`` or -- issue #6027 -- ``check_failed`` with a
    captured, tagged ``sim`` envelope: its derived rows join ``covered`` and
    ``row_sources``. A failed member never contributes asserted rows, and a
    derived-only credit from it never masks the citation's own failure (the
    item still grades ``unmet`` on the failed member).

    Issue #5599: a row whose ``gap`` is the justified object form
    (``justified_gap`` set) is listed under ``justified_gaps`` --
    ``[{id, reason, rationale, issue}]`` -- never in ``declared_gaps``, so
    the free-text form's ``detail`` shape is unchanged and no consumer ever
    meets an object where it expects a string. The key is omitted when no
    justified row is declared, keeping a free-text-only report
    byte-identical; a justified gap is still a gap (never covered, never
    failed).
    """
    if spec_rows is None:
        return {}
    credited = credited or {}
    covered: set[str] = set()
    row_sources: dict[str, str] = {}
    for spec, rows, g in graded:
        member_rows, derived = credited.get(id(spec), (rows, False))
        # Issue #6027: a failed citation credits only derived rows -- those
        # its tagged, limited, passing measurements grade; never asserted
        # ones, and never from a refused (uncaptured) envelope.
        if g[0] != "met" and not derived:
            continue
        for r in member_rows or []:
            covered.add(r)
            # A row covered by any derived citation is machine-checked.
            if derived or r not in row_sources:
                row_sources[r] = "derived" if derived else "asserted"
    failed_rows = sorted({r for _, rows, _ in failed for r in rows or []} - covered)
    justified = [
        {
            "id": row["id"],
            "reason": row["justified_gap"]["reason"],
            "rationale": dict(row["justified_gap"]["rationale"]),
            "issue": row["justified_gap"].get("issue"),
        }
        for row in spec_rows
        if row["justified_gap"] is not None
    ]
    return {
        "failed_rows": failed_rows,
        "uncovered_rows": [
            row["id"]
            for row in spec_rows
            if row["gap"] is None
            and row["justified_gap"] is None
            and row["id"] not in covered
            and row["id"] not in failed_rows
        ],
        "declared_gaps": [
            {"id": row["id"], "gap": row["gap"]}
            for row in spec_rows
            if row["gap"] is not None
        ],
        **({"justified_gaps": justified} if justified else {}),
        "unratified_rows": [
            row["id"]
            for row in spec_rows
            if row["gap"] is None
            and row["justified_gap"] is None
            and not row["ratified"]
        ],
        "row_sources": {r: row_sources[r] for r in sorted(row_sources)},
    }
