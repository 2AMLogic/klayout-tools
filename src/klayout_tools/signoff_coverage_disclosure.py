"""Where T1 item 3's DRC coverage disclosure lives, and the manifest option
that turns it from reported into enforced.

Two halves of one issue live here, moved out of :mod:`.signoff` to keep
that module under its size ratchet:

``drc_coverage_disclosure`` (issue #2002)
    The three ``coverage`` fields ``docs/design-evidence-tiers.md`` item 3
    requires a DRC claim to disclose, quoted verbatim off the cited
    envelope. Reported on the citation, in ``checks[].detail`` and in the
    fleet roll-up -- and, since issue #5789, also *graded* when the
    manifest opts in.

``require_coverage_disclosure`` (issue #5789)
    The disclosure above is claimant-enforced by default: a claim that
    hides every disclosed gap grades ``met`` exactly like a fully-disclosing
    one, because a manifest had no field to state its disclosure in -- the
    gap #2002 left open. A manifest that wants the comparison sets the
    top-level boolean ``"require_coverage_disclosure": true`` and enumerates
    the gaps it claims on item 3's evidence entry, under
    ``coverage_disclosed``:

    .. code-block:: json

        "3": {
          "file": "drc.json",
          "coverage_disclosed": {
            "layers_in_stream_without_rules": ["70/20", "71/20"],
            "rules_skipped": ["met5.4", "met5.5"],
            "deck_scope": ["5.x", "6.x"]
          }
        }

    Under the ratchet a would-be-``met`` item 3 citation whose declaration
    is not **set-equal** (order-insensitive, per field) to the coverage its
    envelope reports grades ``unmet`` / ``coverage_undisclosed``, with
    ``detail.coverage_undisclosed`` naming the per-field ``missing`` entries
    (reported by the envelope, absent from the claim) and ``extra`` entries
    (claimed, no longer reported -- deck drift is a false claim too). Set
    equality, not subset, for exactly that reason. The refused item also
    carries ``detail.cited_evidence`` -- the citation verbatim, in the shape
    a ``met`` item's would have had -- because ``citation`` itself is
    documented as present only on a ``met`` item (the ``nothing_checked``
    precedent, #5479); :func:`item3_coverage_verdict` is the one gate
    :func:`.signoff._build_tier_item`'s per-citation path calls, and a
    malformed key under the ratchet is ``invalid_evidence`` there instead.
    Off by default, so existing manifests grade byte-identically.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The one T1 item whose DRC coverage ``require_coverage_disclosure``
#: ratchets. The manifest key is top-level, but only item 3's doc text
#: names these fields.
COVERAGE_DISCLOSURE_ITEM_ID = 3

#: The reason value an opted-in item 3 renders when the cited report's
#: coverage and the manifest's declaration disagree.
COVERAGE_UNDISCLOSED = "coverage_undisclosed"

#: The three fields item 3's claim must disclose -- the keys a
#: ``coverage_disclosed`` declaration accepts, and the fields the
#: comparison grades.
COVERAGE_FIELDS = (
    "layers_in_stream_without_rules",
    "rules_skipped",
    "deck_scope",
)

#: How many entries of each list the text rendering names before
#: summarising the rest as ``+N more``. The JSON output always carries
#: every entry -- this cap only keeps a terminal line readable for a deck
#: with dozens of rule-free layers.
COVERAGE_PREVIEW = 4


def read_require_coverage_disclosure(manifest: dict[str, Any]) -> tuple[bool, bool]:
    """``(require, invalid)`` for a block manifest's optional top-level
    ``require_coverage_disclosure``.

    Absent, ``null`` or ``false`` is ``(False, False)`` -- grading is
    exactly what it was before the key existed, and no new report key is
    emitted. A non-boolean value is ``(False, True)``, which degrades item
    3 alone to ``invalid_evidence``; like a malformed ``coverage_disclosed``
    it never raises, so one bad key cannot fail the whole report. Mirrors
    :func:`.signoff_derived_rows.read_require_derived_rows`.
    """
    raw = manifest.get("require_coverage_disclosure")
    if raw is None:
        return False, False
    if not isinstance(raw, bool):
        return False, True
    return raw, False


def normalize_declared(
    raw: Any,
) -> tuple[dict[str, list[str]], bool]:
    """``(declared, malformed)`` for an evidence entry's optional
    ``coverage_disclosed`` declaration.

    Absent or ``null`` is ``({}, False)`` -- nothing was declared, which
    under the ratchet is itself a gradeable omission (every reported entry
    is then ``missing``), never a malformed entry. A value that is not an
    object, names a field other than :data:`COVERAGE_FIELDS`, or carries a
    list holding a non-string is malformed: this is an opt-in disclosure
    ratchet, so a key that cannot be read as written must fail closed
    rather than silently read as "off".
    """
    if raw is None:
        return {}, False
    if not isinstance(raw, dict):
        return {}, True
    declared: dict[str, list[str]] = {}
    for field, entries in raw.items():
        if field not in COVERAGE_FIELDS:
            return {}, True
        if not isinstance(entries, list) or not all(
            isinstance(entry, str) for entry in entries
        ):
            return {}, True
        declared[field] = entries
    return declared, False


def declared_problem(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The ``detail`` for item 3's ``invalid_evidence`` when the entry's
    ``coverage_disclosed`` declaration is malformed, or ``None`` when it
    reads as written (including when it is absent)."""
    if normalize_declared(spec.get("coverage_disclosed"))[1]:
        return {
            "problem": (
                "'coverage_disclosed' must be an object whose keys are "
                "layers_in_stream_without_rules, rules_skipped or deck_scope "
                "and whose values are lists of strings"
            )
        }
    return None


def coverage_refusal(
    spec: dict[str, Any], citation: dict[str, Any]
) -> dict[str, Any] | None:
    """The ``detail.coverage_undisclosed`` for a would-be-``met`` item 3
    citation under the ratchet, or ``None`` when the declaration is
    set-equal to the coverage the citation reports (the item stays ``met``
    and emits no new ``detail`` key).

    A citation carrying no ``coverage`` block at all (evidence committed
    before `klt drc` reported coverage) is refused with
    ``reason: "no_coverage_block"``: a project that opts in asserts every
    item-3 claim states its gaps, and evidence that states none cannot
    satisfy that -- the alternative lets a claimant escape the ratchet by
    downgrading evidence. Comparison is verbatim string set-equality per
    field, order-insensitive, so a non-layer ``deck_scope`` token (the
    curated sg13g2 ``"x"``, #5723) compares exactly like a chapter name.
    A malformed declaration is :data:`None` here -- ``declared_problem``
    reports it as ``invalid_evidence`` instead.
    """
    declared, malformed = normalize_declared(spec.get("coverage_disclosed"))
    if malformed:
        return None
    reported = citation.get("coverage")
    if not isinstance(reported, dict):
        return {"coverage_undisclosed": {"reason": "no_coverage_block"}}
    fields: dict[str, Any] = {}
    for field in COVERAGE_FIELDS:
        actual = {str(entry) for entry in reported.get(field) or []}
        claimed = set(declared.get(field) or [])
        if actual == claimed:
            continue
        fields[field] = {
            "missing": sorted(actual - claimed),
            "extra": sorted(claimed - actual),
        }
    if fields:
        return {"coverage_undisclosed": fields}
    return None


#: The ``reason`` of the malformed-key verdicts below -- ``signoff.py``'s
#: ``_REASON_INVALID_EVIDENCE`` contract token, repeated here rather than
#: imported because this module must not depend on the module it was split
#: out of.
_INVALID_EVIDENCE = "invalid_evidence"


def item3_key_invalid(item_id: int, invalid: bool) -> bool:
    """Whether a malformed ``require_coverage_disclosure`` is *this* item's
    ``invalid_evidence``: the read is manifest-wide, the ratchet is item
    3's, so every other item ignores the malformed key entirely."""
    return invalid and item_id == COVERAGE_DISCLOSURE_ITEM_ID


def item3_coverage_verdict(
    item_id: int,
    require_on: bool,
    spec: dict[str, Any],
    graded: tuple[str, str | None, dict[str, Any] | None, dict[str, Any]],
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any]]:
    """The ``(status, reason, citation, detail)`` a per-citation grading
    should return -- ``graded`` (the ordinary verdict) unchanged, or item
    3's refusal under :data:`require_coverage_disclosure`. The whole gate
    :mod:`.signoff`'s per-citation path needs, kept here so the wiring
    there is one call.

    For any item other than :data:`COVERAGE_DISCLOSURE_ITEM_ID`, and with
    the ratchet off, ``graded`` is returned untouched (default grading is
    unchanged, whatever the entry declares). With it on, in order:

    - a malformed ``coverage_disclosed`` is ``unmet`` / ``invalid_evidence``
      with ``detail.problem`` naming the key, whatever the envelope said --
      an opt-in requirement must never fail open, so the check runs before
      the ``met`` gate, not only on a would-be pass;
    - a ``met`` ``drc`` citation whose declaration disagrees with the
      coverage it reports is ``unmet`` / :data:`COVERAGE_UNDISCLOSED` with
      ``detail.coverage_undisclosed`` from :func:`coverage_refusal`, plus
      ``detail.cited_evidence`` -- the citation itself, verbatim, in the
      same shape a ``met`` item's would have carried (the
      ``nothing_checked`` precedent, #5479): ``citation`` is documented as
      present only on a ``met`` item, so the unmet item names what was read
      additively instead. Every other verdict (a failing envelope, a
      ``wrong_kind``, no evidence at all) passes through unchanged.
    """
    status, reason, citation, detail = graded
    if not require_on or item_id != COVERAGE_DISCLOSURE_ITEM_ID:
        return graded
    problem = declared_problem(spec)
    if problem is not None:
        return "unmet", _INVALID_EVIDENCE, None, problem
    if status != "met" or citation is None or citation.get("kind") != "drc":
        return graded
    refusal = coverage_refusal(spec, citation)
    if refusal is None:
        return graded
    return "unmet", COVERAGE_UNDISCLOSED, None, {**refusal, "cited_evidence": citation}


def format_undisclosed(refusal: Mapping[str, Any]) -> str:
    """The text after ``coverage undisclosed: `` -- one ``field: missing …;
    extra …`` clause per disagreeing field, each list capped at
    :data:`COVERAGE_PREVIEW` entries then summarised as ``+N more`` (the
    JSON carries every entry; this cap only keeps a terminal line readable
    for a deck with dozens of rule-free layers). A ``no_coverage_block``
    refusal says that instead -- there are no entries to name."""
    if refusal.get("reason") == "no_coverage_block":
        return "cited evidence reports no coverage block"

    def shown(entries: list[Any]) -> str:
        names = [str(entry) for entry in entries[:COVERAGE_PREVIEW]]
        if len(entries) > COVERAGE_PREVIEW:
            names.append(f"+{len(entries) - COVERAGE_PREVIEW} more")
        return ", ".join(names)

    parts = []
    for field, diff in refusal.items():
        named = []
        if diff.get("missing"):
            named.append(f"missing {shown(diff['missing'])}")
        if diff.get("extra"):
            named.append(f"extra {shown(diff['extra'])}")
        parts.append(f"{field}: {'; '.join(named)}")
    return "; ".join(parts)


def drc_coverage_disclosure(
    kind: str, envelope: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The three ``coverage`` fields ``docs/design-evidence-tiers.md`` item 3
    requires a DRC claim to disclose, quoted verbatim off the cited envelope
    (issue #2002) -- or ``None`` when there is nothing to quote.

    ``None`` in exactly two cases, and they mean the same thing to a reader
    ("this artifact makes no coverage statement"), never "the deck has no
    gaps":

    - ``kind != "drc"``. No other envelope kind carries a ``coverage`` block
      of this shape, and item 3 is the only item whose doc text names these
      fields. (`klt lvs`'s own coverage-shaped disclosures are deliberately
      out of scope -- see `signoff.py`'s "DRC coverage surfacing" docstring
      section.)
    - The envelope has no ``coverage`` key at all, or a non-object one:
      evidence committed before `klt drc` reported coverage. Back-compat is
      the whole reason this is ``None`` rather than a dict of empty lists --
      an old envelope must not read as a deck that checked everything.

    Individual fields are normalised to a list (``[]`` for a missing or
    non-list value) so a consumer never has to re-do that check; this is a
    *shape* normalisation of a present ``coverage`` block, not a fabricated
    coverage claim for an absent one.
    """
    if kind != "drc":
        return None
    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        return None

    def _field(name: str) -> list[Any]:
        value = coverage.get(name)
        return list(value) if isinstance(value, list) else []

    return {
        "layers_in_stream_without_rules": _field("layers_in_stream_without_rules"),
        "rules_skipped": _field("rules_skipped"),
        "deck_scope": _field("deck_scope"),
    }


def format_coverage(coverage: dict) -> str:
    """One line summarising a `drc` citation's three disclosed `coverage`
    fields (issue #2002): each field's entry count, plus the first few
    entries by name.

    Counts first so a non-zero gap is visible without reading the names, and
    every field is always shown -- including a `0` -- because "this deck
    skipped no rules" is exactly the statement item 3 asks a claim to make,
    and it must not be indistinguishable from a field that went unreported
    (an envelope with no `coverage` block at all prints no coverage line at
    all; see the call site).
    """
    parts = []
    for field in COVERAGE_FIELDS:
        entries = coverage.get(field) or []
        shown = ", ".join(str(entry) for entry in entries[:COVERAGE_PREVIEW])
        if len(entries) > COVERAGE_PREVIEW:
            shown += f", +{len(entries) - COVERAGE_PREVIEW} more"
        suffix = f" ({shown})" if entries else ""
        parts.append(f"{field}={len(entries)}{suffix}")
    return ", ".join(parts)
