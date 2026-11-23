"""Tie-coverage readers for T1 item 11 ("Power delivery (structural)").

Split out of ``signoff_power_delivery.py`` (issue #6987) following the
precedent of ``signoff_power_delivery_substrate.py`` (#5346): the missing-tie
*coverage* helpers -- what the cited `klt erc` run's own ``erc_coverage``
says about the declared ``ties[]`` classes -- live here so the parent module
stays within the per-file size ratchet (``scripts/README.md``). Pure moves
plus one new gate; every function keeps its exact signature and behaviour.
"""

from __future__ import annotations

from typing import Any

from .coverage import work_id

#: Issue #6987: a declared ``ties[]`` class the cited `klt erc` run's
#: envelope carries **no verdict** for -- its exact
#: ``erc.missing_tie:["<name>"]`` work identity is absent from
#: ``erc_coverage.checked``, with no explicit skip record and no finding.
#: A zero finding count cannot tell "the rule ran and found nothing" from
#: "the rule never ran", so the class's silence is not a clean tie.
#: ``detail.ties_unchecked`` names the verdictless classes. The
#: producer-format skip record still renders ``supply_spec_incomplete``
#: (checked first, issue #2199), and an envelope that omits the entire
#: ``erc_coverage`` block is graded exactly as it always was (every report
#: before #2179). Fix: re-run `klt erc` so every declared tie class is
#: actually computed.
_REASON_TIE_COVERAGE_INCOMPLETE = "tie_coverage_incomplete"


def _erc_missing_tie_skipped(envelope: dict[str, Any]) -> bool:
    """Whether the cited `klt erc` run reports any ``erc.missing_tie`` work
    it **declined to perform** (issue #2199).

    `klt erc` records a ``ties[]`` entry whose declared tap region cannot
    be told apart from an ordinary source/drain contact in
    ``erc_coverage.skipped`` rather than ``checked``
    (``docs/cli/erc.md`` → "Well/tap connectivity"). Item 11 requires zero
    ``erc.missing_tie`` findings, and such a tie reports zero for a reason
    that has nothing to do with taps -- the same "an uncomputed check is
    not a clean one" rule that already rejects a spec declaring no
    ``ties[]`` at all, applied to a declaration that was made but could not
    be answered.

    Matched on the work identity's ``erc.missing_tie:`` domain prefix
    (:func:`~klayout_tools.coverage.work_id`) rather than on the skip
    reason, so a future `klt erc` that declines this rule for some *other*
    stated reason is caught by the same gate. Issue #2255 is the first such
    reason to actually arrive -- ``degenerate_well_assertion``, a
    caller-asserted substrate region indistinguishable from the whole
    top-cell extent -- and needed no change here, which is the property this
    prefix match was chosen for. Issue #2339's
    ``degenerate_well_selection`` -- a declared well-side class selection
    (``ties[].well_requires``/``well_excludes``) that kept every merged
    shape of the drawn well layer, or none of them -- is the second, and
    needed no change here either. Issue #2377's ``empty_well_region`` -- a
    ``ties[]`` entry naming a *drawn* ``well_layer`` with no geometry at all
    in the stream, the unselected form of the same absence-of-evidence state
    -- is the third, and needed no change here either.
    An envelope with no ``erc_coverage`` block
    (every report before #2179) skips nothing and is graded exactly as it
    was.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return False
    return any(
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and record["id"].startswith("erc.missing_tie:")
        for record in block.get("skipped") or []
    )


def _erc_declared_ties_unchecked(
    envelope: dict[str, Any], tie_names: list[str]
) -> list[str]:
    """The declared ``ties[]`` classes the cited `klt erc` run's own
    ``erc_coverage.checked`` does not carry (issue #6987) -- every declared
    name whose exact ``erc.missing_tie:["<name>"]`` work identity
    (:func:`~klayout_tools.coverage.work_id`, the same form the producer
    records in ``run_erc``) is absent from that list, in declaration order,
    ``[]`` when every declared class has its verdict.

    The gap this closes: item 11 was gated on the rule **firing** (a
    finding) but not on it having **run** for every declared class, so
    deleting a class's identity from ``checked`` -- with an empty
    ``skipped`` list and zero findings -- left the item ``met`` off an
    envelope that carries no verdict for one of its required rules at all.
    ``erc_coverage`` exists precisely to distinguish "the rule ran and
    found nothing" from "the rule never ran"; a zero finding count is
    produced identically by both, so absence from ``checked`` is absence of
    evidence, never a default-clean read.

    Compared as exact work identities, not counts or prefixes, so nothing
    weaker stands in for a missing class: a duplicate of another class's
    ID, a different (or undeclared-disclosed) class's ID, an
    ``checked_by_assertion``/``checked_by_well_assertion`` entry alone (the
    producer records an asserted tie in ``checked`` *and* the assertion
    list, so the assertion list is never evidence a class ran), the
    ``missing_tie_checked`` summary flag, or the class moved verbatim into
    ``skipped`` as a bare string (a shape the explicit-skip gate of
    :func:`_erc_missing_tie_skipped` cannot match -- it reads ``{id,
    reason}`` records) each leave the class verdictless and named here.

    An envelope with no ``erc_coverage`` block (every report before #2179)
    supplies no coverage evidence at all and is graded exactly as it was --
    the same compatibility path :func:`_erc_missing_tie_skipped` and
    ``_erc_missing_tie_disclosed`` (in ``signoff_power_delivery.py``)
    already promise. A block whose ``checked`` is not a list likewise
    supplies no positive evidence for any class, so every declared name is
    returned.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    checked = block.get("checked")
    if not isinstance(checked, list):
        return list(tie_names)
    checked_ids = {identity for identity in checked if isinstance(identity, str)}
    return [
        name
        for name in tie_names
        if work_id("erc.missing_tie", name) not in checked_ids
    ]


def _erc_ties_checked_by_well_assertion(envelope: dict[str, Any]) -> list[str]:
    """The cited `klt erc` run's ``erc_coverage.checked_by_well_assertion``
    (issues #2255 and #2540): the ``erc.missing_tie`` work identities whose
    **well side** rested on caller-named coordinates rather than purely on
    drawn geometry -- ``[]`` for every run that named none, and for every
    report produced before the field existed. Two spec forms land there:

    - ``ties[].well_layer: null`` + ``ties[].well_boxes`` (issue #2255) -- the
      **well region itself** asserted, because the block sits in a native
      substrate that draws no well/tub layer at all;
    - ``ties[].well_requires_boxes`` / ``ties[].well_excludes_boxes`` (issue
      #2540) -- a *drawn* well whose **class selection** was named in boxes,
      because no drawn layer separates the tub's two bias classes.

    Carried into a ``"met"`` item 11 citation beside
    ``erc_ties_checked_by_assertion``'s tap-side list, and deliberately
    **not** merged into it, because the two state different things about the
    same verdict. A ``tap_boxes`` assertion says which of the drawn tap
    geometry counts as the tap; the well is still drawn, and still measured.
    This list says the *well* side was the caller's word -- the weaker of the
    two claims, and the one a grader is most likely to want to see stated
    explicitly. A purely marker-layer selection
    (``well_requires``/``well_excludes``) is not in it: the stream draws its
    own partition, so that is an ordinary geometrically-derived pass.

    It does not change the verdict: item 11 is ``"met"`` on either asserted
    form exactly as on a drawn-well one. It can be, because both are
    falsifiable and `klt erc` falsifies them where it can -- each asserted
    polygon must independently contain a tap that reaches the declared net, an
    assertion indistinguishable from the whole top-cell extent is rejected as
    degenerate (``docs/cli/erc.md`` → "A block with no drawn well at all"),
    and a box selection keeping every shape of the drawn layer or none of them
    is rejected the same way (same doc → "A two-class tub with no marker layer
    to separate them"). Both land in ``erc_coverage.skipped`` where
    :func:`_erc_missing_tie_skipped` already renders them ``unmet``.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    return [
        identity
        for identity in block.get("checked_by_well_assertion") or []
        if isinstance(identity, str)
    ]
