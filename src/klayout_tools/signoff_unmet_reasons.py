"""Roll-up of why a tier report's T1 items are ``unmet`` (issue #5903).

``klt signoff --manifest`` exits ``3`` for any unmet T1 item, so exit status
cannot tell a block that is mid-progress (``no_evidence``) from one whose pin
went stale (``stale_evidence``). Every item already carries ``reason``; this
module only totals them so CI can gate with one ``jq``, without iterating
``items[]``. Moved out of :mod:`.signoff` to keep that module under its size
ratchet.

Since issue #7371 this stdlib-only leaf also owns the shared ``_REASON_*``
unmet-reason strings themselves (moved verbatim from :mod:`.signoff`, which
re-imports the ones still referenced through ``klayout_tools.signoff``):
every ``signoff_*`` sibling that renders or compares a reason imports it
here, at module scope, instead of bouncing through the orchestrator.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Unmet-reason vocabulary (issue #7371)
# --------------------------------------------------------------------------- #

#: ``reason`` values a tier-report item can carry when its ``status`` is
#: ``"unmet"`` -- see :func:`_grade_evidence` and :func:`_build_tier_item`.
#: Issue #826 (Phase 1b of epic #706): the whole point of this enum is that
#: "no runnable check exists for this item" (:data:`_REASON_NO_EVIDENCE`,
#: :data:`_REASON_INVALID_EVIDENCE`, :data:`_REASON_UNREADABLE_EVIDENCE`,
#: :data:`_REASON_UNRECOGNIZED_ENVELOPE`, :data:`_REASON_TIER_NOT_SUPPORTED`)
#: must never collapse, in the JSON output, into the same shape as "a check
#: ran and did not pass" (:data:`_REASON_CHECK_ERRORED`,
#: :data:`_REASON_CHECK_FAILED`, :data:`_REASON_STALE_EVIDENCE`) -- a reader
#: (human or agent) parsing the report must be able to tell "nobody ever
#: checked this" apart from "somebody checked this and it failed" without
#: cross-referencing anything outside the item itself.
_REASON_NO_EVIDENCE = "no_evidence"
#: Issue #6953: no ``evidence`` entry, but the manifest's ``disclosed_evidence``
#: map named a readable local artifact produced outside ``klt`` -- still
#: ``unmet`` with no citation and no credit, distinct from ``no_evidence``
#: ("no entry") so a fleet reader can tell "external record disclosed" from
#: "no record". Unreadable path -> :data:`_REASON_UNREADABLE_EVIDENCE`. See
#: :mod:`.signoff_disclosed_evidence`.
_REASON_EVIDENCE_NOT_KLT_NATIVE = "evidence_not_klt_native"
_REASON_INVALID_EVIDENCE = "invalid_evidence"
_REASON_UNREADABLE_EVIDENCE = "unreadable_evidence"
_REASON_UNRECOGNIZED_ENVELOPE = "unrecognized_envelope"
_REASON_CHECK_ERRORED = "check_errored"
_REASON_CHECK_FAILED = "check_failed"
_REASON_STALE_EVIDENCE = "stale_evidence"
#: Issue #4314: a digital item-5 entry citing only ``klt sim`` (the full-custom
#: sub-case) must also carry a measured per-corner timing-margin metric.
_REASON_TIMING_MARGIN_MISSING = "timing_margin_missing"

#: Issue #2198: the resolved evidence *is* a `klt` envelope -- it carries a
#: kind's **primary** marker (see :data:`_NEAR_MISS_MARKERS`) -- but not the
#: shape discriminator :func:`_classify` pairs that marker with, because the
#: discriminator was added to the verb's response *after* this artifact was
#: written. Deliberately distinct from
#: :data:`_REASON_UNRECOGNIZED_ENVELOPE`, which collapses three unrelated
#: situations already ("not a JSON object", "matches no recognised shape",
#: "matches one but is malformed for it") and, without this reason, a fourth:
#: a well-formed response from an older `klt`. Committed evidence is
#: append-only by construction (``docs/design-evidence-tiers.md``,
#: "Provenance hygiene in evidence records"), so the fleet's stock of
#: envelopes written against older schemas only grows -- and every additive
#: marker field silently converts some of it from "gradeable" to "looks like
#: it isn't a `klt` file". The two remedies differ and the report must say
#: which applies: "re-run this check under a newer `klt`" (version skew) vs.
#: "this citation is wrong" (unrecognised).
_REASON_ENVELOPE_VERSION_SKEW = "envelope_version_skew"

#: Issue #2182: a manifest entry pins ``content_hash``, but the resolved
#: envelope carries **no input hash at all** -- ``resolution["content_hash"]
#: is None`` -- rather than one that mismatches the pin. Two ways this
#: happens today: a `klt functional-verification` envelope, which carries no
#: `provenance` block by design (see :data:`_REASON_STALE_EVIDENCE`'s own
#: docstring reference above and ``docs/cli/signoff.md``); or a `"generic"`
#: envelope (issue #1152) with no `provenance` block at all. Deliberately
#: distinct from :data:`_REASON_STALE_EVIDENCE`, which is reserved for a
#: genuinely *mismatched*, non-``None`` hash (the evidence ran, and provably
#: ran against a different revision than the one pinned -- the remedy is
#: "re-run the check"): here nothing was ever recorded to compare against,
#: so the remedy is different -- "re-produce this evidence with a producer
#: that records provenance" (or unpin `content_hash` for this entry), not
#: "re-run the same producer again". For yield, :func:`_yield_samples_content_hash`
#: looks for the samples beside the report, then in the working directory
#: (#2197). If both disk lookups fail, it tries the declared store manifest
#: (#5963). When none resolves, the digest is null. For a named samples
#: document, ``content_hash_unresolved`` records the searched paths and,
#: if a declared manifest lookup failed, its path and the failure reason.
#:
#: Issue #2496 adds a third route to both of these two reasons, item 11
#: only: :func:`_erc_supply_spec_hash_reason` re-hashes the **spec**
#: document a cited `klt erc` envelope names (not the layout
#: ``resolution["content_hash"]`` above covers) and compares it to that
#: envelope's own ``provenance.spec.content_hash`` (issue #2049) -- a
#: mismatch renders
#: :data:`_REASON_STALE_EVIDENCE`, no recorded hash at all (an envelope
#: produced before #2049) renders this reason. Same two reasons, same
#: "the remedy differs" distinction, applied to the second document a
#: compound item-11 citation depends on instead of the manifest's pinned
#: ``content_hash``.
_REASON_UNVERIFIABLE_PROVENANCE = "unverifiable_provenance"

#: Issue #2718: a passing ``"generic"`` citation for one of the
#: artifact-anchored items (:data:`_ITEMS_REQUIRING_ANCHORED_GENERIC_EVIDENCE`
#: -- 1, 2, 9, 10) that does not carry the binding those items require: the
#: envelope declares no ``t1_item`` (or a non-integer one), names no
#: ``provenance.input.path``, or the manifest entry pins no
#: ``content_hash``. Nothing ties the attestation to an audited artifact,
#: so its ``status: "pass"`` is the caller's word alone. Grouped with the
#: "no runnable check" reasons: the remedy is to anchor the citation, not
#: to re-run anything.
_REASON_UNANCHORED_EVIDENCE = "unanchored_evidence"

#: Issue #2718: a passing ``"generic"`` citation whose envelope declares a
#: ``t1_item`` other than the item it is cited for -- an attestation
#: replayed across items. Checked for every item that accepts
#: ``"generic"`` at all (:data:`_ITEMS_ACCEPTING_GENERIC_EVIDENCE`,
#: including item 8); an item-8 envelope that declares no ``t1_item`` is
#: unaffected, exactly as before.
_REASON_WRONG_ITEM = "wrong_item"

#: Issue #2887: an artifact-anchored ``"generic"`` citation for T1 item 1 or
#: 2 (:data:`_ITEMS_ACCEPTING_REGENERATION_CLAIMS`) whose
#: ``provenance.regeneration.regenerated_hash`` -- the hash the attester's
#: own CI recorded after re-running the generator -- differs from the hash
#: the bound artifact re-hashes to here. The committed artifact is not what
#: its generator produces: the netlist/stream is out of date with its
#: sources (or the generator is nondeterministic, which this cannot tell
#: apart). Distinct from :data:`_REASON_STALE_EVIDENCE`, which says the
#: artifact changed *after* it was attested; here the attested bytes are
#: intact but were never fresh. Grouped with the "a check ran and did not
#: pass" reasons: the remedy is to regenerate and commit the artifact, then
#: re-attest. ``klt signoff`` never runs the generator itself.
_REASON_STALE_ARTIFACT = "stale_artifact"
#: Issue #2342: the evidence entry carried a ``"pointer"`` (an RFC 6901 JSON
#: Pointer naming where inside the cited document the envelope lives -- see
#: :func:`_resolve_json_pointer`), and that pointer did not resolve to a JSON
#: object: it is syntactically malformed, it names a path the document does
#: not contain, or it names a value that is not an object (a string, number,
#: array, or ``null`` -- none of which can ever be a `klt` envelope).
#: Deliberately distinct from :data:`_REASON_UNRECOGNIZED_ENVELOPE`, which
#: says "the thing this citation resolved to is not a shape I recognise": the
#: two are different mistakes with different remedies. An unrecognised
#: envelope means the manifest cited the wrong *item* (fix the citation);
#: an invalid pointer means the manifest cited the right document but named
#: the wrong *place inside it* (fix the pointer) -- and, critically, that the
#: cited bytes were never even classified, so the reason must not imply
#: anything about them. Grouped with the other "no runnable check exists for
#: this item" reasons per issue #826's invariant.
_REASON_INVALID_POINTER = "invalid_pointer"

_REASON_TIER_NOT_SUPPORTED = "tier_not_supported"
#: Issue #5763: the entry's subprocess ran but was killed by
#: :data:`_COMMAND_EVIDENCE_TIMEOUT_S` -- the bound, not the check,
#: produced the unmet grade, so a hung gate reads differently from a
#: failing one (split from :data:`_REASON_COMMAND_FAILED`).
_REASON_COMMAND_TIMEOUT = "command_timeout"
#: Issue #825 (Phase 1 of epic #706): could not be launched or exited
#: nonzero without a parseable stdout envelope (exit 1/2 leaves stdout
#: empty per docs/json-contract.md) -- a timeout is
#: :data:`_REASON_COMMAND_TIMEOUT`; a nonzero exit *with* a parseable
#: envelope flows into :func:`_classify`/:func:`_check_passed` like the
#: file-backed path; distinct from :data:`_REASON_UNREADABLE_EVIDENCE`
#: (zero exit, stdout not valid JSON) -- kept distinguishable per #826.
_REASON_COMMAND_FAILED = "command_failed"

#: Issue #871 (Phase 2b of epic #706): the evidence resolved to a readable,
#: recognised, *passing* envelope -- but its classified kind is not one this
#: item accepts (see :func:`_build_tier_item`'s ``allowed_kinds`` parameter).
#: Grouped with the other "no runnable check exists for this item" reasons
#: (:data:`_REASON_NO_EVIDENCE` et al.) rather than with
#: :data:`_REASON_CHECK_FAILED`: the cited check did not fail on its own
#: terms, it simply does not prove what *this* item requires (e.g. a clean
#: DRC report cited for item 7, "post-layout verification", proves nothing
#: about a schematic-vs-extracted-netlist delta) -- so it must never render
#: `"met"` by borrowing an unrelated check's pass.
_REASON_WRONG_KIND = "wrong_kind"

#: Issue #2176: the manifest cited evidence for a T1 item **this build has
#: no grading rules for at all** -- an item id that appears in the doc the
#: report was built from (``--tiers-doc``/``$KLT_TIERS_DOC``) but not in
#: this build's own bundled copy of it, and that no grading table names
#: either (see :func:`_is_graded_by_build`). The caller is asking a question
#: this build cannot answer: it knows none of that item's accepted envelope
#: kinds, none of its evidence shape, none of its pass conditions.
#:
#: Grouped with the other "no runnable check exists for this item" reasons,
#: for the sharpest form of the reason :data:`_REASON_WRONG_KIND` is: no
#: check was even attempted. Deliberately distinct from ``wrong_kind``
#: ("cite a different artifact -- this build knows which") and from
#: :data:`_REASON_NO_EVIDENCE` ("nothing was cited"): here the citation may
#: be perfectly good and the *build* is what is missing, so the actionable
#: fix is to upgrade `klt` (or grade against the doc this one ships), not to
#: touch the manifest. Without it, such a citation fell through to the
#: unrestricted ``allowed_kinds is None`` path and could render ``"met"`` --
#: a pass produced by rules that do not exist in the running build, and
#: indistinguishable in the report from a correctly-graded row.
#:
#: Note that the evidence is *not* resolved before this refusal: an
#: ungradeable item's command-backed entry is never executed, since this
#: build could not interpret what came back anyway.
_REASON_UNGRADEABLE_BY_BUILD = "ungradeable_by_build"

#: Issue #1959: the evidence resolved to a readable, recognised, *passing*
#: envelope **of a kind this item accepts**, but that run is not the
#: post-layout run the item requires -- today, a `klt
#: functional-verification` regression cited for item 7 that ran without SDF
#: back-annotation (``environment.sdf`` is ``null``), i.e. the pre-layout
#: zero-delay simulation item 7's own checklist text excludes. Deliberately
#: distinct from :data:`_REASON_WRONG_KIND` (which says "cite a different
#: kind of artifact"): here the artifact *is* the right one, it just has to
#: be re-run against the post-route netlist with SDF timing, and per issue
#: #826's invariant the report must say which of the two is missing rather
#: than collapse them into one ambiguous reason. Grouped with the "no
#: runnable check proves this item" reasons for the same rationale as
#: ``wrong_kind``: the cited check did not fail on its own terms.
_REASON_NOT_POST_LAYOUT = "not_post_layout"

#: Issue #1996: the cited envelope's own ``coverage`` block states that the
#: run checked **nothing**, so it says nothing at all about the design --
#: whatever its own ``status`` token (issue #5479). Reached by two routes,
#: both carrying ``nothing_checked_item_detail``; see
#: :mod:`klayout_tools.signoff_nothing_checked` for which producers emit
#: which, and this module's "Vacuous-verdict refusal" section. Grouped with
#: the "no runnable check proves this item" reasons
#: (:data:`_REASON_WRONG_KIND`, :data:`_REASON_NOT_POST_LAYOUT`) rather than
#: with :data:`_REASON_CHECK_FAILED`, per issue #826's invariant: the cited
#: check did not fail on its own terms, it simply measured nothing -- and the
#: fix is to re-run it with something to check, not to fix a defect it found.
_REASON_NOTHING_CHECKED = "nothing_checked"
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


#: Issue #2109: the evidence resolved to a readable, recognised envelope of a
#: kind this item accepts, whose producer applied the common rollup rule
#: (:func:`~klayout_tools.coverage.coverage_rollup`) and reported its
#: **partial** status token -- ``"clean_partial"``, ``"pass_partial"``, the
#: per-kind spelling in :data:`_PARTIAL_STATUS_BY_KIND`. Every check it ran
#: passed, and it also skipped requested work, so its result is real but not
#: unconditional.
#:
#: It *names* the same class of problem as the "no runnable check proves
#: this item" reasons (:data:`_REASON_WRONG_KIND`,
#: :data:`_REASON_NOT_POST_LAYOUT`, :data:`_REASON_NOTHING_CHECKED`), per
#: issue #826's invariant: the cited check did not fail on its own terms,
#: and the fix is to re-run it over the work it skipped, not to fix a defect
#: it found. Reporting it as ``check_failed`` -- which is what happened
#: before this constant existed, since a partial token simply is not the
#: kind's success token -- sent a reader looking for a violation that does
#: not exist.
#:
#: It is *ordered* like :data:`_REASON_CHECK_FAILED` rather than like those
#: three, because it replaces that reason on the same code path: it is
#: decided from the envelope's own verdict, before :func:`_build_tier_item`'s
#: kind gate (which only ever downgrades a would-be ``"met"``). So a partial
#: report cited for an item that does not accept its kind reports
#: ``partial_coverage``, exactly as a *failing* report of that kind reports
#: ``check_failed`` rather than ``wrong_kind`` today. The three gated
#: refusals differ because they apply to an envelope that *passed*, where
#: "cite a different artifact" really is the more actionable answer.
_REASON_PARTIAL_COVERAGE = "partial_coverage"
