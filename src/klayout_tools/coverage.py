"""The shared ``coverage`` block convention (issue #1996).

Several `klt` verbs can reach a top-level ``"pass"``/``"clean"`` verdict on a
run that checked **nothing** -- a DRC deck whose every rule is gated behind a
feature toggle the caller never set, a `klt sim` request whose PVT corner
matrix expanded to zero corners, a `klt pex` run whose testbenches produced no
comparable ``(corner, measurement)`` pair at all. Each of those is a perfectly
well-formed envelope whose verdict is *vacuously* true, and until this module
existed there was no field a downstream reader (`klt signoff`, a fleet report,
a human) could consult to tell one apart from a real, earned pass.

This module is **data-only**: it declares the convention's reason codes and
the two reader helpers consumers use. It runs nothing and imports nothing from
the rest of the package, exactly like :mod:`klayout_tools.metrics`.

## The convention

A verb that can produce a vacuous verdict emits a top-level ``coverage``
object carrying, alongside whatever verb-specific fields it already reports::

    "coverage": {
        "...": "the verb's own coverage fields, unchanged",
        "nothing_checked": <bool>,
        "nothing_checked_reasons": [<reason code>, ...]
    }

- ``nothing_checked`` is ``true`` **only** when the run performed zero actual
  checks, so its own ``status`` says nothing about the design. It is never a
  synonym for "partial coverage": a run that checked one rule out of eighty
  reports ``false`` (the eighty-minus-one gap is what the verb's own
  coverage fields -- `klt drc`'s ``rules_skipped``/
  ``layers_in_stream_without_rules``, etc. -- are for).
- ``nothing_checked_reasons`` names *why*, using the stable codes below, and
  is always a list: empty exactly when ``nothing_checked`` is ``false``. More
  than one code can apply to the same run.
- Both keys are **additive**: adding them to a verb's existing ``coverage``
  block, or adding a ``coverage`` block to a verb that had none, earns no
  ``schema_version`` bump (``docs/json-contract.md``).
- An envelope with **no** ``coverage`` key, or one whose ``coverage`` predates
  this convention, reads as "makes no coverage statement" -- see
  :func:`coverage_nothing_checked`, which returns ``False`` for it rather
  than guessing. Absence is never evidence of a gap, and never evidence of
  full coverage either.

Consumers read it through :func:`coverage_nothing_checked` /
:func:`coverage_nothing_checked_reasons` rather than reaching into the dict,
so the "missing/malformed block reads as no statement" rule is applied
identically everywhere.
"""

from __future__ import annotations

from typing import Any

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

#: `klt drc`, KLayout engine: the PDK-native deck script ran to completion but
#: its own report declares **no rule categories**, i.e. it never reached a
#: single ``output(...)`` call. The common cause is a deck that gates its
#: whole rule set behind a feature-toggle global set via ``-rd``/``--deck-var``
#: that this invocation never set.
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
        "the deck script's report declares no rule categories -- typically a "
        "rule set gated behind a --deck-var this run never set"
    ),
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
    coverage = envelope.get("coverage")
    if not isinstance(coverage, dict):
        return False
    return coverage.get("nothing_checked") is True


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
