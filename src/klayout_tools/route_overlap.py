"""Report-only foreign-geometry overlap pass for ``klt route`` (#5205).

Intersects each routed leg's drawn footprint with the flattened layout minus
the routed net's own connected component, plus the footprints of nets already
routed in the same request. A diagnostic only; ``klt drc`` stays authoritative.
"""

from __future__ import annotations

from typing import Any


def _net_label(net: Any) -> str | None:
    name = getattr(net, "name", None)
    if not name or str(name).startswith("$"):
        return None
    return str(name)


def _owner_cell(
    polygon_region: Any,
    layer_index: int,
    instances: dict[str, dict[str, Any]],
    cache: dict[str, Any],
) -> str | None:
    """The direct-child instance whose own shapes the overlap touches, else
    ``None`` (top-level geometry)."""
    import klayout.db as kdb

    for key in sorted(k for k in instances if "#" in k):
        info = instances[key]
        inst = info["inst"]
        if not inst.bbox().overlaps(polygon_region.bbox()):
            continue
        if key not in cache:
            region = kdb.Region(inst.cell.begin_shapes_rec(layer_index))
            region.transform(inst.trans)
            region.merge()
            cache[key] = region
        if not (cache[key] & polygon_region).is_empty():
            return key
    return None


def _foreign_overlaps(
    leg: dict[str, Any],
    nets_db: Any,
    layout: Any,
    instances: dict[str, dict[str, Any]],
    earlier: dict[tuple[int, int], list[tuple[str, Any]]],
    owner_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    """Positive-area overlaps of one routed leg's drawn footprint with
    foreign metal on its route layer: layout shapes outside the routed net's
    own connected component (the spike's "Foreign-geometry definition"), and
    the footprints of nets already routed in this run."""
    import klayout.db as kdb

    from .gen_compose_routing import _drawn_leg_footprint_region

    layer = leg["route_layer"]
    dbu = nets_db.dbu
    footprint = _drawn_leg_footprint_region(
        leg["points_um"],
        leg["width_um"],
        leg["via_drops"],
        leg["stub_widen"],
        dbu,
        layer,
    )
    layer_index = layout.find_layer(layer[0], layer[1])
    hits: list[tuple[Any, dict[str, Any]]] = []

    if layer in nets_db.flat and layer_index is not None:
        own = kdb.Region()
        for endpoint in leg["endpoints"]:
            net = nets_db.probe(
                endpoint["pin_layer"],
                int(round(endpoint["x_um"] / dbu)),
                int(round(endpoint["y_um"] / dbu)),
            )
            if net is not None:
                own += nets_db.net_shapes(net, layer)
        own.merge()
        foreign = nets_db.flat[layer] - own
        overlap = footprint & foreign
        overlap.merge()
        for polygon in overlap.each():
            region = kdb.Region(polygon)
            centre = polygon.bbox().center()
            owner_net = nets_db.probe(layer, centre.x, centre.y)
            hits.append(
                (
                    polygon,
                    {
                        "cell": _owner_cell(
                            region, layer_index, instances, owner_cache
                        ),
                        "net": None if owner_net is None else _net_label(owner_net),
                    },
                )
            )

    for other_net, other_region in earlier.get(layer, []):
        overlap = footprint & other_region
        overlap.merge()
        for polygon in overlap.each():
            hits.append((polygon, {"cell": None, "net": other_net}))

    return [
        {
            "net": leg["net"],
            "layer": {"layer": layer[0], "datatype": layer[1]},
            "foreign_owner": owner,
            "overlap_bbox_um": {
                "x0": polygon.bbox().left * dbu,
                "y0": polygon.bbox().bottom * dbu,
                "x1": polygon.bbox().right * dbu,
                "y1": polygon.bbox().top * dbu,
            },
            "overlap_area_um2": round(polygon.area() * dbu * dbu, 6),
        }
        for polygon, owner in hits
    ]


def _overlap_pass(
    legs: list[dict[str, Any]],
    layout: Any,
    top: Any,
    instances: dict[str, dict[str, Any]],
    variant: str,
) -> list[dict[str, Any]]:
    """Report-only foreign-geometry overlaps for every routed leg."""
    import klayout.db as kdb

    from .gen_compose_port_access import _MacroNets
    from .gen_compose_routing import _drawn_leg_footprint_region

    if not legs:
        return []
    # Connectivity runs on a flattened scratch copy so every net's shapes
    # (`shapes_of_net`) come back in top-cell coordinates; a net living in a
    # child circuit would otherwise be reported in that cell's frame.
    scratch = kdb.Layout()
    scratch.assign(layout)
    scratch_top = scratch.cell(top.name)
    scratch_top.flatten(True)
    nets_db = _MacroNets(scratch, scratch_top, variant)
    earlier: dict[tuple[int, int], list[tuple[str, Any]]] = {}
    owner_cache: dict[str, Any] = {}
    overlaps: list[dict[str, Any]] = []
    for leg in legs:
        overlaps.extend(
            _foreign_overlaps(leg, nets_db, layout, instances, earlier, owner_cache)
        )
        footprint = _drawn_leg_footprint_region(
            leg["points_um"],
            leg["width_um"],
            leg["via_drops"],
            leg["stub_widen"],
            layout.dbu,
            leg["route_layer"],
        )
        earlier.setdefault(leg["route_layer"], []).append((leg["net"], footprint))
    return overlaps
