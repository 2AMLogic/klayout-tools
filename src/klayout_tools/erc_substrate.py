"""Substrate-continuity post-pass for ``erc.supply_short`` in ``klt erc``.

Implements piece B of ``docs/design/erc-substrate-continuity.md`` (section 3):
a *derived* body-merge pass over the existing ``ties[]`` connectivity graph.

Every merged tap site of every non-degenerate tie is assigned to a **body**
(a well polygon, the shared substrate, or one ``vsubs_iso<n>`` isolation
island); tie-graph clusters that touch the same body are union-found; and the
cross-name short rule is re-run over the merged groups, only for pairs the
primary (metal) graph did not already report.

Properties (each load-bearing, see the design note):

- **Monotone**: only ever *adds* ``erc.supply_short`` findings.
- **No graph mutation**: reads the tie graph, writes nothing back, so
  ``gates[]``, antenna and every other finding are untouched.
- **Join only through drawn, non-degenerate tap geometry.**
- Isolation islands come from :mod:`klayout_tools.erc_islands`, the same code
  ``klt extract`` uses, so island numbering agrees with ``vsubs_iso<n>``.

Piece C (issue #5343) adds the *decidable-or-disclosed* rule (design section
4). The pass is **evaluated** when isolation is derived (the deck declares
``substrate_isolation``), asserted (``ties[].isolation_boxes``), or provably
absent (a family whose isolation layers are known and none is drawn); otherwise
``erc.supply_short`` is disclosed ``inapplicable`` with reason
``substrate_isolation_undecidable`` and a ``missing`` list. A declared
``nets[].same_net_as`` pair suppresses the substrate-path short and satisfies
``erc.expected_short_missing`` (design section 6).
"""

from __future__ import annotations

import itertools
from typing import Any, NamedTuple

from .erc_substrate_bodies import (
    _assign_bodies,
    _cluster_names,
    _is_drawn_well,
    _isolation_box_region,
)

#: ``erc_coverage.checked`` id for the substrate-path work.
SUBSTRATE_WORK_ID = "erc.supply_short.substrate"

#: ``erc_coverage.inapplicable[].reason`` (issue #4384) on
#: ``erc.supply_short`` when the spec declares two or more ``kind: "supply"``
#: nets: ``klt erc`` joins conductors only through the declared
#: ``stackup``/``vias``, so supply rails that are one conductor through a
#: shared substrate read as separate clean islands. A coverage disclosure,
#: not a merge (substrate/well merging was rejected in #2192); additive and
#: leaves ``erc_status`` untouched. Cross-check with ``klt lvs``.
REASON_SUBSTRATE_ISOLATION_UNDECIDABLE = "substrate_isolation_undecidable"

#: ``missing`` entry when the spec declares no ``ties[]`` at all.
NO_TIES_MISSING = "ties[] entries declaring the substrate/well tap geometry"

#: Why the substrate check cannot be evaluated for a named PDK family whose
#: extraction deck declares no ``substrate_isolation`` layer. Only reachable
#: for a family whose built-in deck leaves ``substrate_isolation`` unset --
#: :func:`isolation_missing` returns ``[]`` from the deck first -- so a deck
#: that gains the layer must lose its entry here. ``sg13g2`` / ``sg13cmos5l``
#: were dropped when the IHP decks declared nBuLay (#5345, #5359) and sky130
#: never had one (#5344); ``test_family_missing_has_no_deck_declared_family``
#: fails if a stale entry comes back.
_FAMILY_MISSING: dict[str, str] = {
    "asap7": "no drawn substrate tap geometry to join through (asap7 cells draw "
    "no substrate or well tie)",
}


class SubstrateOutcome(NamedTuple):
    """What :func:`run_substrate_pass` measured. ``bodies is None`` means the
    pass did not run (``missing`` then names why)."""

    bodies: list[dict[str, Any]] | None = None
    missing: list[str] | None = None
    asserted: bool = False
    assertions: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []


def _asserts_isolation(
    tie_layers: list[dict[str, Any]], degenerate_ties: dict[str, str]
) -> bool:
    return any(
        tie["isolation_boxes"] and tie["name"] not in degenerate_ties
        for tie in tie_layers
    )


def isolation_missing(
    *,
    layout: Any,
    top_cell: Any,
    deck: Any,
    deck_name: str | None,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
) -> list[str]:
    """The decidable-or-disclosed rule (design section 4): ``[]`` when the
    substrate check can be evaluated, else the information still missing."""
    if deck is not None and deck.substrate_isolation is not None:
        return []  # derived from the deck's isolation layer
    if _asserts_isolation(tie_layers, degenerate_ties):
        return []  # asserted via ties[].isolation_boxes
    if deck_name in _FAMILY_MISSING:
        return [_FAMILY_MISSING[deck_name], "or assert ties[].isolation_boxes"]
    return [
        "--deck naming the PDK family, so the isolation layers can be measured",
        "or assert ties[].isolation_boxes",
    ]


def _short_finding(
    a: str, b: str, body_key: tuple[str, str], info: dict[str, Any]
) -> dict[str, Any]:
    from .erc import _bbox_dict, _finding

    finding = _finding(
        "erc.supply_short",
        (
            f"declared nets {a!r} and {b!r} are electrically the "
            f"same net through the {body_key[0]} body "
            f"{body_key[1]!r} (shorted via tap geometry)"
        ),
        net=a,
        other_net=b,
        bbox=_bbox_dict(info["sites"].bbox()),
    )
    finding["via"] = "substrate"
    joins = [j for j in info["joins"] if j["net"] in (a, b)]
    joins.sort(
        key=lambda j: (
            j["net"],
            j["tie"],
            j["bbox"]["left"],
            j["bbox"]["bottom"],
            j["bbox"]["right"],
            j["bbox"]["top"],
            j.get("well_box", -1),
        )
    )
    seen: set[tuple[Any, ...]] = set()
    deduped = []
    for join in joins:
        key = (
            join["net"],
            join["tie"],
            tuple(sorted(join["bbox"].items())),
            join.get("well_box"),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(join)
    finding["body"] = {
        "kind": body_key[0],
        "id": body_key[1],
        "ties": sorted(info["ties"]),
        # Issue #5745: name the tap sites that performed the merge, per side.
        "joins": deduped,
    }
    return finding


def substrate_supply_shorts(
    *,
    box_region: Any,
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    islands: list[Any],
    nets_decl: list[dict[str, Any]],
    group_by_name: dict[str, int],
    metal_pairs: set[tuple[str, str]],
    layout: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[tuple[str, str]]]:
    """Run the body-merge pass.

    Returns ``(findings, bodies, joined)``: the substrate-path
    ``erc.supply_short`` findings (``via: "substrate"`` plus ``body``), the
    measured body list for ``provenance.substrate_bodies``, and every sorted
    declared-name pair that shares a body-merged group (what satisfies a
    ``same_net_as`` declaration through the substrate path).
    """
    import klayout.db as kdb

    kind_by_name = {decl["name"]: decl["kind"] for decl in nets_decl}
    supplies = {name for name, kind in kind_by_name.items() if kind == "supply"}
    names_by_cluster = _cluster_names(tie_circuit, set(kind_by_name))
    # Issue #5745: every drawn-well tie's well region, in one place. Taps
    # inside them tap the well, never the substrate, whatever a substrate
    # assertion asserts over that area.
    wells = kdb.Region()
    for tie in tie_layers:
        if _is_drawn_well(tie):
            wells += tie["well_region"]
    uf, bodies = _assign_bodies(
        tie_l2n,
        tie_circuit,
        tie_layers,
        degenerate_ties,
        islands,
        names_by_cluster,
        box_region,
        wells,
        layout,
    )

    group_names: dict[Any, set[str]] = {}
    group_bodies: dict[Any, set[tuple[str, str]]] = {}
    for cluster_id, names in names_by_cluster.items():
        group_names.setdefault(uf.find(("c", cluster_id)), set()).update(names)
    for body_key in bodies:
        group_bodies.setdefault(uf.find(("b", body_key)), set()).add(body_key)

    findings: list[dict[str, Any]] = []
    joined: set[tuple[str, str]] = set()
    for root, names in group_names.items():
        group_keys = group_bodies.get(root, set())
        if group_keys:
            joined.update(itertools.combinations(sorted(names), 2))
        for a, b in itertools.combinations(sorted(names & supplies), 2):
            if group_by_name[a] == group_by_name[b] or (a, b) in metal_pairs:
                continue
            a_clusters = {c for c, ns in names_by_cluster.items() if a in ns}
            adjacent = sorted(
                k for k in group_keys if bodies[k]["clusters"] & a_clusters
            ) or sorted(group_keys)
            if adjacent:
                findings.append(_short_finding(a, b, adjacent[0], bodies[adjacent[0]]))

    body_report = [
        {"kind": key[0], "id": key[1], "ties": sorted(info["ties"])}
        for key, info in sorted(bodies.items())
    ]
    return findings, body_report, joined


def substrate_coverage_work(
    outcome: SubstrateOutcome, nets_decl: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """``(checked, checked_by_assertion, inapplicable)`` for ``erc.supply_short``
    substrate work: graded when the pass ran, disclosed with ``missing`` when
    two or more supplies are declared and it could not."""
    if outcome.bodies is not None:
        asserted = [SUBSTRATE_WORK_ID] if outcome.asserted else []
        return ["erc.supply_short", SUBSTRATE_WORK_ID], asserted, []
    if sum(1 for decl in nets_decl if decl.get("kind") == "supply") < 2:
        return [], [], []
    missing = list(outcome.missing or ["substrate_isolation"])
    record = {"id": "erc.supply_short", "missing": missing}
    record["reason"] = REASON_SUBSTRATE_ISOLATION_UNDECIDABLE
    return [], [], [record]


def substrate_provenance(outcome: SubstrateOutcome) -> dict[str, Any]:
    """``provenance.substrate_bodies`` (plus ``isolation_assertions`` and
    ``substrate_declared_joins`` when non-empty) -- present only when the
    pass ran."""
    if outcome.bodies is None:
        return {}
    extra: dict[str, Any] = {"substrate_bodies": outcome.bodies}
    if outcome.assertions:
        extra["isolation_assertions"] = outcome.assertions
    if outcome.joins:
        extra["substrate_declared_joins"] = outcome.joins
    return extra


def _assertion_echo(
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    bodies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Measured effect of each ``isolation_boxes`` assertion: the isolated
    bodies it produced (empty = the boxes covered no tap of that tie)."""
    echo = []
    for tie in tie_layers:
        if not tie["isolation_boxes"] or tie["name"] in degenerate_ties:
            continue
        hit = sorted(
            b["id"]
            for b in bodies
            if b["id"].startswith("isolation_box[") and tie["name"] in b["ties"]
        )
        echo.append(
            {"tie": tie["name"], "boxes": len(tie["isolation_boxes"]), "bodies": hit}
        )
    return echo


def run_substrate_pass(
    *,
    layout: Any,
    top_cell: Any,
    deck: Any,
    deck_name: str | None = None,
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    nets_decl: list[dict[str, Any]],
    erc_findings: list[dict[str, Any]],
) -> SubstrateOutcome:
    """Gate and run the pass. Extends ``erc_findings`` in place and, for a
    ``same_net_as`` pair joined only through the substrate, drops the
    ``erc.expected_short_missing`` the primary graph raised for it."""
    if not any(tie["name"] not in degenerate_ties for tie in tie_layers):
        return SubstrateOutcome(missing=["a non-degenerate ties[] tap entry"])
    missing = isolation_missing(
        layout=layout,
        top_cell=top_cell,
        deck=deck,
        deck_name=deck_name,
        tie_layers=tie_layers,
        degenerate_ties=degenerate_ties,
    )
    if missing:
        return SubstrateOutcome(missing=missing)
    from ._layout import region as _region
    from .erc import _declared_same_net_pairs, _same_net_group_ids
    from .erc_islands import sorted_islands

    metal_pairs = {
        (f["net"], f["other_net"])
        for f in erc_findings
        if f["rule"] == "erc.supply_short"
    }
    declared_islands = (
        sorted_islands(
            _region(layout, top_cell, deck.substrate_isolation),
            _region(layout, top_cell, deck.nwell)
            if deck.substrate_isolation_needs_nwell_ring
            else None,
        )
        if deck is not None and deck.substrate_isolation is not None
        else []
    )
    findings, bodies, joined = substrate_supply_shorts(
        box_region=_isolation_box_region(layout, tie_layers, degenerate_ties),
        tie_l2n=tie_l2n,
        tie_circuit=tie_circuit,
        tie_layers=tie_layers,
        degenerate_ties=degenerate_ties,
        islands=declared_islands,
        nets_decl=nets_decl,
        group_by_name=_same_net_group_ids(
            nets_decl, _declared_same_net_pairs(nets_decl)
        ),
        metal_pairs=metal_pairs,
        layout=layout,
    )
    erc_findings.extend(findings)
    joins: list[dict[str, Any]] = []
    kept = []
    for finding in erc_findings:
        if finding["rule"] == "erc.expected_short_missing":
            pair = tuple(sorted((finding["net"], finding["other_net"])))
            if pair in joined:
                joins.append({"net": pair[0], "other_net": pair[1], "via": "substrate"})
                continue
        kept.append(finding)
    erc_findings[:] = kept
    return SubstrateOutcome(
        bodies=bodies,
        asserted=_asserts_isolation(tie_layers, degenerate_ties),
        assertions=_assertion_echo(tie_layers, degenerate_ties, bodies),
        joins=sorted(joins, key=lambda j: (j["net"], j["other_net"])),
    )
