"""Pin helpers and plane-changing leg routing for ``klt gen-compose`` (#5426).

Relocated verbatim out of ``gen_compose_routing.py``, which sat exactly at
its per-file size ratchet ceiling with no headroom for routing work. This
cluster is the ``route_bundle``-facing half of plane-bearing waypoint
support (#2930) plus the small pin helpers bundle routing names legs by:
within the module it came from it depended on exactly one name
(``route_two_pin``) and was used by exactly one (``route_bundle``), which
makes it the part of that region that fits under the ratchet's per-file
limit today.

``route_two_pin`` is imported *inside* :func:`_route_plane_segment` rather
than at module scope -- the same deferred-import discipline
``gen_compose_routing`` itself uses against ``gen_compose.py`` -- because
``gen_compose_routing`` imports this module at *its* module scope to give
``route_bundle`` these helpers. A module-scope import back would be a
load-time cycle. ``_pin_ref`` stays reachable as
``klayout_tools.gen_compose_routing._pin_ref`` (and through
``gen_compose``'s re-export of it) via that same re-import.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ._jsonnum import is_number
from .decks import ExtractionDeck


def _pin_ref(pin: dict[str, Any]) -> str:
    """``"block.port"`` -- the compact form router diagnostics name a pin by."""
    return f"{pin['block']}.{pin['port']}"


def _pin_position_um(
    pin: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    offsets_um: dict[str, dict[str, float]],
) -> tuple[float, float] | None:
    """A pin's port position in the *composed* frame, or ``None``.

    ``None`` when the block reported no ``ports[]`` entry for it, or the entry
    carries no usable ``x_um``/``y_um`` -- exactly the case
    :func:`route_two_pin` already reports as "one or both ports report no
    position (empty ports[])". Used only to *order* candidate legs by
    Manhattan distance (:func:`route_bundle`); the routability verdict itself
    always comes from :func:`route_two_pin`.
    """
    block = blocks.get(pin["block"])
    if block is None:
        return None
    port = (block.get("ports") or {}).get(pin["port"])
    if not isinstance(port, dict):
        return None
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if not is_number(x_um) or not is_number(y_um):
        return None
    offset = offsets_um[pin["block"]]
    return (float(x_um) + offset["x"], float(y_um) + offset["y"])


def _leg_conflict_reason(result: dict[str, Any], leg_conflict: Any) -> str | None:
    """Why a :func:`route_two_pin` result cannot stand as a bundle leg, else ``None``.

    The router's own rejection reason when it did not route; otherwise the
    caller-supplied ``leg_conflict`` verdict on the drawn geometry (``None``
    when there is no conflict hook).
    """
    if not result["routed"]:
        return result["reason"]
    if leg_conflict is None:
        return None
    return leg_conflict(
        result["points_um"],
        result.get("via_drops", []),
        result.get("stub_widen", []),
        result.get("route_layer"),
    )


def _changes_plane(leg_spec: dict[str, Any]) -> bool:
    """Whether an explicit leg carries a plane-bearing waypoint (#2930)."""
    return any(plane is not None for plane in leg_spec.get("waypoint_layers") or ())


def _pin_failed_pair(
    pinned_failed_pairs: set[frozenset[tuple[str, str]]],
    pins: list[dict[str, Any]],
    i: int,
    j: int,
) -> None:
    """Block the pin pair of a rejected layer-pinned leg from automatic re-pickup."""
    pinned_failed_pairs.add(
        frozenset(
            (
                (pins[i]["block"], pins[i]["port"]),
                (pins[j]["block"], pins[j]["port"]),
            )
        )
    )


def _segments_field(result: dict[str, Any]) -> dict[str, Any]:
    """Per-plane drawing payloads of a plane-changing leg (#2930), if any."""
    return {"segments": result["segments"]} if result.get("segments") else {}


# Plane-bearing waypoints (#2930): a leg whose `waypoints_um` carries a
# `[x, y, "layer_role"]` triple changes routing plane at that point. It is
# split there into one same-plane segment per plane, each routed by the
# ordinary `route_two_pin()` (so every routability check -- obstacles, rings,
# own-block shorts, the via-drop resolution -- applies to each segment on the
# plane it is drawn on) between two ends that are either a real pin or a
# *junction*: a synthetic one-port, zero-area block placed at the waypoint
# whose port sits on the plane the route continues on. The segment arriving at
# a junction therefore drops to the junction's plane through the very same
# #454/#1567 via ladder it would use to reach a block's pad, and the segment
# leaving it starts on that plane with no drop -- no new via or pad geometry
# exists. The result is one leg whose `segments` carry each plane's drawing
# payload.


class _PlaneChangeContext(NamedTuple):
    """The ``route_bundle()`` state a plane-changing leg is routed against."""

    pins: list[dict[str, Any]]
    blocks: dict[str, dict[str, Any]]
    offsets_um: dict[str, dict[str, float]]
    placed_bboxes_um: dict[str, dict[str, float]]
    route_layer: tuple[int, int] | None
    extraction_deck: ExtractionDeck | None
    block_geometry_for: Any
    geometry_for_layer: Any
    leg_conflict: Any
    grid_um: float


def _split_at_plane_changes(
    waypoints: list[tuple[float, float]],
    waypoint_layers: list[dict[str, Any] | None],
    start_layer: tuple[int, int],
    start_width_um: float,
) -> list[dict[str, Any]]:
    """Split a waypoint path into one same-plane segment per plane (#2930).

    A segment carries its ``layer``, ``width_um`` and the plain ``waypoints``
    that steer it, plus ``start_junction`` / ``end_junction`` (the index of the
    plane-bearing waypoint it begins / ends at) where it does not begin / end
    at a real pin. A waypoint naming the plane already in force changes
    nothing.
    """
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "layer": start_layer,
        "width_um": start_width_um,
        "waypoints": [],
    }
    for k, waypoint in enumerate(waypoints):
        plane = waypoint_layers[k]
        if plane is None or plane["layer"] == current["layer"]:
            current["waypoints"].append(waypoint)
            continue
        current["end_junction"] = k
        segments.append(current)
        current = {
            "layer": plane["layer"],
            "width_um": plane["width_um"],
            "waypoints": [],
            "start_junction": k,
        }
    segments.append(current)
    return segments


def _junction_scope(
    ctx: _PlaneChangeContext,
    waypoints: list[tuple[float, float]],
    waypoint_layers: list[dict[str, Any] | None],
    junction_ids: dict[int, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``blocks`` / offsets / bboxes extended with one junction block per id."""
    scoped_blocks = dict(ctx.blocks)
    scoped_offsets = dict(ctx.offsets_um)
    scoped_bboxes = dict(ctx.placed_bboxes_um)
    for k, junction_id in junction_ids.items():
        x, y = waypoints[k]
        plane = waypoint_layers[k]
        assert plane is not None  # junctions exist only at plane-bearing waypoints
        plane_layer = plane["layer"]
        scoped_blocks[junction_id] = {
            "port_names": {"J"},
            "ports": {
                "J": {
                    "x_um": 0.0,
                    "y_um": 0.0,
                    "direction_deg": 0,
                    "layer": {"layer": plane_layer[0], "datatype": plane_layer[1]},
                    "junction": True,
                }
            },
        }
        scoped_offsets[junction_id] = {"x": x, "y": y}
        scoped_bboxes[junction_id] = {"x0": x, "y0": y, "x1": x, "y1": y}
    return scoped_blocks, scoped_offsets, scoped_bboxes


def _junction_aware_fetch(plane_fetch: Any, junction_blocks: set[str]) -> Any:
    """Wrap a per-block geometry reader so a junction answers for itself.

    A junction owns no drawn geometry: answer for it here instead of asking
    compose()'s per-block reader, which only knows real blocks.
    """
    if plane_fetch is None:
        return None

    def _fetch(block_id: str) -> Any:
        if block_id in junction_blocks:
            return {block_id: None}
        return plane_fetch(block_id)

    return _fetch


def _plane_segment_geometry(
    ctx: _PlaneChangeContext,
    fetch: Any,
    seg_pins: tuple[dict[str, str], ...],
    junction_blocks: set[str],
    i: int,
    j: int,
) -> Any:
    """Own-block geometry for a segment's real-pin ends (last such pin wins)."""
    if fetch is None:
        return None
    geometry = None
    for pin in seg_pins:
        block = pin["block"]
        if block in junction_blocks:
            continue
        if (
            block == ctx.pins[i]["block"] == ctx.pins[j]["block"]
            or ctx.blocks[block].get("source") == "cell"
        ):
            geometry = fetch(block)
    return geometry


def _null_junction_block_ids(result: dict[str, Any], junction_blocks: set[str]) -> None:
    """Drops landing on a junction are not on any block's own geometry: null
    the block id so compose()'s own-block pad check skips them."""
    for key in ("via_drops", "stub_widen"):
        for item in result.get(key, []):
            if item.get("block_id") in junction_blocks:
                item["block_id"] = None


def _route_plane_segment(
    ctx: _PlaneChangeContext,
    scoped: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    seg: dict[str, Any],
    ends: tuple[dict[str, str], dict[str, str]],
    junction_blocks: set[str],
    pin_indices: tuple[int, int],
) -> tuple[dict[str, Any], str | None]:
    """Route one same-plane segment; returns ``(route_two_pin result, reason)``."""
    seg_a, seg_b = ends
    plane_fetch = (
        ctx.block_geometry_for
        if seg["layer"] == ctx.route_layer or ctx.geometry_for_layer is None
        else ctx.geometry_for_layer(seg["layer"])
    )
    fetch = _junction_aware_fetch(plane_fetch, junction_blocks)
    scoped_blocks, scoped_offsets, scoped_bboxes = scoped
    # Deferred (see the module docstring): ``gen_compose_routing`` imports
    # this module at its own module scope, so importing it back at ours
    # would be a load-time cycle.
    from .gen_compose_routing import route_two_pin

    result = route_two_pin(
        seg_a,
        seg_b,
        scoped_blocks,
        scoped_offsets,
        scoped_bboxes,
        seg["width_um"],
        seg["layer"],
        ctx.extraction_deck,
        _plane_segment_geometry(ctx, fetch, ends, junction_blocks, *pin_indices),
        waypoints_um=seg["waypoints"],
        leg_conflict=ctx.leg_conflict,
        block_geometry_for=fetch,
        grid_um=ctx.grid_um,
    )
    _null_junction_block_ids(result, junction_blocks)
    return result, _leg_conflict_reason(result, ctx.leg_conflict)


def _route_plane_change_leg(
    ctx: _PlaneChangeContext,
    i: int,
    j: int,
    leg_spec: dict[str, Any],
    start_layer: tuple[int, int] | None,
    start_width_um: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Route a leg split at its plane-bearing waypoints (#2930).

    Returns ``(result, None)`` with ``result["segments"]`` carrying each
    plane's drawing payload, or ``(None, reason)`` when the pinned leg cannot
    be routed.
    """
    waypoints = list(leg_spec["waypoints_um"])
    waypoint_layers = leg_spec["waypoint_layers"]
    if start_layer is None or ctx.extraction_deck is None:
        return None, (
            "a plane-bearing waypoint needs a resolved routing layer and "
            "PDK extraction deck to build the via ladder between planes"
        )

    segments = _split_at_plane_changes(
        waypoints, waypoint_layers, start_layer, start_width_um
    )
    junction_ids = {
        k: f"__plane_change_{k}__"
        for seg in segments
        for k in (seg.get("start_junction"), seg.get("end_junction"))
        if k is not None
    }
    junction_blocks = set(junction_ids.values())
    scoped = _junction_scope(ctx, waypoints, waypoint_layers, junction_ids)

    drawn: list[dict[str, Any]] = []
    total_um = 0.0
    for seg in segments:
        start_k, end_k = seg.get("start_junction"), seg.get("end_junction")
        seg_a = (
            {"block": junction_ids[start_k], "port": "J"}
            if start_k is not None
            else ctx.pins[i]
        )
        seg_b = (
            {"block": junction_ids[end_k], "port": "J"}
            if end_k is not None
            else ctx.pins[j]
        )
        if not seg["waypoints"]:
            # Always take the caller-steered path (#634) -- the fixed one-jog
            # shape assumes two block ports facing each other, not a free
            # junction. Anchoring on the junction gives a single deterministic
            # elbow (x first, then y) from the pin's stub.
            anchor_k = start_k if start_k is not None else end_k
            assert anchor_k is not None  # a plane-changing leg has a junction
            seg = {**seg, "waypoints": [waypoints[anchor_k]]}
        result, reason = _route_plane_segment(
            ctx, scoped, seg, (seg_a, seg_b), junction_blocks, (i, j)
        )
        if reason is not None:
            return None, _plane_change_failure(reason, start_k, end_k, waypoints)
        total_um += result["route_length_um"]
        drawn.append(
            {
                "points_um": result["points_um"],
                "via_drops": result.get("via_drops", []),
                "stub_widen": result.get("stub_widen", []),
                "route_layer": result.get("route_layer"),
                "width_um": result.get("width_um", seg["width_um"]),
            }
        )

    first = drawn[0]
    return {
        "routed": True,
        "route_length_um": total_um,
        "points_um": [pt for part in drawn for pt in part["points_um"]],
        "via_drops": first["via_drops"],
        "stub_widen": first["stub_widen"],
        "route_layer": first["route_layer"],
        "width_um": first["width_um"],
        "channel_track": 0,
        "segments": drawn,
        "reason": None,
    }, None


def _plane_change_failure(
    reason: str,
    start_k: int | None,
    end_k: int | None,
    waypoints: list[tuple[float, float]],
) -> str:
    """Prefix a segment's rejection reason with the plane change it ended at."""
    at = end_k if end_k is not None else start_k
    if at is None:
        return reason
    x, y = waypoints[at]
    return f"plane change at waypoints_um[{at}] ({x:g}, {y:g}): {reason}"
