"""`klt signoff --manifest`'s ``malformed_coverage`` gate and its disclosure
(issue #5643).

A cited envelope's versioned ``coverage`` block can be present yet
structurally invalid -- hand-edited, mis-trimmed, written by a producer
against a different schema. Envelope aggregation mode already reports that
(``checks[].detail.coverage_state``/``coverage_error``), and the
single-envelope grading route already refuses it
(:func:`~klayout_tools.signoff._kind_gated_refusal`); what issue #5643
closed is the other half. T1 item 11's compound citation
(:func:`~klayout_tools.signoff_power_delivery._grade_power_delivery`)
resolves several parts and applied **no** coverage gate to any of them, so
a malformed block on any part was silently omitted from the citation while
the item rendered ``met`` on the producer's own status token -- a row whose
honesty rests on its coverage disclosure carrying no coverage statement at
all, indistinguishable from an envelope that never had a ``coverage``
block.

Both halves of the fix live here together so the two grading routes cannot
drift apart about the gate or about what a refusal says:

- :func:`malformed_coverage_refusal` -- the hard gate, read through
  :func:`~klayout_tools.coverage.coverage_refusal_reason` and narrowed to
  the malformed row alone. Deliberately *not*
  :func:`~klayout_tools.signoff._coverage_refusal`: that function's `klt
  erc` widening (issue #2179) drops a refusal whenever a valid
  ``erc_coverage`` connectivity scope stands beside the antenna one, which
  is right for the vacuous-verdict question it answers ("does this artifact
  say anything at all") and wrong for this one -- a block that *claims*
  coverage it cannot validate does not become valid because a different
  scope ran. Item 11's per-part gate must fire identically whether or not
  the ``erc`` part carries an ``erc_coverage`` scope. The zero and unknown
  rows are out of scope here (issue #5643): a part whose block is coherent
  but empty keeps grading exactly as before this module existed.
- :func:`malformed_coverage_item_detail` -- the disclosure a refusal row
  owes its reader, in the shape issue #5479 established for
  ``nothing_checked``: ``cited_evidence`` (the same block a ``"met"``
  citation would have carried, naming *which* artifact was read) plus
  ``coverage_error``, the validator's own message for what was invalid,
  read through :func:`~klayout_tools.coverage.coverage_validation_error`
  rather than re-derived. The existing single-envelope route rendered this
  reason with no ``detail`` at all, so a refusal that named neither the
  artifact nor the defect is replaced by one that names both.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from .coverage import (
    RESULT_MALFORMED,
    ROLLUP_TABLE,
    coverage_refusal_reason,
    coverage_validation_error,
)

#: The stable reason code for a structurally invalid versioned ``coverage``
#: block, derived from the rollup table rather than spelled out -- the same
#: derive-don't-duplicate rule ``signoff_nothing_checked``'s
#: ``ZERO_COVERAGE_STATUS`` follows -- so this gate and the coverage
#: contract cannot drift apart about the token.
MALFORMED_COVERAGE_REASON: str = ROLLUP_TABLE[RESULT_MALFORMED].reason


def malformed_coverage_refusal(envelope: Mapping[str, Any]) -> str | None:
    """:data:`MALFORMED_COVERAGE_REASON` when ``envelope``'s versioned
    ``coverage`` block is present and structurally invalid -- ``None``
    otherwise.

    The malformed row of :func:`~klayout_tools.coverage.coverage_refusal_reason`
    and nothing else: every other row (zero, unknown, absent, legacy) keeps
    whatever verdict its caller rendered before this module existed, per
    issue #5643's scope.
    """
    reason = coverage_refusal_reason(cast(dict[str, Any], envelope))
    return reason if reason == MALFORMED_COVERAGE_REASON else None


def malformed_coverage_item_detail(
    envelope: Mapping[str, Any], cited_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``detail`` for a ``malformed_coverage`` **tier item** (issue #5643).

    Two keys, both additive and neither graded on, matching
    :func:`~.signoff_nothing_checked.nothing_checked_item_detail`'s shape:

    - ``cited_evidence`` -- verbatim, the same block a ``"met"`` item's
      ``citation`` would carry (built by
      :func:`~klayout_tools.signoff._citation`), so the reader learns
      *which* artifact's coverage claim failed to validate;
    - ``coverage_error`` -- the validator's own message (what was invalid,
      not just that something was), the same string envelope aggregation
      reports at ``checks[].detail.coverage_error`` so the two modes agree.

    ``citation`` itself stays ``None``: it is documented as present only on
    a ``"met"`` item, and that contract is unchanged.
    """
    return {
        "cited_evidence": dict(cited_evidence),
        "coverage_error": coverage_validation_error(envelope.get("coverage")),
    }


def power_delivery_part_refusal(
    by_kind: Mapping[str, Mapping[str, Any]],
    citation_of: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> tuple[str, str | None, None, dict[str, Any]] | None:
    """Item 11's per-part coverage gate (issue #5643): the ``(status,
    reason, citation, detail)`` verdict for the first resolved part whose
    ``coverage`` block does not validate -- ``None`` when every part's does.

    Walks the parts in :data:`~klayout_tools.signoff_power_delivery.
    _POWER_DELIVERY_PART_ORDER` (imported lazily -- ``signoff_power_delivery``
    imports this module at top level), one part at a time and never a
    merged view, so the gate keeps working when item 11 grades a *set* of
    ``(erc, lvs)`` groups (issue #5597) rather than today's single
    last-wins pair. ``citation_of`` is :func:`~klayout_tools.signoff.
    _citation`, passed in for the same no-cycle reason; the refusal's
    ``detail`` names the refusing part alone, not the whole cited set.
    """
    from .signoff_power_delivery import _POWER_DELIVERY_PART_ORDER

    for kind in _POWER_DELIVERY_PART_ORDER:
        part = by_kind.get(kind)
        if part is None:
            continue
        envelope = part["envelope"]
        refusal = malformed_coverage_refusal(envelope)
        if refusal is not None:
            return (
                "unmet",
                refusal,
                None,
                malformed_coverage_item_detail(envelope, citation_of(part)),
            )
    return None
