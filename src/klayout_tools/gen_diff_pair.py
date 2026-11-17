"""``diff_pair`` layout (issue #5557).

Split out of ``gen.py`` (file-size ratchet), which re-exports the moved
name so ``from .gen import _diff_pair_layout`` keeps working -- the
``gen_snap.py`` pattern. ``gen``-side dependencies are imported lazily
inside function bodies so ``gen`` can import this module at top level
without an import cycle. The ``strap_splits`` geometry (in-cell met1/mcon
straps tying each device's splits into one port triple per device) lives
in :mod:`klayout_tools.gen_diff_pair_strap` -- see that module's
docstring for the track/lane plan and its DRC-safety argument.
"""

from __future__ import annotations

from typing import Any

from .gen_diff_pair_strap import (
    _STRAP_GATE_PAD_GROWTH_UM,
    _STRAP_MARGIN_LEFT_UM,
    _STRAP_MARGIN_RIGHT_UM,
    _strap_layout_um,
)


def _diff_pair_layout(
    w_um: float,
    l_um: float,
    splits: int,
    add_guard_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float | None = None,
    row_spacing_um: float | None = None,
    gate_contact: bool = False,
    gate_pad_clearance_um: float = 0.0,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
    top_endcap_um: float = 0.0,
    tap_reach_um: float = 0.0,
    strap_splits: bool = False,
    strap_grid_um: float = 0.0,
) -> dict[str, Any]:
    """Two matched devices (``"A"``/``"B"``), each split into ``splits``
    unit sub-instances, interleaved in a true common-centroid cross-quad
    checkerboard over a 2-row x ``splits``-col grid: ``label(row, col) = "A"
    if (row + col) is even else "B"`` -- for ``splits=2`` this is exactly the
    classic differential-pair "A B / B A" layout; it generalises the same
    way for any ``splits``, each column always holding one A and one B.

    Both rows place their unit device at the same ``x0_um = col *
    col_pitch``, so the two legs **share one x column per terminal** (issue
    #1495) -- separating the legs in x would shift one leg's x centroid and
    destroy exactly the gradient cancellation this layout exists to provide
    (and no uniform-pitch ordering exists for odd ``splits``: partitioning
    ``2 * splits`` positions into equal-x-centroid halves needs the sum
    ``splits * (2 * splits - 1)`` to be even, which fails for every odd
    ``splits``). The caller-facing caveat is documented in
    ``docs/cli/gen.md`` and surfaced in ``klt gen --list``.

    ``ring_padding_um``/``row_spacing_um`` (issue #484) are the ring-to-core
    padding and the device-row-to-device-row gap, defaulted so omitting them
    reproduces prior geometry byte-for-byte; ``col_pitch`` stays fixed to
    ``MIN_SAME_LAYER_SPACING_UM`` (out of scope for #484). Both default to
    ``None`` rather than binding ``gen``'s constants at module scope -- this
    module imports ``gen`` lazily, never at import time.

    ``gate_contact`` (issue #492) and ``gate_pad_clearance_um`` (issue
    #1450) are forwarded to :func:`_mos_unit_layout`; both grow each unit's
    ``bbox_height_um``, so the row pitch and the auto-sized guard ring
    follow without further arithmetic here. ``contact_gate_offset_um``/
    ``bottom_endcap_um``/``sd_implant_margin_um`` (issue #1577) and
    ``tap_reach_um`` (issue #3648) behave as in ``mos_array`` -- see
    :func:`_mos_unit_layout`'s and :func:`_mos_array_layout`'s own
    docstrings.

    ``strap_splits`` (issue #5557) ties each device's sub-instances together
    in-cell on the family's second routing plane, shifts the checkerboard
    right by the trunk margin, and reports its geometry under ``"strap"``;
    see :mod:`klayout_tools.gen_diff_pair_strap` for the geometry and
    :func:`_strap_layout_um` for the box computation. Requires
    ``gate_contact`` (the gate strap lands on the gate pads' local metal)
    and ``w_um >= STRAP_MIN_W_UM`` (the four S/D tracks must fit the pads);
    ``_diff_pair_validate`` enforces both, and
    ``_diff_pair_layer_params`` rejects every family but the verified one
    before any geometry is drawn. ``strap_grid_um`` is the family's
    manufacturing grid for the strap's track/lane centres (0.0 -- no
    snapping -- only when the caller cannot resolve one; the harness and
    the report path always pass the deck-resolved grid).
    """

    from klayout_tools.gen import (
        CONTACT_SIZE_UM,
        ENCLOSURE_MARGIN_UM,
        GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
        GUARD_RING_DEFAULT_PADDING_UM,
        GUARD_RING_DEFAULT_WIDTH_UM,
        MIN_SAME_LAYER_SPACING_UM,
        _mos_unit_layout,
        _optional_tap_pads,
        _ring_layout,
        _snap_offset_um,
    )

    if ring_padding_um is None:
        ring_padding_um = GUARD_RING_DEFAULT_PADDING_UM
    if row_spacing_um is None:
        row_spacing_um = MIN_SAME_LAYER_SPACING_UM
    gate_pad_growth_um = _STRAP_GATE_PAD_GROWTH_UM if strap_splits else 0.0
    unit = _mos_unit_layout(
        w_um,
        l_um,
        1,
        gate_contact,
        gate_pad_clearance_um=gate_pad_clearance_um,
        contact_gate_offset_um=contact_gate_offset_um,
        bottom_endcap_um=bottom_endcap_um,
        sd_implant_margin_um=sd_implant_margin_um,
        top_endcap_um=top_endcap_um,
        gate_pad_growth_um=gate_pad_growth_um,
    )
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["bbox_height_um"] + row_spacing_um

    counts = {"A": 0, "B": 0}
    cells = []
    strap_shift_um = _STRAP_MARGIN_LEFT_UM if strap_splits else 0.0
    for row in range(2):
        for col in range(splits):
            label = "A" if (row + col) % 2 == 0 else "B"
            counts[label] += 1
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "label": label,
                    "n": counts[label],
                    "x0_um": col * col_pitch + strap_shift_um,
                    "y0_um": row * row_pitch,
                }
            )

    core_w = splits * col_pitch - MIN_SAME_LAYER_SPACING_UM
    core_h = 2 * row_pitch - row_spacing_um
    if strap_splits:
        core_w += _STRAP_MARGIN_LEFT_UM + _STRAP_MARGIN_RIGHT_UM

    strap = None
    if strap_splits:
        pad_half_um = (CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM) / 2
        g_cy = unit["g_xy"][1]
        strap = _strap_layout_um(
            unit,
            cells,
            row_pitch,
            g_cy - pad_half_um - gate_pad_growth_um / 2,
            g_cy + pad_half_um + gate_pad_growth_um / 2,
            strap_grid_um,
        )

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            core_w + 2 * padding,
            core_h + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = _snap_offset_um(-(ring_w + padding), -(ring_w + padding))

    tap_pads = _optional_tap_pads(
        add_guard_ring, tap_reach_um, (0.0, 0.0, core_w, core_h), ring_padding_um
    )

    return {
        "unit": unit,
        "cells": cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "core_w_um": core_w,
        "core_h_um": core_h,
        "ring": ring,
        "ring_offset_um": ring_offset,
        "tap_pads": tap_pads,
        "strap": strap,
    }
