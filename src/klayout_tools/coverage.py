"""Versioned checked-work coverage and the common rollup rule
(issues #1996/#2108/#2109).

Version 1 adds ``schema_version``, ``known``, ``checked`` identities and
``skipped``/``inapplicable``/``unknown`` identity/reason records to a producer's
existing ``coverage`` fields. ``nothing_checked`` remains available, but
requires *known* zero checked work: unknown execution is not proof of zero
or complete execution. Skips describe requested work; inapplicable records
never count as skipped requests. All identities are unique within a block.

``coverage_state`` validates the versioned contract before classifying it.
It reports full/partial/zero/unknown/malformed or legacy. Optional Verilator
code-coverage percentages and older envelopes without the common fields
remain legacy data. An old explicit ``nothing_checked: true`` still reports
zero.

:func:`coverage_rollup` is Phase 2's single decision table on top of that
classification: one rule every producer and every consumer applies, so a
partial result can never be rendered as an unconditional success by one path
and refused by another. :func:`rollup_status` maps its result onto a verb's
own status vocabulary; :func:`coverage_refusal_reason` is the consumer-side
qualification gate. The table, its reason codes, its exit codes and the
migration contract the six per-path adapters build against are documented in
``docs/coverage-contract.md``.

The JSON Schema is ``docs/schemas/coverage.schema.json``. Cross-list identity
uniqueness is additionally enforced here. This module imports no producer or
consumer; the contract can be shared without changing verb boundaries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

COVERAGE_SCHEMA_VERSION = 1
REASON_NO_RELEVANT_CHECKS = "no_relevant_checks"


def work_id(domain: str, *parts: str | int) -> str:
    """Stable, unambiguous identities even when user names contain separators."""
    return domain + ":" + json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


#: The reason codes :data:`NOTHING_CHECKED_REASONS` documents, as module
#: constants so producers never spell one wrong and consumers can match on
#: an imported name rather than a string literal.
#:
#: `klt drc`, curated engine: the deck named by ``deck`` declares no rules at
#: all (a degenerate/empty deck), so there was never anything to run.
REASON_DECK_HAS_NO_RULES = "deck_has_no_rules"

#: `klt drc`, curated engine: the deck declares rules, but every one of them
#: was skipped because its input layer(s) are absent from this stream -- the
#: all-rules-skipped case ``coverage.rules_skipped`` already enumerates.
REASON_ALL_RULES_SKIPPED = "all_rules_skipped"

#: Legacy DRC v1 reason. RDB categories do not establish rule execution;
#: DRC v2 reports uninstrumented external execution as unknown instead.
REASON_DECK_REPORTED_NO_RULES = "deck_reported_no_rules"

#: `klt sim`: the request's PVT corner matrix expanded to zero corners, so no
#: simulation ran and every roll-up counter is trivially zero.
REASON_EMPTY_CORNER_MATRIX = "empty_corner_matrix"

#: `klt sim`: every ``measurements[].limits`` object the request declared
#: consists solely of keys `klt sim` does not recognise (only ``min``/``max``
#: are), so not one declared limit was ever applied -- a typo'd bound reads as
#: "no bound" and every measurement passes unconditionally.
REASON_UNRECOGNIZED_LIMIT_KEYS = "unrecognized_limit_keys"

#: `klt pex`: the run produced no delta rows at all, so no schematic-vs-
#: extracted comparison was performed. Distinct from a comparison that ran and
#: found every row within tolerance -- that case emits one ``delta[]`` row per
#: compared ``(corner, measurement)`` pair, each with ``status: "pass"``.
REASON_NO_DELTA_ROWS = "no_delta_rows"

#: Every declared reason code, mapped to a one-line human explanation. A
#: consumer rendering a refusal can quote this rather than re-deriving prose
#: per verb; an unknown code (a newer `klt` than the reader) is rendered as-is,
#: never dropped.
NOTHING_CHECKED_REASONS: dict[str, str] = {
    REASON_DECK_HAS_NO_RULES: "the deck declares no rules",
    REASON_ALL_RULES_SKIPPED: (
        "every deck rule was skipped (its layer(s) are absent from the stream)"
    ),
    REASON_DECK_REPORTED_NO_RULES: (
        "the legacy deck report declares no rule categories (execution unknown)"
    ),
    REASON_NO_RELEVANT_CHECKS: "no relevant check was performed",
    REASON_EMPTY_CORNER_MATRIX: "the corner matrix expanded to zero corners",
    REASON_UNRECOGNIZED_LIMIT_KEYS: (
        "every declared measurements[].limits object used only unrecognised "
        "keys (min/max are the only ones applied)"
    ),
    REASON_NO_DELTA_ROWS: (
        "no schematic-vs-extracted delta row was produced, so nothing was compared"
    ),
}


def build_nothing_checked(reasons: list[str]) -> dict[str, Any]:
    """The two convention keys, derived from ``reasons``.

    ``nothing_checked`` is exactly "at least one reason applies", so a
    producer can never emit the incoherent ``{"nothing_checked": true,
    "nothing_checked_reasons": []}`` (or its inverse) by hand. ``reasons`` is
    copied and left in the caller's order -- the codes are declared per verb
    and their order is the verb's own narrative, not something to re-sort.
    """
    return {
        "nothing_checked": bool(reasons),
        "nothing_checked_reasons": list(reasons),
    }


def _identity_list_error(value: Any, *, records: bool) -> str | None:
    if not isinstance(value, list):
        return "work must be an array"
    identities = []
    for item in value:
        if records:
            if not isinstance(item, dict) or set(item) != {"id", "reason"}:
                return "work records require id and reason"
            reason = item["reason"]
            if not isinstance(reason, str) or not re.fullmatch(
                r"[a-z][a-z0-9_]*", reason
            ):
                return "work reason must be a nonempty stable code"
            item = item["id"]
        if not isinstance(item, str) or not item.strip():
            return "work identity must be a nonempty string"
        identities.append(item)
    if len(set(identities)) != len(identities):
        return "work identities must be unique"
    return None


def _work_lists_error(block: dict[str, Any]) -> str | None:
    identities = []
    for field in ("checked", "skipped", "inapplicable", "unknown"):
        error = _identity_list_error(block.get(field), records=field != "checked")
        if error:
            return f"{field}: {error}"
        identities.extend(
            block[field] if field == "checked" else [r["id"] for r in block[field]]
        )
    if len(set(identities)) != len(identities):
        return "work identity occurs in more than one coverage category"
    return None


def coverage_validation_error(block: Any) -> str | None:
    """Validate a common v1 block, including consistency of derived fields."""
    if not isinstance(block, dict):
        return "coverage must be an object"
    version = block.get("schema_version")
    if type(version) is not int or version != COVERAGE_SCHEMA_VERSION:
        return "unsupported or missing coverage.schema_version"
    if type(block.get("known")) is not bool:
        return "coverage.known must be a boolean"
    error = _work_lists_error(block)
    if error:
        return error
    return _coverage_consistency_error(block)


def _coverage_consistency_error(block: dict[str, Any]) -> str | None:
    if block["known"] != (not block["unknown"]):
        return "known contradicts unknown work"
    zero = block["known"] and not block["checked"]
    if type(block.get("nothing_checked")) is not bool:
        return "nothing_checked must be a boolean"
    if block["nothing_checked"] != zero:
        return "nothing_checked contradicts checked/unknown work"
    reasons = block.get("nothing_checked_reasons")
    error = _identity_list_error(reasons, records=False)
    if error:
        return f"nothing_checked_reasons: {error}"
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", reason) for reason in reasons):
        return "nothing_checked_reasons must be stable reason codes"
    if bool(reasons) != zero:
        return "nothing_checked_reasons contradicts nothing_checked"
    return None


def build_check_coverage(
    *,
    checked: list[str],
    skipped: list[dict[str, str]] | None = None,
    inapplicable: list[dict[str, str]] | None = None,
    unknown: list[dict[str, str]] | None = None,
    nothing_checked_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Build one deterministic common block without aliasing producer data."""
    block: dict[str, Any] = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "known": not bool(unknown),
        "checked": sorted(checked),
    }
    for key, values in (
        ("skipped", skipped),
        ("inapplicable", inapplicable),
        ("unknown", unknown),
    ):
        block[key] = sorted((dict(r) for r in values or []), key=lambda r: r["id"])
    zero = block["known"] and not checked
    reasons = nothing_checked_reasons or sorted(
        {r["reason"] for r in (skipped or []) + (inapplicable or [])}
    )
    block.update(
        build_nothing_checked((reasons or [REASON_NO_RELEVANT_CHECKS]) if zero else [])
    )
    error = coverage_validation_error(block)
    if error:
        raise ValueError(f"invalid coverage: {error}")
    return block


def coverage_state(envelope: dict[str, Any]) -> str:
    """Classify validated checked-work coverage; never infer legacy fullness."""
    block = envelope.get("coverage")
    legacy = (
        "unknown"
        if envelope.get("engine") == "klayout"
        and isinstance(envelope.get("violations"), list)
        else "legacy"
    )
    if block is None:
        return legacy
    if not isinstance(block, dict):
        return "malformed"
    fields = {
        "schema_version",
        "known",
        "checked",
        "skipped",
        "inapplicable",
        "unknown",
    }
    if not fields.intersection(block):
        return "zero" if block.get("nothing_checked") is True else legacy
    if coverage_validation_error(block):
        return "malformed"
    if not block["known"]:
        return "unknown"
    if block["nothing_checked"]:
        return "zero"
    return "partial" if block["skipped"] else "full"


#: The eight rows of the common rollup decision table (issue #2109). The six
#: coverage-derived rows are spelled exactly as :func:`coverage_state`'s own
#: values, so the table maps onto it one-for-one and a future state with no
#: row here raises rather than silently defaulting to success. The two
#: precedence rows -- a run that errored, and a run whose executed checks
#: actually found a defect -- are decided before coverage is consulted at all.
RESULT_ERRORED = "errored"
RESULT_FAILED = "failed"
RESULT_MALFORMED = "malformed"
RESULT_UNKNOWN = "unknown"
RESULT_ZERO = "zero"
RESULT_PARTIAL = "partial"
RESULT_LEGACY = "legacy"
RESULT_FULL = "full"

#: Exit codes the rollup assigns, matching the meanings ``docs/json-contract.md``
#: already fixes for every verb: ``0`` success, ``3`` a successful run that
#: found a defect, ``4`` a run that reached no usable verdict. Spelled here
#: rather than imported from :mod:`klayout_tools.cli.output` so this module
#: keeps importing nothing from the rest of the package.
EXIT_OK = 0
EXIT_FINDINGS = 3
EXIT_NOT_CHECKED = 4


@dataclass(frozen=True)
class CoverageRollup:
    """One row of the common decision table (issue #2109).

    ``result`` is the stable machine-readable row name (one of the
    ``RESULT_*`` constants). ``reason`` is the stable code a consumer cites
    when this row is not an earned, complete success -- ``None`` only for
    :data:`RESULT_FULL`, which has nothing to explain. ``exit_code`` is the
    CLI exit behaviour the row implies.

    Three nested questions, deliberately distinct rather than collapsed into
    one boolean, because the three have different answers for a partial run:

    - ``reached_verdict`` -- this run says *something* about the design.
      False for the three rows that say nothing usable (zero, unknown,
      malformed) and for a run that errored. This is the hard gate
      :func:`coverage_refusal_reason` applies.
    - ``unconditional`` -- this row may be reported as the verb's
      unconditional success token, and cited as fully passing evidence. A
      nonempty list of skipped *requested* work makes it False: that is the
      operator's rule for #1988, "no verb returns ``pass`` with a non-empty
      skip list".
    - ``complete`` -- this run positively established complete applicable
      coverage. True only for :data:`RESULT_FULL`. Nothing upgrades a
      partial, zero, unknown, malformed or unreported-coverage run into it.

    :data:`RESULT_LEGACY` is the one row where ``unconditional`` and
    ``complete`` disagree, and the disagreement is the whole point: evidence
    that predates the contract keeps grading exactly as the verb-specific
    rules that always governed it say, *and* is never counted as proof of
    completeness. ``docs/coverage-contract.md`` records that policy.
    """

    result: str
    reason: str | None
    exit_code: int
    unconditional: bool
    complete: bool

    @property
    def successful(self) -> bool:
        """Whether this row is a non-failing outcome (exit ``0``).

        True for partial as well as legacy and full: a partial result is a
        real, reportable outcome, it is simply not an *unconditional* success
        and not complete coverage.
        """
        return self.exit_code == EXIT_OK

    @property
    def reached_verdict(self) -> bool:
        """Whether this row states any usable verdict about the design.

        False for exactly the ``exit 4`` rows -- an errored run, and the
        three coverage rows (zero, unknown, malformed) whose evidence cannot
        be read as a statement about the design at all.
        """
        return self.exit_code != EXIT_NOT_CHECKED


#: The decision table itself, as data so a regression matrix can walk every
#: row and no consumer can re-derive a different answer. Read through
#: :func:`coverage_rollup`.
ROLLUP_TABLE: dict[str, CoverageRollup] = {
    RESULT_ERRORED: CoverageRollup(
        result=RESULT_ERRORED,
        reason="check_errored",
        exit_code=EXIT_NOT_CHECKED,
        unconditional=False,
        complete=False,
    ),
    RESULT_FAILED: CoverageRollup(
        result=RESULT_FAILED,
        reason="check_failed",
        exit_code=EXIT_FINDINGS,
        unconditional=False,
        complete=False,
    ),
    RESULT_MALFORMED: CoverageRollup(
        result=RESULT_MALFORMED,
        reason="malformed_coverage",
        exit_code=EXIT_NOT_CHECKED,
        unconditional=False,
        complete=False,
    ),
    RESULT_UNKNOWN: CoverageRollup(
        result=RESULT_UNKNOWN,
        reason="coverage_unknown",
        exit_code=EXIT_NOT_CHECKED,
        unconditional=False,
        complete=False,
    ),
    RESULT_ZERO: CoverageRollup(
        result=RESULT_ZERO,
        reason="nothing_checked",
        exit_code=EXIT_NOT_CHECKED,
        unconditional=False,
        complete=False,
    ),
    RESULT_PARTIAL: CoverageRollup(
        result=RESULT_PARTIAL,
        reason="partial_coverage",
        exit_code=EXIT_OK,
        unconditional=False,
        complete=False,
    ),
    RESULT_LEGACY: CoverageRollup(
        result=RESULT_LEGACY,
        reason="coverage_not_reported",
        exit_code=EXIT_OK,
        unconditional=True,
        complete=False,
    ),
    RESULT_FULL: CoverageRollup(
        result=RESULT_FULL,
        reason=None,
        exit_code=EXIT_OK,
        unconditional=True,
        complete=True,
    ),
}


def coverage_rollup(
    envelope: dict[str, Any],
    *,
    failed: bool = False,
    errored: bool = False,
) -> CoverageRollup:
    """Apply the common rollup rule to one envelope (issue #2109).

    ``failed`` is "at least one executed check found a defect"; ``errored``
    is "this run could not complete". Both are the producer's own
    non-coverage outcome, and both are decided **before** coverage: a run
    that failed is reported as the failure it is, never masked by a coverage
    gap, and a coverage gap is never upgraded away by a clean-looking
    status. ``errored`` wins over ``failed`` -- a run that did not complete
    cannot vouch for the findings it did emit.

    With neither, the row is exactly :func:`coverage_state`'s classification:
    a nonempty skip list of *requested* work yields :data:`RESULT_PARTIAL`
    and never an unconditional success, zero known checked work yields
    :data:`RESULT_ZERO`, and unmeasurable or self-contradictory coverage
    yields :data:`RESULT_UNKNOWN`/:data:`RESULT_MALFORMED`. Inapplicable
    work is not a skipped request and so does not make an otherwise complete
    run partial -- that distinction lives in :func:`coverage_state`, and is
    the reason this rule can be strict about skips without punishing a verb
    for the work its invocation never asked for.
    """
    if errored:
        return ROLLUP_TABLE[RESULT_ERRORED]
    if failed:
        return ROLLUP_TABLE[RESULT_FAILED]
    return ROLLUP_TABLE[coverage_state(envelope)]


def rollup_status(
    rollup: CoverageRollup,
    *,
    success: str = "pass",
    partial: str | None = None,
    failure: str = "fail",
    errored: str = "error",
) -> str:
    """The verb-facing status token for one rollup row (issue #2109).

    Producers differ in what they call success (`klt drc` says ``"clean"``,
    most others ``"pass"``) but must not differ in *when* they may say it,
    so the vocabulary is a parameter and the mapping is not. ``partial``
    defaults to ``f"{success}_partial"``, which reproduces the
    ``"pass_partial"`` token #1997 already shipped for `klt erc`/`klt power`
    rather than competing with it.

    The three non-verdict rows keep the tokens ``docs/json-contract.md``
    already fixed for them -- ``"not_checked"`` and ``"coverage_unknown"``
    (both exit ``4``) -- so an adapter cannot invent a fourth spelling.
    ``malformed`` and ``legacy`` are consumer-side rows: a producer building
    its block through :func:`build_check_coverage` can reach neither, since
    that helper validates what it emits and always emits the common fields.
    They are mapped anyway (to ``"malformed_coverage"`` and to plain
    ``success``) so the table is total and no caller has to special-case a
    row it believes unreachable.
    """
    return {
        RESULT_ERRORED: errored,
        RESULT_FAILED: failure,
        RESULT_MALFORMED: "malformed_coverage",
        RESULT_UNKNOWN: "coverage_unknown",
        RESULT_ZERO: "not_checked",
        RESULT_PARTIAL: partial if partial is not None else f"{success}_partial",
        RESULT_LEGACY: success,
        RESULT_FULL: success,
    }[rollup.result]


def coverage_skipped_work(envelope: dict[str, Any]) -> list[dict[str, str]]:
    """The requested work a :data:`RESULT_PARTIAL` envelope did not check.

    ``[]`` for every other row, including a legacy envelope whose own
    verb-specific fields describe a gap (`klt drc`'s ``rules_skipped``, say):
    those are not the versioned, identity-bearing records this contract
    defines, and re-reading them here would relabel legacy data as a common
    partial claim. Records are returned as copies, in the producer's own
    sorted order, so a consumer can quote them without aliasing the source.
    """
    if coverage_state(envelope) != RESULT_PARTIAL:
        return []
    return [dict(record) for record in envelope["coverage"]["skipped"]]


def coverage_nothing_checked(envelope: dict[str, Any]) -> bool:
    """Whether ``envelope`` states that it checked nothing.

    ``False`` -- "makes no such statement" -- for an envelope with no
    ``coverage`` key, a non-object one, or one predating this convention. That
    is deliberately *not* the same as "this run had full coverage": absence of
    the field is absence of evidence, and a consumer that wants to require a
    positive coverage statement must check for the key itself.

    Only a literal ``True`` counts, so a truthy-but-wrong value (a non-empty
    string from a hand-rolled generic envelope, say) is not mistaken for the
    boolean the convention specifies.
    """
    return coverage_state(envelope) == "zero"


def coverage_refusal_reason(envelope: dict[str, Any]) -> str | None:
    """Why ``envelope`` states no usable verdict at all -- the hard gate.

    The consumer half of :func:`coverage_rollup`'s ``reached_verdict``
    question: ``None`` unless the envelope's coverage is zero, unknown or
    malformed, in which case that row's stable ``reason`` code. This is what
    `klt signoff` refuses evidence on, and it is unchanged by Phase 2 -- a
    partial run *did* check something, so refusing it outright would discard
    a real result rather than qualify it.

    Use :func:`coverage_qualification_reason` for the narrower "is this an
    unconditional, complete pass?" question.

    Callers pass the envelope alone: a run's own failure is a *more*
    actionable reason than its coverage, and every caller already reports
    that first (see :func:`coverage_rollup`'s ``failed``/``errored``
    precedence), so feeding it in here would only let a coverage code mask a
    real defect.
    """
    rollup = coverage_rollup(envelope)
    return None if rollup.reached_verdict else rollup.reason


def coverage_qualification_reason(envelope: dict[str, Any]) -> str | None:
    """Why ``envelope`` may not be reported as an unconditional success.

    ``None`` exactly when :func:`coverage_rollup` says ``unconditional``;
    otherwise the row's stable ``reason`` code -- including
    ``"partial_coverage"`` for successful checks alongside a nonempty list
    of skipped *requested* work, which is the case Phase 2 exists to name.

    Strictly stronger than :func:`coverage_refusal_reason`: every hard
    refusal is also a qualification failure, and partial coverage is a
    qualification failure that is not a hard refusal. A consumer that must
    assert *positively established* complete coverage -- rather than merely
    "not partial" -- reads ``coverage_rollup(envelope).complete`` instead,
    which the legacy row cannot satisfy either.
    """
    rollup = coverage_rollup(envelope)
    return None if rollup.unconditional else rollup.reason


def coverage_nothing_checked_reasons(envelope: dict[str, Any]) -> list[str]:
    """The reason codes behind :func:`coverage_nothing_checked`, in the
    producing verb's own order.

    ``[]`` whenever :func:`coverage_nothing_checked` is ``False``, and also
    for a malformed block that sets ``nothing_checked`` without a usable
    ``nothing_checked_reasons`` list -- a consumer never has to distinguish
    "no reasons" from "no list". Codes are returned verbatim (as ``str``),
    including any this `klt` build does not know, so a newer producer's
    reason survives an older reader.
    """
    if not coverage_nothing_checked(envelope):
        return []
    reasons = envelope["coverage"].get("nothing_checked_reasons")
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons]
