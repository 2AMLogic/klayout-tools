"""``cap_vpp_array`` request validation, PDK-cell lookup and response description.

``cap_vpp_array`` arrays one of the PDK's fixed, pre-drawn vertical-parallel-
plate (interdigitated metal) capacitor cells (sky130's
``sky130_fd_pr__cap_vpp_*``). Unlike every other ``klt gen`` family it draws no
geometry of its own: the cell's GDS in the resolved PDK is the authoritative
source, so this module only locates and inspects that cell. The PCell itself
lives in :mod:`klayout_tools.gen_pcells.cap_vpp_array`.

The cell is read from the PDK at request time and never vendored.
"""

from __future__ import annotations

import functools
import os
from typing import Any

#: Name prefix of the fixed-layout capacitor cells this generator arrays.
CAP_VPP_PREFIX = "sky130_fd_pr__cap_vpp_"

#: PDK families with fixed ``cap_vpp`` cells (sky130 only first).
_SUPPORTED_FAMILIES = ("sky130",)

#: Drawing datatype searched, on a pin label's own layer, for the shape the
#: label sits on (so the reported port carries a bbox, not just a point).
_DRAWING_DATATYPE = 20


def _gds_path(pdk_info: dict[str, Any]) -> str:
    from .gen import GenError
    from .gen_layer_params import _pdk_family

    family = _pdk_family(pdk_info["variant"])
    if family not in _SUPPORTED_FAMILIES:
        raise GenError(
            f"generator 'cap_vpp_array': PDK family '{family}' ships no fixed "
            f"cap_vpp cells -- supported families: {', '.join(_SUPPORTED_FAMILIES)}"
        )
    libs_ref = pdk_info.get("assets", {}).get("libs_ref")
    path = (
        os.path.join(libs_ref, "sky130_fd_pr", "gds", "sky130_fd_pr.gds")
        if libs_ref
        else None
    )
    if not path or not os.path.isfile(path):
        raise GenError(
            "generator 'cap_vpp_array': the PDK's sky130_fd_pr GDS "
            f"(libs.ref/sky130_fd_pr/gds/sky130_fd_pr.gds) was not found for "
            f"variant '{pdk_info['variant']}'"
        )
    return path


@functools.lru_cache(maxsize=4)
def _inspect_gds(path: str, mtime_ns: int) -> dict[str, Any]:
    """Map every ``cap_vpp_*`` cell in ``path`` to its bbox and pin labels."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(path)
    cells: dict[str, Any] = {}
    for cell in layout.each_cell():
        if not cell.name.startswith(CAP_VPP_PREFIX):
            continue
        bbox = cell.dbbox()
        pins = []
        for li in layout.layer_indexes():
            info = layout.get_info(li)
            for shape in cell.shapes(li).each():
                if not shape.is_text():
                    continue
                pos = shape.dtext.trans.disp
                pin_box = None
                dli = layout.find_layer(info.layer, _DRAWING_DATATYPE)
                if dli is not None:
                    hits = [
                        s.dbbox()
                        for s in cell.shapes(dli).each()
                        if s.dbbox().contains(pos.to_p())
                    ]
                    if hits:
                        # smallest enclosing shape: the pin, not a bounding plane
                        pin_box = min(hits, key=lambda b: b.area())
                pins.append(
                    {
                        "label": shape.text_string,
                        "layer": info.layer,
                        "datatype": info.datatype,
                        "x_um": pos.x,
                        "y_um": pos.y,
                        "box_um": (
                            [pin_box.left, pin_box.bottom, pin_box.right, pin_box.top]
                            if pin_box is not None
                            else None
                        ),
                    }
                )
        cells[cell.name] = {
            "bbox_um": [bbox.left, bbox.bottom, bbox.right, bbox.top],
            "pins": pins,
        }
    return cells


def _cell_table(pdk_info: dict[str, Any]) -> dict[str, Any]:
    path = _gds_path(pdk_info)
    return _inspect_gds(path, os.stat(path).st_mtime_ns)


def _cap_vpp_array_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if params["num"] < 1:
        raise GenError("generator 'cap_vpp_array': params.num must be >= 1")
    if params["pitch_um"] < 0:
        raise GenError("generator 'cap_vpp_array': params.pitch_um must be >= 0")


def _lookup(params: dict[str, Any], pdk_info: dict[str, Any]) -> dict[str, Any]:
    """Return the named cell's info, rejecting unknown names / overlapping pitch."""
    from .gen import GenError

    table = _cell_table(pdk_info)
    entry = table.get(params["pdk_cell"])
    if entry is None:
        raise GenError(
            "generator 'cap_vpp_array': unknown params.pdk_cell "
            f"'{params['pdk_cell']}' -- available: {', '.join(sorted(table))}"
        )
    width = entry["bbox_um"][2] - entry["bbox_um"][0]
    pitch = params["pitch_um"]
    if params["num"] > 1 and 0 < pitch < width - 1e-9:
        raise GenError(
            f"generator 'cap_vpp_array': params.pitch_um ({pitch}um) is smaller "
            f"than the cell width ({width:g}um) -- instances would overlap; use "
            "0 to abut or a pitch >= the cell width"
        )
    return entry


def _cap_vpp_array_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    _lookup(params, pdk_info)
    return {"vpp_gds_path": _gds_path(pdk_info)}


def effective_pitch_um(params: dict[str, Any], entry: dict[str, Any]) -> float:
    """``pitch_um`` as drawn: 0 means abut at the cell's bbox width."""
    width = entry["bbox_um"][2] - entry["bbox_um"][0]
    return params["pitch_um"] if params["pitch_um"] > 0 else width


def _cap_vpp_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    from .gen import _grid_snapped

    entry = _lookup(params, pdk_info)
    pitch = effective_pitch_um(params, entry)
    x0, y0, x1, y1 = entry["bbox_um"]
    notes: list[str] = []
    ports = []
    for i in range(params["num"]):
        dx = i * pitch - x0
        dy = -y0
        for pin in entry["pins"]:
            port = {
                "name": f"U{i}_{pin['label']}",
                "net": None,
                "layer": {
                    "layer": pin["layer"],
                    "datatype": pin["datatype"],
                    "name": None,
                },
                "x_um": pin["x_um"] + dx,
                "y_um": pin["y_um"] + dy,
                "width_um": 0.0,
                "direction_deg": 90,
                "bbox_um": None,
            }
            b = pin["box_um"]
            if b is not None:
                port["bbox_um"] = {
                    "x0": b[0] + dx,
                    "y0": b[1] + dy,
                    "x1": b[2] + dx,
                    "y1": b[3] + dy,
                }
                port["width_um"] = min(b[2] - b[0], b[3] - b[1])
            ports.append(port)
    if not entry["pins"]:
        notes.append(
            f"cell '{params['pdk_cell']}' carries no pin labels in the PDK GDS, so "
            "no ports are reported; terminals are not invented"
        )
    notes.append(
        "terminals are not strapped between instances; the cell is placed "
        "as-drawn from the PDK GDS"
    )
    snapped = _grid_snapped(dbu, pitch)
    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": max(pitch - (x1 - x0), 0.0),
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }
