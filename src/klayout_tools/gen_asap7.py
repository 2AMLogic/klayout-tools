"""Pure geometry for the ASAP7 FinFET generators (issue #2762).

ASAP7 is a FinFET process: device width is quantized by *fin count*, gates sit
on a fixed 54 nm pitch, and S/D contacts sit between gates. Everything here is
integer database units at ``ASAP7_DBU_UM`` (0.00025 um = 0.25 nm), so every
coordinate is on the grid by construction -- there is no float snapping.

Numbers come from the pinned lambdapdk v0.2.17 files already recorded in
``klayout_tools.decks.asap7`` and the DRM tables transcribed in
``decks/asap7.drc`` (gate 80 dbu = 20 nm, gate pitch 216 dbu = 54 nm, fin
pitch 108 dbu = 27 nm, fin width 28 dbu = 7 nm); the INVx2 cell of the pinned
RVT library is the drawing idiom (dummy gate at each end, GCut above/below).
Kept free of ``klayout`` and ``gen.py`` imports so the PCell, the validate/
describe callbacks and the grid-snap tests share one layout function.

``nfin`` is the fin count **per gate finger**; the extractor
(``extract_finfet``) reports the total over parallel fingers, which is
``nfin * nf`` (``nfin_total`` in the ``expected`` block).
"""

from __future__ import annotations

from typing import Any

from .gen_spec import _GeneratorSpec

ASAP7_DBU_UM = 0.00025

DEVICES = ("nfet", "pfet")
VT_FLAVOURS = ("rvt", "lvt", "slvt")

NFIN_MAX = 16
NF_MAX = 16

GATE_W = 80
GATE_PITCH = 216
FIN_PITCH = 108
FIN_W = 28
FIN_OFFSET = 40  # fin lower edge above the 108-dbu active grid line
SD_W = 96
SD_X0 = 168  # first S/D column left edge
GATE_X0 = 68  # first (dummy) gate left edge
ACTIVE_Y0 = 108
V0_SIZE = 72
V0_INSET = 12
M1_MIN_LEN = 112  # 72 dbu wide x 112 dbu = 504 nm^2 (M1.A.1)
M1_RAIL_GAP = 72
M1_TIP_GAP = 124  # M1.S.4 tip-to-tip 31 nm

#: (layer, datatype) per role -- asap7.lyp numbers, as in ``decks/asap7.py``.
ASAP7_LAYERS: dict[str, tuple[int, int]] = {
    "well": (1, 0),
    "fin": (2, 0),
    "gate": (7, 0),
    "gcut": (10, 0),
    "active": (11, 0),
    "nselect": (12, 0),
    "pselect": (13, 0),
    "lig": (16, 0),
    "lisd": (17, 0),
    "v0": (18, 0),
    "m1": (19, 0),
    "sdt": (88, 0),
    "slvt": (97, 0),
    "lvt": (98, 0),
}

Box = tuple[int, int, int, int]


def validate_finfet_params(device: str, nfin: int, nf: int, vt: str) -> list[str]:
    """Return a list of problems with the request (empty when valid)."""
    problems = []
    if device not in DEVICES:
        problems.append(f"params.device must be one of {list(DEVICES)}")
    if vt not in VT_FLAVOURS:
        problems.append(f"params.vt must be one of {list(VT_FLAVOURS)}")
    if not 1 <= nfin <= NFIN_MAX:
        problems.append(f"params.nfin must be an integer in 1..{NFIN_MAX}")
    if not 1 <= nf <= NF_MAX:
        problems.append(f"params.nf must be an integer in 1..{NF_MAX}")
    return problems


def _col_x(j: int) -> int:
    return SD_X0 + GATE_PITCH * j


def _gate_x(i: int) -> int:
    return GATE_X0 + GATE_PITCH * i


def finfet_layout(device: str, nfin: int, nf: int, vt: str = "rvt") -> dict[str, Any]:
    """Geometry of one ``nfin`` x ``nf`` FinFET, integer dbu.

    Returns ``{"boxes": {role: [Box, ...]}, "ports": {...}, "bbox": Box}``.
    Columns ``0..nf`` are S/D, even columns the source and odd the drain;
    gates ``1..nf`` are live and gates ``0`` and ``nf + 1`` are dummies.
    """
    width = GATE_PITCH * (nf + 2)
    act_y0 = ACTIVE_Y0
    act_y1 = act_y0 + FIN_PITCH * nfin
    act_x0 = SD_X0 + 16
    act_x1 = _col_x(nf) + SD_W - 16

    # Gate ends sit inside a GCut band, as in the pinned INVx2 cell.
    cut_lo = (-88, 88)
    # The gate M1 pad keeps 31 nm tip-to-tip (M1.S.4) above the source strip.
    lig_y0 = max(act_y1 + 64, act_y0 + V0_SIZE + M1_TIP_GAP - 8)
    lig_y1 = lig_y0 + 88
    cut_hi = (lig_y1 + 24, lig_y1 + 24 + 176)
    gate_y0 = cut_lo[0] + 68
    gate_y1 = cut_hi[1] - 68

    boxes: dict[str, list[Box]] = {k: [] for k in ASAP7_LAYERS}
    boxes["active"].append((act_x0, act_y0, act_x1, act_y1))
    for m in range(1, nfin + 1):
        y = FIN_OFFSET + FIN_PITCH * m
        boxes["fin"].append((0, y, width, y + FIN_W))
    for i in range(nf + 2):
        boxes["gate"].append((_gate_x(i), gate_y0, _gate_x(i) + GATE_W, gate_y1))
    boxes["gcut"].append((0, cut_lo[0], width, cut_lo[1]))
    boxes["gcut"].append((0, cut_hi[0], width, cut_hi[1]))

    # Source columns (even) strap to an M1 rail below the active, drain columns
    # (odd) to a rail above it, so ``nf`` fingers extract as one parallel device.
    s_rail_y = (act_y0 - M1_RAIL_GAP - V0_SIZE, act_y0 - M1_RAIL_GAP)
    d_rail_y0 = max(act_y1 + 64, act_y0 + V0_SIZE + M1_RAIL_GAP)
    d_rail_y = (d_rail_y0, d_rail_y0 + V0_SIZE)
    port_boxes: dict[str, list[Box]] = {"S": [], "D": [], "G": []}
    for j in range(nf + 1):
        x = _col_x(j)
        boxes["sdt"].append((x, act_y0, x + SD_W, act_y1))
        boxes["lisd"].append((x, act_y0, x + SD_W, act_y1))
        vx0 = x + V0_INSET
        vx1 = vx0 + V0_SIZE
        if j % 2 == 0:
            boxes["v0"].append((vx0, act_y0, vx1, act_y0 + V0_SIZE))
            m1 = (vx0, s_rail_y[0], vx1, act_y0 + V0_SIZE)
            port_boxes["S"].append(m1)
        else:
            boxes["v0"].append((vx0, act_y1 - V0_SIZE, vx1, act_y1))
            m1 = (vx0, act_y1 - V0_SIZE, vx1, d_rail_y[1])
            port_boxes["D"].append(m1)
        boxes["m1"].append(m1)
    for name, ys, parity in (("S", s_rail_y, 0), ("D", d_rail_y, 1)):
        cols = [_col_x(j) + V0_INSET for j in range(nf + 1) if j % 2 == parity]
        if len(cols) >= 2:
            rail = (cols[0], ys[0], cols[-1] + V0_SIZE, ys[1])
            boxes["m1"].append(rail)
            port_boxes[name].append(rail)

    lig = (_gate_x(1) - 68, lig_y0, _gate_x(nf) + GATE_W + 8, lig_y1)
    boxes["lig"].append(lig)
    gv0 = (lig[0] + 4, lig_y0 + 8, lig[0] + 4 + V0_SIZE, lig_y0 + 8 + V0_SIZE)
    boxes["v0"].append(gv0)
    gm1 = (gv0[0], gv0[1], gv0[2], gv0[1] + M1_MIN_LEN)
    boxes["m1"].append(gm1)
    port_boxes["G"].append(gm1)

    sel_y0, sel_y1 = 0, act_y1 + FIN_PITCH
    boxes["nselect" if device == "nfet" else "pselect"].append(
        (0, sel_y0, width, sel_y1)
    )
    if device == "pfet":
        boxes["well"].append((0, act_y0 - FIN_PITCH, width, act_y1 + FIN_PITCH))
    if vt != "rvt":
        boxes[vt].append((0, sel_y0, width, sel_y1))

    bbox = (0, cut_lo[0], width, cut_hi[1])
    return {
        "boxes": {k: v for k, v in boxes.items() if v},
        "ports": port_boxes,
        "bbox": bbox,
        "nfin_total": nfin * nf,
    }


def all_boxes(info: dict[str, Any]) -> list[tuple[str, Box]]:
    return [(role, b) for role, bs in info["boxes"].items() for b in bs]


# --------------------------------------------------------------------------- #
# `klt gen` registry callbacks (validate / describe / layer_params)
# --------------------------------------------------------------------------- #


def asap7_finfet_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    problems = validate_finfet_params(
        params["device"], params["nfin"], params["nf"], params["vt"]
    )
    if problems:
        raise GenError("generator 'asap7_finfet': " + "; ".join(problems))


def asap7_finfet_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """No hidden layer params (ASAP7 layers are fixed in
    :data:`ASAP7_LAYERS`); only gates the PDK family."""
    from .gen import GenError
    from .pdk_families import pdk_variant_family

    if pdk_variant_family(pdk_info["variant"]) != "asap7":
        raise GenError(
            "generator 'asap7_finfet': needs an ASAP7 PDK (resolved variant "
            f"'{pdk_info['variant']}'); pass pdk.variant/root for an asap7 "
            "process tree"
        )
    return {}


def asap7_finfet_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    info = finfet_layout(params["device"], params["nfin"], params["nf"], params["vt"])
    m1 = ASAP7_LAYERS["m1"]
    layer = {"layer": m1[0], "datatype": m1[1], "name": None}
    ports = []
    for name, boxes in info["ports"].items():
        # The rail (last box when present) is the port; a lone strip otherwise.
        box = boxes[-1]
        ports.append(
            {
                "name": name,
                "net": None,
                "layer": layer,
                "x_um": (box[0] + box[2]) / 2 * ASAP7_DBU_UM,
                "y_um": (box[1] + box[3]) / 2 * ASAP7_DBU_UM,
                "width_um": (box[2] - box[0]) * ASAP7_DBU_UM,
                "direction_deg": 90,
            }
        )
    return {
        "device_count": 1,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": 18 * 0.001,
            "matched_group_id": None,
            "snapped_to_grid": True,
            "notes": [
                "all coordinates are integer dbu at 0.00025 um on the ASAP7 "
                "fin (108 dbu) / gate (216 dbu) grids",
                "nfin is per gate finger; extracted nfin is the total over "
                "parallel fingers (nfin * nf)",
            ],
        },
        "warnings": [],
        "expected": {
            "device_class": f"{'nmos' if params['device'] == 'nfet' else 'pmos'}"
            f"_{params['vt']}",
            "nfin_per_finger": params["nfin"],
            "fingers": params["nf"],
            "nfin_total": info["nfin_total"],
        },
    }


ASAP7_FINFET_SPEC = _GeneratorSpec(
    name="asap7_finfet",
    summary=(
        "ASAP7 FinFET nFET/pFET: params.nfin fins per gate finger x "
        "params.nf fingers on the 27 nm fin / 54 nm gate grids, with a "
        "dummy gate at each end, SDT/LISD/V0/M1 source+drain straps and "
        "a LIG/V0/M1 gate contact; S/D/G ports on M1. Extracted nfin is "
        "the total over fingers (nfin * nf). ASAP7 PDK only; taps and "
        "guard rings are not generated yet (issue #2762)."
    ),
    dbu=0.00025,
    validate=asap7_finfet_validate,
    describe=asap7_finfet_describe,
    layer_params=asap7_finfet_layer_params,
    flatten_output=True,
)
