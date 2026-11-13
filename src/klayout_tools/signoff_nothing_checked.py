"""`klt signoff`'s ``nothing_checked`` convention, for both of its consumers.

:data:`~klayout_tools.signoff._REASON_NOTHING_CHECKED` (issue #1996) names the
one case a tier item must never grade as evidence and must never report as a
defect: the cited artifact's own ``coverage`` block says the run checked
**nothing**, so its verdict -- whatever token it carries -- states nothing at
all about the design. A DRC deck whose whole rule set was gated behind an unset
``--deck-var``, a `klt sim` corner matrix that expanded to zero corners, a
`klt pex` run with no ``delta[]`` row to compare.

Reading the claim (:func:`nothing_checked_reasons`) and rendering it for the
envelope-aggregation path (:func:`nothing_checked_detail`) live here together
with the tier-report half (:func:`nothing_checked_item_detail`,
:func:`reports_zero_coverage`), so the two modes cannot drift apart about
whether an artifact is empty or about what the report says it read.

**Two routes reach the refusal**, differing only in what the producer called
the run:

- the envelope reports the kind's own *success* token, and
  :func:`~klayout_tools.signoff._kind_gated_refusal` refuses it as vacuous --
  the original #1996 case, and the shape a producer predating the common
  coverage rollup emits;
- the envelope reports :data:`ZERO_COVERAGE_STATUS`, the token
  :func:`~klayout_tools.coverage.rollup_status` assigns the zero-coverage row,
  and :func:`~klayout_tools.signoff._non_passing_reason` names it there instead
  of ``check_failed``.

The second route is what issue #5479 added, and why it had to exist: **every**
producer that has adopted the common rollup reports that token for a run which
checked nothing, so such an envelope fails
:func:`~klayout_tools.signoff._check_passed` and was given ``check_failed``
before the vacuous-verdict gate was ever consulted. The report therefore said
"a check ran and failed" for a run that ran nothing -- the conflation issue
#826 forbids -- and the documented ``nothing_checked`` reason was dead for
current evidence. `klt pex --extract-only` (issue #3739) is simply the first
workflow that commits such an envelope on purpose; the defect was never
pex-specific, and nothing here is per-kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .coverage import (
    RESULT_ZERO,
    ROLLUP_TABLE,
    coverage_nothing_checked,
    coverage_nothing_checked_reasons,
    coverage_state,
    coverage_validation_error,
    rollup_status,
)

#: The status token a producer applying the common rollup rule reports for a
#: run whose coverage is zero. Derived from
#: :func:`~klayout_tools.coverage.rollup_status` rather than spelled out, so
#: this gate and the producers it grades cannot drift apart about it: the three
#: non-verdict rows keep one fixed token each whatever success/failure
#: vocabulary a verb passes in, which is exactly why reading it is safe for
#: every kind at once rather than per kind.
ZERO_COVERAGE_STATUS = rollup_status(ROLLUP_TABLE[RESULT_ZERO])


def nothing_checked_reasons(envelope: Mapping[str, Any]) -> list[str] | None:
    """The reason codes behind ``envelope``'s own "I checked nothing"
    statement (issue #1996) -- or ``None`` when it makes no such statement.

    ``None`` (not ``[]``) for every envelope whose ``coverage`` block is
    absent, non-object, or reports ``nothing_checked: false``, so a caller can
    gate on the *presence* of a claim with a plain ``is None`` test and never
    has to decide what an empty list means. That covers evidence committed
    before the convention existed, and every kind that does not emit a
    ``coverage`` block at all -- neither reads as a vacuous run.

    Deliberately kind-agnostic, unlike the per-kind disclosures beside its
    caller: ``nothing_checked`` is one shared convention across every verb
    that can reach a vacuous verdict (`klt drc`, `klt sim`, `klt pex` today),
    so hard-coding the list would silently ignore the next verb that adopts
    it. Reading is delegated to :mod:`klayout_tools.coverage` so the "missing
    block makes no statement" rule is applied identically by every consumer.

    A run may report more than one reason; the list is returned in the
    producing verb's own order, verbatim -- including any code this `klt`
    build does not itself know, so a newer producer's reason survives.
    """
    block = cast(dict[str, Any], envelope)
    if not coverage_nothing_checked(block):
        return None
    return coverage_nothing_checked_reasons(block)


def nothing_checked_detail(
    envelope: Mapping[str, Any], *, partial: Mapping[str, Any] | None
) -> dict[str, Any]:
    """:func:`~klayout_tools.signoff._detail`'s ``nothing_checked_reasons`` key
    (issue #1996) and the coverage-rollup keys beside it, or an empty dict when
    ``envelope`` makes no coverage statement at all.

    A merge-in fragment rather than a mutation of the caller's dict, matching
    how every other optional detail key beside it is decided -- and keeping
    the "is this key present at all?" branch in one named place rather than
    adding a fifth conditional to :func:`~klayout_tools.signoff._detail`
    itself. ``partial`` is that module's own partial-coverage disclosure
    (issue #2109), passed in rather than recomputed here: it is a statement
    about *skipped requested work*, which is the opposite case from this one.
    """
    reasons = nothing_checked_reasons(envelope)
    detail: dict[str, Any] = (
        {} if reasons is None else {"nothing_checked_reasons": reasons}
    )
    state = coverage_state(cast(dict[str, Any], envelope))
    if state != "legacy":
        detail["coverage_state"] = state
    if state == "malformed":
        detail["coverage_error"] = coverage_validation_error(envelope.get("coverage"))
    if partial is not None:
        detail["coverage_qualification"] = partial
    return detail


def reports_zero_coverage(envelope: Mapping[str, Any]) -> bool:
    """Whether ``envelope``'s own top-level ``status`` is
    :data:`ZERO_COVERAGE_STATUS`.

    Only half of the question -- the producer's *claim* that it landed on the
    zero-coverage row. The envelope's ``coverage`` block still has to agree,
    which the caller establishes through
    :func:`~klayout_tools.signoff._coverage_refusal` rather than by re-reading
    the block here: that function carries the kind-specific widenings (`klt
    erc`'s second coverage scope, `klt sim`'s unlimited measurements) and
    those must not be duplicated.

    Read at the top level only. A nested verdict that rolls up separately --
    `klt power`'s ``em_verdict.status`` -- is deliberately not consulted: its
    envelope carries no top-level ``status`` at all, and conflating the two
    would relabel a kind whose grading rule lives elsewhere.
    """
    return envelope.get("status") == ZERO_COVERAGE_STATUS


def nothing_checked_item_detail(
    envelope: Mapping[str, Any], cited_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``detail`` for a ``nothing_checked`` **tier item** (issue #5479).

    Two keys, both additive and neither graded on:

    - ``nothing_checked_reasons`` -- the envelope's own stable reason codes
      (``["no_delta_rows"]`` for a `klt pex` run with nothing to compare),
      through :func:`nothing_checked_reasons`, so an unrecognised code from a
      newer producer survives here exactly as on the ``checks[]`` path;
    - ``cited_evidence`` -- ``cited_evidence`` verbatim: the caller passes the
      same block a ``"met"`` item's ``citation`` would carry, built by
      :func:`~klayout_tools.signoff._citation` rather than by a second field
      list here, so the two can never list different fields. It names *what
      was read*: the file or command, the kind, the envelope's own ``status``,
      its ``content_hash``, and the per-kind passthroughs (a `drc` report's
      ``coverage``, a `pex` report's ``body_bias``, issue #1983).

    ``citation`` itself stays ``None``: it is documented as present only on a
    ``"met"`` item (``docs/cli/signoff.md``, ``docs/json-contract.md``) and
    that contract is unchanged. The distinction this restores is the one a
    committed record needs -- a reader can tell *which* extraction was cited,
    rather than seeing a verdict with no artifact behind it at all. Attached on
    both routes above, which must not differ in what they say about one run.
    """
    return {
        "nothing_checked_reasons": nothing_checked_reasons(envelope) or [],
        "cited_evidence": dict(cited_evidence),
    }
