"""Shape-insertion helpers for the ``mos_array`` PCell (split out of
``mos_array.py`` for the file-size ratchet)."""

from __future__ import annotations

from typing import Any


def _mos_cap_enabled(pcell: Any) -> bool:
    """``pcell.mos_cap`` (the PCell params are dynamic attributes)."""
    return bool(pcell.mos_cap)


def _insert_mos_cap_mark(pcell: Any, info: dict[str, Any], dbu: float) -> None:
    """Draw the MOS-capacitor marker (issue #3283) when ``pcell.mos_cap`` is
    set: the same enclosing box as the voltage-flavor marker, so it covers
    every unit device."""
    if not _mos_cap_enabled(pcell):
        return
    from klayout_tools.gen import _insert_boxes

    _insert_boxes(
        pcell.cell,
        pcell.layout.layer(pcell.mos_cap_mark_layer),
        dbu,
        [info["voltage_flavor_mark_box_um"]],
    )


def _mirror_boxes(
    boxes: list[tuple[float, float, float, float]], total_len_um: float
) -> list[tuple[float, float, float, float]]:
    """Flip unit-local boxes about the unit's vertical axis (issue #3552)."""
    return [(total_len_um - x1, y0, total_len_um - x0, y1) for x0, y0, x1, y1 in boxes]


def _unit_role_boxes(
    unit_boxes: dict[str, list[tuple[float, float, float, float]]],
    role: str,
    total_len_um: float,
    mirrored: bool,
) -> list[tuple[float, float, float, float]]:
    """One role's unit-local boxes, flipped for a ``mirrored`` cell."""
    boxes = unit_boxes[role]
    return _mirror_boxes(boxes, total_len_um) if mirrored else boxes


def _insert_unit_shapes(
    cell: Any,
    dbu: float,
    role_layers: list[tuple[str, int]],
    unit_boxes: dict[str, list[tuple[float, float, float, float]]],
    placements: list[dict[str, Any]],
    share_sd: bool,
    total_len_um: float,
) -> None:
    """Draw each placed unit's per-role boxes (issue #3551).

    Under ``share_sd`` adjacent units draw the shared S/D segment's
    active/contact/metal at identical coordinates; insert each distinct shape
    once rather than as stacked duplicates. A placement with ``mirrored`` set
    (issue #3552) draws its boxes flipped about the unit's vertical axis."""
    from klayout_tools.gen import _insert_boxes

    drawn: set[tuple[str, int, int, int, int]] = set()
    for c in placements:
        for role, li in role_layers:
            boxes = _unit_role_boxes(
                unit_boxes, role, total_len_um, c.get("mirrored", False)
            )
            if not share_sd:
                _insert_boxes(cell, li, dbu, boxes, c["x0_um"], c["y0_um"])
                continue
            for x0, y0, x1, y1 in boxes:
                ox, oy = c["x0_um"], c["y0_um"]
                shifted = (x0 + ox, y0 + oy, x1 + ox, y1 + oy)
                key = (
                    role,
                    int(round(shifted[0] / dbu)),
                    int(round(shifted[1] / dbu)),
                    int(round(shifted[2] / dbu)),
                    int(round(shifted[3] / dbu)),
                )
                if key not in drawn:
                    drawn.add(key)
                    _insert_boxes(cell, li, dbu, [shifted])
