"""Caller-selected port coordinates for ``klt gen-compose`` ``pins[]`` (#3971).

A ``pins[]`` entry may carry an optional ``request`` object::

    {"net": "A", "block": "U0", "port": "A_in",
     "request": {"x_um": 3.17, "layer_role": "metal"}}

Without ``request`` a pin is promoted exactly where the block drew its port.
With it, :func:`plan_port_requests` draws one short axis-aligned stub on the
port's own layer from the port's attach point to the requested coordinate and
the pin label / composed ``ports[]`` entry land at that coordinate. The
requested value is never snapped or moved: a coordinate that cannot be drawn
legally is a structured failure (:class:`~.gen_compose.GenComposeError`
carrying a distinct ``code``), not a nearby substitute.

Bounded on purpose: no vias, no layer changes (``layer_role`` must resolve to
the port's own drawn layer), at most one jog (an L when both axes are
requested). This is not a router.

Dependency direction mirrors ``gen_compose_port_access.py``: ``gen_compose.py``
imports this module, which imports ``gen_compose`` names only inside functions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._jsonnum import is_number

#: Error codes (``error.code`` in the JSON envelope), documented in
#: ``docs/cli/gen-compose.md``.
CODE_INVALID = "port_request_invalid"
CODE_UNSUPPORTED_LAYER = "port_request_unsupported_layer"
CODE_OFF_GRID = "port_request_off_grid"
CODE_OUT_OF_BOUNDS = "port_request_out_of_bounds"
CODE_CONFLICT = "port_request_conflict"
CODE_ON_CONNECTIVITY_PORT = "port_request_on_connectivity_port"

_ALLOWED_REQUEST_KEYS = {"x_um", "y_um", "layer_role"}
_EPS = 1e-9


def _error(message: str, code: str, **details: Any) -> Exception:
    from .gen_compose import GenComposeError

    return GenComposeError(message, code=code, details=details or None)


def parse_port_request(raw: Any, where: str) -> dict[str, Any]:
    """Validate one ``pins[].request`` object; returns ``{x_um?, y_um?,
    layer_role?}`` holding only the keys the caller supplied."""
    if not isinstance(raw, dict):
        raise _error(f"{where}.request must be a JSON object", CODE_INVALID)
    unknown = sorted(set(raw) - _ALLOWED_REQUEST_KEYS)
    if unknown:
        raise _error(
            f"{where}.request has unknown key(s) {unknown}; allowed: "
            f"{sorted(_ALLOWED_REQUEST_KEYS)}",
            CODE_INVALID,
        )
    parsed: dict[str, Any] = {}
    for axis in ("x_um", "y_um"):
        if axis in raw:
            value = raw[axis]
            if not is_number(value) or value != value or abs(value) == float("inf"):
                raise _error(
                    f"{where}.request.{axis} must be a finite number", CODE_INVALID
                )
            parsed[axis] = float(value)
    if "x_um" not in parsed and "y_um" not in parsed:
        raise _error(
            f"{where}.request must name at least one of x_um / y_um", CODE_INVALID
        )
    if "layer_role" in raw:
        role = raw["layer_role"]
        if not isinstance(role, str) or not role:
            raise _error(
                f"{where}.request.layer_role must be a non-empty string", CODE_INVALID
            )
        parsed["layer_role"] = role
    return parsed


def _on_grid(value: float, grid: float) -> bool:
    steps = value / grid
    return abs(steps - round(steps)) < 1e-6


@dataclass
class _Ctx:
    """The composed-frame facts every request is planned against."""

    blocks: dict[str, dict[str, Any]]
    offsets_um: dict[str, dict[str, float]]
    placed_bboxes_um: dict[str, dict[str, float]]
    resolve_layer: Callable[[str], tuple[int, int]]
    grid_for_layer: Callable[[tuple[int, int]], float]
    min_width_for_layer: Callable[[tuple[int, int]], float | None]
    min_spacing_for_layer: Callable[[tuple[int, int]], float | None]
    block_geometry: Callable[[str, tuple[int, int]], dict[str, Any] | None]
    routed_regions: list[tuple[str, frozenset[tuple[str, str]], Any, Any]]
    dbu: float

    @property
    def bounds(self) -> dict[str, float]:
        boxes = list(self.placed_bboxes_um.values())
        return {
            "x0": min(b["x0"] for b in boxes),
            "y0": min(b["y0"] for b in boxes),
            "x1": max(b["x1"] for b in boxes),
            "y1": max(b["y1"] for b in boxes),
        }

    def to_dbu(self, value: float) -> int:
        return int(round(value / self.dbu))


def _check_layer(
    ctx: _Ctx, where: str, request: dict, layer: tuple[int, int], port: str
) -> None:
    role = request.get("layer_role")
    if role is not None and ctx.resolve_layer(role) != layer:
        raise _error(
            f"{where}: request.layer_role '{role}' resolves to a different "
            f"layer than port '{port}' draws on ({layer[0]}/{layer[1]}); "
            "a requested coordinate is reached on the port's own layer "
            "only (no via-drop or layer change)",
            CODE_UNSUPPORTED_LAYER,
            layer_role=role,
            port_layer={"layer": layer[0], "datatype": layer[1]},
        )


def _check_grid(ctx: _Ctx, where: str, request: dict, layer: tuple[int, int]) -> None:
    grid = ctx.grid_for_layer(layer)
    for axis in ("x_um", "y_um"):
        if axis not in request:
            continue
        value = request[axis]
        if grid > 0 and not _on_grid(value, grid):
            raise _error(
                f"{where}: request.{axis} {value:.6g}um is off the "
                f"{grid:g}um manufacturing grid of layer {layer[0]}/{layer[1]}; "
                "the requested coordinate is not moved",
                CODE_OFF_GRID,
                grid_um=grid,
            )
        if abs(value / ctx.dbu - round(value / ctx.dbu)) > 1e-6:
            raise _error(
                f"{where}: request.{axis} {value:.6g}um is not representable "
                f"on the layout database unit ({ctx.dbu:g}um)",
                CODE_OFF_GRID,
                dbu_um=ctx.dbu,
            )


def _stub_width(ctx: _Ctx, where: str, port: dict, layer: tuple[int, int]) -> float:
    reported = port.get("width_um")
    reported_um = float(reported) if isinstance(reported, (int, float)) else 0.0
    width = max(reported_um, ctx.min_width_for_layer(layer) or 0.0)
    if width <= 0:
        raise _error(
            f"{where}: cannot size a stub for port '{port['name']}' (no "
            "reported width and no deck minimum width)",
            CODE_INVALID,
        )
    return width


def _stub_polyline(
    attach: tuple[float, float], target: tuple[float, float], half: float
) -> list[tuple[float, float]]:
    """Attach point to target, one jog at most. The last segment runs half a
    width past the target so the pin label written exactly at the target sits
    inside the metal."""
    points = [attach]
    if target[0] != attach[0] and target[1] != attach[1]:
        points.append((target[0], attach[1]))
    if target != attach:
        points.append(target)
    if len(points) < 2:
        return [(attach[0] - half, attach[1]), (attach[0] + half, attach[1])]
    (px, py), (qx, qy) = points[-2], points[-1]
    points[-1] = (
        qx + half * ((qx > px) - (qx < px)),
        qy + half * ((qy > py) - (qy < py)),
    )
    return points


def _check_bounds(ctx: _Ctx, where: str, target: tuple[float, float]) -> None:
    b = ctx.bounds
    if (
        b["x0"] - _EPS <= target[0] <= b["x1"] + _EPS
        and b["y0"] - _EPS <= target[1] <= b["y1"] + _EPS
    ):
        return
    raise _error(
        f"{where}: requested coordinate ({target[0]:.6g}, {target[1]:.6g})um "
        "falls outside the composed placement bounding box "
        f"({b['x0']:.6g}, {b['y0']:.6g}) - ({b['x1']:.6g}, {b['y1']:.6g})um",
        CODE_OUT_OF_BOUNDS,
        bbox_um=b,
    )


def _conflict(
    where: str, target: tuple[float, float], what: str, **who: Any
) -> Exception:
    return _error(
        f"{where}: the stub to ({target[0]:.6g}, {target[1]:.6g})um {what}",
        CODE_CONFLICT,
        conflicts_with=who,
    )


def _check_block_obstacles(
    ctx: _Ctx,
    where: str,
    entry: dict,
    layer: tuple[int, int],
    attach,
    target,
    region,
    grown,
) -> None:
    import klayout.db as kdb

    x, y = ctx.to_dbu(attach[0]), ctx.to_dbu(attach[1])
    probe = kdb.Region(kdb.Box(x - 1, y - 1, x + 1, y + 1))
    spacing = ctx.min_spacing_for_layer(layer)
    for other_id in ctx.blocks:
        geom = ctx.block_geometry(other_id, layer)
        if geom is None:
            continue
        obstacles = geom["region"]
        if other_id == entry["block"]:
            # The shape under the attach point is the stub's own net.
            obstacles = obstacles - obstacles.interacting(probe)
        if (grown & obstacles).is_empty():
            continue
        hit = (
            "overlaps"
            if not (region & obstacles).is_empty()
            else f"comes within the {spacing:g}um minimum spacing of"
        )
        raise _conflict(
            where,
            target,
            f"{hit} drawn metal of block '{other_id}' on layer {layer[0]}/{layer[1]}",
            block=other_id,
        )


def _check_routes_and_stubs(
    ctx: _Ctx, where: str, entry: dict, layer, target, grown, stub_regions
) -> None:
    pin_set = frozenset({(entry["block"], entry["port"])})
    for other_net, other_pins, other_layer, other_region in ctx.routed_regions:
        if other_layer is not None and other_layer != layer:
            continue
        if pin_set & other_pins or (grown & other_region).is_empty():
            continue
        raise _conflict(
            where,
            target,
            f"conflicts with the already-routed net '{other_net}'",
            net=other_net,
        )
    for other_net, other_region in stub_regions:
        if not (grown & other_region).is_empty():
            raise _conflict(
                where,
                target,
                f"conflicts with the requested-port stub of pin '{other_net}' "
                "(overlap or closer than the minimum same-layer spacing)",
                pin=other_net,
            )


def _plan_one(ctx: _Ctx, index: int, entry: dict, stub_regions: list) -> dict[str, Any]:
    from .gen_compose import _port_has_geometry
    from .gen_compose_routing import _drawn_route_region

    request = entry["request"]
    block_id, port_name = entry["block"], entry["port"]
    where = f"request.pins[{index}] (net '{entry['net']}')"
    port: dict[str, Any] = ctx.blocks[block_id]["ports"].get(port_name) or {}
    if not _port_has_geometry(port):
        raise _error(
            f"{where}: block '{block_id}' port '{port_name}' reports no "
            "{x_um, y_um, layer} geometry, so there is nothing to attach "
            "a requested coordinate to",
            CODE_INVALID,
        )
    offset = ctx.offsets_um[block_id]
    layer = (port["layer"]["layer"], port["layer"]["datatype"])
    _check_layer(ctx, where, request, layer, port_name)
    _check_grid(ctx, where, request, layer)

    attach = (float(port["x_um"]) + offset["x"], float(port["y_um"]) + offset["y"])
    target = (request.get("x_um", attach[0]), request.get("y_um", attach[1]))
    _check_bounds(ctx, where, target)
    width = _stub_width(ctx, where, {**port, "name": port_name}, layer)
    draw = _stub_polyline(attach, target, width / 2.0)

    region = _drawn_route_region(draw, width, ctx.dbu)
    spacing = ctx.min_spacing_for_layer(layer)
    spacing_dbu = ctx.to_dbu(spacing) if spacing else 0
    grown = region.sized(spacing_dbu) if spacing_dbu > 0 else region
    _check_block_obstacles(ctx, where, entry, layer, attach, target, region, grown)
    _check_routes_and_stubs(ctx, where, entry, layer, target, grown, stub_regions)
    stub_regions.append((entry["net"], region))

    layer_dict = {"layer": layer[0], "datatype": layer[1]}
    return {
        "requested": {
            **{k: request[k] for k in ("x_um", "y_um") if k in request},
            "layer": layer_dict,
        },
        "achieved": {"x_um": target[0], "y_um": target[1], "layer": layer_dict},
        "stub": {"points_um": draw, "width_um": width, "layer": layer},
    }


def plan_port_requests(
    promoted_pins: list[dict[str, Any]], **kwargs: Any
) -> dict[int, dict[str, Any]]:
    """Plan every ``pins[]`` entry carrying a ``request``.

    ``kwargs`` are the composed-frame facts the caller already holds
    (``blocks``, ``offsets_um``, ``placed_bboxes_um``, ``dbu``, the
    ``resolve_layer`` / ``grid_for_layer`` / ``min_width_for_layer`` /
    ``min_spacing_for_layer`` / ``block_geometry`` lookups and the
    ``routed_regions`` already accepted).

    Returns ``{pin index: plan}``; a plan holds ``requested`` / ``achieved``
    (``{x_um, y_um, layer}``) and the ``stub`` (``points_um``, ``width_um``,
    ``layer``) to draw. Raises on the first infeasible request.
    """
    ctx = _Ctx(**kwargs)
    stub_regions: list[tuple[str, Any]] = []
    return {
        index: _plan_one(ctx, index, entry, stub_regions)
        for index, entry in enumerate(promoted_pins)
        if entry.get("request") is not None
    }
