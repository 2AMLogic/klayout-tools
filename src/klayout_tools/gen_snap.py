"""Grid-snapping box helpers for the ``gen`` layout builders.

Split out of :mod:`klayout_tools.gen` (file-size ratchet); ``gen`` re-exports
the names so existing ``from .gen import _snap_square_box_um`` keeps working.
"""

from __future__ import annotations

_GRID_DBU_UM = 0.001


def _snap_rect_box_um(
    cx_um: float, cy_um: float, half_w_um: float, half_h_um: float, dbu_um: float
) -> tuple[float, float, float, float]:
    """Rectangular sibling of :func:`_snap_square_box_um` (issue #2449): a
    ``2 * half_w_um`` x ``2 * half_h_um`` box whose centre and half-extents are
    snapped to integer dbu *before* the edges are derived, so both drawn
    dimensions are exact (e.g. a 0.19um x 2.0um precision-poly licon)."""
    cx_dbu = round(cx_um / dbu_um)
    cy_dbu = round(cy_um / dbu_um)
    hw_dbu = round(half_w_um / dbu_um)
    hh_dbu = round(half_h_um / dbu_um)
    return (
        (cx_dbu - hw_dbu) * dbu_um,
        (cy_dbu - hh_dbu) * dbu_um,
        (cx_dbu + hw_dbu) * dbu_um,
        (cy_dbu + hh_dbu) * dbu_um,
    )


def _snap_square_box_um(
    cx_um: float, cy_um: float, half_um: float, dbu_um: float
) -> tuple[float, float, float, float]:
    """A ``2 * half_um``-wide square centred on ``(cx_um, cy_um)``, snapped
    to the ``dbu_um`` manufacturing grid *before* the box edges are derived,
    so the returned box's width and height are exactly ``2 * round(half_um /
    dbu_um) * dbu_um`` regardless of where the un-snapped centre falls
    relative to the grid.

    This matters because :func:`_insert_boxes`/``_insert_ring``'s ``_to_box``
    round each of a box's *four edges* to the dbu grid independently
    (``int(round(x0 / dbu))`` ... ``int(round(x1 / dbu))``). Building the box
    from an un-snapped float centre first (``cx - half``, ``cx + half``) lets
    ordinary floating-point noise put one edge just below a half-dbu grid
    boundary and the other just above it -- Python's banker's-rounding
    ``round()`` can then round those two edges in *different* directions,
    silently drawing a box 1 dbu narrower/shorter than intended. That is
    exactly the ``contact.width.1`` failure mode diagnosed in issue #685:
    ``CONTACT_SIZE_UM`` carries zero DRC margin above gf180mcu's ``CO.1``
    threshold, so a single dbu of asymmetric rounding is enough to trip it.
    Snapping the centre (and the half-width, which is already grid-exact for
    every current caller) to integer dbu counts *first* makes both edges
    derive from the same rounded integer, so they can never drift apart.
    """
    cx_dbu = round(cx_um / dbu_um)
    cy_dbu = round(cy_um / dbu_um)
    half_dbu = round(half_um / dbu_um)
    return (
        (cx_dbu - half_dbu) * dbu_um,
        (cy_dbu - half_dbu) * dbu_um,
        (cx_dbu + half_dbu) * dbu_um,
        (cy_dbu + half_dbu) * dbu_um,
    )


def _snap_offset_um(
    ox_um: float, oy_um: float, dbu_um: float = _GRID_DBU_UM
) -> tuple[float, float]:
    """A placement offset ``(ox_um, oy_um)`` snapped to the ``dbu_um``
    manufacturing grid, so boxes already grid-exact in their own local frame
    stay grid-exact after the offset is applied.

    This is the placement-offset counterpart of :func:`_snap_square_box_um`
    (issue #2442): :func:`_insert_boxes`/``_insert_ring`` round each of a
    box's four edges *independently* after adding the offset
    (``int(round((x0 + ox_um) / dbu))`` ...). An un-snapped offset derived
    from an arbitrary float param (e.g. ``ring_padding_um``, whose only
    documented constraint is ``>= 0``) can land one edge of an exactly
    ``CONTACT_SIZE_UM``-square contact just below a half-dbu rounding
    boundary and the opposite edge just above it, silently drawing a
    221x220dbu contact that trips gf180mcu's ``contact.width.1`` max-size
    bound. Snapping the offset to integer dbu *first* makes every shifted
    edge derive from the same grid as the un-shifted box, so per-edge
    rounding can never drift an edge pair apart."""
    return (round(ox_um / dbu_um) * dbu_um, round(oy_um / dbu_um) * dbu_um)
