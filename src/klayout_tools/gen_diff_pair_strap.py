"""Strap geometry constants + math for ``diff_pair``'s ``strap_splits``
(issue #5557).

The strap ties each matched device's ``splits`` sub-instances together
in-cell on the family's second routing plane (sky130: met1 + mcon) and
reports one ``Q1_S``-style port triple per device, so ``layout-plan
execute``'s k-th-device <-> k-th-candidate binding connects every
sub-instance with no change to itself. Verified on sky130 only -- every
other family is rejected (:data:`DIFF_PAIR_STRAP_FAMILIES`), never drawn
unverified. Split out of ``gen_diff_pair.py`` (file-size ratchet);
``gen``-side dependencies are imported lazily inside function bodies.

Geometry::

    y (one row)   S/D tracks 1-4 (met1 rails centred on the S/D pads)
    = w + stem    gate-pad zone, tracks 5-6 (met1 rails on the grown gate pads)
    inter-row     trunks only
    x margins     left: the two gate-net trunks; right: the four S/D-net trunks

Each row carries six met1 rails at six y tracks and each net's row-0 and
row-1 rails are joined by a vertical met1 trunk in its own x-margin lane.
The one same-layer hazard -- a rail crossing another net's trunk -- is
designed out two ways:

- **Track assignment mirrors between the rows within each zone** (row 0,
  bottom to top: ``Q1_S Q1_D Q2_S Q2_D | Q1_G Q2_G``; row 1:
  ``Q2_D Q2_S Q1_D Q1_S | Q2_G Q1_G``). A trunk spans from its row-0 rail
  up to its row-1 rail; with the mirrored assignment every *other* net's
  rail in a row sits either below that trunk's bottom edge or above its
  top edge, never inside its span.
- **Lanes are zoned**: S/D trunks share the right margin, gate trunks the
  left, ordered so a net with a higher row-0 track owns a lane nearer the
  pads (its trunk then only ever crosses rails that stop short of its
  lane). An S/D net and a gate net can never share a margin: the S/D band
  sits below the gate band in *both* rows, so each would need the other's
  lane both nearer and farther.

met1 crosses foreign li1 pads, the guard-ring band and tap pads without
connecting (only an mcon joins li1 to met1, dropped only on the strap's own
pads), so the margins need no clearance but their fellow trunks'. The gate
landing pad grows upward (:data:`_STRAP_GATE_PAD_GROWTH_UM`) to host the
gate tracks' mcons; the grown ``bbox_height_um``/core then grow the row
pitch, guard ring, tap pads, well box and ``bbox_um`` through the existing
arithmetic.
"""

from __future__ import annotations

from typing import Any

#: Families whose strapped geometry is *verified* -- DRC-clean under ``klt
#: drc --deck sky130`` and extract-verified, mirroring the "verified, not
#: assumed by analogy" bar ``gen_layer_params``'s
#: ``_MOS_ARRAY_WELL_TAP_FAMILIES`` and ``_GENERATOR_FAMILY_DEFERRED`` hold
#: themselves to. ``strap_splits`` on any other family is an application
#: error from :func:`_diff_pair_layer_params` (before any geometry exists) --
#: never unverified geometry. A family is added here only after its own
#: curated-deck DRC + extract verification has actually been run.
DIFF_PAIR_STRAP_FAMILIES: frozenset[str] = frozenset({"sky130"})

#: sky130's mcon (li1<->met1 via) is a fixed-size 0.17um cut -- the same
#: value ``gen_layer_params._PDK_CUT_FIXED_SIZE_UM`` records for (67, 44)
#: under ``"sky130"`` (deck rule ``ct.1``); restated here as a strap-local
#: constant because the strap draws mcons directly rather than through the
#: contact-clamp path.
_STRAP_MCON_SIZE_UM = 0.17

#: met1 enclosure of mcon, sky130 curated deck ``met1.enclosing.mcon.1``
#: (official ``m1.4``, 0.03um). Applied on every side of every strap mcon.
_STRAP_MET1_MCON_ENCLOSURE_UM = 0.03

#: li1 enclosure of mcon assumed for the pad-side half of the same stack.
#: The curated deck checks met1's enclosure of mcon but no li1-side rule;
#: holding the strap to the same 0.03um keeps the drawn stack inside the
#: official li1-encloses-mcon budget rather than relying on that absence.
_STRAP_LI1_MCON_ENCLOSURE_UM = 0.03

#: met1 rail height: one fixed-size mcon plus the met1 enclosure on both
#: sides. Also the trunk width, so a rail and its trunk join edge-to-edge.
_STRAP_RAIL_H_UM = _STRAP_MCON_SIZE_UM + 2 * _STRAP_MET1_MCON_ENCLOSURE_UM

#: Track pitch: rail height plus the same-layer clearance between
#: neighbouring tracks (0.4um, the module-wide minimum
#: ``MIN_SAME_LAYER_SPACING_UM``, rather than met1's own 0.14um deck
#: minimum -- the same "one clearance story" precedent
#: ``_mos_unit_strapped_layout`` set).
_STRAP_TRACK_PITCH_UM = _STRAP_RAIL_H_UM + 0.4

#: Lane pitch between neighbouring trunks in a margin.
_STRAP_LANE_PITCH_UM = _STRAP_TRACK_PITCH_UM

#: How far an mcon centre must sit inside its pad's metal: half the cut
#: plus the assumed li1 enclosure. This is the per-edge inset the track
#: centring reserves inside each pad's metal height.
_STRAP_EDGE_INSET_UM = _STRAP_MCON_SIZE_UM / 2 + _STRAP_LI1_MCON_ENCLOSURE_UM

#: Height of the four-track S/D rail block (three pitches plus one rail).
#: The S/D pads are exactly ``w_um`` tall, so centring this block in the pad
#: is what sets the strap's minimum device width.
_STRAP_SD_BLOCK_UM = 3 * _STRAP_TRACK_PITCH_UM + _STRAP_RAIL_H_UM

#: Height of the two-track gate rail block (one pitch plus one rail).
_STRAP_GATE_BLOCK_UM = _STRAP_TRACK_PITCH_UM + _STRAP_RAIL_H_UM

#: Minimum ``w_um`` for ``strap_splits``: the S/D rail block plus the mcon
#: inset on both pad edges. Below it the four tracks cannot fit inside the
#: S/D pads and the request is rejected (never silently under-strapped).
STRAP_MIN_W_UM = _STRAP_SD_BLOCK_UM + 2 * _STRAP_EDGE_INSET_UM

#: Upward growth of the gate landing pad's metal in strap mode: the gate
#: pad's li1 is ``CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM`` = 0.42um tall
#: by default, whose mcon window (height minus two insets = 0.19um) is too
#: short for the 0.86um two-track block; this growth widens the window to
#: exactly the block. Folded into ``_mos_unit_layout``'s new
#: ``gate_pad_growth_um``, so the row pitch, well box, guard ring and
#: ``bbox_um`` all follow it.
_STRAP_GATE_PAD_GROWTH_UM = _STRAP_GATE_BLOCK_UM - (0.42 - 2 * _STRAP_EDGE_INSET_UM)

#: Lane distance (um) from the pad-zone right edge to the nearest S/D trunk
#: centre: the trunk's inner half-width plus the module same-layer clearance
#: from the pad zone it abuts. Further lanes step outward by
#: :data:`_STRAP_LANE_PITCH_UM`.
_STRAP_SD_FIRST_LANE_UM = 0.4 + _STRAP_EDGE_INSET_UM

#: Lane distances from the pad-zone edge outward, right margin: the four
#: S/D nets in lane order (a net with a higher row-0 track owns a lane
#: nearer the pads): Q2_D, Q2_S, Q1_D, Q1_S.
_STRAP_SD_LANE_DISTANCES_UM = tuple(
    _STRAP_SD_FIRST_LANE_UM + index * _STRAP_LANE_PITCH_UM for index in range(4)
)

#: How far left of the pad-zone edge an S/D rail reaches: the first
#: column's S pad centre sits ``CONTACT_SIZE_UM/2 + ENCLOSURE_MARGIN_UM``
#: (= 0.21um) inside the zone and each rail overhangs its leftmost mcon by
#: :data:`_STRAP_EDGE_INSET_UM`, so the reach is ``0.21 - 0.115 = 0.095um``
#: past the zone edge.
_STRAP_SD_RAIL_OVERHANG_UM = round(0.21 - _STRAP_EDGE_INSET_UM, 6)

#: Lane distance for the nearest (Q2_G) gate trunk on the left margin. The
#: gate trunks' y-spans cross every S/D rail row, so the nearest trunk must
#: hold the module same-layer clearance (0.4um) from that reach:
#: ``d >= trunk_half + clearance - overhang``.
_STRAP_GATE_FIRST_LANE_UM = round(
    _STRAP_EDGE_INSET_UM + 0.4 - _STRAP_SD_RAIL_OVERHANG_UM, 6
)

#: Lane distances for the left (gate) margin: Q2_G nearest, Q1_G outer.
_STRAP_GATE_LANE_DISTANCES_UM = tuple(
    _STRAP_GATE_FIRST_LANE_UM + index * _STRAP_LANE_PITCH_UM for index in range(2)
)

#: Right-margin width added to the core for the four S/D trunks.
_STRAP_MARGIN_RIGHT_UM = round(
    _STRAP_SD_LANE_DISTANCES_UM[-1] + _STRAP_EDGE_INSET_UM, 6
)

#: Left-margin width added to the core for the two gate trunks.
_STRAP_MARGIN_LEFT_UM = round(
    _STRAP_GATE_LANE_DISTANCES_UM[-1] + _STRAP_EDGE_INSET_UM, 6
)

#: Strap nets in device-major order -- the order the strapped report's
#: ``ports[]`` is emitted in (device 1 first, S/D/G within a device), and
#: the index space for the row-0 track assignment (net ``n`` rides row 0's
#: track ``n``, bottom to top).
_STRAP_NET_ORDER = (
    (1, "S"),
    (1, "D"),
    (1, "G"),
    (2, "S"),
    (2, "D"),
    (2, "G"),
)

#: Row-0 track (bottom to top, 1-based) per strap net: the four S/D nets on
#: the S/D-pad tracks in the lane-ordering interleave (Q1_S, Q1_D, Q2_S,
#: Q2_D), then the two gate nets on the gate-pad tracks. Gate tracks must
#: stay in the gate zone -- a gate mcon outside the gate pads' metal lands
#: on no li1 and straps nothing.
_STRAP_ROW0_TRACKS = {
    (1, "S"): 1,
    (1, "D"): 2,
    (2, "S"): 3,
    (2, "D"): 4,
    (1, "G"): 5,
    (2, "G"): 6,
}

#: Row-1 track per strap net: the row-0 assignment *mirrored within each
#: zone* -- S/D nets map ``n -> 5 - n`` across tracks 1-4, gate nets swap
#: tracks 5 and 6. This mirroring is what keeps every foreign rail outside
#: every trunk's y-span (see the module docstring).
_STRAP_ROW1_TRACKS = {
    (1, "S"): 4,
    (1, "D"): 3,
    (2, "S"): 2,
    (2, "D"): 1,
    (1, "G"): 6,
    (2, "G"): 5,
}

#: Which margin each net's trunk rides: S/D nets right, gate nets left.
_STRAP_NET_LANE_SIDE = {
    (1, "S"): "right",
    (1, "D"): "right",
    (2, "S"): "right",
    (2, "D"): "right",
    (1, "G"): "left",
    (2, "G"): "left",
}

#: Lane distance per strap net (the lane-order rules above, resolved).
_STRAP_LANE_DISTANCE_BY_NET = {
    (2, "D"): _STRAP_SD_LANE_DISTANCES_UM[0],
    (2, "S"): _STRAP_SD_LANE_DISTANCES_UM[1],
    (1, "D"): _STRAP_SD_LANE_DISTANCES_UM[2],
    (1, "S"): _STRAP_SD_LANE_DISTANCES_UM[3],
    (2, "G"): _STRAP_GATE_LANE_DISTANCES_UM[0],
    (1, "G"): _STRAP_GATE_LANE_DISTANCES_UM[1],
}

#: Row -> strap net -> 0-based physical-track index in the shared track
#: list (four S/D tracks then two gate tracks, bottom to top). Row 0 rides
#: its base track; row 1 rides the mirrored one.
_STRAP_ROW_TRACK_INDEX = {
    0: {net: track - 1 for net, track in _STRAP_ROW0_TRACKS.items()},
    1: {net: track - 1 for net, track in _STRAP_ROW1_TRACKS.items()},
}


def _strap_track_centers_um(
    window_lo_um: float,
    window_hi_um: float,
    track_count: int,
    grid_um: float,
) -> list[float]:
    """Track centres for ``track_count`` rails centred in a pad's metal
    window ``[window_lo_um, window_hi_um]`` -- the mcon-bearing extent of
    the pads the rails land on.

    The whole rail block (``(count - 1)`` pitches plus one rail height) is
    centred in the window, then each centre is snapped to ``grid_um`` (the
    family's manufacturing grid -- mcon and met1 are grid-checked by the
    sky130 OFFGRID rules) so every drawn strap edge lands on-grid even when
    the caller's ``w_um`` is an odd multiple of the grid. The inset
    (:data:`_STRAP_EDGE_INSET_UM`) already keeps ``centre -/+ mcon_half``
    li1-enclosed for any window at least :data:`STRAP_MIN_W_UM` tall;
    centring only adds slack.
    """
    block_h = (track_count - 1) * _STRAP_TRACK_PITCH_UM + _STRAP_RAIL_H_UM
    first = window_lo_um + (window_hi_um - window_lo_um - block_h) / 2
    first += _STRAP_EDGE_INSET_UM
    centers = [first + index * _STRAP_TRACK_PITCH_UM for index in range(track_count)]
    if grid_um > 0:
        centers = [round(c / grid_um) * grid_um for c in centers]
    return centers


def _strap_layout_um(
    unit: dict[str, Any],
    cells: list[dict[str, Any]],
    row_pitch_um: float,
    pad_metal_lo_um: float,
    pad_metal_hi_um: float,
    grid_um: float,
) -> dict[str, Any]:
    """Compute the strap's met1/mcon geometry in the *grown* core frame.

    ``unit``/``cells`` are ``_diff_pair_layout``'s own structures (pad
    centres ``s_xy``/``d_xy``/``g_xy`` per unit; ``cells[]`` already shifted
    right by the left margin). ``pad_metal_lo/hi_um`` bound the gate pads'
    metal (the grown li1 pad) in a row's local y; ``row_pitch_um`` places
    row 1's tracks at absolute y. Returns::

        {
          "metal_boxes_um": [(x0, y0, x1, y1), ...],  # met1 rails + trunks
          "via_boxes_um":   [(x0, y0, x1, y1), ...],  # fixed-size mcons
          "ports": [{"device": 1, "terminal": "S", "x_um": ..., "y_um": ...},
                    ...]  # device-major order, one entry per device terminal
        }

    Row assignment: cells carry ``row``/``label`` (``"A"`` = device 1,
    ``"B"`` = device 2). A net's rails exist in exactly the rows where the
    net has pads; the trunk (and the row-0 port anchor) exists only when
    both rows do, so ``splits=1`` -- one sub-instance per device, in
    opposite rows -- still reports one port per device terminal, anchored
    on the single rail the device actually has.
    """
    row_tracks = {
        row: _strap_track_centers_um(0.0, unit["height_um"], 4, grid_um)
        + _strap_track_centers_um(pad_metal_lo_um, pad_metal_hi_um, 2, grid_um)
        for row in (0, 1)
    }
    pad_zone_left = min(c["x0_um"] for c in cells)
    pad_zone_right = max(c["x0_um"] for c in cells) + unit["total_len_um"]

    terminal_xy = {"S": unit["s_xy"], "D": unit["d_xy"], "G": unit["g_xy"]}
    mcon_half = _STRAP_MCON_SIZE_UM / 2
    rail_half = _STRAP_RAIL_H_UM / 2

    metal_boxes: list[tuple[float, float, float, float]] = []
    via_boxes: list[tuple[float, float, float, float]] = []
    ports: list[dict[str, Any]] = []
    for net in _STRAP_NET_ORDER:
        device, terminal = net
        label = "A" if device == 1 else "B"
        px, _py = terminal_xy[terminal]
        side = _STRAP_NET_LANE_SIDE[net]
        # Right-margin lanes step outward from the pad zone's right edge,
        # left-margin lanes from its left edge.
        lane_anchor = pad_zone_right if side == "right" else pad_zone_left
        lane_x = (
            lane_anchor
            + (1.0 if side == "right" else -1.0) * (_STRAP_LANE_DISTANCE_BY_NET[net])
        )
        if grid_um > 0:
            lane_x = round(lane_x / grid_um) * grid_um
        net_cells = [c for c in cells if c["label"] == label]
        net_rows = sorted({c["row"] for c in net_cells})
        track_y_by_row: dict[int, float] = {}
        for row in net_rows:
            row_cells = [c for c in net_cells if c["row"] == row]
            row_y0 = row * row_pitch_um
            track_y = row_y0 + row_tracks[row][_STRAP_ROW_TRACK_INDEX[row][net]]
            track_y_by_row[row] = track_y
            pad_xs = [c["x0_um"] + px for c in row_cells]
            if side == "right":
                metal_boxes.append(
                    (
                        min(pad_xs) - rail_half,
                        track_y - rail_half,
                        lane_x + rail_half,
                        track_y + rail_half,
                    )
                )
            else:
                metal_boxes.append(
                    (
                        lane_x - rail_half,
                        track_y - rail_half,
                        max(pad_xs) + rail_half,
                        track_y + rail_half,
                    )
                )
            for pad_x in pad_xs:
                via_boxes.append(
                    (
                        pad_x - mcon_half,
                        track_y - mcon_half,
                        pad_x + mcon_half,
                        track_y + mcon_half,
                    )
                )
        if len(track_y_by_row) == 2:
            lo, hi = sorted(track_y_by_row.values())
            metal_boxes.append(
                (lane_x - rail_half, lo - rail_half, lane_x + rail_half, hi + rail_half)
            )
            port_y = track_y_by_row[0]
        else:
            port_y = track_y_by_row[net_rows[0]]
        ports.append(
            {"device": device, "terminal": terminal, "x_um": lane_x, "y_um": port_y}
        )
    return {
        "metal_boxes_um": metal_boxes,
        "via_boxes_um": via_boxes,
        "ports": ports,
    }
