"""OBS region assembly and the per-layer routing-resource figure for
``klt lef-abstract`` (issue #5677).

:func:`resolve_obs_regions` builds the per-LEF-layer obstruction regions
exactly as ``lef_abstract._resolve_obs`` always did -- every shape drawn on
a GDS layer that maps to a routing-type LEF layer, unioned per LEF layer,
shifted into macro-local coordinates, clipped to the outline, and with the
declared pin ports subtracted back out. It moved here (behaviour unchanged)
so ``lef_abstract.py`` -- pinned by ``file-size-baseline.json``'s ratchet --
could absorb issue #5677's wiring without growing.

:func:`routing_resource` then reads those same final regions to answer the
question a hierarchical floorplan has to settle before it commits: *how
much routing resource does this abstract leave over the macro, per layer?*
It emits one entry per routing layer of the tech LEF -- including fully
free layers, so an unobstructed layer is never silently absent -- reporting
the obstructed fraction of the macro outline and the count of
preferred-direction tracks whose full-length strip across the macro stays
unobstructed, which makes "will N nets fit across this macro?" answerable
from the abstract itself instead of from a multi-hour top-level route.
"""

from __future__ import annotations

from typing import Any

#: The ``DIRECTION`` values a tech-LEF routing layer may declare (LEF 5.7
#: grammar, uppercase) -- anything else leaves the preferred direction
#: unknown, so no track grid can be enumerated for the layer.
_VALID_DIRECTIONS = ("HORIZONTAL", "VERTICAL")


def resolve_obs_regions(
    *,
    layout: Any,
    top_cell: Any,
    gds_to_lef: dict[tuple[int, int], str],
    routing_layers: dict[str, dict[str, Any]],
    pin_boxes_by_layer: dict[str, list[tuple[int, int, int, int]]],
    origin_x_dbu: int,
    origin_y_dbu: int,
    width_dbu: int,
    height_dbu: int,
    dbu_um: float,
    obs_mode: str = "detailed",
) -> dict[str, Any]:
    """Build the obstruction regions: every shape drawn on a GDS layer that
    maps to a routing-type LEF layer, unioned per LEF layer, shifted into
    macro-local coordinates, clipped to the outline, and with the declared
    pin ports subtracted back out.

    Returns ``{lef_layer_name: merged kdb.Region}`` in **macro-local dbu** --
    the exact OBS geometry ``_write_lef`` turns into shapes, so
    :func:`routing_resource` computes its figures over what the abstract
    actually writes. Layers whose region ends up empty (nothing drawn, or
    everything clipped/subtracted away) are absent: the abstract writes no
    OBS for them.

    ``obs_mode`` (issue #4283) selects the granularity: ``detailed`` keeps
    the merged per-layer geometry, ``coarse`` collapses each layer to one
    bounding rectangle before the pin-port subtraction keeps declared ports
    unblocked.
    """
    import klayout.db as kdb

    regions_by_layer: dict[str, Any] = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        lef_layer = gds_to_lef.get((info.layer, info.datatype))
        if lef_layer is None or lef_layer not in routing_layers:
            continue
        region = kdb.Region(top_cell.begin_shapes_rec(layer_index))
        if region.is_empty():
            continue
        regions_by_layer.setdefault(lef_layer, kdb.Region()).__iadd__(region)

    outline_box_dbu = kdb.Box(
        origin_x_dbu, origin_y_dbu, origin_x_dbu + width_dbu, origin_y_dbu + height_dbu
    )
    local_outline_box = kdb.Box(0, 0, width_dbu, height_dbu)
    translation = kdb.Trans(kdb.Vector(-origin_x_dbu, -origin_y_dbu))

    final_regions: dict[str, Any] = {}
    for lef_layer in sorted(regions_by_layer):
        region = regions_by_layer[lef_layer]
        region.merge()
        region = region & kdb.Region(outline_box_dbu)
        region = region.transformed(translation)
        if obs_mode == "coarse" and not region.is_empty():
            # Issue #4283: one bounding rectangle per layer; the pin
            # subtraction below keeps declared ports unblocked.
            region = kdb.Region(region.bbox())

        pin_boxes = pin_boxes_by_layer.get(lef_layer)
        if pin_boxes:
            pin_region = kdb.Region([kdb.Box(*box) for box in pin_boxes])
            region = region - pin_region
        region.merge()
        region &= kdb.Region(local_outline_box)

        if not region.is_empty():
            final_regions[lef_layer] = region

    return final_regions


def _free_track_count(
    *,
    region: Any | None,
    horizontal: bool,
    offset_um: float,
    pitch_um: float,
    strip_um: float,
    width_dbu: int,
    height_dbu: int,
    dbu_um: float,
) -> int:
    """Count the preferred-direction tracks whose **full-length** strip
    across the macro stays entirely unobstructed.

    A ``HORIZONTAL`` layer routes horizontally, so its tracks are horizontal
    strips stacked along y (pitch/offset on the y axis) and vice versa --
    each strip spans the whole macro extent in the preferred direction, at
    the layer's own ``WIDTH``, centred on the track. A track only counts
    when its strip sits fully inside the macro outline (a strip hanging
    over the edge is not a usable crossing) and ``strip & region`` is empty.
    """
    import klayout.db as kdb

    cross_dbu = height_dbu if horizontal else width_dbu
    span_dbu = width_dbu if horizontal else height_dbu
    half_dbu = round(strip_um / dbu_um / 2)
    pitch_dbu = round(pitch_um / dbu_um)
    centre_dbu = round(offset_um / dbu_um)

    free = 0
    while centre_dbu - half_dbu < cross_dbu:
        lo = centre_dbu - half_dbu
        hi = centre_dbu + half_dbu
        if lo >= 0 and hi <= cross_dbu:
            strip = (
                kdb.Box(0, lo, span_dbu, hi)
                if horizontal
                else kdb.Box(lo, 0, hi, span_dbu)
            )
            if region is None or (region & kdb.Region(strip)).is_empty():
                free += 1
        centre_dbu += pitch_dbu
    return free


def resource_entry(
    *,
    region: Any | None,
    layer: dict[str, Any],
    width_dbu: int,
    height_dbu: int,
    dbu_um: float,
    warnings: list[str],
) -> dict[str, Any]:
    """One ``routing_resource`` entry for tech-LEF routing ``layer``,
    computed from the final OBS ``region`` (``None``: the layer has no
    obstruction geometry at all -- the fully free case).

    A layer whose ``DIRECTION``/``PITCH`` (or ``WIDTH``, which sizes the
    strip) is missing reports ``free_tracks: null`` plus a ``warnings[]``
    entry -- never a guessed track count. ``obstructed_fraction`` needs
    none of those: it is plain obstructed area over the macro outline.
    """
    name = layer["name"]
    direction = layer.get("direction")
    pitch_um: float | None = None
    free_tracks: int | None = None

    if direction not in _VALID_DIRECTIONS:
        warnings.append(
            f"routing_resource: layer '{name}' declares no usable DIRECTION "
            f"(got {direction!r}) -- free_tracks unknown"
        )
    else:
        horizontal = direction == "HORIZONTAL"
        # Tracks repeat along the *non*-preferred axis, so a HORIZONTAL
        # layer's track grid steps in y (pitch/offset on y) and a VERTICAL
        # layer's in x. A missing OFFSET defaults to 0 -- the LEF/DEF
        # convention for an unstated track origin; a missing PITCH or WIDTH
        # stays `null` below, never guessed.
        pitch_um = layer.get("pitch_y_um" if horizontal else "pitch_x_um")
        offset_um = layer.get("offset_y_um" if horizontal else "offset_x_um") or 0.0
        strip_um = layer.get("width_um")
        if not pitch_um or not strip_um:
            missing = "PITCH" if not pitch_um else "WIDTH"
            warnings.append(
                f"routing_resource: layer '{name}' declares no {missing} -- "
                "free_tracks unknown"
            )
            pitch_um = None
        else:
            free_tracks = _free_track_count(
                region=region,
                horizontal=horizontal,
                offset_um=offset_um,
                pitch_um=pitch_um,
                strip_um=strip_um,
                width_dbu=width_dbu,
                height_dbu=height_dbu,
                dbu_um=dbu_um,
            )

    outline_area_dbu = width_dbu * height_dbu
    obstructed_area_dbu = region.area() if region is not None else 0
    fraction = (
        round(obstructed_area_dbu / outline_area_dbu, 6) if outline_area_dbu else 0.0
    )
    return {
        "layer": name,
        "direction": direction,
        "pitch_um": pitch_um,
        "obstructed_fraction": fraction,
        "free_tracks": free_tracks,
    }


def routing_resource(
    *,
    regions_by_layer: dict[str, Any],
    routing_layers: dict[str, dict[str, Any]],
    width_dbu: int,
    height_dbu: int,
    dbu_um: float,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """One entry per routing layer of the tech LEF (layer-name sorted) --
    layers with OBS geometry **and** fully free layers alike, so a free
    layer is never silently absent from the figure."""
    return [
        resource_entry(
            region=regions_by_layer.get(name),
            layer=layer,
            width_dbu=width_dbu,
            height_dbu=height_dbu,
            dbu_um=dbu_um,
            warnings=warnings,
        )
        for name, layer in sorted(routing_layers.items())
    ]
