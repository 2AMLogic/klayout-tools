"""``klt route``: route between labelled pins of an already-placed layout.

Slice 1 of the design in ``docs/design/route-labelled-pins-spike.md`` (#5200,
implemented by #5205). A thin front end over the existing ``gen-compose``
machinery -- it contains **no routing logic**:

* every *direct child instance* of the top cell becomes one synthetic block
  (``gds_path``/``cell_name`` of the child, ``offset_um`` and orientation from
  the instance transform, ``bbox_um`` from the placed instance);
* each ``pins[]`` label is resolved to an endpoint by
  :func:`~.gen_compose_port_access.derive_label_ports` -- the same
  label-to-net search ``gen-compose``'s ``ports_from_labels`` uses;
* each two-pin net is routed by :func:`~.gen_compose_routing.route_two_pin`,
  unchanged, and drawn by :func:`~.gen_compose._draw_routed_entry` -- the same
  drawing primitive ``gen-compose`` writes with;
* one new, **report-only** pass (:func:`_foreign_overlaps`) intersects each
  drawn leg's footprint with the flattened layout minus the routed net's own
  shapes and reports every positive-area overlap.

The overlap pass is a diagnostic like the routers' own checks: ``klt drc``
stays authoritative for spacing and rule compliance.
"""

import os
from typing import Any

from ._layout import load_layout, write_layout
from .route_instances import (
    _build_block,
    _enumerate_instances,
    _orientation_of,
    _resolve_instance,
    _select_top_cell,
)
from .route_overlap import _overlap_pass
from .route_request import RouteError, parse_request

__all__ = ["RouteError", "route"]


def _via_sizes(
    routed: list[dict[str, Any]], variant: str
) -> tuple[dict[Any, float], dict[Any, float]]:
    """Per-via-layer cut sizes and per-landing-pad sizes, resolved exactly as
    ``compose()`` does before it hands them to the drawing step."""
    from .gen_compose import (
        _VIA_DROP_SIZE_UM,
        _fixed_cut_side_um,
        _via_fixed_size_um,
    )
    from .gen_compose_routing import _min_width_um_for_layer, _resolve_landing_pad_sizes

    via_drop_size_um: dict[tuple[int, int], float] = {}
    for route in routed:
        for drop in route.get("via_drops", []):
            via_pair = drop["via_layer"]
            if via_pair not in via_drop_size_um:
                floor = _min_width_um_for_layer(variant, via_pair)
                via_drop_size_um[via_pair] = _fixed_cut_side_um(
                    max(_VIA_DROP_SIZE_UM, floor[0] if floor is not None else 0.0),
                    _via_fixed_size_um(variant, via_pair),
                )
    return via_drop_size_um, _resolve_landing_pad_sizes(
        routed, variant, via_drop_size_um
    )


def _resolve_routing(req: dict[str, Any]) -> tuple[Any, Any, Any]:
    """``(route_layer, label_layer, extraction_deck)`` for the request's PDK
    and ``routing`` block, with the deck's minimum-width floor enforced."""
    from .decks import get_extraction_deck
    from .gen import GenError
    from .gen_compose import GenComposeError
    from .gen_compose_routing import (
        _min_width_um_for_layer,
        _pdk_family,
        _resolve_label_layer,
        _resolve_route_layer,
    )

    variant = req["variant"]
    try:
        route_layer = _resolve_route_layer(variant, req["layer_role"])
        extraction_deck = get_extraction_deck(_pdk_family(variant))
    except (GenComposeError, GenError) as exc:
        raise RouteError(str(exc)) from exc
    floor = _min_width_um_for_layer(variant, route_layer)
    if floor is not None and req["width_um"] < floor[0] - 1e-9:
        raise RouteError(
            f"request.routing.width_um ({req['width_um']}um) is narrower than the "
            f"resolved PDK deck's minimum width for layer_role "
            f"'{req['layer_role']}' -- '{floor[1]}' requires >= {floor[0]}um"
        )
    return route_layer, _resolve_label_layer(variant, route_layer), extraction_deck


def _attach_port(
    block: dict[str, Any],
    instance_id: str,
    info: dict[str, Any],
    pin: dict[str, Any],
    req: dict[str, Any],
    where: str,
) -> None:
    """Derive ``pin``'s label port on ``block`` (once per label)."""
    from .gen_compose import GenComposeError, _orient_port
    from .gen_compose_port_access import derive_label_ports

    if pin["label"] in block["port_names"]:
        return
    try:
        ports, _reports, _warnings = derive_label_ports(
            instance_id, req["gds_path"], info["master"], pin["spec"], req["variant"]
        )
    except GenComposeError as exc:
        raise RouteError(
            f"{where}: {exc}",
            code="unknown_label",
            details={"cell": instance_id, "label": pin["label"]},
        ) from exc
    port = _orient_port(ports[0], block["orientation"])
    if pin["direction_deg"] is not None:
        port["direction_deg"] = pin["direction_deg"]
    block["ports"][pin["label"]] = port
    block["port_names"].add(pin["label"])


def _build_blocks(
    req: dict[str, Any], top: Any
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[list[tuple[str, dict[str, Any]]]],
]:
    """``(blocks, instances, pin_refs)``. Every direct child is a block (an
    obstacle by bbox); only blocks a pin names get a port table, and only those
    must have a supported transform. ``pin_refs[i]`` is net ``i``'s
    ``[(instance id, pin), (instance id, pin)]``."""
    instances, ambiguous = _enumerate_instances(top)
    blocks = {
        key: _build_block(key, instances[key], req["gds_path"], "none")
        for key in sorted(k for k in instances if "#" in k)
    }
    pin_refs: list[list[tuple[str, dict[str, Any]]]] = []
    for n_index, net in enumerate(req["nets"]):
        refs = []
        for p_index, pin in enumerate(net["pins"]):
            where = f"request.nets[{n_index}].pins[{p_index}]"
            instance_id, info = _resolve_instance(
                pin["cell"], instances, ambiguous, where
            )
            block = blocks[instance_id]
            block["orientation"] = _orientation_of(info["inst"], instance_id)
            _attach_port(block, instance_id, info, pin, req, where)
            refs.append((instance_id, pin))
        pin_refs.append(refs)
    for block in blocks.values():
        # Placed instance boxes are already oriented; rebuild the local bbox.
        placed, off = block["_placed_bbox_um"], block["_offset_um"]
        block["bbox_um"] = {
            "x0": placed["x0"] - off["x"],
            "y0": placed["y0"] - off["y"],
            "x1": placed["x1"] - off["x"],
            "y1": placed["y1"] - off["y"],
        }
    return blocks, instances, pin_refs


def _route_nets(
    req: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    pin_refs: list[list[tuple[str, dict[str, Any]]]],
    routing: tuple[Any, Any, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """``(routes, unrouted_nets, legs)``: the report rows plus the raw legs
    (drawing and overlap input) for every net, via ``route_two_pin``."""
    from .gen_compose_routing import read_block_layer_geometry, route_two_pin

    route_layer, label_layer, extraction_deck = routing
    offsets_um = {bid: dict(b["_offset_um"]) for bid, b in blocks.items()}
    placed_bboxes_um = {bid: dict(b["_placed_bbox_um"]) for bid, b in blocks.items()}
    geometry_cache: dict[str, dict[str, Any] | None] = {}

    def geometry_for(block_id: str) -> dict[str, dict[str, Any] | None]:
        if block_id not in geometry_cache:
            geometry_cache[block_id] = read_block_layer_geometry(
                block_id, blocks[block_id], offsets_um[block_id], route_layer
            )
        return geometry_cache

    routes: list[dict[str, Any]] = []
    unrouted: list[dict[str, str]] = []
    legs: list[dict[str, Any]] = []
    for net, refs in zip(req["nets"], pin_refs, strict=True):
        (id_a, pin_a), (id_b, pin_b) = refs
        geometry_for(id_a)
        geometry_for(id_b)
        result = route_two_pin(
            {"block": id_a, "port": pin_a["label"]},
            {"block": id_b, "port": pin_b["label"]},
            blocks,
            offsets_um,
            placed_bboxes_um,
            req["width_um"],
            route_layer=route_layer,
            extraction_deck=extraction_deck,
            block_geometry=geometry_cache,
            block_geometry_for=geometry_for,
        )
        if not result["routed"]:
            unrouted.append({"net": net["net"], "reason": result.get("reason") or ""})
            continue
        endpoints = [
            _endpoint(blocks[block_id], offsets_um[block_id], pin)
            for block_id, pin in refs
        ]
        effective_layer = result.get("route_layer") or route_layer
        legs.append(
            {
                "net": net["net"],
                "points_um": [tuple(p) for p in result["points_um"]],
                "width_um": result["width_um"],
                "via_drops": result["via_drops"],
                "stub_widen": result["stub_widen"],
                "route_layer": effective_layer,
                "label_layer": label_layer,
                "endpoints": endpoints,
            }
        )
        routes.append(
            {
                "net": net["net"],
                "status": "routed",
                "layer": {"layer": effective_layer[0], "datatype": effective_layer[1]},
                "width_um": result["width_um"],
                "length_um": result["route_length_um"],
                "points_um": [list(p) for p in result["points_um"]],
                "endpoints": [
                    {k: e[k] for k in ("cell", "label", "x_um", "y_um")}
                    for e in endpoints
                ],
            }
        )
    return routes, unrouted, legs


def _endpoint(
    block: dict[str, Any], offset: dict[str, float], pin: dict[str, Any]
) -> dict[str, Any]:
    port = block["ports"][pin["label"]]
    return {
        "cell": pin["cell"],
        "label": pin["label"],
        "x_um": float(port["x_um"]) + offset["x"],
        "y_um": float(port["y_um"]) + offset["y"],
        "pin_layer": (port["layer"]["layer"], port["layer"]["datatype"]),
    }


def _write_output(
    req: dict[str, Any],
    layout: Any,
    top: Any,
    legs: list[dict[str, Any]],
    routing: tuple[Any, Any, Any],
) -> dict[str, str] | None:
    """Draw every routed leg into the (already analysed) layout and write it."""
    from .gen_compose import _draw_routed_entry

    if req["output"] is None:
        return None
    route_layer, label_layer, _deck = routing
    via_drop_size_um, landing_pad_size_um = _via_sizes(legs, req["variant"])
    for leg in legs:
        _draw_routed_entry(
            layout,
            top,
            leg,
            layout.dbu,
            route_layer,
            label_layer,
            via_drop_size_um,
            landing_pad_size_um,
        )
    write_layout(layout, req["output"], RouteError)
    return {"gds_path": req["output"], "cell_name": top.name}


def route(request: Any, request_dir: str | None = None) -> dict[str, Any]:
    """Run one ``klt route`` request and return the report (see
    ``docs/cli/route.md``). Raises :class:`RouteError` for an application
    error (exit code 1)."""
    req = parse_request(request, request_dir or os.getcwd())
    layout = load_layout(req["gds_path"], RouteError)
    top = _select_top_cell(layout, req["top_cell"], req["gds_path"])
    routing = _resolve_routing(req)
    blocks, instances, pin_refs = _build_blocks(req, top)
    routes, unrouted, legs = _route_nets(req, blocks, pin_refs, routing)
    overlaps = _overlap_pass(legs, layout, top, instances, req["variant"])
    output = _write_output(req, layout, top, legs, routing)
    status = "overlaps" if overlaps else "unrouted" if unrouted else "clean"
    return {
        "schema_version": 1,
        "status": status,
        "layout": {"gds_path": req["gds_path"], "top_cell": top.name},
        "output": output,
        "dbu_um": layout.dbu,
        "routes": routes,
        "unrouted_nets": unrouted,
        "overlaps": overlaps,
    }
