"""Grouped citations for T1 item 11 power delivery (issue #5597).

A partition whose physical evidence is several independently signed-off
streams needs one ``(erc, lvs)`` group per stream. The flat compound form
resolves to one entry per kind -- silently dropping every but the last (an
order-dependent false ``met``) -- so the grouped form is a manifest entry
whose every element is itself a compound citation, graded independently by
exactly item 11's own rules and aggregated here: ``met`` only when every
group grades ``met``, with the per-group verdicts named either way.

This module also carries what the grouped form adds around that grading:
``normalize_evidence_groups`` (the grouped shape ``_normalize_evidence_parts``
returns), item 11's two text renderings -- ``print_power_delivery`` (the
compound-citation summary, moved from ``cli/signoff_cmd.py`` for the same
ceiling) and ``print_group_coverage`` (the ``groups:`` coverage line) -- and
``parts_citation_or_none`` (the parts-only citation a failing group
still owes the report of record). It lives outside ``signoff.py`` /
``signoff_power_delivery.py`` / ``cli/signoff_cmd.py`` per the per-file
size ratchet (see ``scripts/README.md``), the same move this module family
(``signoff_multi_member``, ``signoff_coverage_disclosure``, ...) uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .signoff_base import _citation_label
from .signoff_multi_member import graded_input_provenance, with_member_coverage
from .signoff_unmet_reasons import _REASON_INVALID_EVIDENCE


def normalize_evidence_groups(raw: Any) -> list[list[dict[str, Any]]] | None:
    """Normalize a **compound** item's manifest entry (issue #2025, T1 item
    11 only) into **groups** of :func:`_normalize_evidence_entry` specs, or
    ``None`` if any part matches neither evidence shape.

    A JSON array is the compound shape: every element is an ordinary
    evidence entry (a path or a ``file``/``command`` object), one artifact
    per cited check. An entry whose own checks failed stays in the list --
    this is normalization, not grading, and an item must be able to
    reach ``"met"`` without the artifact that was mistyped. An empty array is
    likewise invalid: it cites nothing, but says it cites something.

    A non-array entry is accepted too, as a one-element group -- so the
    ordinary ``"11": "erc.json"`` shape is a well-formed (if, on its own,
    insufficient) citation rather than a schema error.

    Issue #5597 adds the **grouped** form: an array whose every element is
    itself such an array, one independent compound citation per element.
    Nesting is all-or-nothing -- an outer list mixing a group with a bare
    entry, or carrying an empty inner list, is ``None`` (invalid), mirroring
    the rule that one malformed part fails the whole item rather than being
    dropped. The result is always a list of groups, so the flat form is the
    one-group case and nothing downstream needs two shapes.
    """
    from .signoff import _normalize_evidence_entry

    if isinstance(raw, list):
        if not raw:
            return None
        if any(isinstance(part, list) for part in raw):
            groups: list[list[dict[str, Any]]] = []
            for part in raw:
                if not isinstance(part, list) or not part:
                    return None
                specs = [_normalize_evidence_entry(entry) for entry in part]
                if any(spec is None for spec in specs):
                    return None
                groups.append([spec for spec in specs if spec is not None])
            return groups
        specs = [_normalize_evidence_entry(part) for part in raw]
        if any(spec is None for spec in specs):
            return None
        return [[spec for spec in specs if spec is not None]]

    spec = _normalize_evidence_entry(raw)
    return None if spec is None else [[spec]]


def missing_parts_verdict(
    by_kind: dict[str, Any],
) -> tuple[str, str | None, None, dict[str, Any]]:
    """The ``"unmet"`` verdict for a cited set lacking the ``erc`` and/or
    ``lvs`` part (issue #4193). An ERC part whose spec does not resolve
    reports its own, more actionable reason first; otherwise
    :data:`_REASON_POWER_DELIVERY_PARTS_MISSING` names the absent parts and
    credits the supplies a resolved ERC spec proved. Never ``"met"``.
    """
    from .signoff_power_delivery import (
        _POWER_DELIVERY_PART_ORDER,
        _REASON_POWER_DELIVERY_PARTS_MISSING,
        _resolve_erc_supply_spec,
    )

    erc = by_kind.get("erc")
    supply_spec = None
    if erc is not None:
        supply_spec, reason, detail = _resolve_erc_supply_spec(erc)
        if supply_spec is None:
            return "unmet", reason, None, detail
    block: dict[str, Any] = {
        "missing_parts": [k for k in ("erc", "lvs") if k not in by_kind],
        "cited_parts": [k for k in _POWER_DELIVERY_PART_ORDER if k in by_kind],
    }
    if supply_spec is not None:
        block["supply_nets"] = list(supply_spec["supply_nets"])
    return (
        "unmet",
        _REASON_POWER_DELIVERY_PARTS_MISSING,
        None,
        {"power_delivery": block},
    )


def parts_citation_or_none(
    by_kind: dict[str, Any], group_provenance: dict[str, Any] | None
) -> dict[str, Any] | None:
    """``{"parts": [...]}`` covering every resolved part, in the canonical
    part order (issue #5597): the citation a failing *group* still owes the
    report of record. ``power_delivery`` states facts about a proven grid,
    so an unmet group carries only its resolved ``parts`` -- never the
    ``power_delivery`` block a met citation derives from them."""
    from .signoff import _citation
    from .signoff_power_delivery import _POWER_DELIVERY_PART_ORDER

    if group_provenance is None:
        return None
    # Resolution indexes each distinct kind in manifest order before canonical sorting.
    group_provenance.update(graded_input_provenance(next(iter(by_kind.values()))))
    return {
        "parts": [
            _citation(by_kind[kind])
            for kind in _POWER_DELIVERY_PART_ORDER
            if kind in by_kind
        ]
    }


def grade_power_delivery_groups(
    groups: list[list[dict[str, Any]]],
    *,
    partition_kind: str,
    power_port_policy: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Grade T1 item 11 over a **set of groups** (issue #5597) -- each group
    independently through :func:`_grade_power_delivery`, exactly as
    :func:`~klayout_tools.signoff._grade_multi_citation` aggregates item 4's
    per-cell list -- and return ``(status, reason, citation, failure_detail,
    extra)``.

    The item is ``"met"`` only when **every** group grades ``met``; a
    failing group makes it ``"unmet"`` with that group's reason (the first
    failing one named) and every failing group's label under
    ``detail.failed_citations`` -- so the order of groups in the manifest can
    no longer decide the verdict. ``extra`` carries the multi-citation keys
    item 4's list form already emits: ``citations_graded`` (one entry per
    group, on ``met`` and ``unmet` alike, labelled by the group's leading
    part), ``citations`` (one compound citation per group; a failing group
    carries only its resolved ``parts``, since ``power_delivery`` states
    facts about a proven grid), and the item-level ``citation`` is the
    leading group's -- the contract every pre-existing consumer reads.
    """
    from .signoff_power_delivery import _grade_power_delivery

    graded = []
    summaries = []
    for group in groups:
        provenance = graded_input_provenance(None)
        grade = _grade_power_delivery(
            group,
            partition_kind=partition_kind,
            power_port_policy=power_port_policy,
            repo_root=repo_root,
            group_provenance=provenance,
        )
        graded.append((group, *grade))
        summaries.append(
            {
                "citation": _citation_label(group[0]),
                "status": grade[0],
                "reason": grade[1],
                **provenance,
            }
        )
    extra: dict[str, Any] = {"citations_graded": summaries}
    failed = [entry for entry in graded if entry[1] != "met"]
    if failed:
        detail = {
            **failed[0][4],
            "failed_citations": [_citation_label(entry[0][0]) for entry in failed],
        }
        # Unmet still accounts for every resolved group's cited envelopes
        # (`citations` entries in group order; a group whose *resolution*
        # failed -- stale, errored, unreadable -- has no citation to carry,
        # and its reason names it instead).
        extra["citations"] = [entry[3] for entry in graded if entry[3] is not None]
        return "unmet", failed[0][2], None, detail, extra
    extra["citations"] = [entry[3] for entry in graded]
    return "met", None, graded[0][3], {}, extra


def grade_grouped_power_delivery(
    item_id: int,
    raw_entry: Any,
    *,
    partition_kind: str,
    power_port_policy: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> tuple[str, str | None, dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Normalize T1 item 11's manifest entry and grade it over **groups**
    (issue #5597), returning ``(status, reason, citation, failure_detail,
    extra)`` -- the 4-tuple every caller already reads plus the grouped
    ``extra`` (``{}`` unless a multi-group citation gained aggregation).

    ``_normalize_evidence_parts`` returning ``None`` is the malformed-cited-
    set verdict. One group grades exactly as always -- byte-identical
    output, no grouped keys, ``extra`` empty -- and only a multi-group
    citation gains the per-group aggregation, reported with the same
    ``members_covered``/``members_total`` fields the per-cell items carry
    (issue #5031; :data:`~klayout_tools.signoff_multi_member.MEMBER_COUNT_ITEMS`
    includes item 11's grouped form).

    A citation carrying ``requires`` (item 5 only, issue #7807) refuses the
    item as ``invalid_evidence`` before any part is normalized or run, rather
    than normalizing without the key and grading as if it were absent.
    """
    from .signoff_member_requires import misplaced_requires_problem
    from .signoff_power_delivery import _grade_power_delivery

    if (problem := misplaced_requires_problem(item_id, raw_entry)) is not None:
        return "unmet", _REASON_INVALID_EVIDENCE, None, {"problem": problem}, {}
    groups = normalize_evidence_groups(raw_entry)
    if groups is None:
        return "unmet", _REASON_INVALID_EVIDENCE, None, {}, {}
    if len(groups) == 1:
        status, reason, citation, failure_detail = _grade_power_delivery(
            groups[0],
            partition_kind=partition_kind,
            power_port_policy=power_port_policy,
            repo_root=repo_root,
        )
        return status, reason, citation, failure_detail, {}
    status, reason, citation, failure_detail, extra = grade_power_delivery_groups(
        groups,
        partition_kind=partition_kind,
        power_port_policy=power_port_policy,
        repo_root=repo_root,
    )
    return (
        status,
        reason,
        citation,
        failure_detail,
        with_member_coverage(item_id, True, raw_entry, extra),
    )


def print_power_delivery(citation: dict) -> None:
    """Print T1 item 11's compound-citation summary (issue #2025), or
    nothing at all for every other item's single-artifact citation.

    Its own function for the same reason
    :func:`~klayout_tools.cli.signoff_cmd._print_input_verified` is (and
    since issue #2234's extra line, the same C901 budget reason): the
    caller, :func:`~klayout_tools.cli.signoff_cmd._print_tier_report_text`,
    is already at the complexity ratchet's limit. Landed here, beside
    :func:`print_group_coverage`, out of ``cli/signoff_cmd.py`` for its
    size-ratchet ceiling (issue #5597).

    The second line (issue #2234) names the ties whose tap geometry rested
    on a caller **assertion** (``ties[].tap_boxes``) rather than on a drawn
    PDK marker. It is disclosure only -- such a tie is graded ``met``
    exactly as a marker-derived one, and `klt erc` has already rejected a
    degenerate or unmatched assertion before the citation could reach here
    -- but leaving that provenance reachable only through the JSON would
    make the two indistinguishable in the rendering a reviewer actually
    reads. Printed only when non-empty, so a purely marker-derived report
    (and every report produced before the field existed) renders exactly as
    it did before.

    The third line (issue #2255) does the same for the *well* side: the ties
    whose substrate region was itself asserted (``ties[].well_layer: null``
    + ``ties[].well_boxes``), for a block that draws no well/tub layer at
    all. Same disclosure-only status, same print-when-non-empty rule, and
    deliberately its own line rather than being folded into the tap one --
    "which drawn geometry is the tap" and "where the substrate is" are
    different claims, and the second is the weaker of the two.
    """
    power_delivery = citation.get("power_delivery")
    if not power_delivery:
        return
    print(
        "        power delivery: supplies="
        f"{', '.join(power_delivery['supply_nets']) or 'none'}, "
        f"pdn={'yes' if power_delivery['pdn'] else 'no (no P&R cited)'}, "
        f"power_connectivity={power_delivery['power_connectivity_status']}"
    )
    asserted = power_delivery.get("ties_checked_by_assertion") or []
    if asserted:
        print(
            f"        taps asserted by the caller: {len(asserted)} "
            f"({', '.join(asserted)})"
        )
    well_asserted = power_delivery.get("ties_checked_by_well_assertion") or []
    if well_asserted:
        # Issue #2540 widened this list from "the substrate region was
        # asserted" (no drawn well at all) to "the well side rested on
        # caller-named coordinates", which now also covers a *drawn* well
        # whose bias class was selected by boxes. The line names both rather
        # than only the older form, so the count can never read as a claim
        # the spec did not make.
        print(
            "        well side rested on the caller's word (asserted region, "
            f"or a box-selected class): {len(well_asserted)} "
            f"({', '.join(well_asserted)})"
        )


def print_group_coverage(item: dict) -> None:
    """Print T1 item 11's per-group coverage for a multi-group citation
    (issue #5597): the grouped form's ``members_covered``/``members_total``
    -- groups cited vs. groups that individually graded ``met`` -- are
    otherwise reachable only through the JSON, and a reviewer reading the
    terminal rendering must see the count a `met` (or `unmet`) verdict
    rests on. Absent for a single-group citation, whose rendering is
    unchanged, and for items 2/3/4, whose member counts stay JSON-only as
    before."""
    # Item 11 only: items 2/3/4 also carry member counts (issue #5031),
    # but their members are per-cell reports, not citation groups -- a
    # "groups:" line would misdescribe them and change rendering that
    # issue leaves untouched. The guard lives here, not at the call site
    # in `_print_tier_report_text`, so the renderer stays exactly at the
    # complexity ratchet's max (issue #5597 review).
    if item.get("id") != 11:
        return
    total = item.get("members_total")
    if not total or total < 2:
        return
    print(
        f"        groups: {item.get('members_covered')}/{total} cited groups graded met"
    )
