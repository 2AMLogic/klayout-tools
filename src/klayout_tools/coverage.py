"""Versioned checked-work coverage and legacy readers (issues #1996/#2108).

Version 1 adds ``schema_version``, ``known``, ``checked`` identities and
``skipped``/``inapplicable``/``unknown`` identity/reason records to a producer's
existing ``coverage`` fields. ``nothing_checked`` remains available, but
requires *known* zero checked work: unknown execution is not proof of zero
or complete execution. Skips describe requested work; inapplicable records
never count as skipped requests. All identities are unique within a block.

``coverage_state`` validates the versioned contract before classifying it.
It reports full/partial/zero/unknown/malformed or legacy; it does not impose
the Phase 2 partial-success policy. Optional Verilator code-coverage data
and older envelopes without the common fields remain legacy data. An old
explicit ``nothing_checked: true`` still reports zero.

The JSON Schema is ``docs/schemas/coverage.schema.json``. Cross-list identity
uniqueness is additionally enforced here. This module imports no producer or
consumer; the contract can be shared without changing verb boundaries.
"""

from __future__ import annotations

import json
import re
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
        if envelope.get("engine") == "klayout" and "violation_count" in envelope
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
    """Phase 1 qualification gate; partial-work policy belongs to Phase 2."""
    return {
        "zero": "nothing_checked",
        "unknown": "coverage_unknown",
        "malformed": "malformed_coverage",
    }.get(coverage_state(envelope))


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
