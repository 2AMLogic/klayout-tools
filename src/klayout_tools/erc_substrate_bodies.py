"""Body-merge geometry core for the substrate-continuity post-pass.

The pure-geometry half of the ``erc.supply_short`` body-merge pass (design
``docs/design/erc-substrate-continuity.md`` section 3): every merged tap site
of every non-degenerate tie is assigned to a **body** (a well polygon, the
shared substrate, or one ``vsubs_iso<n>`` isolation island), and tie-graph
clusters that touch the same body are union-found. No findings, coverage or
provenance concerns live here -- :mod:`klayout_tools.erc_substrate` keeps the
pass orchestration and the finding shape.

Issue #5745 fixes live here too: a substrate assertion reaching over a drawn
well can no longer capture that well's own taps (``wells``), and the union
records *which* tap polygon welded a net to a body (``joins`` evidence).
"""

from __future__ import annotations

from typing import Any

from ._unionfind import UnionFind


def _is_drawn_well(tie: dict[str, Any]) -> bool:
    """A tie naming a drawn ``well_layer`` taps that well; any other form
    (``well_boxes`` / ``well_complement_of``) taps the substrate."""
    return bool(
        tie["well_layer"] and not tie["well_boxes"] and not tie["well_complement_of"]
    )


def _drawn_well_bodies(tie: dict[str, Any]) -> list[tuple[tuple[str, str], Any]]:
    """Bodies for a drawn-well tie: one per merged well polygon its taps
    touch, keyed by that polygon's bounding box. A drawn-well tie keeps its
    own sites: its taps are its own well's by construction."""
    import klayout.db as kdb

    sites = tie["tap_region"]
    bodies: list[tuple[tuple[str, str], Any]] = []
    for well_poly in tie["well_region"].merged().each():
        here = sites.interacting(kdb.Region(well_poly))
        if not here.is_empty():
            box = well_poly.bbox()
            key = f"{box.left},{box.bottom},{box.right},{box.top}"
            bodies.append((("well", key), here))
    return bodies


def _box_island_bodies(
    sites: Any, box_region: Any
) -> tuple[Any, list[tuple[tuple[str, str], Any]]]:
    """``(outside, bodies)`` for the asserted ``isolation_boxes``: the tap
    sites inside each isolated island become their own substrate body keyed
    ``isolation_box[...]``; ``outside`` is what no box covered."""
    from .erc_islands import partition_region_by_islands, sorted_islands

    box_islands = sorted_islands(box_region)
    outside, per_box = partition_region_by_islands(sites, box_islands)
    bodies: list[tuple[tuple[str, str], Any]] = []
    for box, part in zip(box_islands, per_box, strict=True):
        if not part.is_empty():
            b = box.bbox()
            bodies.append(
                (
                    (
                        "substrate",
                        f"isolation_box[{b.left},{b.bottom},{b.right},{b.top}]",
                    ),
                    part,
                )
            )
    return outside, bodies


def _substrate_bodies(
    tie: dict[str, Any], islands: list[Any], box_region: Any, wells: Any
) -> list[tuple[tuple[str, str], Any]]:
    """Bodies for a tie whose substrate comes from an assertion or a
    complement: isolation-box islands first, then declared islands, then the
    shared ``vsubs`` remainder."""
    from .erc_islands import partition_region_by_islands

    sites = tie["tap_region"]
    if wells is not None and not wells.is_empty():
        # Issue #5745: taps inside a drawn well tap that well -- junction-
        # isolated from the substrate -- so an asserted substrate never
        # claims another tie's well taps, however far its boxes reach.
        sites = sites - wells
    bodies: list[tuple[tuple[str, str], Any]] = []
    outside = sites
    if box_region is not None and not box_region.is_empty():
        outside, box_bodies = _box_island_bodies(sites, box_region)
        bodies.extend(box_bodies)
    outside, per_island = partition_region_by_islands(outside, islands)
    if not outside.is_empty():
        bodies.append((("substrate", "vsubs"), outside))
    for index, part in enumerate(per_island):
        if not part.is_empty():
            bodies.append((("substrate", f"vsubs_iso{index}"), part))
    return bodies


def _tie_bodies(
    tie: dict[str, Any], islands: list[Any], box_region: Any = None, wells: Any = None
) -> list[tuple[tuple[str, str], Any]]:
    """``[(body_key, tap_sites_in_body), ...]`` for one tie.

    ``body_key`` is ``(kind, id)``: a well body is keyed by its merged
    polygon's bounding box, the substrate by ``vsubs`` / ``vsubs_iso<n>``.

    ``wells`` (issue #5745) is the union of every *drawn-well* tie's well
    region. A tap inside a drawn well taps that well -- it is junction-
    isolated from the substrate -- so a tie whose substrate comes from an
    assertion or a complement never claims another tie's well taps, however
    far its asserted boxes reach. A drawn-well tie keeps its own sites: its
    taps are its own well's by construction.
    """
    if _is_drawn_well(tie):
        return _drawn_well_bodies(tie)
    return _substrate_bodies(tie, islands, box_region, wells)


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


def _isolation_box_region(
    layout: Any, tie_layers: list[dict[str, Any]], degenerate_ties: dict[str, str]
) -> Any:
    """Union of every live ``isolation_boxes`` assertion. It is about where the
    *substrate* is isolated, so it applies to every tie's tap sites there, not
    just the asserting tie's (tap geometry is net-agnostic)."""
    import klayout.db as kdb

    from .erc_well_regions import _boxes_region

    region = kdb.Region()
    for tie in tie_layers:
        if tie["isolation_boxes"] and tie["name"] not in degenerate_ties:
            region += _boxes_region(kdb, layout, tie["isolation_boxes"])
    return region


def _asserted_box_regions(tie: dict[str, Any], layout: Any) -> list[Any] | None:
    """One region per entry of this tie's ``well_boxes`` assertion, or
    ``None`` when its sites did not come from one -- the shape needed to name
    *which* asserted box produced a given tap site (issue #5745)."""
    if not tie["well_boxes"]:
        return None
    import klayout.db as kdb

    from .erc_well_regions import _boxes_region

    return [_boxes_region(kdb, layout, [box]) for box in tie["well_boxes"]]


def _well_box_index(box_regions: list[Any] | None, poly: Any) -> int | None:
    """Index of the asserted ``well_boxes`` entry whose area touches this tap
    polygon (lowest index when several do), or ``None`` when the tie's sites
    were not asserted. Touch-or-overlap, matching every other spatial test
    in this module."""
    if box_regions is None:
        return None
    pb = poly.bbox()
    for index, region in enumerate(box_regions):
        b = region.bbox()
        if (
            pb.left <= b.right
            and b.left <= pb.right
            and pb.bottom <= b.top
            and b.bottom <= pb.top
        ):
            return index
    return None


def _join_records(
    tie: dict[str, Any], hit: Any, box_regions: list[Any] | None
) -> list[dict[str, Any]]:
    """One ``{net, tie, bbox, well_box?}`` record per joining tap polygon
    (issue #5745): the evidence that a body merge used this tie's geometry."""
    from .erc import _bbox_dict

    records: list[dict[str, Any]] = []
    for poly in hit.each():
        record: dict[str, Any] = {
            "tie": tie["name"],
            "bbox": _bbox_dict(poly.bbox()),
        }
        index = _well_box_index(box_regions, poly)
        if index is not None:
            record["well_box"] = index
        records.append({"net": tie["net"], **record})
    return records


def _attribute_net(
    uf: UnionFind,
    bodies: dict[tuple[str, str], dict[str, Any]],
    tie: dict[str, Any],
    net: Any,
    net_taps: Any,
    tie_bodies: list[tuple[tuple[str, str], Any]],
    box_regions: list[Any] | None,
    names_by_cluster: dict[int, set[str]],
) -> None:
    """Weld one tie net to every body its tap polygons touch, recording the
    joining-tap evidence under the net that owns it."""
    for body_key, body_sites in tie_bodies:
        hit = net_taps.interacting(body_sites)
        if hit.is_empty():
            continue
        info = bodies.setdefault(
            body_key,
            {
                "ties": set(),
                "sites": body_sites.dup(),
                "clusters": set(),
                "joins": [],
            },
        )
        # Attribute the tie only where its declared net is the one
        # reaching this body (a tie's tap geometry is net-agnostic),
        # and record the joining tap evidence under that same net --
        # this tie's layer contains every tap in its region, so an
        # ungated record would name another net's tie (issue #5745).
        if tie["net"] in names_by_cluster.get(net.cluster_id, ()):
            info["ties"].add(tie["name"])
            info["joins"].extend(_join_records(tie, hit, box_regions))
        info["clusters"].add(net.cluster_id)
        uf.union(("c", net.cluster_id), ("b", body_key))


def _merge_tie_sites(
    bodies: dict[tuple[str, str], dict[str, Any]],
    tie_bodies: list[tuple[tuple[str, str], Any]],
) -> None:
    """Grow each already-attributed body's merged site region with this
    tie's sites (a short finding reports the body's full extent)."""
    for body_key, body_sites in tie_bodies:
        if body_key in bodies:
            bodies[body_key]["sites"] = (
                bodies[body_key]["sites"] + body_sites
            ).merged()


def _assign_bodies(
    tie_l2n: Any,
    tie_circuit: Any,
    tie_layers: list[dict[str, Any]],
    degenerate_ties: dict[str, str],
    islands: list[Any],
    names_by_cluster: dict[int, set[str]],
    box_region: Any,
    wells: Any,
    layout: Any,
) -> tuple[UnionFind, dict[tuple[str, str], dict[str, Any]]]:
    """Union-find tie-graph clusters over the bodies their tap sites touch.

    Returns ``(union_find, bodies)``; ``bodies[key]`` carries the attributed
    ``ties``, the merged tap ``sites``, the touching ``clusters`` and the
    ``joins`` evidence (issue #5745): one ``{tie, bbox, well_box?}`` record
    per tap polygon that welded a cluster to the body, so a short finding can
    name the geometry that performed the merge.
    """
    uf = UnionFind()
    bodies: dict[tuple[str, str], dict[str, Any]] = {}
    for tie in tie_layers:
        if tie["name"] in degenerate_ties:
            continue
        tie_bodies = _tie_bodies(tie, islands, box_region, wells)
        box_regions = _asserted_box_regions(tie, layout)
        for net in tie_circuit.each_net():
            net_taps = tie_l2n.polygons_of_net(net, tie["tap_index"])
            if net.cluster_id == 0 or net_taps.is_empty():
                continue
            _attribute_net(
                uf,
                bodies,
                tie,
                net,
                net_taps,
                tie_bodies,
                box_regions,
                names_by_cluster,
            )
        _merge_tie_sites(bodies, tie_bodies)
    return uf, bodies
