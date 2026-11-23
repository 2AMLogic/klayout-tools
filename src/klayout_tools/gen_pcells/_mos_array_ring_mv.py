"""Issue #6503's medium-voltage ring geometry for ``mos_array``'s PCell.

Split into its own module (the ``_mos_array_shapes`` precedent) because the
repo's monotonic file-size ratchet requires additive work to land in new
files. For gf180mcu medium-voltage requests (:func:`mv_ring_resolved`) both
helpers size a shape off the tap ring's **final** Comp edge -- what
:func:`klayout_tools.gen._insert_ring` actually draws after snapping the
ring's outer box outward onto the manufacturing grid (issue #3364) -- never
off the nominal box, which can sit a grid step inside the drawn edge and
silently spend the enclosure margin (and draw the well itself off-grid,
tripping ``nwell_OFFGRID``). Growth is quantized on the dbu grid via
:func:`klayout_tools.gen._grow_box_um`, so the drawn margin is exact
whatever the caller's own grid alignment. Any other request keeps the
original nominal ring-well box, so its geometry is untouched.
"""

from __future__ import annotations

from typing import Any

from ..gen_base import WELL_ENCLOSURE_MARGIN_UM


def declare_ring_mv_params(pcell: Any) -> None:
    """Declare the device well enclosure and MV marker margin over the ring."""
    pcell.param(
        "device_well_enclosure_um",
        pcell.TypeDouble,
        "NWell enclosure of device active (µm)",
        default=WELL_ENCLOSURE_MARGIN_UM,
        hidden=True,
    )
    pcell.param(
        "voltage_flavor_ring_margin_um",
        pcell.TypeDouble,
        "Harness-resolved margin the voltage_flavor marker box "
        "additionally grows past the guard ring's final tap edge "
        "when both are drawn (0.0 keeps the marker anchored to the "
        "array alone; see _PDK_VOLTAGE_FLAVOR_RING_MARGIN_UM)",
        default=0.0,
        hidden=True,
    )


def mv_ring_resolved(pcell: Any) -> bool:
    """Whether this request resolves gf180mcu's medium-voltage ring geometry
    (issue #6503): the ``voltage_flavor`` marker is drawn *and* the family
    resolved a marker-past-ring margin. This is exactly the case whose
    ``ring_well_margin_um`` was raised to DF.4d_MV's 0.16um (see
    :func:`klayout_tools.gen_layer_mv_ring.apply_mos_array_mv_ring_margins`),
    and the only one that needs the ring well sized off the final snapped
    edge."""
    return bool(pcell.voltage_flavor_mark_present) and (
        pcell.voltage_flavor_ring_margin_um > 0.0
    )


def ring_well_box_um(
    pcell: Any,
    ring: dict[str, Any],
    ox_um: float,
    oy_um: float,
    dbu: float,
) -> tuple[float, float, float, float]:
    """The ring's well tie box.

    For gf180mcu medium-voltage (:func:`mv_ring_resolved`), the ring's final
    (grid-snapped) outer box grown by the PCell's ``ring_well_margin_um`` on
    all four sides (issue #6503) -- the same snap-then-grow discipline
    :func:`klayout_tools.gen._insert_implant_ring` applies to its own
    extension past the ring.

    Every other request keeps the original *nominal* box -- the unsnapped
    ring outer box grown by the margin -- byte-for-byte. At an off-grid
    ``ring_padding_um`` the outward snap would otherwise move that well one
    grid step (and, through the #6025 union, a sky130 ``hvi`` marker with
    it), breaking #6503's preservation criterion for every family and
    voltage flavour that never asked for DF.4d_MV.
    """
    from klayout_tools.gen import _grow_box_um, _shift_box, _snap_ring_box_um

    if not mv_ring_resolved(pcell):
        margin = pcell.ring_well_margin_um
        return _shift_box(
            (
                -margin,
                -margin,
                ring["outer_w_um"] + margin,
                ring["outer_h_um"] + margin,
            ),
            ox_um,
            oy_um,
        )
    return _grow_box_um(
        _snap_ring_box_um(
            _shift_box(ring["outer_box_um"], ox_um, oy_um),
            pcell.mfg_grid_um,
            dbu,
        ),
        pcell.ring_well_margin_um,
        dbu,
    )


def mv_cover_box(
    pcell: Any, info: dict[str, Any], dbu: float
) -> tuple[float, float, float, float] | None:
    """The box the voltage-flavor marker must additionally cover to enclose
    the guard ring's final (grid-snapped) tap edge by the PCell's
    ``voltage_flavor_ring_margin_um`` (issue #6503) -- ``None`` when there
    is no ring or the family resolves no margin, so the marker stays
    core-anchored (unioned only with the ring well per #6025).

    A marker sized off the array core alone ends up *inside* the ring band
    with less than DV.6's 0.24um of enclosure at the default padding,
    partially overlaps the band as ``ring_padding_um`` grows (DV.7), and
    once the band escapes it sits inside DV.3's 0.24um marker-to-COMP space
    -- a rule-swapping window no single padding avoids. Covering the ring
    resolves enclosure and spacing together at every padding.
    """
    from klayout_tools.gen import _grow_box_um, _shift_box, _snap_ring_box_um

    ring = info["ring"]
    ring_margin_um = pcell.voltage_flavor_ring_margin_um
    if ring is None or ring_margin_um <= 0.0:
        return None
    ox_um, oy_um = info["ring_offset_um"]
    return _grow_box_um(
        _snap_ring_box_um(
            _shift_box(ring["outer_box_um"], ox_um, oy_um), pcell.mfg_grid_um, dbu
        ),
        ring_margin_um,
        dbu,
    )
