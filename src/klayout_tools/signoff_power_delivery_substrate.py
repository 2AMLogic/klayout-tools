"""Substrate-path helpers for item 11's power-delivery grade (issue #5346).

Split out of :mod:`klayout_tools.signoff_power_delivery` to respect the
per-file size ratchet.
"""

from __future__ import annotations

from typing import Any

#: Issue #5346 (piece E of #5336): the cited ``klt erc`` run *disclosed* that
#: it could not decide substrate-path ``erc.supply_short``
#: (``erc_coverage.inapplicable[erc.supply_short]``, reason
#: ``substrate_isolation_undecidable``, with a ``missing`` list). An
#: undecided question is not a clean answer, so the item is ``"unmet"``; the
#: ``missing`` list is surfaced in ``detail.power_delivery``. Fix: declare
#: the isolation the run named (``ties[].isolation_boxes``) or the deck layer.
REASON_SUBSTRATE_ISOLATION_UNDECIDABLE = "substrate_isolation_undecidable"
#: ``erc_coverage.checked`` id of the substrate-path ``erc.supply_short`` work.
_SUBSTRATE_WORK_ID = "erc.supply_short.substrate"


def _erc_substrate_undecidable(envelope: dict[str, Any]) -> list[str] | None:
    """The ``missing`` list of a cited ``klt erc`` run that disclosed
    ``erc.supply_short`` as ``substrate_isolation_undecidable`` (issue
    #5346), else ``None``.

    Reads the design note's section 8 contract only -- the older
    ``substrate_continuity_not_modelled`` reason (issue #4384) is a different
    string and keeps grading as it did. A record with no usable ``missing``
    list still counts (``[]``): the disclosure itself is what blocks.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return None
    for record in block.get("inapplicable") or []:
        if (
            isinstance(record, dict)
            and record.get("id") == "erc.supply_short"
            and record.get("reason") == REASON_SUBSTRATE_ISOLATION_UNDECIDABLE
        ):
            return [m for m in record.get("missing") or [] if isinstance(m, str)]
    return None


def _erc_substrate_continuity(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """``{"checked": True, "by_assertion": <bool>}`` when the cited run
    evaluated substrate-path ``erc.supply_short``
    (``erc_coverage.checked`` carries ``erc.supply_short.substrate``), else
    ``None`` -- so a pre-substrate run renders its citation unchanged.
    ``by_assertion`` states the verdict rested on caller coordinates
    (``checked_by_assertion``)."""
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict) or _SUBSTRATE_WORK_ID not in (
        block.get("checked") or []
    ):
        return None
    return {
        "checked": True,
        "by_assertion": _SUBSTRATE_WORK_ID in (block.get("checked_by_assertion") or []),
    }


def add_substrate_continuity(
    power_delivery: dict[str, Any], envelope: dict[str, Any]
) -> None:
    """Add ``substrate_continuity`` to the citation when the run evaluated it."""
    substrate_continuity = _erc_substrate_continuity(envelope)
    if substrate_continuity is not None:
        power_delivery["substrate_continuity"] = substrate_continuity


def substrate_undecidable_gate(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """The detail payload to return with ``substrate_isolation_undecidable``
    when the cited run disclosed substrate isolation as undecidable (issue
    #5346: an undecided question is not a clean one), else ``None``."""
    undecidable = _erc_substrate_undecidable(envelope)
    if undecidable is None:
        return None
    return {"power_delivery": {"substrate_isolation_missing": undecidable}}


def erc_ties_checked_by_assertion(envelope: dict[str, Any]) -> list[str]:
    """The cited `klt erc` run's ``erc_coverage.checked_by_assertion``
    (issue #2234): the ``erc.missing_tie`` work identities whose tap region
    came from a caller **assertion** (``ties[].tap_boxes``) rather than
    PDK-marker narrowing -- ``[]`` for every run that used none, and for
    every report produced before the field existed.

    Carried into a ``"met"`` item 11 citation's ``power_delivery`` block
    (:func:`_grade_power_delivery`) for the same reason `klt erc` grades it
    as its own classification rather than folding it into ``checked``: an
    asserted tie is real, evaluated work -- the geometry was intersected and
    the connectivity walked, and a degenerate or unmatched assertion is
    rejected exactly as any other narrowing form is (``docs/cli/erc.md`` →
    "A tie with no distinguishing marker layer at all") -- but *which
    geometry counts as the tap* rested on the caller's word rather than on a
    drawn marker. That is a provenance difference a reader of the verdict of
    record should not have to re-open the cited ERC envelope to discover.
    It does not change the verdict: item 11 is ``"met"`` on an asserted tie
    exactly as on a marker-derived one.
    """
    block = envelope.get("erc_coverage")
    if not isinstance(block, dict):
        return []
    return [
        identity
        for identity in block.get("checked_by_assertion") or []
        if isinstance(identity, str)
    ]
