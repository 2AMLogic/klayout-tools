"""Per-block blocker reduction for the ``klt signoff`` fleet roll-up.

Split out of ``signoff.py`` (issue #5636) with the rest of the fleet
roll-up (issue #827, Phase 1c of epic #706); this module owns the pure
reduction half that folds one block's graded tier report into the
roll-up's per-block view: its DRC/ERC coverage disclosure
(:func:`_drc_coverage_rows`), the blocking item it reports
(:func:`_blocking_t1_item`, trimmed by :func:`_blocking_item_view`) and
the ungraded items behind it (:func:`_ungraded_t1_items`).

``signoff_fleet.py`` imports these back at its own top level; the names
this reduction needs from ``signoff.py`` (the ungradeable-item
classifiers and the T1 satisfied statuses) are imported into the
functions that need them, deferred into each function body --
``signoff.py`` imports this subsystem at its own top level, so a
module-level back-reference would be a circular import.
"""

from __future__ import annotations

from typing import Any


def _drc_coverage_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The coverage disclosure carried by every ``"met"`` ``drc``-kind
    citation in one block's rendered items (issue #2002), one row per
    citation: ``{"item", "partition", "layers_in_stream_without_rules",
    "rules_skipped", "deck_scope"}``.

    A list rather than a single object because a mixed-signal block renders
    item 3 once per partition, and because a row is identified by the item it
    backs, not assumed to be item 3 -- nothing here hard-codes which item a
    DRC citation may appear under.

    Empty when no item resolved to a coverage-reporting `klt drc` envelope:
    an unmet item 3, a cited envelope committed before ``coverage`` existed,
    or a fleet whose blocks predate it entirely. **Empty therefore means "no
    coverage was reported", never "the deck covered everything"** -- the same
    distinction :func:`_drc_coverage_disclosure` draws by returning ``None``.

    Reduces the per-block tier report this roll-up already computed; it reads
    no evidence of its own, so a block's roll-up row and its full tier report
    can never disagree about what the deck covered -- the same discipline
    :func:`_blocking_t1_item` follows.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        citation = item.get("citation") or {}
        coverage = citation.get("coverage")
        if not isinstance(coverage, dict):
            continue
        rows.append(
            {
                "item": item["id"],
                "partition": item["partition"],
                **coverage,
            }
        )
    return rows


def _blocking_t1_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a trimmed view of the T1 item in ``items`` this roll-up reports
    as the block's blocker, or ``None`` if every T1 item is met.

    The candidate order is :func:`build_tier_report`'s own item order (item
    id, then partition), with a **three-class priority** layered on top
    (issues #2178, #2203). Concretely:

    1. the first unmet T1 item this build has no grading rules for at all
       (:func:`_is_ungradeable_by_build`), if any;
    2. otherwise the first unmet T1 item that has a check behind it;
    3. otherwise the first unmet **structurally ungradeable** T1 item
       (:func:`_is_structurally_ungradeable_item` -- items 1, 2, 9 and 10
       today);
    4. otherwise ``None`` (every T1 item is met).

    **Rule 2 over rule 3 is issue #2178's rule.** ``docs/cli/signoff.md``'s
    "Items 1, 2, 9, and 10" section tells a manifest author that the honest
    default for those four is to leave them **uncited**, since no `klt` verb
    can check their topical relevance -- which means every honestly-authored
    manifest renders item 1 ``unmet``/``no_evidence`` *by construction*.
    Reducing on "first unmet in render order" therefore answered "blocked on
    item 1" for every such block, hiding whatever its real gaps were.
    Demoting those four keeps the answer to "what is the next thing to fix" a
    thing that can actually be fixed by running something. Rule 3 then keeps
    the degenerate case honest rather than silently clean: a block whose
    *only* unmet items are those four is still not T1, so it must still name
    a blocker -- one of those four -- never ``None``.

    **Rule 1 is issue #2203's rule, and it is the deliberate opposite.**
    Before it, an ``ungradeable_by_build`` row (an item id only a
    ``--tiers-doc``/``$KLT_TIERS_DOC`` copy of the doc lists) was swept into
    rule 3 *incidentally*, because it satisfies
    :func:`_is_structurally_ungradeable_item` too -- an id no grading table
    names is structurally ungradeable by that function's own derivation. But
    #2178's argument for demoting an item does not transfer to it, and in
    fact inverts:

    - **Items 1/2/9/10 are demoted because they are expected.** Every honest
      manifest has them unmet; they are the roll-up's background noise, and
      a weak answer to "why isn't this block T1 yet".
    - **An ``ungradeable_by_build`` row is never expected**, and it is the
      *sharpest* available answer to that question: it says this roll-up
      could not evaluate that item at all. Every other explanation it could
      print is conditional on it being able to grade the checklist it was
      handed; when it cannot, naming some other item implies a completeness
      the verdict does not have. It is also the only class of blocker a
      manifest edit cannot clear -- a ``graded_by_build: False`` item can
      never render ``"met"`` on this build, so "blocked on item 4, run `klt
      lvs`" would point the reader at work that cannot get this block to T1.
      The fix is a newer `klt` (or grading against the doc this one ships).

    So it outranks *both* other classes, cited (``ungradeable_by_build``) or
    uncited (``no_evidence``) -- the class is read from ``graded_by_build``,
    not from ``reason``, because the build is the gap either way.

    The full set demoted by rules 1 and 2 -- both ungradeable classes -- is
    reported alongside as the roll-up row's ``ungraded_items``
    (:func:`_ungraded_t1_items`), so nothing is dropped. When rule 1 fires,
    the named blocker is itself one of those rows; that was already true of
    rule 3's fallback before #2203, so the two fields' relationship is
    unchanged.

    T2-T4 ladder rows are never candidates -- they are always ``"unmet"`` by
    design (this toolkit's closed loop targets T1) and are not what gates the
    ``tier == "T1"`` verdict this roll-up reports against.

    This stays a pure reduction of ``items``: no evidence is re-read and no
    item's ``status``/``reason`` is recomputed, so the row and the block's own
    tier report can never disagree about *why* an item is unmet -- only about
    which unmet item is worth naming first.
    """
    from .signoff import (
        _T1_SATISFIED_STATUSES,
        _is_structurally_ungradeable_item,
        _is_ungradeable_by_build,
    )

    # One pass, three "first match" slots rather than an early return: rule 1
    # dominates rules 2 and 3, and an ungradeable-by-build item can sit
    # anywhere in render order, so no candidate is final until every T1 row
    # has been seen.
    by_build: dict[str, Any] | None = None
    gradeable: dict[str, Any] | None = None
    structural: dict[str, Any] | None = None

    for item in items:
        if item["tier"] != "T1" or item["status"] in _T1_SATISFIED_STATUSES:
            continue
        # Order matters: today every ungradeable-by-build id is *also*
        # structurally ungradeable, so this branch has to be asked first for
        # the two classes to stay distinguishable at all.
        if _is_ungradeable_by_build(item):
            if by_build is None:
                by_build = _blocking_item_view(item)
        elif _is_structurally_ungradeable_item(item["id"]):
            if structural is None:
                structural = _blocking_item_view(item)
        elif gradeable is None:
            gradeable = _blocking_item_view(item)

    for candidate in (by_build, gradeable, structural):
        if candidate is not None:
            return candidate
    return None


def _ungraded_t1_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every unmet T1 item in ``items`` with **no runnable check behind it in
    this roll-up**, in render order, trimmed to the same ``{"id", "title",
    "partition", "reason"}`` shape ``blocking_item`` uses (issues #2178,
    #2203).

    That is the union of the two ungradeable classes
    :func:`_blocking_t1_item` ranks separately:

    - **structurally ungradeable** (:func:`_is_structurally_ungradeable_item`
      -- items 1, 2, 9 and 10 today): no `klt` verb exists for the id at all;
    - **ungradeable by this build** (:func:`_is_ungradeable_by_build`): an id
      only a ``--tiers-doc``/``$KLT_TIERS_DOC`` copy of the doc lists, which
      this build has no rules for.

    The union is written out explicitly even though the first predicate
    happens to cover both populations today (issue #2203): this function's
    contract is the union, not whatever one derived predicate incidentally
    spans, and relying on that coincidence is exactly what made the two
    indistinguishable in the blocker reduction.

    Reported so that ranking those items below a runnable gap *demotes* them
    rather than hides them: a reader still sees that this block claims items
    1/2/9/10 with nothing cited behind them, but is no longer told that is
    the one thing standing between it and T1 when a real, runnable gap exists
    elsewhere. Empty for a block that cites all four (met items are never
    listed) and for a block at ``tier: "T1"``.

    Unchanged by #2203's re-ranking: which rows are listed here is the same
    set as before -- only which of them ``blocking_item`` names can move.

    Like :func:`_drc_coverage_rows`, this reduces the per-block tier report
    this roll-up already computed and reads no evidence of its own.
    """
    from .signoff import (
        _T1_SATISFIED_STATUSES,
        _is_structurally_ungradeable_item,
        _is_ungradeable_by_build,
    )

    return [
        _blocking_item_view(item)
        for item in items
        if item["tier"] == "T1"
        and item["status"] not in _T1_SATISFIED_STATUSES
        and (
            _is_structurally_ungradeable_item(item["id"])
            or _is_ungradeable_by_build(item)
        )
    ]


def _blocking_item_view(item: dict[str, Any]) -> dict[str, Any]:
    """The trimmed, roll-up-sized view of one rendered tier-report item --
    shared by ``blocking_item`` and every ``ungraded_items`` row so the two
    fields can never drift into different shapes."""
    return {
        "id": item["id"],
        "title": item["title"],
        "partition": item["partition"],
        "reason": item["reason"],
    }
