"""Pure geometry for the ASAP7 substrate/well tap generator (issue #4841).

A tap is an ACTIVE island with no gate over it, contacted through the
SDT/LISD/V0/M1 stack and covered by the matching implant:

* ``ptap`` -- p+ tap (PSELECT) in the p-substrate; ties an nFET body. No WELL.
* ``ntap`` -- n+ tap (NSELECT) inside a WELL; ties a pFET body.

It reuses the ``gen_asap7`` grids (fin pitch, S/D column pitch) so the ACTIVE
height is a whole number of 27 nm fin pitches and every S/D column sits on
the 54 nm gate pitch. Everything is integer dbu at ``ASAP7_DBU_UM``.

``ring=True`` draws a rectangular ACTIVE ring (a body ring around a device
region): the bottom and top bars are ``nfin`` fins tall and ``ncol`` columns
wide, the left/right bars are one column wide, and ``inner_nfin`` fins of
empty hole sit between the bars.
"""

from __future__ import annotations

from typing import Any

from .gen_asap7 import (
    ACTIVE_Y0,
    ASAP7_DBU_UM,
    ASAP7_LAYERS,
    FIN_OFFSET,
    FIN_PITCH,
    FIN_W,
    M1_RAIL_GAP,
    NF_MAX,
    NFIN_MAX,
    SD_W,
    V0_INSET,
    V0_SIZE,
    Box,
    _col_x,
)
from .gen_spec import _GeneratorSpec

__all__ = [
    "ASAP7_TAP_SPEC",
    "TAP_DEVICES",
    "all_boxes",
    "tap_layout",
    "validate_tap_params",
]

TAP_DEVICES = ("ptap", "ntap")

#: ACTIVE inset inside an S/D column (matches ``finfet_layout``).
ACT_INSET = 16
#: NSELECT/PSELECT enclosure of ACTIVE (46 nm horizontal, 27 nm vertical).
SEL_EN_X = 184
SEL_EN_Y = 108


def validate_tap_params(
    device: str, nfin: int, ncol: int, ring: bool, inner_nfin: int
) -> list[str]:
    """Return a list of problems with the request (empty when valid)."""
    problems = []
    if device not in TAP_DEVICES:
        problems.append(f"params.device must be one of {list(TAP_DEVICES)}")
    # A one-column tap is 16 nm wide, so ACTIVE.A.1A (864 nm^2) needs 2 fins.
    if not 2 <= nfin <= NFIN_MAX:
        problems.append(f"params.nfin must be an integer in 2..{NFIN_MAX}")
    if not 1 <= ncol <= NF_MAX:
        problems.append(f"params.ncol must be an integer in 1..{NF_MAX}")
    if ring and ncol < 2:
        problems.append("params.ncol must be at least 2 when params.ring is true")
    if not 1 <= inner_nfin <= NFIN_MAX:
        problems.append(f"params.inner_nfin must be an integer in 1..{NFIN_MAX}")
    return problems


def tap_layout(
    device: str,
    nfin: int,
    ncol: int,
    ring: bool = False,
    inner_nfin: int = 2,
) -> dict[str, Any]:
    """Geometry of one tap (or tap ring), integer dbu.

    Returns ``{"boxes": {role: [Box, ...]}, "ports": {"B": [...]}, "bbox": Box}``.
    The body port ``B`` is the M1 rail below the bottom bar.
    """
    act_y0 = ACTIVE_Y0
    bar_h = FIN_PITCH * nfin
    inner_h = FIN_PITCH * inner_nfin if ring else 0
    act_y1 = act_y0 + (2 * bar_h + inner_h if ring else bar_h)
    act_x0 = _col_x(0) + ACT_INSET
    act_x1 = _col_x(ncol - 1) + SD_W - ACT_INSET

    sel = (
        act_x0 - SEL_EN_X,
        act_y0 - SEL_EN_Y,
        act_x1 + SEL_EN_X,
        act_y1 + SEL_EN_Y,
    )

    boxes: dict[str, list[Box]] = {k: [] for k in ASAP7_LAYERS}
    if not ring:
        boxes["active"].append((act_x0, act_y0, act_x1, act_y1))
    else:
        side_w = SD_W - 2 * ACT_INSET
        boxes["active"].append((act_x0, act_y0, act_x1, act_y0 + bar_h))
        boxes["active"].append((act_x0, act_y1 - bar_h, act_x1, act_y1))
        boxes["active"].append((act_x0, act_y0, act_x0 + side_w, act_y1))
        boxes["active"].append((act_x1 - side_w, act_y0, act_x1, act_y1))

    for m in range(1, (act_y1 - act_y0) // FIN_PITCH + 1):
        y = FIN_OFFSET + FIN_PITCH * m
        boxes["fin"].append((sel[0], y, sel[2], y + FIN_W))

    # The rails sit outside the ACTIVE: below it for the bottom bar, above it
    # for the ring's top bar (both joined by the left/right column straps).
    lo_rail = (act_y0 - M1_RAIL_GAP - V0_SIZE, act_y0 - M1_RAIL_GAP)
    hi_rail_y0 = act_y1 + 64
    hi_rail = (hi_rail_y0, hi_rail_y0 + V0_SIZE)
    port_boxes: dict[str, list[Box]] = {"B": []}
    edge_cols = {0, ncol - 1}
    for j in range(ncol):
        x = _col_x(j)
        vx0 = x + V0_INSET
        vx1 = vx0 + V0_SIZE
        full_height = ring and j in edge_cols
        if full_height:
            boxes["sdt"].append((x, act_y0, x + SD_W, act_y1))
            boxes["lisd"].append((x, act_y0, x + SD_W, act_y1))
        elif ring:
            for y0, y1 in ((act_y0, act_y0 + bar_h), (act_y1 - bar_h, act_y1)):
                boxes["sdt"].append((x, y0, x + SD_W, y1))
                boxes["lisd"].append((x, y0, x + SD_W, y1))
        else:
            boxes["sdt"].append((x, act_y0, x + SD_W, act_y1))
            boxes["lisd"].append((x, act_y0, x + SD_W, act_y1))
        # Bottom contact: V0 + M1 stub down to the lower rail.
        boxes["v0"].append((vx0, act_y0, vx1, act_y0 + V0_SIZE))
        boxes["m1"].append((vx0, lo_rail[0], vx1, act_y0 + V0_SIZE))
        if ring:
            boxes["v0"].append((vx0, act_y1 - V0_SIZE, vx1, act_y1))
            boxes["m1"].append((vx0, act_y1 - V0_SIZE, vx1, hi_rail[1]))
            if full_height:
                # Strap tying the two rails together through this column.
                boxes["m1"].append((vx0, lo_rail[0], vx1, hi_rail[1]))
    cols = [_col_x(j) + V0_INSET for j in range(ncol)]
    if ncol >= 2:
        lo = (cols[0], lo_rail[0], cols[-1] + V0_SIZE, lo_rail[1])
        boxes["m1"].append(lo)
        port_boxes["B"].append(lo)
        if ring:
            boxes["m1"].append((cols[0], hi_rail[0], cols[-1] + V0_SIZE, hi_rail[1]))
    else:
        port_boxes["B"].append((cols[0], lo_rail[0], cols[0] + V0_SIZE, lo_rail[1]))

    sel_layer = "pselect" if device == "ptap" else "nselect"
    boxes[sel_layer].append(sel)
    if device == "ntap":
        boxes["well"].append(sel)

    bbox = (
        sel[0],
        min(sel[1], lo_rail[0]),
        sel[2],
        max(sel[3], hi_rail[1]) if ring else sel[3],
    )
    return {
        "boxes": {k: v for k, v in boxes.items() if v},
        "ports": port_boxes,
        "bbox": bbox,
    }


def all_boxes(info: dict[str, Any]) -> list[tuple[str, Box]]:
    return [(role, b) for role, bs in info["boxes"].items() for b in bs]


# --------------------------------------------------------------------------- #
# `klt gen` registry callbacks (validate / describe / layer_params)
# --------------------------------------------------------------------------- #


def asap7_tap_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    problems = validate_tap_params(
        params["device"],
        params["nfin"],
        params["ncol"],
        bool(params["ring"]),
        params["inner_nfin"],
    )
    if problems:
        raise GenError("generator 'asap7_tap': " + "; ".join(problems))


def asap7_tap_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """No hidden layer params; only gates the PDK family."""
    from .gen import GenError
    from .pdk_families import pdk_variant_family

    if pdk_variant_family(pdk_info["variant"]) != "asap7":
        raise GenError(
            "generator 'asap7_tap': needs an ASAP7 PDK (resolved variant "
            f"'{pdk_info['variant']}'); pass pdk.variant/root for an asap7 "
            "process tree"
        )
    return {}


def asap7_tap_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    ring = bool(params["ring"])
    info = tap_layout(
        params["device"], params["nfin"], params["ncol"], ring, params["inner_nfin"]
    )
    m1 = ASAP7_LAYERS["m1"]
    box = info["ports"]["B"][-1]
    port = {
        "name": "B",
        "net": None,
        "layer": {"layer": m1[0], "datatype": m1[1], "name": None},
        "x_um": (box[0] + box[2]) / 2 * ASAP7_DBU_UM,
        "y_um": (box[1] + box[3]) / 2 * ASAP7_DBU_UM,
        "width_um": (box[2] - box[0]) * ASAP7_DBU_UM,
        "direction_deg": 90,
    }
    ptap = params["device"] == "ptap"
    return {
        "device_count": 1,
        "ports": [port],
        "drc_hints": {
            "min_spacing_um": 18 * 0.001,
            "matched_group_id": None,
            "snapped_to_grid": True,
            "notes": [
                "all coordinates are integer dbu at 0.00025 um on the ASAP7 "
                "fin (108 dbu) / gate (216 dbu) grids",
                "a tap has no gate, so klt extract reports no device for it; "
                "the body tie is checked on the drawn layers, not extracted",
            ],
        },
        "warnings": [],
        "expected": {
            "tap_type": "p+ in p-substrate" if ptap else "n+ in n-well",
            "ties": "nfet body" if ptap else "pfet body",
            "implant_layer": "pselect" if ptap else "nselect",
            "has_well": not ptap,
            "ring": ring,
            "columns": params["ncol"],
        },
    }


ASAP7_TAP_SPEC = _GeneratorSpec(
    name="asap7_tap",
    summary=(
        "ASAP7 substrate/well tap: ptap (p+ in p-substrate, ties an nFET "
        "body) or ntap (n+ in n-well, ties a pFET body), params.nfin fins "
        "tall x params.ncol S/D columns, on the 27 nm fin / 54 nm column "
        "grids with SDT/LISD/V0/M1 contacts; params.ring draws a body ring. "
        "Body port B on M1. ASAP7 PDK only."
    ),
    dbu=0.00025,
    validate=asap7_tap_validate,
    describe=asap7_tap_describe,
    layer_params=asap7_tap_layer_params,
    flatten_output=True,
)
