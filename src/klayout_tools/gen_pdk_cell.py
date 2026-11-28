"""``pdk_cell`` request validation, PDK-cell lookup and response description.

``pdk_cell`` places one of a PDK's *fixed*, pre-drawn GDS cells (bondpad, ESD,
...) as-drawn, for any PDK family and without importing a pcell library. It
generalizes :mod:`klayout_tools.gen_cap_vpp` (same array/pitch/port semantics,
shared validation, lookup and inspector, issue #8081); the cell is read from the
resolved PDK at request time and never vendored. The PCell lives in
:mod:`klayout_tools.gen_pcells.pdk_cell`.
"""

from __future__ import annotations

import glob
import os
from typing import Any

from .gen_base import GenError
from .gen_cap_vpp import (
    _cap_vpp_array_describe,
    _lookup_cell,
    _table_at,
    _validate_array_params,
)
from .gen_spec import _GeneratorSpec

_NAME = "pdk_cell"


def _gds_path(params: dict[str, Any], pdk_info: dict[str, Any]) -> str:
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


def _pdk_cell_validate(params: dict[str, Any]) -> None:
    if not params["library"]:
        raise GenError(f"generator '{_NAME}': params.library is required")
    if not params["pdk_cell"]:
        raise GenError(f"generator '{_NAME}': params.pdk_cell is required")
    _validate_array_params(_NAME, params)


def _lookup_in(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Lookup in the library GDS at an already-resolved ``path``."""
    return _lookup_cell(
        _NAME,
        _table_at(path, ""),
        params,
        scope=f" in library '{params['library']}'",
    )


def _pdk_cell_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    path = _gds_path(params, pdk_info)
    _lookup_in(path, params)
    return {"vpp_gds_path": path}


def _pdk_cell_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    entry = _lookup_in(_gds_path(params, pdk_info), params)
    return _cap_vpp_array_describe(params, dbu, pdk_info, entry=entry)


_PDK_CELL_SPEC = _GeneratorSpec(
    name=_NAME,
    summary=(
        "Row of any PDK fixed GDS cell (bondpad, ESD, ...) from a "
        "libs.ref/<library> GDS, instantiated as-drawn on params.pitch_um; "
        "ports are read from the cell's own pin labels and no pcell "
        "library is imported (issue #5290)."
    ),
    dbu=0.001,
    validate=_pdk_cell_validate,
    describe=_pdk_cell_describe,
    layer_params=_pdk_cell_layer_params,
)
