"""Emit a LEF abstract (``MACRO`` block with ``PIN``/``OBS`` sections) from a
block's GDSII/OASIS layout plus its ``klt socket-check`` descriptor.

Pure library: :func:`run_lef_abstract` returns plain Python data (a ``dict``
of JSON-serialisable primitives) and never prints, mirroring
``socket_check.py``/``extract.py``. Serialisation and human-readable
formatting live in the CLI command module (``cli/lef_abstract_cmd.py``).

This is Capability A of [Epic #393](https://github.com/2AMLogic/klayout-tools/issues/393)
("mixed-signal as a first-class path"), issue #438: turn an analog block's
drawn layout plus its socket descriptor (``docs/schemas/socket.schema.json``,
the same contract ``klt socket-check`` already validates a layout against)
into a LEF abstract OpenROAD can place as a hard macro
(``klt place-and-route``, #425) alongside its own standard cells.

Reuses ``socket_check.py``'s own descriptor loader/validator
(``_load_descriptor``) rather than duplicating ~150 lines of shape
validation -- an intentional cross-module import of a private helper, the
same precedent ``gen_compose.py`` already set importing ``gen.py``'s
``_PDK_ROLE_LAYERS``/``_pdk_family`` (each verb module stays self-contained
for *its own* logic, but a shared, already-tested validator is reused rather
than copy-pasted).

Translation from socket descriptor to LEF ``MACRO``
------------------------------------------------------

- ``outline`` -> the macro's ``ORIGIN 0 0`` + ``SIZE`` (width/height of the
  outline box). Every emitted coordinate is shifted by ``(-x0, -y0)`` into
  this macro-local frame, matching real LEF macros (verified against a real
  sky130 standard-cell LEF and a real sky130 SRAM hard-macro LEF -- both
  declare ``ORIGIN 0.000 0.000`` and report absolute-looking coordinates that
  are actually already macro-local).
- ``pins[]`` -> one ``PIN <name> ... PORT ... END`` per declared pin.
  ``direction``/``use`` come from the descriptor when given, else a small
  documented heuristic (:func:`_classify_pin`) -- LEF has no way to *derive*
  signal direction from geometry alone. Port geometry is **real** drawn
  shapes when the layout actually has metal at the declared position
  (searched on the pin's own declared ``layer``, mirroring how ``klt
  socket-check`` already reads text labels off that same layer), falling
  back to a **synthesized** box (``width_um``/``height_um`` if declared,
  else the resolved tech LEF routing layer's own ``WIDTH``) when it does
  not -- each pin's response entry says which (``geometry_source``), never
  silently fabricating precision the layout doesn't actually have.
- Everything else drawn on a *routing-type* tech-LEF layer (per
  :mod:`klayout_tools.lef_header`) becomes ``OBS`` -- unioned per LEF layer,
  clipped to the outline, and with the declared pin ports subtracted back out
  (so a router is never told a declared connection point is also blocked).
  Non-routing layers (wells, diffusion, poly -- irrelevant to a digital
  router working only in metal) are not represented in the abstract at all,
  matching what an *abstract* view is for.
- ``reserved_layers``/``budgets`` are **not** translated -- they describe the
  integrator's own reservations and unverified interface budgets, not this
  block's own obstruction geometry; see ``docs/cli/socket-check.md``'s LEF
  translation section for the full field-by-field mapping this module
  implements.

Tech-LEF plumbing
-------------------

``cell_library`` resolves a tech LEF exactly as ``klt synthesize``/``klt
place-and-route`` already do (via :func:`klayout_tools.pdk.lef_files`) --
its header (per :mod:`klayout_tools.lef_header`, issue #438's other half)
supplies the routing-layer set OBS geometry is restricted to, and each
layer's ``WIDTH`` (the synthesized-pin-geometry fallback). The GDS
(layer, datatype) -> LEF layer *name* translation reuses the same
open_pdks-shipped KLayout layer-map file ``klt place-and-route``'s own
DEF->GDS merge already resolves (:func:`klayout_tools.pdk.find_pdk`'s
``assets.klayout`` + ``tech/<variant>.map``) -- never a second, invented
mapping.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._layout import load_layout
from ._provenance import build_provenance
from .lef_header import read_lef_header
from .pdk import PdkNotFoundError, find_pdk, lef_files
from .socket_check import SocketCheckError, _load_descriptor

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Name substrings (checked case-insensitively as whole ``_``-delimited
#: tokens, e.g. ``AVDD33`` matches ``VDD``, ``VD`` does not) that classify a
#: pin as a supply when the descriptor does not declare an explicit
#: ``use``/``direction`` -- this repo's own documented heuristic (there is
#: no mechanical way to derive this from geometry alone), not an
#: open_pdks/LEF-spec convention. See :func:`_classify_pin`.
_GROUND_TOKENS = ("VSS", "VGND", "GND", "VNB")
_POWER_TOKENS = ("VDD", "VPWR", "VCC", "VPB")

#: Fallback synthesized-pin square size (micrometres) when neither the
#: descriptor's ``width_um``/``height_um`` nor a resolved tech-LEF routing
#: layer ``WIDTH`` is available -- should essentially never trigger for a
#: real sky130 deck (every routing layer declares ``WIDTH``), kept only as a
#: last-resort so pin emission never raises for missing geometry data.
_DEFAULT_SYNTHESIZED_PIN_UM = 0.1

_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


class LefAbstractError(Exception):
    """Raised when a LEF abstract cannot be emitted: a missing/unreadable
    layout or socket descriptor, an unresolvable top cell, an unresolvable
    PDK/tech-LEF for ``cell_library``, or an unwritable output path.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching ``SocketCheckError``/``PlaceAndRouteError``.
    """


def run_lef_abstract(
    layout_path: str,
    socket_path: str,
    *,
    macro_name: str,
    cell_library: str,
    output_path: str,
    top: str | None = None,
    macro_class: str = "BLOCK",
    symmetry: list[str] | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Emit a LEF abstract for the block at ``layout_path`` per its socket
    descriptor at ``socket_path``, writing it to ``output_path``.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/socket-check.md``'s LEF translation section)::

        {
            "schema_version": 1,
            "file": <layout_path>,
            "socket": <socket_path>,
            "output": <output_path>,
            "macro_name": <macro_name>,
            "class": <macro_class>,
            "site": None,
            "symmetry": [...],
            "size_um": {"width": <float>, "height": <float>},
            "cell_library": <cell_library>,
            "tech_lef": <resolved tech LEF path>,
            "pins": [
                {"name", "direction", "use", "layer", "geometry_source",
                 "rects_um": [[x0, y0, x1, y1], ...]},
                ...
            ],
            "pin_count": <int>,
            "obs": [{"layer": str, "shape_count": int}, ...],
            "obs_shape_count": <int>,
            "warnings": [...],
            "provenance": {...},
        }

    Raises :class:`LefAbstractError` for a missing/unreadable layout or
    socket descriptor, an ambiguous/missing top cell, an unresolvable PDK or
    tech LEF for ``cell_library``, or a write failure.
    """
    layout = load_layout(layout_path, LefAbstractError)
    top_cell = _resolve_top_cell(layout, top, layout_path)

    try:
        descriptor = _load_descriptor(socket_path)
    except SocketCheckError as exc:
        raise LefAbstractError(str(exc)) from exc

    if not macro_name:
        raise LefAbstractError("macro_name must be a non-empty string")
    if symmetry is not None and not all(isinstance(s, str) and s for s in symmetry):
        raise LefAbstractError("symmetry must be a list of non-empty strings")

    try:
        pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
    except PdkNotFoundError as exc:
        raise LefAbstractError(str(exc)) from exc

    lefs = lef_files(cell_library, variant=pdk_info["variant"], root=pdk_info["root"])
    tech_lef = lefs["tech_lef"]
    if tech_lef is None:
        raise LefAbstractError(
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

    layer_map_path = _resolve_layer_map(pdk_info)
    if layer_map_path is None:
        raise LefAbstractError(
            f"no KLayout GDS<->LEF layer map found for resolved PDK install "
            f"'{pdk_info['variant']}' -- cannot translate socket descriptor "
            "layers into LEF layer names"
        )
    gds_to_lef = _load_gds_to_lef_layer_map(layer_map_path)

    dbu_um = layout.dbu
    outline = descriptor["outline"]
    origin_x_dbu = round(outline["x0"] / dbu_um)
    origin_y_dbu = round(outline["y0"] / dbu_um)
    width_um = outline["x1"] - outline["x0"]
    height_um = outline["y1"] - outline["y0"]

    warnings: list[str] = []
    pins_report, pin_boxes_by_layer = _resolve_pins(
        layout=layout,
        top_cell=top_cell,
        pins=descriptor["pins"],
        gds_to_lef=gds_to_lef,
        routing_layers=routing_layers,
        origin_x_dbu=origin_x_dbu,
        origin_y_dbu=origin_y_dbu,
        dbu_um=dbu_um,
        warnings=warnings,
    )

    obs_report = _resolve_obs(
        layout=layout,
        top_cell=top_cell,
        gds_to_lef=gds_to_lef,
        routing_layers=routing_layers,
        pin_boxes_by_layer=pin_boxes_by_layer,
        origin_x_dbu=origin_x_dbu,
        origin_y_dbu=origin_y_dbu,
        width_dbu=round(width_um / dbu_um),
        height_dbu=round(height_um / dbu_um),
        dbu_um=dbu_um,
    )

    _write_lef(
        output_path=output_path,
        macro_name=macro_name,
        macro_class=macro_class,
        symmetry=symmetry or [],
        width_um=width_um,
        height_um=height_um,
        pins=pins_report,
        obs=obs_report,
    )

    provenance = build_provenance(
        deck_name=cell_library,
        deck_path=tech_lef,
        pdk=pdk_info,
        input_path=layout_path,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "file": layout_path,
        "socket": socket_path,
        "output": output_path,
        "macro_name": macro_name,
        "class": macro_class,
        "site": None,
        "symmetry": symmetry or [],
        "size_um": {"width": round(width_um, 6), "height": round(height_um, 6)},
        "cell_library": cell_library,
        "tech_lef": tech_lef,
        "pins": [
            {
                "name": pin["name"],
                "direction": pin["direction"],
                "use": pin["use"],
                "layer": pin["lef_layer"],
                "geometry_source": pin["geometry_source"],
                "rects_um": pin["rects_um"],
            }
            for pin in pins_report
        ],
        "pin_count": len(pins_report),
        "obs": [
            {"layer": layer_name, "shape_count": len(shapes)}
            for layer_name, shapes in obs_report
        ],
        "obs_shape_count": sum(len(shapes) for _layer_name, shapes in obs_report),
        "warnings": warnings,
        "provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# top-cell resolution (mirrors extract.py's own _resolve_top_cell)
# --------------------------------------------------------------------------- #


def _resolve_top_cell(layout: Any, top: str | None, path: str) -> Any:
    if top is not None:
        cell = layout.cell(top)
        if cell is None:
            raise LefAbstractError(f"cell '{top}' not found in '{path}'")
        return cell

    top_cells = list(layout.top_cells())
    if len(top_cells) == 0:
        raise LefAbstractError(f"'{path}' has no top cell")
    if len(top_cells) > 1:
        names = ", ".join(sorted(cell.name for cell in top_cells))
        raise LefAbstractError(
            f"'{path}' has {len(top_cells)} top cells ({names}); "
            "pass --top to select one"
        )
    return top_cells[0]


# --------------------------------------------------------------------------- #
# pin direction/use heuristic
# --------------------------------------------------------------------------- #


def _classify_pin(name: str) -> tuple[str, str]:
    """``(direction, use)`` heuristic for a pin the descriptor does not
    explicitly classify (see module docstring's "Translation from socket
    descriptor to LEF MACRO" section). Name tokens are matched
    case-insensitively as whole alphanumeric runs (``AVDD33`` matches
    ``VDD`` via its ``AVDD`` token containing it; ``VD`` alone does not
    match anything) against :data:`_GROUND_TOKENS`/:data:`_POWER_TOKENS`.
    Falls back to ``("INOUT", "ANALOG")`` -- LEF has no way to derive true
    signal direction from geometry alone, and ``ANALOG`` is the LEF-spec
    ``USE`` value for exactly this class of pin (this module's macros are
    analog blocks, per Epic #393's own scope).
    """
    tokens = [tok.upper() for tok in _NAME_TOKEN_RE.findall(name)]
    for token in tokens:
        if any(marker in token for marker in _GROUND_TOKENS):
            return "INOUT", "GROUND"
        if any(marker in token for marker in _POWER_TOKENS):
            return "INOUT", "POWER"
    return "INOUT", "ANALOG"


# --------------------------------------------------------------------------- #
# GDS (layer, datatype) -> LEF layer name (open_pdks KLayout map file)
# --------------------------------------------------------------------------- #


def _resolve_layer_map(pdk_info: dict[str, Any]) -> str | None:
    """The same open_pdks KLayout LEF/DEF layer-map file
    ``place_and_route.py``'s own DEF->GDS merge resolves
    (``libs.tech/klayout/tech/<variant>.map``) -- this module's own copy of
    that resolution (each verb module in this repo is self-contained,
    matching the existing precedent noted in ``place_and_route.py``)."""
    klayout_dir = pdk_info["assets"].get("klayout")
    if klayout_dir is None:
        return None
    candidate = os.path.join(klayout_dir, "tech", f"{pdk_info['variant']}.map")
    return candidate if os.path.isfile(candidate) else None


def _load_gds_to_lef_layer_map(map_path: str) -> dict[tuple[int, int], str]:
    """Parse an open_pdks KLayout ``.map`` file into a ``{(gds_layer,
    gds_datatype): lef_layer_name}`` dict.

    File shape (whitespace-delimited, one entry per line):
    ``<lef_layer_name> <comma-separated purposes> <gds_layer> <gds_datatype>``.
    ``NAME``/``DIEAREA`` pseudo-entries (annotation purposes, not a real
    drawn layer) are skipped. A ``(layer, datatype)`` pair with more than
    one purpose entry for the same LEF layer name (e.g. sky130's ``met1``
    ``NET``/``LEFPIN`` purposes at different datatypes) simply contributes
    multiple dict entries mapping to the same name -- never a conflict,
    since distinct purposes always use distinct datatypes in a real
    open_pdks map file.
    """
    mapping: dict[tuple[int, int], str] = {}
    with open(map_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) < 4:
                continue
            lef_name = parts[0]
            if lef_name in ("NAME", "DIEAREA"):
                continue
            try:
                gds_layer = int(parts[-2])
                gds_datatype = int(parts[-1])
            except ValueError:
                continue
            mapping[(gds_layer, gds_datatype)] = lef_name
    return mapping


# --------------------------------------------------------------------------- #
# pins
# --------------------------------------------------------------------------- #


def _resolve_pins(
    *,
    layout: Any,
    top_cell: Any,
    pins: list[dict[str, Any]],
    gds_to_lef: dict[tuple[int, int], str],
    routing_layers: dict[str, dict[str, Any]],
    origin_x_dbu: int,
    origin_y_dbu: int,
    dbu_um: float,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], dict[str, list[tuple[int, int, int, int]]]]:
    """Resolve each descriptor pin's LEF layer, direction/use, and port
    geometry. Returns ``(pins_report, pin_boxes_by_lef_layer_dbu)`` -- the
    second value (macro-local dbu boxes, keyed by LEF layer name) is reused
    by :func:`_resolve_obs` to subtract declared pin geometry back out of
    the obstruction region.
    """
    import klayout.db as kdb

    pins_report: list[dict[str, Any]] = []
    pin_boxes_by_layer: dict[str, list[tuple[int, int, int, int]]] = {}

    for pin in pins:
        layer_tuple = pin["layer"]
        lef_layer = gds_to_lef.get(layer_tuple)
        if lef_layer is None or lef_layer not in routing_layers:
            warnings.append(
                f"pin '{pin['name']}': layer {layer_tuple[0]}/{layer_tuple[1]} does "
                "not resolve to a known routing LEF layer -- emitted with no PORT "
                "geometry"
            )
            direction, use = _pin_classification(pin)
            pins_report.append(
                {
                    "name": pin["name"],
                    "direction": direction,
                    "use": use,
                    "lef_layer": None,
                    "geometry_source": "none",
                    "rects_um": [],
                    "rects_dbu": [],
                }
            )
            continue

        layer_index = layout.find_layer(*layer_tuple)
        x_dbu = round(pin["x"] / dbu_um)
        y_dbu = round(pin["y"] / dbu_um)

        drawn_boxes: list[tuple[int, int, int, int]] = []
        if layer_index is not None:
            point = kdb.Point(x_dbu, y_dbu)
            iterator = top_cell.begin_shapes_rec(layer_index)
            while not iterator.at_end():
                shape = iterator.shape()
                polygon = shape.polygon
                if polygon is not None:
                    bbox = polygon.transformed(iterator.trans()).bbox()
                    if bbox.contains(point):
                        drawn_boxes.append(
                            (bbox.left, bbox.bottom, bbox.right, bbox.top)
                        )
                iterator.next()

        if drawn_boxes:
            geometry_source = "drawn"
            boxes_dbu = drawn_boxes
        else:
            geometry_source = "synthesized"
            boxes_dbu = [
                _synthesized_pin_box(
                    pin, lef_layer, routing_layers, x_dbu, y_dbu, dbu_um
                )
            ]
            warnings.append(
                f"pin '{pin['name']}': no drawn geometry found on layer "
                f"{layer_tuple[0]}/{layer_tuple[1]} at ({pin['x']}, {pin['y']}) um -- "
                "synthesized a placeholder PORT rectangle"
            )

        local_boxes = [
            (
                x0 - origin_x_dbu,
                y0 - origin_y_dbu,
                x1 - origin_x_dbu,
                y1 - origin_y_dbu,
            )
            for x0, y0, x1, y1 in boxes_dbu
        ]
        pin_boxes_by_layer.setdefault(lef_layer, []).extend(local_boxes)

        direction, use = _pin_classification(pin)
        pins_report.append(
            {
                "name": pin["name"],
                "direction": direction,
                "use": use,
                "lef_layer": lef_layer,
                "geometry_source": geometry_source,
                "rects_um": [
                    [round(v * dbu_um, 6) for v in box] for box in local_boxes
                ],
                "rects_dbu": local_boxes,
            }
        )

    pins_report.sort(key=lambda p: p["name"])
    return pins_report, pin_boxes_by_layer


def _pin_classification(pin: dict[str, Any]) -> tuple[str, str]:
    direction = pin.get("direction")
    use = pin.get("use")
    if direction is not None and use is not None:
        return direction, use
    heuristic_direction, heuristic_use = _classify_pin(pin["name"])
    return direction or heuristic_direction, use or heuristic_use


def _synthesized_pin_box(
    pin: dict[str, Any],
    lef_layer: str,
    routing_layers: dict[str, dict[str, Any]],
    x_dbu: int,
    y_dbu: int,
    dbu_um: float,
) -> tuple[int, int, int, int]:
    width_um = pin.get("width_um")
    height_um = pin.get("height_um")
    if width_um is None or height_um is None:
        layer_width_um = routing_layers[lef_layer].get("width_um")
        fallback_um = layer_width_um or _DEFAULT_SYNTHESIZED_PIN_UM
        width_um = width_um if width_um is not None else fallback_um
        height_um = height_um if height_um is not None else fallback_um

    half_w_dbu = max(1, round((width_um / 2) / dbu_um))
    half_h_dbu = max(1, round((height_um / 2) / dbu_um))
    return (
        x_dbu - half_w_dbu,
        y_dbu - half_h_dbu,
        x_dbu + half_w_dbu,
        y_dbu + half_h_dbu,
    )


# --------------------------------------------------------------------------- #
# OBS
# --------------------------------------------------------------------------- #


def _resolve_obs(
    *,
    layout: Any,
    top_cell: Any,
    gds_to_lef: dict[tuple[int, int], str],
    routing_layers: dict[str, dict[str, Any]],
    pin_boxes_by_layer: dict[str, list[tuple[int, int, int, int]]],
    origin_x_dbu: int,
    origin_y_dbu: int,
    width_dbu: int,
    height_dbu: int,
    dbu_um: float,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Build the obstruction geometry: every shape drawn on a GDS layer that
    maps to a routing-type LEF layer, unioned per LEF layer, shifted into
    macro-local coordinates, clipped to the outline, and with the declared
    pin ports subtracted back out.

    Returns ``[(lef_layer_name, [shape, ...]), ...]``, sorted by layer name,
    skipping layers with no resulting geometry. Each ``shape`` is already
    converted to **micrometres** (macro-local) so :func:`_write_lef` never
    touches ``kdb`` objects: ``{"kind": "rect", "rect_um": [x0, y0, x1, y1]}``
    for an axis-aligned box (the common case), or ``{"kind": "polygon",
    "points_um": [[x, y], ...]}`` for anything else (e.g. an L-shaped region
    left after subtracting a pin's port) -- LEF's ``OBS``/``PORT`` sections
    accept both a ``RECT`` and a ``POLYGON`` statement.
    """
    import klayout.db as kdb

    regions_by_layer: dict[str, Any] = {}
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        lef_layer = gds_to_lef.get((info.layer, info.datatype))
        if lef_layer is None or lef_layer not in routing_layers:
            continue
        region = kdb.Region(top_cell.begin_shapes_rec(layer_index))
        if region.is_empty():
            continue
        regions_by_layer.setdefault(lef_layer, kdb.Region()).__iadd__(region)

    outline_box_dbu = kdb.Box(
        origin_x_dbu, origin_y_dbu, origin_x_dbu + width_dbu, origin_y_dbu + height_dbu
    )
    local_outline_box = kdb.Box(0, 0, width_dbu, height_dbu)
    translation = kdb.Trans(kdb.Vector(-origin_x_dbu, -origin_y_dbu))

    result: list[tuple[str, list[dict[str, Any]]]] = []
    for lef_layer in sorted(regions_by_layer):
        region = regions_by_layer[lef_layer]
        region.merge()
        region = region & kdb.Region(outline_box_dbu)
        region = region.transformed(translation)

        pin_boxes = pin_boxes_by_layer.get(lef_layer)
        if pin_boxes:
            pin_region = kdb.Region([kdb.Box(*box) for box in pin_boxes])
            region = region - pin_region
        region.merge()
        region &= kdb.Region(local_outline_box)

        shapes = [_polygon_to_um_shape(poly, dbu_um) for poly in region.each_merged()]
        if shapes:
            result.append((lef_layer, shapes))

    return result


def _polygon_to_um_shape(polygon: Any, dbu_um: float) -> dict[str, Any]:
    """Convert one merged (dbu-space) ``kdb.Polygon`` into a plain,
    um-scaled shape dict -- ``rect`` for a simple axis-aligned box, else
    ``polygon`` (using :meth:`kdb.Polygon.to_simple_polygon` to resolve any
    hole left by a pin-port subtraction into a single, self-touching point
    list -- an acceptable simplification for an *abstract* view, see this
    module's docstring)."""
    if polygon.is_box():
        bbox = polygon.bbox()
        return {
            "kind": "rect",
            "rect_um": [
                round(bbox.left * dbu_um, 6),
                round(bbox.bottom * dbu_um, 6),
                round(bbox.right * dbu_um, 6),
                round(bbox.top * dbu_um, 6),
            ],
        }
    simple = polygon.to_simple_polygon()
    return {
        "kind": "polygon",
        "points_um": [
            [round(pt.x * dbu_um, 6), round(pt.y * dbu_um, 6)]
            for pt in simple.each_point_hull()
        ],
    }


# --------------------------------------------------------------------------- #
# LEF text emission
# --------------------------------------------------------------------------- #


def _rect_line(x0: float, y0: float, x1: float, y1: float) -> str:
    coords = " ".join(_fmt_num(v) for v in (x0, y0, x1, y1))
    return f"        RECT {coords} ;"


def _fmt_num(value: float) -> str:
    """Format a micrometre coordinate for LEF text: fixed 4-decimal
    precision -- e.g. ``1.5`` -> ``"1.5000"``, ``0.375`` -> ``"0.3750"``.
    A trailing run of zeros is harmless LEF syntax; fixed precision (rather
    than trimming) keeps every emitted coordinate the same width and avoids
    ever emitting a bare integer token some LEF readers are stricter about."""
    return f"{value:.4f}"


def _write_lef(
    *,
    output_path: str,
    macro_name: str,
    macro_class: str,
    symmetry: list[str],
    width_um: float,
    height_um: float,
    pins: list[dict[str, Any]],
    obs: list[tuple[str, list[dict[str, Any]]]],
) -> None:
    lines: list[str] = [
        "VERSION 5.7 ;",
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        "",
        f"MACRO {macro_name}",
        f"  CLASS {macro_class} ;",
        f"  FOREIGN {macro_name} 0 0 ;",
        "  ORIGIN 0 0 ;",
        f"  SIZE {_fmt_num(width_um)} BY {_fmt_num(height_um)} ;",
    ]
    if symmetry:
        lines.append(f"  SYMMETRY {' '.join(symmetry)} ;")

    for pin in pins:
        lines.append(f"  PIN {pin['name']}")
        lines.append(f"    DIRECTION {pin['direction']} ;")
        lines.append(f"    USE {pin['use']} ;")
        if pin["rects_um"]:
            lines.append("    PORT")
            lef_layer = pin["lef_layer"]
            lines.append(f"      LAYER {lef_layer} ;")
            for x0, y0, x1, y1 in pin["rects_um"]:
                lines.append(_rect_line(x0, y0, x1, y1))
            lines.append("    END")
        lines.append(f"  END {pin['name']}")

    for lef_layer, shapes in obs:
        if not shapes:
            continue
        lines.append("  OBS")
        lines.append(f"    LAYER {lef_layer} ;")
        for shape in shapes:
            if shape["kind"] == "rect":
                lines.append(_rect_line(*shape["rect_um"]))
            else:
                coords = " ".join(
                    f"{_fmt_num(x)} {_fmt_num(y)}" for x, y in shape["points_um"]
                )
                lines.append(f"        POLYGON {coords} ;")
        lines.append("  END")

    lines.append(f"END {macro_name}")
    lines.append("END LIBRARY")

    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise LefAbstractError(
            f"could not write LEF abstract '{output_path}': {exc}"
        ) from exc


__all__ = [
    "SCHEMA_VERSION",
    "LefAbstractError",
    "run_lef_abstract",
]
