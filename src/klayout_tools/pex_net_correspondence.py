"""Geometric correspondence between runset and curated PEX nets (issue #6606).

Both engines allocate anonymous net numbers independently. The merged writer
uses this join to attach curated parasitics to the runset device nodes.
See docs/design/runset-pex-net-correspondence.md for the measured decision.

Every metal polygon of the selected runset circuit contributes a strictly
interior probe to the curated extractor. The layer comes from the source
GDS layer/datatype or, for an anonymous layer, an unambiguous comparison
with complete stream polygons. Confirmed hits on multiple curated clusters
show a split. Hits by multiple runset nets on one cluster show a merge.
Only a unique match with complete coverage receives parasitics.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .pex_errors import PexError


def _is_anonymous(name: str) -> bool:
    """Whether a net name is KLayout's own ``$N`` placeholder -- either
    spelling: bare in memory, backslash-escaped in the written netlist."""
    return re.fullmatch(r"\$[0-9]+", name.removeprefix("\\")) is not None


# --------------------------------------------------------------------------- #
# Geometry: layer-role matching and strictly-interior point sampling
# --------------------------------------------------------------------------- #


def _interior_point(poly: Any) -> Any | None:
    """A point strictly inside ``poly``, or ``None``.

    The bbox centre when it is strictly interior; otherwise an inward
    bisector step from the first hull vertex whose step lands inside (a
    convex vertex's bisector is locally interior for a simple polygon).
    Strictly interior is what the join's soundness rests on: a point on a
    polygon's boundary can lie exactly on a *different* net's shape.
    """
    import klayout.db as kdb

    box = poly.bbox()
    centre = kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2)

    def strictly_inside(point: Any) -> bool:
        # Polygon.inside includes edges. each_edge visits the hull and holes.
        return poly.inside(point) and not any(
            e.contains(point) for e in poly.each_edge()
        )

    if strictly_inside(centre):
        return centre
    hull = list(poly.each_point_hull())
    for index, vertex in enumerate(hull):
        prev_p, next_p = hull[index - 1], hull[(index + 1) % len(hull)]
        sum_x = (prev_p.x - vertex.x) + (next_p.x - vertex.x)
        sum_y = (prev_p.y - vertex.y) + (next_p.y - vertex.y)
        if sum_x == 0 and sum_y == 0:
            continue
        reach = max(
            1,
            min(
                abs(prev_p.x - vertex.x) + abs(prev_p.y - vertex.y),
                abs(next_p.x - vertex.x) + abs(next_p.y - vertex.y),
            )
            // 4,
        )
        norm = max(1, abs(sum_x) + abs(sum_y))
        step = kdb.Point(
            vertex.x + sum_x * reach // norm, vertex.y + sum_y * reach // norm
        )
        if strictly_inside(step):
            return step
    return None


def _union_of_net_regions(lvs: Any, nets: Sequence[Any], layer_index: int) -> Any:
    """The runset database's whole per-layer region, accumulated over every
    net of the selected circuit, including subcircuit shapes."""
    import klayout.db as kdb

    region: Any = None
    for net in nets:
        part = lvs.polygons_of_net(net, layer_index, True)
        if part is not None and not part.is_empty():
            region = part if region is None else region + part
    return kdb.Region() if region is None else region


def _anonymous_role(
    region: Any, metal_masks: Mapping[str, Any], other_masks: Sequence[Any]
) -> tuple[str | None, list[str]]:
    """An anonymous layer can omit islands absent from the selected circuit.
    Compare complete overlapping islands, so a via cut inside a pad cannot
    identify the pad's metal. Coincident masks leave the source ambiguous."""
    if region.is_empty():
        return None, []
    area = region.area()
    candidates = []
    for role, mask in metal_masks.items():
        overlap = region & mask
        if not overlap.is_empty() and overlap.area() >= 0.99 * area:
            candidates.append(role)
    if not candidates:
        return None, []

    def same_islands(mask: Any) -> bool:
        return (mask.overlapping(region) ^ region).is_empty()

    exact = [role for role in candidates if same_islands(metal_masks[role])]
    other = any(same_islands(mask) for mask in other_masks)
    if len(candidates) == 1 and exact and not other:
        return exact[0], []
    if other and not exact:
        return None, []  # identified contact, via or other non-metal mask
    return None, candidates


def _stream_masks(
    layout: Any, cell: Any, role_by_gds: Mapping[tuple[int, int], str]
) -> tuple[dict[str, Any], list[Any]]:
    """Merge each source mask so touching shapes form complete conductor islands."""
    import klayout.db as kdb

    metal_masks = {}
    other_masks = []
    for layer in layout.layer_indexes():
        mask = kdb.Region(cell.begin_shapes_rec(layer)).merged()
        info = layout.get_info(layer)
        role = role_by_gds.get((info.layer, info.datatype))
        if role is not None:
            metal_masks[role] = mask
        elif not mask.is_empty():
            other_masks.append(mask)
    return metal_masks, other_masks


def _layer_roles(
    lvs: Any,
    circuit: Any,
    layout: Any,
    top_cell: Any,
    metals: Sequence[tuple[int, int]],
) -> tuple[dict[int, str], dict[int, list[str]]]:
    """Use the stream layer/datatype first, then compare anonymous layers."""

    role_by_gds = {pair: f"metal{i}" for i, pair in enumerate(metals)}
    roles = {}
    anonymous = []
    for layer in lvs.layer_indexes():
        info = lvs.layer_info(layer)
        if info.layer < 0:
            anonymous.append(layer)
        elif (info.layer, info.datatype) in role_by_gds:
            roles[layer] = role_by_gds[info.layer, info.datatype]
    uncertain = {}
    if anonymous:
        metal_masks, other_masks = _stream_masks(layout, top_cell, role_by_gds)
        nets = list(circuit.each_net())
        for layer in anonymous:
            region = _union_of_net_regions(lvs, nets, layer)
            role, candidates = _anonymous_role(region, metal_masks, other_masks)
            if role is not None:
                roles[layer] = role
            elif candidates:
                uncertain[layer] = candidates
    return roles, uncertain


def _layer_probes(
    region: Any,
    cluster: int,
    layer: int,
    roles: Sequence[str],
    *,
    certain: bool,
    dbu_um: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    probes = []
    missing_point = False
    prefix = "kltcorr" if certain else "kltmaybe"
    for index, polygon in enumerate(region.each()):
        point = _interior_point(polygon)
        if point is None:
            missing_point = True
            continue
        for role in roles:
            probes.append(
                {
                    "name": f"{prefix}|{role}|{cluster}|{layer}-{index}",
                    "layer": role,
                    "x": point.x * dbu_um,
                    "y": point.y * dbu_um,
                }
            )
    details = {
        "runset_cluster_id": cluster,
        "layer_index": layer,
        "candidate_roles": list(roles),
    }
    incomplete = []
    if not certain:
        reason = "ambiguous_layer" if len(roles) > 1 else "unidentified_layer"
        incomplete.append({**details, "reason": reason})
    if missing_point:
        incomplete.append({**details, "reason": "no_interior_probe"})
    return probes, incomplete


def _contact_landings(
    lvs: Any,
    layout: Any,
    cell: Any,
    contact: tuple[int, int] | None,
    metals: Sequence[tuple[int, int]],
) -> dict[int, Any]:
    """The curated deck connects its contact layer directly to metal0.
    Intersect a proven runset contact with that landing metal to detect a
    runset missing the contact-to-metal connection. An unrelated overlying
    metal cannot witness this connection."""
    import klayout.db as kdb

    if not metals or contact is None:
        return {}
    first_metal = layout.find_layer(*metals[0])
    if first_metal is None:
        return {}
    landings = {}
    for index in lvs.layer_indexes():
        info = lvs.layer_info(index)
        if (info.layer, info.datatype) == contact:
            landings[index] = kdb.Region(cell.begin_shapes_rec(first_metal))
    return landings


def _coordinates_agree(
    lvs: Any,
    circuit: Any,
    cell: Any,
    metals: Sequence[tuple[int, int]],
    roles: Mapping[int, str],
    landings: Mapping[int, Any],
) -> bool:
    """Check the frame and the actual islands before interpreting integer points."""
    import klayout.db as kdb

    layout = cell.layout()
    # A runset DBU override can rescale even the saved circuit boundary while
    # the LVS file still advertises the input DBU. Some circuits omit whole
    # source islands, so either the boundary or a whole mask must anchor them.
    boundary = circuit.boundary.bbox().to_itype(layout.dbu)
    if (boundary & cell.bbox()) != boundary:
        return False
    anchored = boundary == cell.bbox()
    nets = list(circuit.each_net())
    for layer in roles.keys() | landings.keys():
        info = lvs.layer_info(layer)
        pair = (
            (info.layer, info.datatype)
            if info.layer >= 0
            else metals[int(roles[layer][5:])]
        )
        stream_layer = layout.find_layer(*pair)
        region = _union_of_net_regions(lvs, nets, layer)
        if region.is_empty():
            continue
        if stream_layer is None:
            return False
        source = kdb.Region(cell.begin_shapes_rec(stream_layer)).merged()
        if (source ^ region).is_empty():
            anchored = True
        elif not (source.overlapping(region) ^ region).is_empty():
            return False
    return anchored or not (roles or landings)


def build_probes(
    lvsdb_path: str,
    top_cell: Any,
    *,
    metals: Sequence[tuple[int, int]],
    contact: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Probe every identified metal polygon and record gaps in net coverage.

    Probes of candidate roles on an unidentified layer can rule out another
    net's exclusive match. They never establish a match or disagreement.
    """
    import klayout.db as kdb

    layout = top_cell.layout()
    top = top_cell.name
    lvs = kdb.LayoutVsSchematic()
    lvs.read(lvsdb_path)
    circuit = lvs.netlist().circuit_by_name(top)
    if circuit is None:
        raise PexError(
            "net correspondence: the runset database carries no circuit named "
            f"'{top}', so its nets cannot be joined with the curated deck's"
        )
    roles, uncertain = _layer_roles(lvs, circuit, layout, top_cell, metals)
    landings = _contact_landings(lvs, layout, top_cell, contact, metals)
    if not _coordinates_agree(lvs, circuit, top_cell, metals, roles, landings):
        return [], [
            {"runset_cluster_id": net.cluster_id, "reason": "coordinate_mismatch"}
            for net in circuit.each_net()
        ]
    roles.update(dict.fromkeys(landings, "metal0"))
    layers = sorted(roles.keys() | uncertain.keys())
    probes = []
    incomplete = []
    for net in circuit.each_net():
        for layer in layers:
            region = lvs.polygons_of_net(net, layer, True)
            if region is None or region.is_empty():
                continue
            if layer in landings:
                region = region & landings[layer]
            candidates = [roles[layer]] if layer in roles else uncertain[layer]
            points, gaps = _layer_probes(
                region,
                net.cluster_id,
                layer,
                candidates,
                certain=layer in roles,
                dbu_um=layout.dbu,
            )
            probes.extend(points)
            incomplete.extend(gaps)
    return probes, incomplete


# --------------------------------------------------------------------------- #
# The join over probe results
# --------------------------------------------------------------------------- #


def _probe_hits(
    probe_results: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, set[tuple[str, int]]], set[tuple[str, int]], set[int]]:
    """Return confirmed hits, possible cluster hits and nets with a probe miss."""
    hits: dict[int, set[tuple[str, int]]] = defaultdict(set)
    possible: set[tuple[str, int]] = set()
    missed: set[int] = set()
    for result in probe_results:
        parts = str(result.get("name") or "").split("|")
        if len(parts) != 4 or parts[0] not in {"kltcorr", "kltmaybe"}:
            continue
        if not parts[2].isdecimal():
            continue
        cluster = int(parts[2])
        if result.get("found"):
            hit = (str(result["net"]), int(result["cluster_id"]))
            if parts[0] == "kltcorr":
                hits[cluster].add(hit)
            else:
                possible.add(hit)
        elif parts[0] == "kltcorr":
            missed.add(cluster)
    return hits, possible, missed


def correspondence(
    runset_nets: Sequence[Mapping[str, Any]],
    probe_results: Sequence[Mapping[str, Any]],
    curated_parasitic_nets: Sequence[Mapping[str, Any]],
    incomplete_geometry: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Classify the two extractions by cluster ID on each side.

    Runset report rows provide display names. Curated probe results identify
    hits, and the parasitic rows enumerate even clusters with no probe hit.
    """
    hits, possible, missed = _probe_hits(probe_results)
    names = {entry["net_id"]: entry["name"] for entry in runset_nets}
    gaps = [
        {**gap, "runset_net": names[gap["runset_cluster_id"]]}
        for gap in incomplete_geometry
    ]
    gaps.extend(
        {"runset_cluster_id": i, "runset_net": names[i], "reason": "unresolved_probe"}
        for i in sorted(missed)
    )
    incomplete = {entry["runset_cluster_id"] for entry in gaps}
    # A fragment with no probe can belong to any curated cluster, including
    # one reached from another metal through vias. No peer can prove exclusivity.
    unbounded = any(gap["reason"] == "no_interior_probe" for gap in gaps)
    matched: list[dict[str, Any]] = []
    unmatched_runset: list[str] = []
    disagreements: list[dict[str, Any]] = []
    reverse: dict[tuple[str, int], list[int]] = defaultdict(list)
    for runset_net in names:
        for hit in hits.get(runset_net, ()):
            reverse[hit].append(runset_net)
    for (name, cluster), fragments in sorted(reverse.items()):
        if len(fragments) > 1:
            disagreements.append(
                {
                    "kind": "merge",
                    "curated_net": name,
                    "cluster_id": cluster,
                    "runset_nets": sorted(names[i] for i in fragments),
                }
            )
    for runset_net in names:
        net_hits = sorted(hits.get(runset_net, ()))
        if (
            len(net_hits) == 1
            and len(reverse[net_hits[0]]) == 1
            and net_hits[0] not in possible
            and not unbounded
            and runset_net not in incomplete
        ):
            name, cluster = net_hits[0]
            matched.append(
                {
                    "runset": names[runset_net],
                    "runset_cluster_id": runset_net,
                    "curated": name,
                    "cluster_id": cluster,
                }
            )
            continue
        unmatched_runset.append(names[runset_net])
        if len(net_hits) > 1:
            disagreements.append(
                {
                    "kind": "split",
                    "runset_net": names[runset_net],
                    "curated_nets": [name for name, _ in net_hits],
                    "curated_cluster_ids": [cluster for _, cluster in net_hits],
                }
            )

    matched_clusters = {entry["cluster_id"] for entry in matched}
    unmatched = sorted(
        (entry["net"], entry["net_id"])
        for entry in curated_parasitic_nets
        if entry["net_id"] not in matched_clusters
    )
    return {
        "matched": sorted(matched, key=lambda entry: entry["runset"]),
        "unmatched_runset_nets": sorted(unmatched_runset),
        "unmatched_curated_nets": [name for name, _ in unmatched],
        "unmatched_curated_clusters": [
            {"curated": name, "cluster_id": cluster} for name, cluster in unmatched
        ],
        "disagreements": sorted(
            disagreements,
            key=lambda entry: (
                entry["kind"],
                str(entry.get("runset_net") or entry.get("curated_net")),
            ),
        ),
        "incomplete_geometry": gaps,
        "probe_count": len(probe_results),
    }


def preferred_renames(
    matched: Sequence[Mapping[str, Any]], all_runset_nets: Sequence[str]
) -> dict[str, str]:
    """``{runset name: merged name}``: a named runset net keeps its own name
    (device-side truth); an anonymous one adopts its curated counterpart's
    label when that has one -- unless that label collides with any other
    net's kept name (a runset net already called that, or a second anonymous
    net adopting the same label), in which case the anonymous spelling
    stands. Mirrors the curated writer's own duplicate-label discipline
    (its ``_dup<n>`` suffixing) with the runset's own fallback: no rename."""
    kept = {net.casefold() for net in all_runset_nets if not _is_anonymous(net)}
    renames: dict[str, str] = {}
    adopted: set[str] = set()
    for entry in sorted(matched, key=lambda item: item["runset"]):
        runset_net, curated_net = entry["runset"], entry["curated"]
        if not _is_anonymous(runset_net) or _is_anonymous(curated_net):
            continue
        # A curated display label can contain spaces or SPICE syntax. Only
        # adopt labels that form one unambiguous node token in a written card.
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_$|]*", curated_net):
            continue
        if curated_net.casefold() in kept or curated_net.casefold() in adopted:
            continue
        renames[runset_net] = curated_net
        adopted.add(curated_net.casefold())
    return renames
