"""Pure geometry for the ``diode`` generator (issue #4300).

A sky130 junction diode: ``n_psub`` (n+ diffusion in the p-substrate, the
anode being the substrate itself, tied through a separate p+ tap) or
``p_nwell`` (p+ diffusion in an nwell, the cathode being an n+ tap in that
well). Kept free of ``klayout`` and ``gen.py`` imports so the PCell and the
describe/validate callbacks share one layout function.

``areaid.ed`` (81/19) is deliberately never drawn: the sky130 extraction deck
treats it as an *exclusion* on the diode terminals, so an ESD-flagged diode
would not extract as ``diode_*`` at all.
"""

from __future__ import annotations

from typing import Any

DIODE_KINDS = ("n_psub", "p_nwell")
DIODE_VOLTAGE_CLASSES = ("05v5", "11v0")

#: Smallest diffusion edge (um) that fits one licon with enclosure margin.
DIODE_MIN_EDGE_UM = 0.4

_LICON_UM = 0.17
_LICON_PITCH_UM = 0.34
_LICON_INSET_UM = 0.115
_LI_ENCLOSURE_UM = 0.08
_SDM_ENCLOSURE_UM = 0.125
_MARKER_ENCLOSURE_UM = 0.2
_HVI_ENCLOSURE_UM = 0.4
_TAP_GAP_UM = 0.7
_TAP_WIDTH_UM = 0.4
_WELL_MARGIN_UM = 0.4

Box = tuple[float, float, float, float]


def _grow(box: Box, margin: float) -> Box:
    return (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)


def _contacts(box: Box) -> tuple[list[Box], Box]:
    """A grid of licons inside diffusion ``box`` and the li1 box covering them."""
    x0, y0, x1, y1 = box
    nx = max(1, int((x1 - x0 - 2 * _LICON_INSET_UM - _LICON_UM) / _LICON_PITCH_UM) + 1)
    ny = max(1, int((y1 - y0 - 2 * _LICON_INSET_UM - _LICON_UM) / _LICON_PITCH_UM) + 1)
    span_x = (nx - 1) * _LICON_PITCH_UM + _LICON_UM
    span_y = (ny - 1) * _LICON_PITCH_UM + _LICON_UM
    cx0 = (x0 + x1 - span_x) / 2
    cy0 = (y0 + y1 - span_y) / 2
    cuts = [
        (
            cx0 + i * _LICON_PITCH_UM,
            cy0 + j * _LICON_PITCH_UM,
            cx0 + i * _LICON_PITCH_UM + _LICON_UM,
            cy0 + j * _LICON_PITCH_UM + _LICON_UM,
        )
        for i in range(nx)
        for j in range(ny)
    ]
    return cuts, _grow((cx0, cy0, cx0 + span_x, cy0 + span_y), _LI_ENCLOSURE_UM)


def _diode_layout(
    kind: str, voltage_class: str, width_um: float, length_um: float
) -> dict[str, Any]:
    """Boxes (um) per role, plus the two terminal li1 boxes.

    The junction diffusion's lower-left corner is the origin; the tap
    (substrate tap for ``n_psub``, well tap for ``p_nwell``) sits to its right.
    """
    diff: Box = (0.0, 0.0, width_um, length_um)
    tap: Box = (
        width_um + _TAP_GAP_UM,
        0.0,
        width_um + _TAP_GAP_UM + _TAP_WIDTH_UM,
        length_um,
    )
    diff_cuts, diff_li = _contacts(diff)
    tap_cuts, tap_li = _contacts(tap)

    junction_sdm = "nplus" if kind == "n_psub" else "pplus"
    tap_sdm = "pplus" if kind == "n_psub" else "nplus"
    boxes: dict[str, list[Box]] = {
        "active": [diff],
        "tap": [tap],
        junction_sdm: [_grow(diff, _SDM_ENCLOSURE_UM)],
        tap_sdm: [_grow(tap, _SDM_ENCLOSURE_UM)],
        "diode_mark": [_grow(diff, _MARKER_ENCLOSURE_UM)],
        "contact": diff_cuts + tap_cuts,
        "metal": [diff_li, tap_li],
    }
    if kind == "p_nwell":
        boxes["well"] = [
            (
                -_WELL_MARGIN_UM,
                -_WELL_MARGIN_UM,
                tap[2] + _WELL_MARGIN_UM,
                length_um + _WELL_MARGIN_UM,
            )
        ]
    if voltage_class == "11v0":
        # nwell.9: a well touching hvi must sit wholly inside it.
        hvi_base = boxes["well"][0] if kind == "p_nwell" else diff
        boxes["hvi"] = [_grow(hvi_base, _HVI_ENCLOSURE_UM)]

    # The junction side's terminal is the diffusion's li1; the other is the tap's.
    if kind == "n_psub":
        anode_li, cathode_li = tap_li, diff_li
    else:
        anode_li, cathode_li = diff_li, tap_li
    return {
        "boxes": boxes,
        "anode_li": anode_li,
        "cathode_li": cathode_li,
        "bbox_um": _bbox(boxes),
    }


def _bbox(boxes: dict[str, list[Box]]) -> Box:
    every = [b for group in boxes.values() for b in group]
    return (
        min(b[0] for b in every),
        min(b[1] for b in every),
        max(b[2] for b in every),
        max(b[3] for b in every),
    )
