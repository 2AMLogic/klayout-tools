"""Mesh-cell geometry helpers for ``klt power``'s resistive-network
extraction (:mod:`klayout_tools.power`).

Split out of ``power.py`` verbatim to respect the per-file size ratchet
while issue #5791 added the per-edge resistance-provenance citations; the
moved code is unchanged.
"""

from __future__ import annotations


def _cell_sides(
    column: int,
    row: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    horizontal_major: bool,
) -> tuple[
    tuple[tuple[int, int], tuple[str, int, int], float, float, float, float, bool], ...
]:
    """The four sides of one mesh cell as ``(neighbour_key, port_key, port_x,
    port_y, half_length_um, cross_um, is_major_axis_end)`` tuples.

    ``port_key`` is deliberately shared by the two cells on either side of a
    boundary, so they meet at **one** node; ``half_length_um`` is the
    centre-to-side travel along the flow direction and ``cross_um`` the
    conducting width across it.
    """
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w, half_h = (x1 - x0) / 2, (y1 - y0) / 2
    width_um, height_um = x1 - x0, y1 - y0
    return (
        (
            (column - 1, row),
            ("v", column, row),
            x0,
            cy,
            half_w,
            height_um,
            horizontal_major,
        ),
        (
            (column + 1, row),
            ("v", column + 1, row),
            x1,
            cy,
            half_w,
            height_um,
            horizontal_major,
        ),
        (
            (column, row - 1),
            ("h", column, row),
            cx,
            y0,
            half_h,
            width_um,
            not horizontal_major,
        ),
        (
            (column, row + 1),
            ("h", column, row + 1),
            cx,
            y1,
            half_h,
            width_um,
            not horizontal_major,
        ),
    )
