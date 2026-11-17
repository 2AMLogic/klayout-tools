"""Tech-LEF/PDK plumbing and layout-free helpers for ``klt lef-abstract``
(:mod:`klayout_tools.lef_abstract`).

Extracted as pure moves from ``run_lef_abstract``/``_resolve_obs`` (issue
#5714 review) so ``lef_abstract.py`` stays within its per-file size-ratchet
ceiling; every helper takes the verb's ``error`` exception class as a
parameter (mirroring :func:`klayout_tools._layout.load_layout`) so this
module never imports ``lef_abstract`` -- no import cycle.

- :func:`resolve_tech_context` resolves ``cell_library`` to a tech LEF
  exactly as ``klt synthesize``/``klt place-and-route`` do and reads what
  both abstract modes need from it: the header, the routing-layer subset,
  the PDK info, and the KLayout GDS<->LEF layer map.
- :func:`resolve_dbu_um` takes the database unit from the loaded layout
  when there is one, else -- the layout-free (``--no-layout``, issue
  #5714) path -- from the tech LEF's ``DATABASE MICRONS``.
- :func:`seed_outline_obs` seeds the layout-free black-box OBS: the whole
  outline blocks every routing layer, with the declared pin ports
  subtracted back out by the caller.
"""

from __future__ import annotations

from typing import Any

from .lef_header import read_lef_header
from .pdk import (
    PdkNotFoundError,
    find_pdk,
    lef_files,
    load_gds_to_lef_layer_map,
    resolve_klayout_layer_map,
)


def resolve_tech_context(
    cell_library: str,
    pdk_variant: str | None,
    pdk_root: str | None,
    error: type[Exception],
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[tuple[int, int], str],
]:
    """Resolve ``cell_library`` to its tech LEF and read what both abstract
    modes need from it.

    Returns ``(pdk_info, tech_lef, tech_header, routing_layers,
    gds_to_lef)``. Raises ``error`` for an unresolvable PDK, a missing tech
    LEF for ``cell_library``, or a missing KLayout layer map.
    """
    try:
        pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
    except PdkNotFoundError as exc:
        raise error(str(exc)) from exc

    lefs = lef_files(cell_library, variant=pdk_info["variant"], root=pdk_info["root"])
    tech_lef = lefs["tech_lef"]
    if tech_lef is None:
        raise error(
            f"tech LEF not found for standard-cell library '{cell_library}' "
            f"under resolved PDK install '{pdk_info['variant']}' at "
            f"'{pdk_info['root']}'"
        )
    tech_header = read_lef_header(tech_lef)
    routing_layers = {
        layer["name"]: layer
        for layer in tech_header["layers"]
        if layer["type"] == "ROUTING"
    }

    layer_map_path, _layer_map_resolution = resolve_klayout_layer_map(pdk_info)
    if layer_map_path is None:
        raise error(
            f"no KLayout GDS<->LEF layer map found for resolved PDK install "
            f"'{pdk_info['variant']}' -- cannot translate socket descriptor "
            "layers into LEF layer names"
        )
    gds_to_lef = load_gds_to_lef_layer_map(layer_map_path)
    return pdk_info, tech_lef, tech_header, routing_layers, gds_to_lef


def resolve_dbu_um(
    layout: Any,
    tech_header: dict[str, Any],
    tech_lef: str,
    error: type[Exception],
) -> float:
    """Database unit in micrometres: the loaded layout's own, else -- the
    layout-free (``--no-layout``, issue #5714) path -- the tech LEF's
    ``DATABASE MICRONS``. Raises ``error`` when neither is available."""
    if layout is not None:
        return layout.dbu
    # No layout to supply a database unit: take the tech LEF's.
    database_microns = tech_header.get("database_microns")
    if not database_microns:
        raise error(
            f"tech LEF '{tech_lef}' declares no DATABASE MICRONS -- cannot "
            "determine a database unit without a layout"
        )
    return 1.0 / database_microns


def seed_outline_obs(
    routing_layers: dict[str, dict[str, Any]],
    origin_x_dbu: int,
    origin_y_dbu: int,
    width_dbu: int,
    height_dbu: int,
) -> dict[str, Any]:
    """Layout-free black-box OBS seeding (issue #5714): the whole outline
    blocks every routing layer. Declared pin ports are subtracted back out
    by the caller, so a router is never told a declared connection point is
    also blocked. Returns ``{lef_layer: kdb.Region}`` in outline (absolute)
    dbu coordinates, mirroring the layout-driven seeding shape."""
    import klayout.db as kdb

    regions_by_layer: dict[str, Any] = {}
    for layer_name in routing_layers:
        regions_by_layer[layer_name] = kdb.Region(
            kdb.Box(
                origin_x_dbu,
                origin_y_dbu,
                origin_x_dbu + width_dbu,
                origin_y_dbu + height_dbu,
            )
        )
    return regions_by_layer
