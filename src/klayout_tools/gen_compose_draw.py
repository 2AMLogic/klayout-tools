"""Routed-entry drawing for ``klt gen-compose`` and ``klt route``.

``_draw_routed_entry`` -- one ``routed_geometry`` entry drawn into the
composed top cell as a native ``kdb.Path`` backbone plus its via-drop
cut(s), landing pads and stub-widen boxes -- moved here verbatim from
``gen_compose.py`` (issue #6058 needed the per-hop landing-role branch in
its pad loop, and ``gen_compose.py`` sits at its file-size ratchet
ceiling with no headroom). ``gen_compose.py`` re-exports both names at
module scope, so every pre-existing
``from klayout_tools.gen_compose import _draw_routed_entry`` path --
``klt route``'s own writer included -- keeps resolving unchanged, the
same convention the earlier splits of this family established.

Same conventions as ``gen_compose.py`` itself:
``gen_compose.py``'s module-scope names (``_VIA_DROP_SIZE_UM``,
``_VIA_LANDING_SIZE_UM``) are imported *inside* the functions that use
them, so this module never has a load-time dependency back on it.
"""

from __future__ import annotations

import math
from typing import Any

from .gen_compose_routing import _polyline_midpoint_um as _polyline_midpoint_um


def _insert_via_cuts(
    shapes: Any, drop: dict[str, Any], cx: int, cy: int, half: int, dbu: float
) -> None:
    """Draw a via-drop's cut(s) centred on ``(cx, cy)`` (dbu): one square, or
    -- when ``drop["array"]`` is set by a label-derived port's
    ``access.via_array`` (#2927) -- the rows x cols array at its deck pitch."""
    import klayout.db as kdb

    array = drop.get("array")
    if not array:
        shapes.insert(kdb.Box(cx - half, cy - half, cx + half, cy + half))
        return
    pitch = int(round(array["pitch_um"] / dbu))

    def axis(count: int) -> list[int]:
        return [int(round((i - (count - 1) / 2.0) * pitch)) for i in range(count)]

    for dy in axis(array["rows"]):
        for dx in axis(array["cols"]):
            x, y = cx + dx, cy + dy
            shapes.insert(kdb.Box(x - half, y - half, x + half, y + half))


def _draw_routed_entry(
    layout: Any,
    top: Any,
    route: dict[str, Any],
    dbu: float,
    route_layer: tuple[int, int] | None = None,
    label_layer: tuple[int, int] | None = None,
    via_drop_size_um: dict[tuple[int, int], float] | None = None,
    landing_pad_size_um: dict[Any, float] | None = None,
    intermediate_landing_pad_size_um: dict[Any, float] | None = None,
) -> None:
    """Draw one ``routed_geometry`` entry into ``top`` (the per-route half of
    :func:`_write_composed_gds`, shared with ``klt route``).

    ``intermediate_landing_pad_size_um`` (issue #6058) carries
    ``routing.minimal_ladder``'s per-pair intermediate sides; a drop whose
    own ``terminal_landing_layers`` marking classifies one of its
    ``landing_layers`` as intermediate draws that pad at the intermediate
    side. ``None``/unmarked (the default, ``klt route``, and every
    pre-#6058 caller) keeps today's terminal sizing, unchanged.
    """
    import klayout.db as kdb

    from .gen_compose import _VIA_DROP_SIZE_UM, _VIA_LANDING_SIZE_UM
    from .gen_compose_routing import _drop_array_extra_um

    points = route["points_um"]
    if not points or len(points) < 2:
        return
    entry_route_layer = route.get("route_layer", route_layer) or route_layer
    assert entry_route_layer is not None  # route_layer or the entry's own
    entry_label_layer = route.get("label_layer", label_layer)
    layer_index = layout.layer(entry_route_layer[0], entry_route_layer[1])
    path_points = [
        kdb.Point(int(round(x / dbu)), int(round(y / dbu))) for (x, y) in points
    ]
    width_dbu = int(round(route["width_um"] / dbu))
    top.shapes(layer_index).insert(kdb.Path(path_points, width_dbu))

    # `label` is False for every leg of a multi-leg (bundle) net after
    # its first (#1073): the legs are one conductor, so one kdb.Text
    # names it -- exactly as a two-pin net's single path gets exactly
    # one label. Absent/True keeps the pre-#1073 one-label-per-entry
    # behaviour for any other caller.
    if entry_label_layer is not None and route.get("label", True):
        lx_um, ly_um = _polyline_midpoint_um(points)
        label_point = kdb.Point(int(round(lx_um / dbu)), int(round(ly_um / dbu)))
        top.shapes(layout.layer(entry_label_layer[0], entry_label_layer[1])).insert(
            # Trans(Vector) -- the typed overload of the same translation
            # kdb.Trans(label_point) performs at runtime.
            kdb.Text(route["net"], kdb.Trans(kdb.Vector(label_point.x, label_point.y)))
        )

    # Via-drops (#454; generalized to a multi-hop ladder by issue
    # #1567): each entry drops one via hop at exactly the target
    # pin's own position -- a via square on `via_layer`, plus a
    # landing-pad square on *both* of that hop's own
    # `landing_layers` (_VIA_LANDING_SIZE_UM, independent of the
    # route's own trace width) so the via's enclosure requirement
    # holds regardless of how thin routing.width_um is. A single-hop
    # drop's two `landing_layers` are exactly (this leg's own
    # effective layer, the pin's own layer) -- the pre-#1567 shape --
    # and the backbone's own Path already terminates exactly at this
    # same point (manhattan_backbone's endpoints are the raw pin
    # positions), so the landing pad always overlaps -- and merges
    # with -- the trace. A multi-hop ladder instead carries one
    # `via_drops` entry per hop, all at the identical (x, y): drawing
    # each one exactly the same way as the single-hop case builds
    # the full stack -- the backbone's own landing pad from the first
    # hop, the pin's own landing pad from the last hop, and one
    # shared landing pad per intermediate level in between (drawn
    # twice, once by each of its two neighbouring hops, at the same
    # position with each hop's required side; the squares merge).
    #
    # The via square's own side (#1501) is looked up per `via_layer`
    # in `via_drop_size_um` -- resolved by `compose()` against the
    # same curated deck `klt drc` judges the drawn square with --
    # falling back to the PDK-independent `_VIA_DROP_SIZE_UM`
    # constant for any layer the caller didn't resolve a floor for
    # (an unresolvable PDK family, or a pre-#1501 caller that never
    # populates `via_drop_size_um` at all).
    #
    # Each landing pad's own side (#2072/#2074) is looked up per
    # (landing layer, via layer) in `landing_pad_size_um`, resolved
    # against the area floor and this hop's via-enclosure rule. Fall back
    # to the fixed `_VIA_LANDING_SIZE_UM` constant unchanged for any
    # layer the caller didn't resolve a floor for. A hop's two
    # `landing_layers` can therefore draw two *differently*-sized
    # squares at the identical (x, y) -- e.g. a met2 pad at the
    # pre-#2072 0.42um fixed size alongside a met3 pad floored up to
    # clear `met3.area.1` -- since each layer's own area minimum is
    # independent of its neighbour's.
    for drop in route.get("via_drops", []):
        via_pair = drop["via_layer"]
        via_layer_index = layout.layer(via_pair[0], via_pair[1])
        cx = int(round(drop["x_um"] / dbu))
        cy = int(round(drop["y_um"] / dbu))
        via_size_um = (via_drop_size_um or {}).get(via_pair, _VIA_DROP_SIZE_UM)
        via_half_dbu = int(round((via_size_um / 2.0) / dbu))
        # #2927: a label-derived port's `access.via_array` draws a
        # rows x cols cut array centred on the pin; pads below grow
        # to enclose it.
        _insert_via_cuts(top.shapes(via_layer_index), drop, cx, cy, via_half_dbu, dbu)
        array_extra_um = _drop_array_extra_um(drop)
        # Issue #6058: which of this hop's two landing layers are terminal
        # planes for the endpoint this drop serves -- recorded by
        # ``_resolve_via_drops`` when it still knew the endpoint's port
        # layer and the leg's effective route layer. A layer outside that
        # marking is an intermediate plane of the ladder and, in a
        # ``routing.minimal_ladder`` request, draws at its own per-pair
        # enclosure/area floor instead of the contact-derived baseline
        # (falling back to the terminal side for any pair the intermediate
        # dict does not carry -- the conservative, never-smaller answer).
        terminal_layers = drop.get("terminal_landing_layers")
        for landing_pair in drop.get("landing_layers", ()):
            if (
                terminal_layers is not None
                and landing_pair not in terminal_layers
                and intermediate_landing_pad_size_um is not None
                and (landing_pair, via_pair) in intermediate_landing_pad_size_um
            ):
                landing_size_um = intermediate_landing_pad_size_um[
                    (landing_pair, via_pair)
                ]
            else:
                landing_size_um = (landing_pad_size_um or {}).get(
                    (landing_pair, via_pair), _VIA_LANDING_SIZE_UM
                )
            landing_size_um += array_extra_um
            # Round minimum sizes outward, preserving the margin
            # against the *rounded* via too. The epsilon only removes
            # floating-point noise at an integer grid coordinate.
            landing_half_dbu = math.ceil(
                max(
                    landing_size_um / (2.0 * dbu),
                    via_half_dbu + (landing_size_um - via_size_um) / (2.0 * dbu),
                )
                - 1e-9
            )
            landing_box = kdb.Box(
                cx - landing_half_dbu,
                cy - landing_half_dbu,
                cx + landing_half_dbu,
                cy + landing_half_dbu,
            )
            top.shapes(layout.layer(landing_pair[0], landing_pair[1])).insert(
                landing_box
            )

    # Stub-widen (#496): each entry (route_two_pin's own
    # _endpoint_stub_widen_um) re-draws the backbone's own first
    # segment out of one endpoint -- from that pin's exact position,
    # `length_um` along its own `direction_deg`, exactly the same
    # span the narrow backbone above already covers -- at the pin's
    # own reported `width_um` instead of the route's. Drawn on this
    # entry's own effective layer, the same layer/cell the
    # backbone's own Path is already on, so it merges into one shape
    # with both the backbone and (when the pin's own pad is also on
    # that layer, which is the only case this fires for -- see the
    # docstring) the block's own pad underneath.
    for widen in route.get("stub_widen", []):
        cx = int(round(widen["x_um"] / dbu))
        cy = int(round(widen["y_um"] / dbu))
        half_dbu = int(round((widen["width_um"] / 2.0) / dbu))
        length_dbu = int(round(widen["length_um"] / dbu))
        if widen["direction_deg"] == 90:
            widen_box = kdb.Box(cx - half_dbu, cy, cx + half_dbu, cy + length_dbu)
        else:  # 270
            widen_box = kdb.Box(cx - half_dbu, cy - length_dbu, cx + half_dbu, cy)
        top.shapes(layer_index).insert(widen_box)
