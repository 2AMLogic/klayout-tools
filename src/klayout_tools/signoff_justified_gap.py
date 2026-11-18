"""The manifest's ``spec_rows`` declaration and its justified declared gaps.

Two concerns moved out of :mod:`.signoff` to keep that module under its size
ratchet, following the same precedent as :mod:`.signoff_derived_rows` and
:mod:`.signoff_na`:

``spec_rows`` (issue #2959, #3768)
    The manifest's declared row set for T1 items 5 and 6, read by
    :func:`_read_spec_rows` and resolved per partition by -- well, not
    here: :func:`_resolve_spec_rows` stayed behind (it needs the partition
    machinery). Every name this module defines is re-imported at the top of
    :mod:`.signoff`, so ``signoff.<name>`` keeps resolving exactly as
    before the split.

``justified declared gaps`` (issue #5599)
    A ``spec_rows`` row's ``gap`` may be the free-text string (unchanged
    meaning) or the justified **object** form: a human reason, a rationale
    artifact pinned by content hash -- normalized through
    :func:`_na_document`, the same hash-binding shape the item-6
    ``not_applicable`` form uses -- and the issue that would close it. A
    declared gap is pending work, never permanent (operator ruling on
    #5858, 2026-11-17): naming the closing issue is the point of the form,
    and a justified gap that names none is reported as a finding, never
    refused. Neither form ever counts as coverage; the roll-up is
    deliberately unchanged (that question is #5858, settled by the ruling:
    gaps keep blocking T1).
"""

from __future__ import annotations

from typing import Any

from ._digest import sha256_file
from .signoff_na import _na_spec_format

#: T1 items whose manifest entry may be a **list** of citations with optional
#: ``spec_rows`` coverage (issue #2959). Item 5, and item 6 (issue #2908: a
#: block whose spec has several statistical rows, only some evidenced).
_ITEMS_ACCEPTING_MULTI_CITATION: frozenset[int] = frozenset({5, 6})

#: A justified gap's rationale artifact is missing or unreadable.
_REASON_JUSTIFIED_GAP_RATIONALE_UNREADABLE = "justified_gap_rationale_unreadable"
#: The rationale artifact is present but its content hash does not match the
#: declared one.
_REASON_JUSTIFIED_GAP_RATIONALE_STALE = "justified_gap_rationale_stale"
#: A justified gap that names no issue (operator ruling on #5858,
#: 2026-11-17: a declared gap is pending work -- the open issue that would
#: close it should be recorded; its absence is a finding, never a refusal).
FINDING_JUSTIFIED_GAP_WITHOUT_ISSUE_LINK = "justified_gap_without_issue_link"

_SpecRowsKey = tuple[int, "str | None"]


def _na_document(
    raw: Any, label: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize one ``{"file", "content_hash"}`` reference of the
    ``not_applicable`` form; ``(ref, None)`` or ``(None, detail)``."""
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("file"), str)
        or not raw["file"]
        or not isinstance(raw.get("content_hash"), str)
        or not raw["content_hash"]
    ):
        return None, {
            "document": label,
            "problem": "needs a string 'file' and a string 'content_hash'",
        }
    ref = {"file": raw["file"], "content_hash": raw["content_hash"]}
    if label == "spec" and ("format" in raw or "id_column" in raw):
        problem = _na_spec_format(raw, ref)
        if problem is not None:
            return None, {"document": label, "problem": problem}
    return ref, None


def _read_spec_rows(
    manifest: dict[str, Any],
) -> dict[_SpecRowsKey, list[dict[str, Any]] | None]:
    """Read the manifest's optional ``spec_rows`` declaration (issue #2959):
    ``{"<item id>": [{"id": "<row>", "gap": "<reason>"?, "ratified": bool?,
    "timing_margin": bool?}, ...]}``. A key may be partition-qualified
    (``"5.analog"``, issue #3768), mirroring the evidence-key convention of
    ``_lookup_evidence``; see ``_resolve_spec_rows``.

    Returns ``(item id, partition | None) -> rows``; a malformed declaration
    maps to ``None`` so only that item degrades to ``invalid_evidence``, never
    the whole report. A ``gap`` row is one no producible envelope can cover; it
    is reported as a declared gap and never counts as covered. ``ratified``
    (default ``true``, issue #2908) is ``false`` for a row whose target is a
    proposed, unratified limit: evidence against it is provisional and never
    grades ``met``. ``timing_margin`` (default ``false``, issue #4314) marks the
    row a digital ``sim``-only item 5 must evidence with a measured per-corner
    timing margin.

    Issue #5599: ``gap`` may also be the **justified** object form --
    ``{"reason": str, "rationale": {"file", "content_hash"}, "issue": str?}``.
    The stored row keeps ``gap`` ``None`` (so the free-text shape is
    untouched) and carries the normalized object under ``justified_gap``; a
    rationale reference is normalized by :func:`_na_document` here and
    hash-checked at grading time by
    :func:`_check_justified_gap_rationales`. An object that does not
    validate degrades the item to ``invalid_evidence`` -- never silently to
    the free-text form.
    """
    raw = manifest.get("spec_rows")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {(item_id, None): None for item_id in _ITEMS_ACCEPTING_MULTI_CITATION}
    out: dict[_SpecRowsKey, list[dict[str, Any]] | None] = {}
    for key, rows in raw.items():
        head, sep, suffix = str(key).partition(".")
        try:
            item_id = int(head)
        except ValueError:
            continue
        slot: _SpecRowsKey = (item_id, suffix if sep else None)
        parsed: list[dict[str, Any]] | None = []
        if not isinstance(rows, list) or not rows:
            parsed = None
        else:
            valid: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                row_id = row.get("id") if isinstance(row, dict) else None
                gap = row.get("gap") if isinstance(row, dict) else None
                ratified = row.get("ratified", True) if isinstance(row, dict) else True
                margin = (
                    row.get("timing_margin", False) if isinstance(row, dict) else False
                )
                # Issue #5599: a string ``gap`` keeps its free-text meaning;
                # an object ``gap`` is the justified form and must normalize;
                # any other shape is malformed.
                justified_gap = (
                    _read_justified_gap(gap) if isinstance(gap, dict) else None
                )
                if (
                    not isinstance(row_id, str)
                    or not row_id
                    or row_id in seen
                    or (gap is not None and not isinstance(gap, (str, dict)))
                    or (isinstance(gap, str) and not gap)
                    or (isinstance(gap, dict) and justified_gap is None)
                    or not isinstance(ratified, bool)
                    or not isinstance(margin, bool)
                ):
                    valid = []
                    break
                seen.add(row_id)
                valid.append(
                    {
                        "id": row_id,
                        "gap": gap if isinstance(gap, str) else None,
                        "justified_gap": justified_gap,
                        "ratified": ratified,
                        "timing_margin": margin,
                    }
                )
            parsed = valid or None
        out[slot] = parsed
    return out


def _read_justified_gap(gap: Any) -> dict[str, Any] | None:
    """Normalize the justified object form of ``spec_rows[].gap`` (issue
    #5599): ``{"reason": str, "rationale": {"file", "content_hash"},
    "issue": str?}`` -> the normalized object, or ``None`` when it does not
    validate. The rationale reference is normalized by :func:`_na_document`
    -- the same hash-binding shape the item-6 ``not_applicable`` form uses;
    its file is hash-checked at grading time. ``issue`` -- the open issue
    that would close the gap -- is recorded when present and reported as a
    finding when absent; it is optional, never graded."""
    if not isinstance(gap, dict):
        return None
    reason = gap.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    rationale, _bad = _na_document(gap.get("rationale"), "rationale")
    if rationale is None:
        return None
    issue = gap.get("issue")
    if issue is not None and (not isinstance(issue, str) or not issue):
        return None
    out: dict[str, Any] = {"reason": reason, "rationale": rationale}
    if issue is not None:
        out["issue"] = issue
    return out


def _check_justified_gap_rationales(
    spec_rows: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any]] | None:
    """Hash-check every justified gap's rationale artifact (issue #5599) ->
    ``(reason, detail)`` naming the first row whose artifact fails, or
    ``None`` when every one checks. Checked at grading time like any cited
    artifact: a missing or unreadable file is
    ``justified_gap_rationale_unreadable``, a mismatched content hash
    ``justified_gap_rationale_stale``. A broken declaration is never
    silently demoted to the free-text form."""
    for row in spec_rows or []:
        justified_gap = row.get("justified_gap")
        if not justified_gap:
            continue
        ref = justified_gap["rationale"]
        digest = sha256_file(ref["file"])
        if digest is None:
            return _REASON_JUSTIFIED_GAP_RATIONALE_UNREADABLE, {
                "row": row["id"],
                "document": "rationale",
                "file": ref["file"],
            }
        actual = f"sha256:{digest}"
        if actual != ref["content_hash"]:
            return _REASON_JUSTIFIED_GAP_RATIONALE_STALE, {
                "row": row["id"],
                "document": "rationale",
                "file": ref["file"],
                "expected_content_hash": ref["content_hash"],
                "actual_content_hash": actual,
            }
    return None


def justified_gap_report_fields(
    items: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """``(count, findings)`` for a built report's item entries (issue
    #5599): how many justified gaps were declared in valid ``spec_rows``,
    and one ``justified_gap_without_issue_link`` finding per gap that names
    no issue. A gap is pending work, so the issue that would close it
    should be recorded; its absence is reported, never refused."""
    count = 0
    findings: list[dict[str, Any]] = []
    for entry in items:
        for gap in (entry.get("detail") or {}).get("justified_gaps") or []:
            count += 1
            if gap["issue"] is None:
                findings.append(
                    {
                        "finding": FINDING_JUSTIFIED_GAP_WITHOUT_ISSUE_LINK,
                        "item": entry["id"],
                        **(
                            {"partition": entry["partition"]}
                            if entry["partition"]
                            else {}
                        ),
                        "row": gap["id"],
                    }
                )
    return count, findings
