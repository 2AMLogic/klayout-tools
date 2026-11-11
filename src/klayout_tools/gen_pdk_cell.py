"""``pdk_cell`` request validation, PDK-cell lookup and response description.

``pdk_cell`` places one of a PDK's *fixed*, pre-drawn GDS cells (bondpad, ESD,
...) as-drawn, for any PDK family and without importing a pcell library. It
generalizes :mod:`klayout_tools.gen_cap_vpp` (same array/pitch/port semantics,
shared inspector); the cell is read from the resolved PDK at request time and
never vendored. The PCell lives in :mod:`klayout_tools.gen_pcells.pdk_cell`.
"""

from __future__ import annotations

import glob
import os
from typing import Any

from .gen_cap_vpp import _cap_vpp_array_describe, _inspect_gds

_NAME = "pdk_cell"


def _gds_path(params: dict[str, Any], pdk_info: dict[str, Any]) -> str:
    from .gen import GenError

    library = params["library"]
    libs_ref = pdk_info.get("assets", {}).get("libs_ref")
    if not libs_ref or not os.path.isdir(libs_ref):
        raise GenError(
            f"generator '{_NAME}': no libs.ref directory was found for PDK "
            f"variant '{pdk_info['variant']}'"
        )
    available = sorted(
        d
        for d in os.listdir(libs_ref)
        if os.path.isdir(os.path.join(libs_ref, d, "gds"))
    )
    if not library or os.path.basename(library) != library or library not in available:
        raise GenError(
            f"generator '{_NAME}': unknown params.library '{library}' -- "
            f"available (libs.ref/*/gds): {', '.join(available) or 'none'}"
        )
    gds_dir = os.path.join(libs_ref, library, "gds")
    preferred = os.path.join(gds_dir, f"{library}.gds")
    if os.path.isfile(preferred):
        return preferred
    found = sorted(glob.glob(os.path.join(gds_dir, "*.gds")))
    if len(found) == 1:
        return found[0]
    raise GenError(
        f"generator '{_NAME}': libs.ref/{library}/gds holds "
        f"{'no GDS' if not found else 'several GDS files and no ' + library + '.gds'}"
        " -- cannot pick the library's cell GDS"
    )


def _cell_table(params: dict[str, Any], pdk_info: dict[str, Any]) -> dict[str, Any]:
    path = _gds_path(params, pdk_info)
    return _inspect_gds(path, os.stat(path).st_mtime_ns, "")


def _pdk_cell_validate(params: dict[str, Any]) -> None:
    from .gen import GenError

    if not params["library"]:
        raise GenError(f"generator '{_NAME}': params.library is required")
    if not params["pdk_cell"]:
        raise GenError(f"generator '{_NAME}': params.pdk_cell is required")
    if params["num"] < 1:
        raise GenError(f"generator '{_NAME}': params.num must be >= 1")
    if params["pitch_um"] < 0:
        raise GenError(f"generator '{_NAME}': params.pitch_um must be >= 0")


def _pdk_cell_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    path = _gds_path(params, pdk_info)
    _lookup(params, pdk_info)
    return {"pdk_cell_gds_path": path}


def _lookup(params: dict[str, Any], pdk_info: dict[str, Any]) -> dict[str, Any]:
    """Return the named cell's info, rejecting unknown names / overlapping pitch."""
    from .gen import GenError

    table = _cell_table(params, pdk_info)
    entry = table.get(params["pdk_cell"])
    if entry is None:
        raise GenError(
            f"generator '{_NAME}': unknown params.pdk_cell "
            f"'{params['pdk_cell']}' in library '{params['library']}' -- "
            f"available: {', '.join(sorted(table))}"
        )
    width = entry["bbox_um"][2] - entry["bbox_um"][0]
    pitch = params["pitch_um"]
    if params["num"] > 1 and 0 < pitch < width - 1e-9:
        raise GenError(
            f"generator '{_NAME}': params.pitch_um ({pitch}um) is smaller than "
            f"the cell width ({width:g}um) -- instances would overlap; use 0 to "
            "abut or a pitch >= the cell width"
        )
    return entry


def _pdk_cell_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    entry = _lookup(params, pdk_info)
    return _cap_vpp_array_describe(params, dbu, pdk_info, entry=entry)
