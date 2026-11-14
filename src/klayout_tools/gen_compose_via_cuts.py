"""Own-block via-cut clearance check for ``klt gen-compose`` (#5398).

Split out of ``gen_compose.py`` / ``gen_compose_routing.py`` (both under the
per-file size ratchet). ``gen_compose`` imports this module, which imports
``gen_compose`` names only inside functions (as ``gen_compose_port_request``).
"""

from __future__ import annotations

from typing import Any


def via_cut_block_spacing_violation_um(
    drop: dict[str, Any],
    cut_side_um: float,
    geometry: dict[str, Any],
    spacing_um: float,
    insert_cuts: Any,
) -> float | None:
    """Whether one via-drop's drawn cut(s) come within ``spacing_um`` of a
    block's own cuts on the same via layer without touching them (#5398).

    ``geometry`` is ``read_block_layer_geometry`` on the *cut* layer, in the
    composed frame. ``insert_cuts`` is ``gen_compose._insert_via_cuts`` -- called
    with a ``kdb.Region`` so the compared footprint is exactly what
    ``_write_composed_gds`` draws, array included. Cuts the new cuts overlap
    are exempt (they merge), mirroring
    ``_leg_block_spacing_violation_um``; only a near miss is reported.

    Returns the closest offending distance (um), or ``None``.
    """
    import klayout.db as kdb

    dbu = geometry["dbu"]
    spacing_dbu = int(round(spacing_um / dbu))
    if spacing_dbu <= 0:
        return None
    drawn = kdb.Region()
    insert_cuts(
        drawn,
        drop,
        int(round(drop["x_um"] / dbu)),
        int(round(drop["y_um"] / dbu)),
        int(round((cut_side_um / 2.0) / dbu)),
        dbu,
    )
    obstacles = geometry["region"].not_interacting(drawn)
    if obstacles.is_empty():
        return None
    violations = obstacles.separation_check(drawn, spacing_dbu)
    if violations.is_empty():
        return None
    return min(edge_pair.distance() for edge_pair in violations) * dbu


def own_via_cut_clearance_reason(
    via_drops: list[dict[str, Any]],
    pdk_variant: str,
    min_spacing_for_layer: Any,
    own_block_layer_geometry: Any,
) -> str | None:
    """Own-block via-cut clearance check (#5398): every other route-time
    check compares *metal*. A via-drop's cut lands on its own pin, and when
    that pin sits inside a PDK-authored cell with its own cut array (sg13g2's
    `npn13G2` emitter/base via1 arrays) the new cut can land a few nm from --
    without overlapping -- the cell's own cuts: a `via1.space.1` finding per
    neighbouring cut, plus the metal pads that enclose it. Cuts the new cut
    overlaps merge with it and are exempt (same convention as #1904), so only
    a genuine near miss rejects. Returns the rejection reason, or ``None``.
    """
    from .gen_compose import _insert_via_cuts

    for drop in via_drops:
        drop_block = drop.get("block_id")
        cut_layer = drop.get("via_layer")
        if drop_block is None or cut_layer is None:
            continue
        cut_spacing = min_spacing_for_layer(cut_layer)
        if cut_spacing is None:
            continue
        cut_geometry = own_block_layer_geometry(drop_block, cut_layer)
        if cut_geometry is None:
            continue
        cut_spacing_um, cut_rule_id = cut_spacing
        cut_violation_um = via_cut_block_spacing_violation_um(
            drop,
            via_drop_cut_side_um(pdk_variant, cut_layer),
            cut_geometry,
            cut_spacing_um,
            _insert_via_cuts,
        )
        if cut_violation_um is not None:
            return (
                f"draws a via cut on layer {cut_layer} at "
                f"({drop['x_um']:.4g}, {drop['y_um']:.4g}) that comes "
                f"within {cut_violation_um:.4g}um of block "
                f"'{drop_block}''s own via cuts on that layer -- closer "
                f"than the resolved deck's own '{cut_rule_id}' minimum "
                "same-layer spacing rule (no overlap, but a real "
                "`klt drc` violation on the composed layout, issue "
                "#5398: the pin sits inside a cell that already draws "
                "its own via array). Reach the net at a port outside "
                "that array, or route this net on the block's own "
                "metal plane (routing.layer_role) so no via is needed"
            )
    return None


def via_drop_cut_side_um(variant: str, via_pair: tuple[int, int]) -> float:
    """The drawn side (um) of a via-drop cut on ``via_pair``: the generic
    ``_VIA_DROP_SIZE_UM`` floored up to the deck's ``width`` rule (#1501),
    then clamped *down* to the layer's fixed size where the foundry rule is a
    maximum too (sky130's ``via``, 0.15um; sg13g2's ``Via1``-``Via4``,
    0.19um, #2585) -- the generic 0.22um square is over that maximum. The one
    definition shared by the drawing side and the route-time own-block
    cut-clearance check (#5398), so the two cannot disagree."""
    from .gen_compose import (
        _VIA_DROP_SIZE_UM,
        _fixed_cut_side_um,
        _min_width_um_for_layer,
        _via_fixed_size_um,
    )

    floor = _min_width_um_for_layer(variant, via_pair)
    return _fixed_cut_side_um(
        max(_VIA_DROP_SIZE_UM, floor[0] if floor is not None else 0.0),
        _via_fixed_size_um(variant, via_pair),
    )
