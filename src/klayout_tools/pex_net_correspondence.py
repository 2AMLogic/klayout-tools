"""The geometric net correspondence joining a runset-engine ``klt pex``
leg's nets with a curated deck's parasitic extraction (issue #6606).

``klt pex --engine klayout-runset`` (issue #6559) extracts its leg with the
PDK's own LVS runset, so the device set is the right one -- but with no
parasitics: the runset and a curated deck number their unlabelled nets
independently (``\\$1``, ``docs/cli/extract.md``'s "Anonymous net
numbering"), so curated parasitics cannot be attached to runset devices by
name. This module puts the two extractions in correspondence **by
geometry**; :mod:`klayout_tools.pex_merged_leg` builds the merged leg on
top of it. The measured decision -- why this rule and not the alternatives
-- is ``docs/design/runset-pex-net-correspondence.md``; this docstring
records the rule itself.

Every metal polygon of the selected runset circuit contributes a strictly
interior probe to the curated extractor. The layer comes from the source
GDS layer/datatype or, for an anonymous layer, an unambiguous comparison
with complete stream polygons. Confirmed hits on multiple curated clusters
show a split. Hits by multiple runset nets on one cluster show a merge.
Only a unique match with complete coverage receives parasitics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


def _pex_error(message: str) -> Exception:
    """A ``PexError`` built lazily, so importing this module never imports
    ``pex`` (which imports this module's caller's pieces at load time)."""
    from .pex import PexError

    return PexError(message)


def _is_anonymous(name: str) -> bool:
    """Whether a net name is KLayout's own ``$N`` placeholder -- either
    spelling: bare in memory, backslash-escaped in the written netlist."""
    return name.lstrip("\\").startswith("$")


def _escaped_net_name(raw: str) -> str:
    """The report/netlist node spelling of a net name (issue #1162's
    backslash-escaped ``\\$N``) -- the spelling both the curated probe
    results and the runset leg's own node tokens use."""
    return "\\" + raw if raw.startswith("$") else raw


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
    name: str,
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
                    "name": f"{prefix}|{role}|{name}|{layer}-{index}",
                    "layer": role,
                    "x": point.x * dbu_um,
                    "y": point.y * dbu_um,
                }
            )
    details = {"runset_net": name, "layer_index": layer, "candidate_roles": list(roles)}
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


def build_probes(
    lvsdb_path: str,
    layout_path: str,
    *,
    top: str,
    metals: Sequence[tuple[int, int]],
    dbu_um: float,
    contact: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Probe every identified metal polygon and record gaps in net coverage.

    Probes of candidate roles on an unidentified layer can rule out another
    net's exclusive match. They never establish a match or disagreement.
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(layout_path)
    top_cell = layout.cell(top)
    if top_cell is None:
        raise _pex_error(
            f"net correspondence: layout '{layout_path}' has no cell '{top}' "
            "to read the curated deck's metal layers from"
        )
    lvs = kdb.LayoutVsSchematic()
    lvs.read(lvsdb_path)
    circuit = lvs.netlist().circuit_by_name(top)
    if circuit is None:
        raise _pex_error(
            "net correspondence: the runset database carries no circuit named "
            f"'{top}', so its nets cannot be joined with the curated deck's"
        )
    roles, uncertain = _layer_roles(lvs, circuit, layout, top_cell, metals)
    landings = _contact_landings(lvs, layout, top_cell, contact, metals)
    roles.update(dict.fromkeys(landings, "metal0"))
    layers = sorted(roles.keys() | uncertain.keys())
    probes = []
    incomplete = []
    for net in circuit.each_net():
        name = _escaped_net_name(net.expanded_name())
        for layer in layers:
            region = lvs.polygons_of_net(net, layer, True)
            if region is None or region.is_empty():
                continue
            if layer in landings:
                region = region & landings[layer]
            candidates = [roles[layer]] if layer in roles else uncertain[layer]
            points, gaps = _layer_probes(
                region, name, layer, candidates, certain=layer in roles, dbu_um=dbu_um
            )
            probes.extend(points)
            incomplete.extend(gaps)
    return probes, incomplete


# --------------------------------------------------------------------------- #
# The join over probe results
# --------------------------------------------------------------------------- #


def _probe_hits(
    probe_results: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, set[tuple[str, int]]], set[tuple[str, int]], set[str]]:
    """Return confirmed hits, possible cluster hits and nets with a probe miss."""
    hits: dict[str, set[tuple[str, int]]] = defaultdict(set)
    possible: set[tuple[str, int]] = set()
    missed: set[str] = set()
    for result in probe_results:
        parts = str(result.get("name") or "").split("|")
        if len(parts) != 4 or parts[0] not in {"kltcorr", "kltmaybe"}:
            continue
        if result.get("found"):
            hit = (str(result["net"]), int(result["cluster_id"]))
            if parts[0] == "kltcorr":
                hits[parts[2]].add(hit)
            else:
                possible.add(hit)
        elif parts[0] == "kltcorr":
            missed.add(parts[2])
    return hits, possible, missed


def correspondence(
    runset_net_names: Sequence[str],
    probe_results: Sequence[Mapping[str, Any]],
    curated_parasitic_nets: Sequence[Mapping[str, Any]],
    incomplete_geometry: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The ``net_correspondence`` envelope block (issue #6606 AC 2/3).

    ``probe_results`` is the curated run's ``probes[]`` (a ``found`` entry
    carries the final curated ``net`` name and ``cluster_id`` -- identity,
    robust to two nets sharing one label); ``runset_net_names`` the selected
    circuit's nets in the report's escaped spelling;
    ``curated_parasitic_nets`` the curated ``parasitics.nets[]`` entries, so
    a curated parasitic net nobody matched is listed even though it never
    probes anything itself.
    """
    hits, possible, missed = _probe_hits(probe_results)
    gaps = list(incomplete_geometry)
    gaps.extend(
        {"runset_net": net, "reason": "unresolved_probe"} for net in sorted(missed)
    )
    incomplete = {entry["runset_net"] for entry in gaps}
    matched: list[dict[str, Any]] = []
    unmatched_runset: list[str] = []
    disagreements: list[dict[str, Any]] = []
    reverse: dict[tuple[str, int], list[str]] = defaultdict(list)
    for runset_net in runset_net_names:
        for hit in hits.get(runset_net, ()):
            reverse[hit].append(runset_net)
    for (name, cluster), runset_nets in sorted(reverse.items()):
        if len(runset_nets) > 1:
            disagreements.append(
                {
                    "kind": "merge",
                    "curated_net": name,
                    "cluster_id": cluster,
                    "runset_nets": sorted(runset_nets),
                }
            )
    for runset_net in runset_net_names:
        net_hits = sorted(hits.get(runset_net, ()))
        if (
            len(net_hits) == 1
            and len(reverse[net_hits[0]]) == 1
            and net_hits[0] not in possible
            and runset_net not in incomplete
        ):
            name, cluster = net_hits[0]
            matched.append(
                {"runset": runset_net, "curated": name, "cluster_id": cluster}
            )
            continue
        unmatched_runset.append(runset_net)
        if len(net_hits) > 1:
            disagreements.append(
                {
                    "kind": "split",
                    "runset_net": runset_net,
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
        if curated_net.casefold() in kept or curated_net.casefold() in adopted:
            continue
        renames[runset_net] = curated_net
        adopted.add(curated_net.casefold())
    return renames
