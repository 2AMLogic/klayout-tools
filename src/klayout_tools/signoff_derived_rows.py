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

Row-scoped credit from a failing envelope (issue #6027)
    A ``check_failed`` ``sim`` citation grades ``unmet`` as a unit, but its
    envelope's per-row truth survives the failure: rows its tagged,
    ``limits``-bearing, passing measurements grade now count as ``derived``
    coverage (and leave ``failed_rows``) instead of being mis-reported as
    failed. Only derived rows are credited -- a failed citation never covers
    a row on the manifest's word alone, and an envelope that a stale-pin,
    binding, kind, provenance or vacuous-verdict (``no_limits_checked``
    included) refusal would reject is not captured at all
    (:func:`_maybe_record_failed_sim` decides).

    A failed envelope's ``status: pass`` is only an *aggregate*: `klt sim`
    rolls a measurement up as ``pass`` while a bound was skipped
    (unrecognised key, ``null`` value, ``not_reached`` at one corner). So the
    credit is stricter than the ``met`` derivation: the tagged measurement
    needs a recognised, non-null ``min``/``max``, and -- when the envelope
    carries the producer's checked-work ``coverage`` record -- a checked
    ``limit`` for it and none skipped at any corner
    (:func:`_failed_envelope_rows`). A tagged, limit-declaring measurement
    that does not qualify also keeps its row out of the credit.

The envelope-capture plumbing (:func:`_record_envelope` /
:func:`_remember_envelope`) lives here too, moved out of :mod:`.signoff` for
the same ceiling: it exists to feed the derived-row check.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .coverage import coverage_refusal_reason, work_id
from .signoff_derived_bounds import derived_bounds_checked
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


#: Mirrors ``sim._RECOGNISED_LIMIT_KEYS`` -- the only ``limits`` keys `klt sim`
#: applies; any other key is silently inert there. Not importable from here
#: (``sim`` is far too heavy for the signoff layer), so a test pins the two.
_RECOGNISED_LIMIT_KEYS = ("min", "max")


def _sim_coverage_counts(envelope: Mapping[str, Any]) -> tuple[int, int] | None:
    """``(measurements_declared, measurements_with_limits)`` off a `klt sim`
    envelope's ``coverage`` block, or ``None`` when absent or non-integer
    (pre-coverage evidence keeps grading exactly as before)."""
    block = envelope.get("coverage")
    if not isinstance(block, dict):
        return None
    declared = block.get("measurements_declared")
    limited = block.get("measurements_with_limits")
    if type(declared) is not int or type(limited) is not int:
        return None
    return declared, limited


def _sim_no_limits_applied(envelope: Mapping[str, Any]) -> bool:
    """Whether a `klt sim` envelope declares measurements yet none carry a
    ``limits`` bound (issue #3769). ``capture_only`` runs declare none.

    Moved here from :mod:`.signoff` (issue #6027) so the failed-citation
    capture applies exactly the refusal the ``met`` path does, from one
    definition, without :mod:`.signoff` growing past its size ceiling."""
    counts = _sim_coverage_counts(envelope)
    return counts is not None and counts[0] > 0 and counts[1] == 0


def _limit_work_names(ids: Iterable[Any]) -> set[str]:
    """Measurement names behind the bound-check work ids in ``ids`` -- the
    identities :func:`~.coverage.work_id` gives `klt sim`'s per-corner checks
    (``limit:[corner_index, corner_id, name, key]``) and its per-group
    ``derive`` checks (``derived_limit:[name, group_id, key]``, issue #7184)."""
    names: set[str] = set()
    for raw in ids:
        if not (isinstance(raw, str) and raw.startswith(("limit:", "derived_limit:"))):
            continue
        try:
            parts = json.loads(raw.partition(":")[2])
        except ValueError:
            continue
        index = 2 if raw.startswith("limit:") else 1
        if (
            isinstance(parts, list)
            and len(parts) == 4
            and isinstance(parts[index], str)
        ):
            names.add(parts[index])
    return names


def _bound_check_record(envelope: dict[str, Any]) -> tuple[set[str], set[str]] | None:
    """Checked work IDs and measurement names with a skipped limit.

    Counter-only and pre-coverage reports have no individual work record.
    """
    block = envelope.get("coverage")
    if not isinstance(block, dict) or "checked" not in block:
        return None
    checked, skipped = block.get("checked"), block.get("skipped")
    return (
        {key for key in checked if isinstance(key, str)}
        if isinstance(checked, list)
        else set(),
        _limit_work_names(
            r.get("id")
            for r in (skipped if isinstance(skipped, list) else [])
            if isinstance(r, dict)
        ),
    )


def _bounds_checked(
    measurement: dict[str, Any],
    corners: Any,
    record: tuple[set[str], set[str]] | None,
) -> bool:
    """Confirm each declared bound for each reported corner using its full work ID."""
    name, limits = measurement.get("name"), measurement.get("limits")
    if not isinstance(name, str) or not name or not isinstance(limits, dict):
        return False
    keys = [key for key in _RECOGNISED_LIMIT_KEYS if limits.get(key) is not None]
    if not keys:
        return False
    if isinstance(measurement.get("derive"), dict):
        return derived_bounds_checked(measurement, record, keys)
    if record is None:
        return True
    checked, skipped = record
    if name in skipped or not isinstance(corners, list) or not corners:
        return False
    for index, corner in enumerate(corners):
        corner_id = corner.get("corner_id") if isinstance(corner, dict) else None
        if not isinstance(corner_id, str):
            return False
        if any(
            work_id("limit", index, corner_id, name, key) not in checked for key in keys
        ):
            return False
    return True


def _failed_envelope_rows(
    tagged: list[dict[str, Any]], envelope: dict[str, Any]
) -> set[str]:
    """The rows a ``check_failed`` ``sim`` envelope may credit (issue #6027).

    A measurement grades its row only when it passed *and* its bound was
    really applied: a recognised, non-null ``min``/``max``
    that the envelope's checked-work record
    (:func:`_bound_check_record`) shows checked and never skipped, at every
    corner it was reported for. The aggregate ``status: pass`` alone proves
    neither -- `klt sim` rolls up ``pass`` over a skipped bound, and over one
    corner ``not_reached`` beside a passing one.

    A tagged measurement that declares ``limits`` yet does not qualify keeps
    its row out of the credit: the row's own bound failed or went unchecked,
    so a sibling measurement's pass must not stand in for it. A tag without
    ``limits`` is inert, as on the ``met`` path."""
    record = _bound_check_record(envelope)
    graded: set[str] = set()
    ungraded: set[str] = set()
    for m in tagged:
        if not m.get("limits"):
            continue
        passed = m.get("status") == "pass" and _bounds_checked(
            m, envelope.get("corners"), record
        )
        (graded if passed else ungraded).add(m["spec_row"])
    return graded - ungraded


def envelope_tagged_rows(
    envelope: dict[str, Any],
    *,
    kind: str | None = None,
    unratified: frozenset[str] = frozenset(),
    failed: bool = False,
) -> tuple[bool, set[str]]:
    """Return whether the envelope has tagged measurements and which rows they cover.

    A passing measurement with limits credits its tagged row. A failed sim
    citation also needs evidence that every bound was checked at every corner
    (#6027). Only a yield citation can credit a ``reported`` measurement,
    and only for an unratified row (#5490).
    """
    raw = envelope.get("measurements")
    entries = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    tagged = [
        m for m in entries if isinstance(m.get("spec_row"), str) and m["spec_row"]
    ]
    if failed:
        return bool(tagged), _failed_envelope_rows(tagged, envelope)
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
    failed: bool = False,
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
        envelope, failed=failed, kind=kind, unratified=unratified
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
            failed=g[0] != "met",
            kind=(g[2] or {}).get("kind"),
            unratified=unratified,
        )
        for spec, rows, g in graded
    }


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
    gate -- exactly what ``sim`` sees of ``signoff._coverage_refusal``:
    :func:`~.coverage.coverage_refusal_reason` *and* the sim-specific
    ``no_limits_checked`` refusal (:func:`_sim_no_limits_applied`), so a run
    that declared measurements but applied no bound anywhere credits no row
    -- and the ``content_hash`` pin state: the same trio the ``met`` path
    applies to a ``sim`` envelope.
    """
    if unmet is None or unmet[1] != _CHECK_FAILED or check_kind != "sim":
        return
    if allowed_kinds is not None and check_kind not in allowed_kinds:
        return
    if coverage_refusal_reason(envelope) is not None:
        return
    if _sim_no_limits_applied(envelope):
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
