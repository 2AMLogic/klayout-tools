"""SiGe HBT (``npn13G2*``) support for ``klt gen bjt_array`` on ``sg13g2``
(issue #4850).

Unlike the base-layer ``bjt_array`` of the other PDK families, an sg13g2
HBT is **not** drawn from scratch: the PDK ships the cells ``npn13G2``,
``npn13G2L`` and ``npn13G2V`` in ``libs.ref/sg13g2_pr/gds/sg13g2_pr.gds``,
and a from-scratch emitter/SiGe-base/collector drawing could not be shown to
match the PDK-authored geometry. This module therefore reads the named cell
out of that stream and instantiates it on a ``rows`` x ``cols`` grid. No
PyCell shim is needed, and nothing from the PDK is vendored -- the GDS is
read from the user's own install at generation time.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import klayout.db as kdb

#: Request flavor (case-insensitive) -> the cell name in ``sg13g2_pr.gds``.
HBT_FLAVOR_CELLS: dict[str, str] = {
    "npn13G2": "npn13G2",
    "npn13G2l": "npn13G2L",
    "npn13G2v": "npn13G2V",
}
DEFAULT_HBT_FLAVOR = "npn13G2"

#: Gap (um) left between the bounding boxes of adjacent HBT cells.
HBT_UNIT_GAP_UM = 2.0

_HBT_GDS_RELPATH = ("sg13g2_pr", "gds", "sg13g2_pr.gds")

#: Candidate layers (GDS layer, datatype) a pin label can sit over, in
#: priority order: Metal1, Activ, GatPoly.
_PIN_CANDIDATE_LAYERS = ((8, 0), (1, 0), (5, 0))
_TEXT_LAYER = (63, 0)


def resolve_flavor(flavor: str) -> str:
    """Return the canonical request flavor for ``flavor`` ('' = default)."""
    from .gen import GenError

    if not flavor:
        return DEFAULT_HBT_FLAVOR
    for key in HBT_FLAVOR_CELLS:
        if key.lower() == flavor.lower():
            return key
    raise GenError(
        "generator 'bjt_array': params.flavor must be one of "
        f"{', '.join(HBT_FLAVOR_CELLS)} on PDK family 'sg13g2' (got {flavor!r})"
    )


def hbt_gds_path(pdk_info: dict[str, Any]) -> str:
    """Absolute path of the PDK's ``sg13g2_pr.gds`` (may not exist)."""
    libs_ref = (pdk_info.get("assets") or {}).get("libs_ref")
    if not libs_ref:
        libs_ref = os.path.join(
            str(pdk_info.get("root") or ""),
            str(pdk_info.get("variant") or ""),
            "libs.ref",
        )
    return os.path.join(libs_ref, *_HBT_GDS_RELPATH)


def load_hbt(
    pdk_info: dict[str, Any], flavor: str
) -> tuple[kdb.Layout, kdb.Cell, dict[str, Any]]:
    """Read the PDK's HBT cell for ``flavor``.

    Returns ``(source_layout, source_cell, info)`` where ``info`` holds the
    canonical flavor, cell name, GDS path and the pin labels (``E``/``B``/
    ``C``) found in the cell. Raises :class:`GenError` -- never returns an
    empty cell -- when the stream or the cell is missing/unreadable.
    """
    import klayout.db as kdb

    from .gen import GenError

    flavor = resolve_flavor(flavor)
    cell_name = HBT_FLAVOR_CELLS[flavor]
    path = hbt_gds_path(pdk_info)
    if not os.path.isfile(path):
        raise GenError(
            f"generator 'bjt_array': PDK HBT library not found: {path} -- "
            f"the resolved ihp-sg13g2 install must ship {cell_name!r} in "
            "libs.ref/sg13g2_pr/gds/sg13g2_pr.gds (check `klt pdk find`)"
        )
    src = kdb.Layout()
    try:
        src.read(path)
    except Exception as exc:  # noqa: BLE001 - KLayout raises bare RuntimeError
        raise GenError(
            f"generator 'bjt_array': cannot read PDK HBT library {path}: {exc}"
        ) from exc
    cell = src.cell(cell_name)
    if cell is None:
        available = sorted(c.name for c in src.each_cell() if "npn" in c.name.lower())
        raise GenError(
            f"generator 'bjt_array': cell {cell_name!r} (flavor {flavor!r}) is "
            f"absent from {path} -- HBT cells present: "
            f"{', '.join(available) or 'none'}"
        )
    if cell.bbox().empty():
        raise GenError(f"generator 'bjt_array': cell {cell_name!r} in {path} is empty")
    info = {
        "flavor": flavor,
        "cell": cell_name,
        "source_gds": path,
        "pins": _pins(src, cell),
    }
    return src, cell, info


def _pins(src: kdb.Layout, cell: kdb.Cell) -> dict[str, dict[str, Any]]:
    """E/B/C pin labels (um, in the source cell's frame) with the layer each
    sits over."""
    import klayout.db as kdb

    dbu = src.dbu
    text_li = src.find_layer(*_TEXT_LAYER)
    pins: dict[str, dict[str, Any]] = {}
    if text_li is None:
        return pins
    cand = [(p, src.find_layer(*p)) for p in _PIN_CANDIDATE_LAYERS]
    for shape in cell.begin_shapes_rec(text_li):
        s = shape.shape()
        if not s.is_text() or s.text_string not in ("E", "B", "C"):
            continue
        if s.text_string in pins:
            continue
        pos = shape.trans() * kdb.Point(s.text_pos.x, s.text_pos.y)
        layer = _TEXT_LAYER
        for pair, li in cand:
            if li is None:
                continue
            region = kdb.Region(cell.begin_shapes_rec(li))
            if not (
                region & kdb.Region(kdb.Box(pos.x, pos.y, pos.x, pos.y).enlarged(1, 1))
            ).is_empty():
                layer = pair
                break
        pins[s.text_string] = {
            "x_um": pos.x * dbu,
            "y_um": pos.y * dbu,
            "layer": layer,
        }
    return pins


def hbt_pitch_um(src: kdb.Layout, cell: kdb.Cell) -> tuple[float, float]:
    box = cell.bbox()
    return (
        box.width() * src.dbu + HBT_UNIT_GAP_UM,
        box.height() * src.dbu + HBT_UNIT_GAP_UM,
    )


def produce_hbt_array(
    layout: kdb.Layout,
    top: kdb.Cell,
    params: dict[str, Any],
    pdk_info: dict[str, Any],
) -> dict[str, Any]:
    """Copy the PDK HBT cell into ``layout`` and place a ``rows`` x ``cols``
    grid of it in ``top``. Returns the :func:`load_hbt` info dict."""
    import klayout.db as kdb

    src, cell, info = load_hbt(pdk_info, params.get("flavor", ""))
    unit = layout.create_cell(info["cell"])
    unit.copy_tree(cell)
    box = unit.bbox()
    px, py = hbt_pitch_um(src, cell)
    dbu = layout.dbu
    for r in range(params["rows"]):
        for c in range(params["cols"]):
            dx = round(c * px / dbu) - box.left
            dy = round(r * py / dbu) - box.bottom
            top.insert(kdb.CellInstArray(unit.cell_index(), kdb.Trans(dx, dy)))
    return info


def describe_hbt_array(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    """``_bjt_array_describe`` counterpart for the HBT path."""
    src, cell, info = load_hbt(pdk_info, params.get("flavor", ""))
    box = cell.bbox()
    px, py = hbt_pitch_um(src, cell)
    count = params["rows"] * params["cols"]
    ports = []
    idx = 0
    for r in range(params["rows"]):
        for c in range(params["cols"]):
            idx += 1
            ox = c * px - box.left * src.dbu
            oy = r * py - box.bottom * src.dbu
            for term in ("E", "B", "C"):
                pin = info["pins"].get(term)
                if pin is None:
                    continue
                layer, dt = pin["layer"]
                ports.append(
                    {
                        "name": f"Q{idx}_{term}",
                        "net": None,
                        "layer": {"layer": layer, "datatype": dt, "name": None},
                        "x_um": ox + pin["x_um"],
                        "y_um": oy + pin["y_um"],
                        "width_um": 0.16,
                        "direction_deg": 90,
                    }
                )
    ignored = [
        n
        for n in ("emitter_um", "topology", "dummy", "add_collector_ring")
        if n in params
    ]
    notes = [
        f"sg13g2 HBT: {count} instance(s) of the PDK cell {info['cell']!r} read "
        f"from {info['source_gds']}; params {', '.join(ignored)} do not apply "
        "to the PDK-authored HBT and are ignored (no ring, dummies or "
        "common-centroid pairing is drawn)",
        "extraction/LVS recognition of npn13G2 is not claimed by this "
        "generator (tracked by #4397)",
    ]
    missing = [t for t in ("E", "B", "C") if t not in info["pins"]]
    if missing:
        notes.append(f"pin label(s) {', '.join(missing)} not found in the PDK cell")
    return {
        "device_count": count,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": HBT_UNIT_GAP_UM,
            "matched_group_id": f"bjt_array:{params['rows']}x{params['cols']}:"
            f"{info['flavor']}",
            "snapped_to_grid": False,
            "notes": notes,
        },
        "warnings": [],
        "hbt": {
            "flavor": info["flavor"],
            "cell": info["cell"],
            "source_gds": info["source_gds"],
            "count": count,
        },
    }
