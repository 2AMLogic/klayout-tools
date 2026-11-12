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

Evaluated only when the extraction deck declares ``substrate_isolation``
(gf180mcu, sky130); other families keep the
``substrate_continuity_not_modelled`` disclosure until the deck work lands.
"""

from __future__ import annotations

import itertools
from typing import Any

from ._unionfind import UnionFind

#: ``erc_coverage.checked`` id for the substrate-path work.
SUBSTRATE_WORK_ID = "erc.supply_short.substrate"


def substrate_pass_applies(
    nets_decl: list[dict[str, Any]],
    deck: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
) -> bool:
    """Whether the post-pass runs: 2+ supply nets, a deck that declares
    ``substrate_isolation`` and at least one non-degenerate tie."""
    if sum(1 for decl in nets_decl if decl.get("kind") == "supply") < 2:
        return False
    if deck is None or deck.substrate_isolation is None:
        return False
    return any(tie["name"] not in degenerate_ties for tie in tie_layers)


def _is_drawn_well(tie: dict[str, Any]) -> bool:
    """A tie naming a drawn ``well_layer`` taps that well; any other form
    (``well_boxes`` / ``well_complement_of``) taps the substrate."""
    return bool(
        tie["well_layer"] and not tie["well_boxes"] and not tie["well_complement_of"]
    )


def _tie_bodies(
    tie: dict[str, Any], islands: list[Any]
) -> list[tuple[tuple[str, str], Any]]:
    """``[(body_key, tap_sites_in_body), ...]`` for one tie.

    ``body_key`` is ``(kind, id)``: a well body is keyed by its merged
    polygon's bounding box, the substrate by ``vsubs`` / ``vsubs_iso<n>``.
    """
    import klayout.db as kdb

    from .erc_islands import partition_region_by_islands

    sites = tie["tap_region"]
    bodies: list[tuple[tuple[str, str], Any]] = []
    if _is_drawn_well(tie):
        for well_poly in tie["well_region"].merged().each():
            here = sites.interacting(kdb.Region(well_poly))
            if not here.is_empty():
                box = well_poly.bbox()
                key = f"{box.left},{box.bottom},{box.right},{box.top}"
                bodies.append((("well", key), here))
        return bodies
    outside, per_island = partition_region_by_islands(sites, islands)
    if not outside.is_empty():
        bodies.append((("substrate", "vsubs"), outside))
    for index, part in enumerate(per_island):
        if not part.is_empty():
            bodies.append((("substrate", f"vsubs_iso{index}"), part))
    return bodies


def _cluster_names(tie_circuit: Any, declared: set[str]) -> dict[int, set[str]]:
    """Declared net names labelled on each tie-graph cluster."""
    names: dict[int, set[str]] = {}
    for net in tie_circuit.each_net():
        if net.cluster_id == 0:
            continue
        for label in net.expanded_name().split(","):
            if label in declared:
                names.setdefault(net.cluster_id, set()).add(label)
    return names


def _assign_bodies(
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    islands: list[Any],
    names_by_cluster: dict[int, set[str]],
) -> tuple[UnionFind, dict[tuple[str, str], dict[str, Any]]]:
    """Union-find tie-graph clusters over the bodies their tap sites touch.

    Returns ``(union_find, bodies)``; ``bodies[key]`` carries the attributed
    ``ties``, the merged tap ``sites`` and the touching ``clusters``.
    """
    uf = UnionFind()
    bodies: dict[tuple[str, str], dict[str, Any]] = {}
    for tie in tie_layers:
        if tie["name"] in degenerate_ties:
            continue
        tie_bodies = _tie_bodies(tie, islands)
        for net in tie_circuit.each_net():
            net_taps = tie_l2n.polygons_of_net(net, tie["tap_index"])
            if net.cluster_id == 0 or net_taps.is_empty():
                continue
            for body_key, body_sites in tie_bodies:
                if net_taps.interacting(body_sites).is_empty():
                    continue
                info = bodies.setdefault(
                    body_key,
                    {"ties": set(), "sites": body_sites.dup(), "clusters": set()},
                )
                # Attribute the tie only where its declared net is the one
                # reaching this body (a tie's tap geometry is net-agnostic).
                if tie["net"] in names_by_cluster.get(net.cluster_id, ()):
                    info["ties"].add(tie["name"])
                info["clusters"].add(net.cluster_id)
                uf.union(("c", net.cluster_id), ("b", body_key))
        for body_key, body_sites in tie_bodies:
            if body_key in bodies:
                bodies[body_key]["sites"] = (
                    bodies[body_key]["sites"] + body_sites
                ).merged()
    return uf, bodies


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
    finding["body"] = {
        "kind": body_key[0],
        "id": body_key[1],
        "ties": sorted(info["ties"]),
    }
    return finding


def substrate_supply_shorts(
    *,
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    islands: list[Any],
    nets_decl: list[dict[str, Any]],
    group_by_name: dict[str, int],
    metal_pairs: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the body-merge pass.

    Returns ``(findings, bodies)``: the substrate-path ``erc.supply_short``
    findings (``via: "substrate"`` plus ``body``) and the measured body list
    for ``provenance.substrate_bodies``.
    """
    kind_by_name = {decl["name"]: decl["kind"] for decl in nets_decl}
    supplies = {name for name, kind in kind_by_name.items() if kind == "supply"}
    names_by_cluster = _cluster_names(tie_circuit, set(kind_by_name))
    uf, bodies = _assign_bodies(
        tie_l2n, tie_circuit, tie_layers, degenerate_ties, islands, names_by_cluster
    )

    group_names: dict[Any, set[str]] = {}
    group_bodies: dict[Any, set[tuple[str, str]]] = {}
    for cluster_id, names in names_by_cluster.items():
        group_names.setdefault(uf.find(("c", cluster_id)), set()).update(names)
    for body_key in bodies:
        group_bodies.setdefault(uf.find(("b", body_key)), set()).add(body_key)

    findings: list[dict[str, Any]] = []
    for root, names in group_names.items():
        group_keys = group_bodies.get(root, set())
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
    return findings, body_report


def substrate_provenance(bodies: list[dict[str, Any]] | None) -> dict[str, Any]:
    """``provenance.substrate_bodies`` -- present only when the pass ran."""
    return {} if bodies is None else {"substrate_bodies": bodies}


def run_substrate_pass(
    *,
    layout: Any,
    top_cell: Any,
    deck: Any,
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    nets_decl: list[dict[str, Any]],
    erc_findings: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Gate and run the pass; returns the measured bodies (``None`` when the
    pass does not apply). Extends ``erc_findings`` in place."""
    if not substrate_pass_applies(nets_decl, deck, tie_layers, degenerate_ties):
        return None
    from ._layout import region as _region
    from .erc import _declared_same_net_pairs, _same_net_group_ids
    from .erc_islands import sorted_islands

    metal_pairs = {
        (f["net"], f["other_net"])
        for f in erc_findings
        if f["rule"] == "erc.supply_short"
    }
    findings, bodies = substrate_supply_shorts(
        tie_l2n=tie_l2n,
        tie_circuit=tie_circuit,
        tie_layers=tie_layers,
        degenerate_ties=degenerate_ties,
        islands=sorted_islands(
            _region(layout, top_cell, deck.substrate_isolation),
            _region(layout, top_cell, deck.nwell)
            if deck.substrate_isolation_needs_nwell_ring
            else None,
        ),
        nets_decl=nets_decl,
        group_by_name=_same_net_group_ids(
            nets_decl, _declared_same_net_pairs(nets_decl)
        ),
        metal_pairs=metal_pairs,
    )
    erc_findings.extend(findings)
    return bodies
