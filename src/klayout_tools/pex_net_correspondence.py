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

The join, in one paragraph
--------------------------

Both extractions cluster the *same original shapes* (the same GDS) into
nets. For each net of the runset's selected circuit, sample strictly
interior points of its per-net polygons on the runset database's *metal*
layers only, and ask the curated extraction which net each point belongs to
(``klt extract --probe``'s own point-to-net machinery, evaluated on the
curated run's live ``LayoutToNetlist``). Exactly one distinct curated net
hit -> a match. More than one -> the runset net spans several curated nets
(a **split** disagreement). A curated net hit by several runset nets -> a
**merge** disagreement. No hit -> unmatched. Every outcome is reported in
the envelope's ``extraction.net_correspondence``; parasitics attach only to
1:1 matches, and a disagreement is reported, never guessed (issue #6606
AC 2/3).

Three restrictions the measurements forced (the decision doc carries the
numbers): sample points are **strictly interior** (a boundary point of one
net's polygon can lie exactly on another net's shape -- measured: a
substrate cluster's bbox centre landing on a gate-metal edge probed the
gate net), the join runs on **metal layers only** (device layers overlap by
construction -- the channel zone is inside both the source/drain and the
gate clusters of either engine; a well *contains* the devices' diffusion),
and a runset-internal layer joins one metal role only when **>= 99% of its
area lies inside that role's own GDS layer** (the runset database's layer
indices are anonymous; shape-set identity against the original stream is
what says "this internal layer *is* metal1").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MAX_POINTS_PER_LAYER = 16
MAX_POINTS_PER_NET = 64

#: An internal layer joins a metal role when at least this fraction of its
#: own area lies inside the role's GDS layer. Original metal layers are
#: single-source handles, so the real figure is 100%; anything mixed is a
#: derived layer and joins nothing.
_LAYER_IDENTITY_AREA_FRACTION = 0.99


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
    if not any(
        (
            centre.x == box.left,
            centre.x == box.right,
            centre.y == box.bottom,
            centre.y == box.top,
        )
    ) and poly.inside(centre):
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
        if step != vertex and poly.inside(step):
            return step
    return None


def _flat_region(layout: Any, top_cell: Any, layer: int, datatype: int) -> Any:
    """The flattened original-stream shapes of one GDS layer, or an empty
    region when the stream does not draw it."""
    import klayout.db as kdb

    index = layout.find_layer(layer, datatype)
    if index is None:
        return kdb.Region()
    return kdb.Region(top_cell.begin_shapes_rec(index))


def _union_of_net_regions(lvs: Any, nets: Sequence[Any], layer_index: int) -> Any:
    """The runset database's whole per-layer region, accumulated over every
    net of every circuit it carries."""
    import klayout.db as kdb

    region: Any = None
    for net in nets:
        part = lvs.polygons_of_net(net, layer_index, True)
        if part is not None and not part.is_empty():
            region = part if region is None else region + part
    return kdb.Region() if region is None else region


def metal_role_map(
    lvs: Any, layout: Any, top_cell: Any, metals: Sequence[tuple[int, int]]
) -> dict[int, str]:
    """``{internal layer index: metal role}`` for the runset database's own
    layers whose shape set is (>= 99% by area) one of the curated deck's
    metal GDS layers -- the layer identity that lets a sampled point probe
    exactly the role it is real geometry of."""
    nets = [net for c in lvs.netlist().each_circuit() for net in c.each_net()]
    gds_regions = {
        f"metal{index}": _flat_region(layout, top_cell, layer, datatype)
        for index, (layer, datatype) in enumerate(metals)
    }
    roles: dict[int, str] = {}
    for layer_index in lvs.internal_layout().layer_indexes():
        region = _union_of_net_regions(lvs, nets, layer_index)
        if region.is_empty():
            continue
        area = region.area()
        for role in sorted(gds_regions):
            inside = (region & gds_regions[role]).area()
            if inside >= area * _LAYER_IDENTITY_AREA_FRACTION:
                roles[layer_index] = role
                break
    return roles


def _net_probe_points(
    lvs: Any, net: Any, roles: Mapping[int, str]
) -> dict[str, list[Any]]:
    """``{role: [kdb.Point, ...]}`` -- strictly interior sample points of
    ``net``'s polygons, per matched metal role, capped per layer and net."""
    points: dict[str, list[Any]] = {}
    total = 0
    for layer_index, role in sorted(roles.items()):
        region = lvs.polygons_of_net(net, layer_index, True)
        if region is None:
            continue
        polygons = list(region.each())
        stride = max(1, len(polygons) // MAX_POINTS_PER_LAYER + 1)
        for poly in polygons[::stride]:
            if total >= MAX_POINTS_PER_NET:
                return points
            point = _interior_point(poly)
            if point is not None:
                points.setdefault(role, []).append(point)
                total += 1
    return points


def build_probes(
    lvsdb_path: str,
    layout_path: str,
    *,
    top: str,
    metals: Sequence[tuple[int, int]],
    dbu_um: float,
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[float, float]]]]:
    """The ``run_extract`` ``probes`` list for the correspondence -- one
    probe per runset-net sample point, on the metal role its layer matched
    -- plus ``{runset net name: [(x_um, y_um), ...]}`` keyed in the
    report's escaped ``nets[].name`` spelling, so the join's keys line up
    with the curated probe results and the written netlist's node tokens."""
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
    roles = metal_role_map(lvs, layout, top_cell, metals)
    probes: list[dict[str, Any]] = []
    positions: dict[str, list[tuple[float, float]]] = {}
    for net in circuit.each_net():
        name = _escaped_net_name(net.expanded_name())
        for role, pts in _net_probe_points(lvs, net, roles).items():
            entries = positions.setdefault(name, [])
            for index, point in enumerate(pts):
                probes.append(
                    {
                        "name": f"kltcorr|{role}|{name}|{index}",
                        "layer": role,
                        "x": point.x * dbu_um,
                        "y": point.y * dbu_um,
                    }
                )
                entries.append((point.x * dbu_um, point.y * dbu_um))
    return probes, positions


# --------------------------------------------------------------------------- #
# The join over probe results
# --------------------------------------------------------------------------- #


def correspondence(
    runset_net_names: Sequence[str],
    probe_results: Sequence[Mapping[str, Any]],
    curated_parasitic_nets: Sequence[Mapping[str, Any]],
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
    hits: dict[str, set[tuple[str, int]]] = {}
    for result in probe_results:
        if not result.get("found"):
            continue
        parts = str(result.get("name") or "").split("|")
        if len(parts) != 4 or parts[0] != "kltcorr":
            continue
        hit = (str(result["net"]), int(result["cluster_id"]))
        hits.setdefault(parts[2], set()).add(hit)

    matched: list[dict[str, Any]] = []
    unmatched_runset: list[str] = []
    disagreements: list[dict[str, Any]] = []
    reverse: dict[tuple[str, int], list[str]] = {}
    for runset_net in runset_net_names:
        net_hits = sorted(hits.get(runset_net, ()))
        if len(net_hits) == 1:
            reverse.setdefault(net_hits[0], []).append(runset_net)
        else:
            unmatched_runset.append(runset_net)
            if net_hits:
                disagreements.append(
                    {
                        "kind": "split",
                        "runset_net": runset_net,
                        "curated_nets": [name for name, _ in net_hits],
                    }
                )
    for curated_hit, runset_nets in sorted(reverse.items()):
        if len(runset_nets) > 1:
            # A curated net several runset nets reach is a merge
            # disagreement: its parasitics attach to neither fragment, so
            # every fragment is unmatched too -- never silently merged.
            unmatched_runset.extend(runset_nets)
            disagreements.append(
                {
                    "kind": "merge",
                    "curated_net": curated_hit[0],
                    "runset_nets": sorted(runset_nets),
                }
            )
        else:
            matched.append({"runset": runset_nets[0], "curated": curated_hit[0]})
    matched_curated = {entry["curated"] for entry in matched}
    return {
        "matched": sorted(matched, key=lambda entry: entry["runset"]),
        "unmatched_runset_nets": sorted(unmatched_runset),
        "unmatched_curated_nets": sorted(
            {
                str(entry["net"])
                for entry in curated_parasitic_nets
                if str(entry["net"]) not in matched_curated
            }
        ),
        "disagreements": sorted(
            disagreements,
            key=lambda entry: (
                entry["kind"],
                str(entry.get("runset_net") or entry.get("curated_net")),
            ),
        ),
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
    kept = {net for net in all_runset_nets if not _is_anonymous(net)}
    renames: dict[str, str] = {}
    adopted: set[str] = set()
    for entry in sorted(matched, key=lambda item: item["runset"]):
        runset_net, curated_net = entry["runset"], entry["curated"]
        if not _is_anonymous(runset_net) or _is_anonymous(curated_net):
            continue
        if curated_net in kept or curated_net in adopted:
            continue
        renames[runset_net] = curated_net
        adopted.add(curated_net)
    return renames
