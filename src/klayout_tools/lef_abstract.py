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
  shapes when the layout actually has metal at the declared position,
  searched across **every** GDS ``(layer, datatype)`` pair that the
  resolved layer map maps to the same LEF layer *name* as the pin's own
  declared ``layer`` -- not just that single declared datatype. This
  matters because ``klt socket-check`` reads text labels off the *exact*
  declared ``layer`` (a PDK-specific pin/label datatype, e.g. gf180mcu's
  ``(34, 10)``), which on some PDKs is a different datatype than the one
  the actual metal is drawn on (e.g. gf180mcu's ``(34, 0)``) -- both
  resolve to the same LEF layer (e.g. ``Metal1``), so searching every
  sibling datatype (mirroring :func:`_resolve_obs`'s existing per-LEF-layer
  union across datatypes) finds the real geometry a single-datatype search
  would silently miss (issue #1614). Falls back to a **synthesized** box
  (``width_um``/``height_um`` if declared, else the resolved tech LEF
  routing layer's own ``WIDTH``) only when no datatype sharing that LEF
  layer name has drawn geometry at the declared position -- each pin's
  response entry says which (``geometry_source``), never silently
  fabricating precision the layout doesn't actually have.
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
from typing import Any

from ._layout import load_layout, resolve_top_cell
from ._provenance import build_provenance
from .lef_header import read_lef_header
from .pdk import PdkNotFoundError, find_pdk, lef_files
from .socket_check import (
    GROUND_NAME_TOKENS,
    POWER_NAME_TOKENS,
    SocketCheckError,
    _load_descriptor,
    classify_pin_by_name,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: The supply-pin name heuristic (``_GROUND_TOKENS``/``_POWER_TOKENS``) and
#: :func:`_classify_pin` live in :mod:`klayout_tools.socket_check` since
#: issue #2839 (as :func:`~klayout_tools.socket_check.classify_pin_by_name`),
#: so `klt socket-check`'s ``power_ports`` check and this command's emitted
#: ``PIN USE`` share one classification and can never disagree.
_GROUND_TOKENS = GROUND_NAME_TOKENS
_POWER_TOKENS = POWER_NAME_TOKENS

#: Fallback synthesized-pin square size (micrometres) when neither the
#: descriptor's ``width_um``/``height_um`` nor a resolved tech-LEF routing
#: layer ``WIDTH`` is available -- should essentially never trigger for a
#: real sky130 deck (every routing layer declares ``WIDTH``), kept only as a
#: last-resort so pin emission never raises for missing geometry data.
_DEFAULT_SYNTHESIZED_PIN_UM = 0.1


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
            "unroutable_pins": [
                {"name": <str>, "layer": [<gds_layer>, <gds_datatype>]}, ...
            ],
            "obs": [{"layer": str, "shape_count": int}, ...],
            "obs_shape_count": <int>,
            "warnings": [...],
            "self_check": {
                "status": "pass" | "fail",
                "findings": [{"code", "message", "line", "macro", "pin"}, ...],
                "missing_macros": [...],
                "unexpected_macros": [...],
            },
            "power_pins": {...},   # issue #2839, see check_power_pins
            "provenance": {...},
        }

    ``power_pins`` (issue #2839) is :func:`check_power_pins`'s supply-pin
    verdict over the same read-back bytes. It never changes the exit code
    unless the CLI's ``--require-power-pins`` asks it to.

    ``self_check`` (issue #2769) is :func:`check_lef_structure` run on the
    file read back from ``output_path`` after writing -- the on-disk bytes,
    not this function's in-memory pin data -- against the expected macro
    ``{macro_name}`` and the descriptor's own declared pins. A ``"fail"``
    still returns the report (the file is left on disk for diagnosis); a
    caller must gate consumption of the LEF on ``self_check.status``. A
    structural pass says nothing about routability or P&R acceptance, and a
    PORT-less (``geometry_source: "none"``) or synthesized pin is not a
    structural failure.

    ``unroutable_pins`` (issue #464) is a programmatically-checkable echo of
    every ``pins[]`` entry emitted with ``geometry_source: "none"`` -- a pin
    whose declared ``layer`` did not resolve to a ``TYPE ROUTING`` tech-LEF
    layer, so no ``PORT`` at all was written for it (see the "Pins" section
    of this module's own docstring). ``[]`` on a run with no such pins. This
    is purely a *promotion* of information already present per-pin in
    ``pins[].geometry_source``/``warnings[]`` -- a caller composing this
    command's output into ``klt place-and-route``'s own ``request.macros``
    field can check this array directly instead of grepping ``warnings[]``
    strings, or rely on ``klt place-and-route`` itself, which independently
    cross-checks the emitted LEF's own ``PIN`` blocks against the netlist's
    actual wiring before invoking OpenROAD (see that command's "Hard-macro
    placement" docs section) -- either signal reflects the same underlying
    condition.

    Raises :class:`LefAbstractError` for a missing/unreadable layout or
    socket descriptor, an ambiguous/missing top cell, an unresolvable PDK or
    tech LEF for ``cell_library``, a write failure, or a failure to read the
    written file back for its self-check.
    """
    layout = load_layout(layout_path, LefAbstractError)
    top_cell = resolve_top_cell(layout, top, LefAbstractError, path=layout_path)

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

    layer_map_path, _layer_map_resolution = _resolve_layer_map(pdk_info)
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

    # Issue #2769: validate the bytes actually written, not `pins_report`.
    # Expected pins come from the original descriptor, so a pin the writer
    # dropped (or put in the wrong macro) cannot pass by construction.
    lef_text = _read_back_lef(output_path)
    self_check = check_lef_structure(
        lef_text,
        {macro_name: [pin["name"] for pin in descriptor["pins"]]},
    )
    # Issue #2839: the supply-pin verdict, also graded on the bytes on disk.
    power_pins = check_power_pins(pins_report, lef_text)

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
                # Issue #2839: the descriptor's declared GDS (layer,
                # datatype), so a consumer can match this PIN against the
                # same pin's `klt socket-check` port facts.
                "gds_layer": pin["gds_layer"],
                "geometry_source": pin["geometry_source"],
                "rects_um": pin["rects_um"],
            }
            for pin in pins_report
        ],
        "pin_count": len(pins_report),
        "unroutable_pins": _unroutable_pins(pins_report, descriptor["pins"]),
        "obs": [
            {"layer": layer_name, "shape_count": len(shapes)}
            for layer_name, shapes in obs_report
        ],
        "obs_shape_count": sum(len(shapes) for _layer_name, shapes in obs_report),
        "warnings": warnings,
        "self_check": self_check,
        "power_pins": power_pins,
        "provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# pin direction/use heuristic
# --------------------------------------------------------------------------- #


def _classify_pin(name: str) -> tuple[str, str]:
    """``(direction, use)`` heuristic for a pin the descriptor does not
    explicitly classify -- :func:`~klayout_tools.socket_check.classify_pin_by_name`
    (moved there by issue #2839; see that function for the rule)."""
    return classify_pin_by_name(name)


# --------------------------------------------------------------------------- #
# GDS (layer, datatype) -> LEF layer name (open_pdks KLayout map file)
# --------------------------------------------------------------------------- #


def _resolve_layer_map(pdk_info: dict[str, Any]) -> tuple[str | None, str]:
    """The same open_pdks KLayout LEF/DEF layer-map file
    ``place_and_route.py``'s own DEF->GDS merge resolves
    (``libs.tech/klayout/tech/<variant>.map``) -- this module's own copy of
    that resolution (each verb module in this repo is self-contained,
    matching the existing precedent noted in ``place_and_route.py``).

    Prefers the variant-named file (``<variant>.map``, ``resolution=
    "exact"``); falls back to a family-level file (``<family>.map``,
    ``resolution="family"``) when the variant-named file is absent -- some
    open_pdks families (gf180mcu, unlike sky130) ship a single
    ``klayout.tech`` map file shared across every variant rather than one
    per variant (issue #1029). Returns ``(None, "none")`` when neither
    exists (or the variant ships no ``klayout`` asset at all)."""
    klayout_dir = pdk_info["assets"].get("klayout")
    if klayout_dir is None:
        return None, "none"
    tech_dir = os.path.join(klayout_dir, "tech")
    variant = pdk_info["variant"]
    exact = os.path.join(tech_dir, f"{variant}.map")
    if os.path.isfile(exact):
        return exact, "exact"

    family = variant
    if len(family) > 1 and family[-1].isupper():
        family = family[:-1]
    # IHP-Open-PDK (issue #2444): the install variant is ``ihp-sg13g2`` but
    # the shipped map is named after the process, ``sg13g2.map`` -- without
    # this the DEF->GDS merge silently fell back to KLayout's sequential
    # default layer numbering (Via1 cuts on 8/0, Metal2 pads on 9/0, ...).
    if family.startswith("ihp-"):
        family = family[len("ihp-") :]
    if family != variant:
        family_candidate = os.path.join(tech_dir, f"{family}.map")
        if os.path.isfile(family_candidate):
            return family_candidate, "family"

    return None, "none"


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


def _invert_gds_to_lef(
    gds_to_lef: dict[tuple[int, int], str],
) -> dict[str, list[tuple[int, int]]]:
    """Invert ``gds_to_lef`` into ``{lef_layer_name: [(gds_layer,
    gds_datatype), ...]}``, sorted for deterministic search order.

    Used by :func:`_resolve_pins` to search every GDS datatype that shares a
    pin's resolved LEF layer *name* for drawn geometry -- not just the
    single ``(layer, datatype)`` the socket descriptor's ``pins[].layer``
    happens to declare -- mirroring :func:`_resolve_obs`'s existing
    per-LEF-layer union across every GDS datatype. Necessary because a PDK
    can (and open_pdks' own gf180mcu convention does) put a routing layer's
    drawn metal and its pin/net text label on two different datatypes of
    the same LEF layer; see issue #1614.
    """
    result: dict[str, list[tuple[int, int]]] = {}
    for gds_pair, lef_layer in gds_to_lef.items():
        result.setdefault(lef_layer, []).append(gds_pair)
    for pairs in result.values():
        pairs.sort()
    return result


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
    lef_layer_to_gds = _invert_gds_to_lef(gds_to_lef)

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
                    "gds_layer": [layer_tuple[0], layer_tuple[1]],
                    "geometry_source": "none",
                    "rects_um": [],
                    "rects_dbu": [],
                }
            )
            continue

        # Search every GDS datatype that resolves to this same LEF layer
        # name, not just the single one the descriptor declared -- a PDK
        # (e.g. gf180mcu) can put a routing layer's drawn metal and its
        # pin/net text label on two different datatypes of the same LEF
        # layer (issue #1614). The declared datatype is always searched
        # first, so single-datatype PDK conventions (sky130) are unaffected.
        search_layers = [layer_tuple] + [
            candidate
            for candidate in lef_layer_to_gds.get(lef_layer, ())
            if candidate != layer_tuple
        ]

        x_dbu = round(pin["x"] / dbu_um)
        y_dbu = round(pin["y"] / dbu_um)
        point = kdb.Point(x_dbu, y_dbu)

        drawn_boxes: list[tuple[int, int, int, int]] = []
        for search_layer in search_layers:
            layer_index = layout.find_layer(*search_layer)
            if layer_index is None:
                continue
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
            if len(search_layers) > 1:
                sibling_desc = ", ".join(
                    f"{layer}/{datatype}" for layer, datatype in search_layers[1:]
                )
                searched_desc = (
                    f"declared layer {layer_tuple[0]}/{layer_tuple[1]} or any "
                    f"sibling datatype resolving to LEF layer '{lef_layer}' "
                    f"({sibling_desc})"
                )
            else:
                searched_desc = f"layer {layer_tuple[0]}/{layer_tuple[1]}"
            warnings.append(
                f"pin '{pin['name']}': no drawn geometry found on {searched_desc} "
                f"at ({pin['x']}, {pin['y']}) um -- synthesized a placeholder PORT "
                "rectangle"
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
                "gds_layer": [layer_tuple[0], layer_tuple[1]],
                "geometry_source": geometry_source,
                "rects_um": [
                    [round(v * dbu_um, 6) for v in box] for box in local_boxes
                ],
                "rects_dbu": local_boxes,
            }
        )

    pins_report.sort(key=lambda p: p["name"])
    return pins_report, pin_boxes_by_layer


def _unroutable_pins(
    pins_report: list[dict[str, Any]], descriptor_pins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Issue #464's top-level ``unroutable_pins[]`` echo: every emitted pin
    with ``geometry_source: "none"`` (no ``PORT`` at all), paired with its
    original *declared* GDS ``(layer, datatype)`` -- the same information
    the per-pin ``warnings[]`` string already reports in prose, promoted to
    a structured, programmatically-checkable field. Name-sorted, matching
    ``pins_report``'s own sort order."""
    descriptor_layer_by_name = {pin["name"]: pin["layer"] for pin in descriptor_pins}
    return [
        {"name": pin["name"], "layer": list(descriptor_layer_by_name[pin["name"]])}
        for pin in pins_report
        if pin["geometry_source"] == "none"
    ]


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
        # `each_point()` -- *not* `each_point_hull()`, which exists only on
        # the hole-bearing `kdb.Polygon`, never on the `kdb.SimplePolygon`
        # `to_simple_polygon()` just returned (issue #1613).
        "points_um": [
            [round(pt.x * dbu_um, 6), round(pt.y * dbu_um, 6)]
            for pt in simple.each_point()
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


# --------------------------------------------------------------------------- #
# structural self-check of the emitted LEF (issue #2769)
# --------------------------------------------------------------------------- #
#
# A deliberately small validator for exactly the LEF subset `_write_lef`
# emits -- library-level statements (`VERSION`, `BUSBITCHARS`, ...), `MACRO
# <name>` ... `END <name>`, `PIN <name>` ... `END <name>`, anonymous `PORT`
# ... `END` / `OBS` ... `END`, `;`-terminated statements inside them, and a
# final `END LIBRARY`. It is NOT a general LEF parser and validates no
# geometry: a structural pass says the file is well-nested and names exactly
# the expected macros/pins, not that a router or P&R tool will accept it.
#
# Conventions it relies on (all true of `_write_lef`'s own output): `#`
# starts a comment running to end of line; tokens are whitespace-separated
# (a `;` glued to the end of a token is split off); the name after `MACRO`,
# `PIN` or a named `END` sits on the same line as the keyword; keywords are
# matched upper-case, identifiers are compared exactly (case preserved).


def _read_back_lef(output_path: str) -> str:
    """Read the just-written LEF back from disk for :func:`check_lef_structure`.

    The self-check deliberately validates the bytes actually on disk, not the
    in-memory data the writer was handed. A read/decode failure is an
    application error (:class:`LefAbstractError`, CLI exit 1), not a
    structural finding."""
    try:
        with open(output_path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise LefAbstractError(
            f"could not read back LEF abstract '{output_path}' for its "
            f"structural self-check: {exc}"
        ) from exc


#: Keywords that open or close a block in the emitter's subset. Only
#: recognised in statement-initial position.
_LEF_BLOCK_KEYWORDS = frozenset({"MACRO", "PIN", "PORT", "OBS", "END"})


def _lef_tokens(text: str) -> list[tuple[str, int, bool]]:
    """Tokenise LEF text into ``(token, line_number, first_on_line)``
    triples, dropping ``#`` comments and splitting a trailing ``;`` off any
    token it is glued to."""
    tokens: list[tuple[str, int, bool]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        first = True
        for word in raw.split("#", 1)[0].split():
            pieces = (
                [word[:-1], ";"] if len(word) > 1 and word.endswith(";") else [word]
            )
            for piece in pieces:
                tokens.append((piece, line_number, first))
                first = False
    return tokens


def _finding(
    code: str,
    message: str,
    *,
    line: int | None = None,
    macro: str | None = None,
    pin: str | None = None,
) -> dict[str, Any]:
    return {"code": code, "message": message, "line": line, "macro": macro, "pin": pin}


class _LefStructureParser:
    """Single-pass nesting/naming walk over :func:`_lef_tokens` output.

    Records findings rather than stopping at the first one, and recovers
    from each defect conservatively so later checks still run (one fault
    must not conceal another)."""

    def __init__(self, tokens: list[tuple[str, int, bool]]) -> None:
        self.tokens = tokens
        self.pos = 0
        # Each open block: {"kind", "name", "line", "decl"}.
        self.stack: list[dict[str, Any]] = []
        # Each MACRO declaration: {"name", "line", "pins": {name: line}}.
        self.macros: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.version_seen = False
        self.end_library_line: int | None = None

    # -- token helpers ------------------------------------------------------

    def _take_name(self, line: int) -> str | None:
        """Consume and return the token after a keyword when it is on the
        same line and is not a ``;``; else ``None`` (consumes nothing)."""
        if self.pos < len(self.tokens):
            token, token_line, _first = self.tokens[self.pos]
            if token_line == line and token != ";":
                self.pos += 1
                return token
        return None

    def _block_label(self, block: dict[str, Any]) -> str:
        if block["name"] is None:
            return block["kind"]
        return f"{block['kind']} {block['name']}"

    def _enclosing_macro(self) -> str | None:
        for block in reversed(self.stack):
            if block["kind"] == "MACRO":
                return block["name"]
        return None

    def _report_unclosed(self, block: dict[str, Any], reason: str) -> None:
        self.findings.append(
            _finding(
                "unclosed_block",
                f"{self._block_label(block)} opened on line {block['line']} "
                f"is never closed ({reason})",
                line=block["line"],
                macro=block["name"]
                if block["kind"] == "MACRO"
                else self._macro_of(block),
                pin=block["name"] if block["kind"] == "PIN" else None,
            )
        )

    def _macro_of(self, block: dict[str, Any]) -> str | None:
        decl = block.get("decl")
        return decl["name"] if decl is not None else None

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        handlers = {
            "MACRO": self._open_macro,
            "PIN": self._open_pin,
            "PORT": self._open_anonymous,
            "OBS": self._open_anonymous,
            "END": self._close,
            "VERSION": self._version,
        }
        while self.pos < len(self.tokens):
            token, line, _first = self.tokens[self.pos]
            if self.end_library_line is not None:
                self.findings.append(
                    _finding(
                        "content_after_end_library",
                        f"unexpected content '{token}' after END LIBRARY "
                        f"(line {self.end_library_line})",
                        line=line,
                    )
                )
                break
            handler = handlers.get(token)
            if handler is None:
                self._skip_statement(line)
            else:
                handler(token, line)
        self._finish()

    def _skip_statement(self, line: int) -> None:
        """Consume a ``;``-terminated statement. Stops early -- reporting
        ``unterminated_statement`` -- at EOF or at a block keyword that
        starts a new line, so a missing ``;`` cannot swallow an ``END``."""
        start = self.pos
        while self.pos < len(self.tokens):
            token, _line, first = self.tokens[self.pos]
            if self.pos > start and first and token in _LEF_BLOCK_KEYWORDS:
                break
            self.pos += 1
            if token == ";":
                return
        self.findings.append(
            _finding(
                "unterminated_statement",
                f"statement '{self.tokens[start][0]}' starting on line {line} "
                "has no terminating ';'",
                line=line,
                macro=self._enclosing_macro(),
            )
        )

    def _version(self, _token: str, line: int) -> None:
        value_pos = self.pos + 1
        if not self.stack and value_pos < len(self.tokens):
            value, value_line, _first = self.tokens[value_pos]
            if value != ";" and value_line == line:
                self.version_seen = True
        self._skip_statement(line)

    # -- block openers ------------------------------------------------------

    def _open_macro(self, _token: str, line: int) -> None:
        self.pos += 1
        name = self._take_name(line)
        if name is None:
            self.findings.append(
                _finding("missing_name", f"MACRO on line {line} has no name", line=line)
            )
        if self.stack:
            self.findings.append(
                _finding(
                    "misplaced_block",
                    f"MACRO {name} on line {line} is nested inside "
                    f"{self._block_label(self.stack[-1])}",
                    line=line,
                    macro=name,
                )
            )
        decl = None
        if name is not None:
            decl = {"name": name, "line": line, "pins": {}}
            self.macros.append(decl)
        self.stack.append({"kind": "MACRO", "name": name, "line": line, "decl": decl})

    def _open_pin(self, _token: str, line: int) -> None:
        self.pos += 1
        name = self._take_name(line)
        parent = self.stack[-1] if self.stack else None
        decl = parent["decl"] if parent and parent["kind"] == "MACRO" else None
        macro = self._enclosing_macro()
        if name is None:
            self.findings.append(
                _finding(
                    "missing_name",
                    f"PIN on line {line} has no name",
                    line=line,
                    macro=macro,
                )
            )
        if parent is None or parent["kind"] != "MACRO":
            where = "library level" if parent is None else self._block_label(parent)
            self.findings.append(
                _finding(
                    "misplaced_block",
                    f"PIN {name} on line {line} is not directly inside a MACRO "
                    f"(found at {where})",
                    line=line,
                    macro=macro,
                    pin=name,
                )
            )
        elif decl is not None and name is not None:
            self._record_pin(decl, name, line)
        self.stack.append({"kind": "PIN", "name": name, "line": line, "decl": decl})

    def _record_pin(self, decl: dict[str, Any], name: str, line: int) -> None:
        if name in decl["pins"]:
            self.findings.append(
                _finding(
                    "duplicate_pin",
                    f"PIN {name} declared again in MACRO {decl['name']} on line "
                    f"{line} (first on line {decl['pins'][name]})",
                    line=line,
                    macro=decl["name"],
                    pin=name,
                )
            )
            return
        decl["pins"][name] = line

    def _open_anonymous(self, token: str, line: int) -> None:
        self.pos += 1
        required_parent = "PIN" if token == "PORT" else "MACRO"
        parent = self.stack[-1] if self.stack else None
        if parent is None or parent["kind"] != required_parent:
            where = "library level" if parent is None else self._block_label(parent)
            self.findings.append(
                _finding(
                    "misplaced_block",
                    f"{token} on line {line} must be directly inside a "
                    f"{required_parent} (found at {where})",
                    line=line,
                    macro=self._enclosing_macro(),
                )
            )
        decl = parent.get("decl") if parent else None
        self.stack.append({"kind": token, "name": None, "line": line, "decl": decl})

    # -- END ----------------------------------------------------------------

    def _close(self, _token: str, line: int) -> None:
        self.pos += 1
        name = self._take_name(line)
        if name == "LIBRARY" and not (
            self.stack and self.stack[-1]["name"] == "LIBRARY"
        ):
            for block in reversed(self.stack):
                self._report_unclosed(block, f"END LIBRARY reached on line {line}")
            self.stack.clear()
            self.end_library_line = line
            return
        if not self.stack:
            label = f"END {name}" if name else "END"
            self.findings.append(
                _finding(
                    "orphan_end",
                    f"{label} on line {line} closes nothing",
                    line=line,
                    macro=name,
                )
            )
            return
        top = self.stack[-1]
        if top["name"] == name:
            self.stack.pop()
            return
        if name is None:
            self._unnamed_end(top, line)
            return
        self._mismatched_end(top, name, line)

    def _unnamed_end(self, top: dict[str, Any], line: int) -> None:
        self.findings.append(
            _finding(
                "unnamed_end",
                f"anonymous END on line {line} where 'END {top['name']}' is "
                f"required to close {self._block_label(top)} (line {top['line']})",
                line=line,
                macro=top["name"] if top["kind"] == "MACRO" else self._macro_of(top),
                pin=top["name"] if top["kind"] == "PIN" else None,
            )
        )
        self.stack.pop()

    def _mismatched_end(self, top: dict[str, Any], name: str, line: int) -> None:
        for index in range(len(self.stack) - 2, -1, -1):
            if self.stack[index]["name"] == name:
                # A deeper block's name: the blocks above it were never closed.
                for block in reversed(self.stack[index + 1 :]):
                    self._report_unclosed(block, f"END {name} reached on line {line}")
                del self.stack[index:]
                return
        expected = f"END {top['name']}" if top["name"] else "END"
        self.findings.append(
            _finding(
                "end_name_mismatch",
                f"END {name} on line {line} does not match {self._block_label(top)} "
                f"(line {top['line']}); expected '{expected}'",
                line=line,
                macro=top["name"] if top["kind"] == "MACRO" else self._macro_of(top),
                pin=top["name"] if top["kind"] == "PIN" else None,
            )
        )
        self.stack.pop()

    # -- EOF ----------------------------------------------------------------

    def _finish(self) -> None:
        for block in reversed(self.stack):
            self._report_unclosed(block, "end of file reached -- truncated output?")
        self.stack.clear()
        if self.end_library_line is None:
            self.findings.append(
                _finding("missing_end_library", "no END LIBRARY statement")
            )
        if not self.version_seen:
            self.findings.append(
                _finding("missing_version", "no library-level VERSION statement")
            )


def _macro_identity_findings(
    macros: list[dict[str, Any]], expected: dict[str, list[str]]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Compare declared MACRO names against the expected set: duplicates,
    missing and unexpected names (exact, case-sensitive)."""
    findings: list[dict[str, Any]] = []
    first_line: dict[str, int] = {}
    for decl in macros:
        if decl["name"] in first_line:
            findings.append(
                _finding(
                    "duplicate_macro",
                    f"MACRO {decl['name']} declared again on line {decl['line']} "
                    f"(first on line {first_line[decl['name']]})",
                    line=decl["line"],
                    macro=decl["name"],
                )
            )
        else:
            first_line[decl["name"]] = decl["line"]
    missing = sorted(set(expected) - set(first_line))
    unexpected = sorted(set(first_line) - set(expected))
    for name in missing:
        findings.append(
            _finding(
                "missing_macro", f"expected MACRO {name} is not declared", macro=name
            )
        )
    for name in unexpected:
        findings.append(
            _finding(
                "unexpected_macro",
                f"MACRO {name} (line {first_line[name]}) is not an expected macro",
                line=first_line[name],
                macro=name,
            )
        )
    return findings, missing, unexpected


def _pin_identity_findings(
    macros: list[dict[str, Any]], expected: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """For each expected macro that is declared, every expected pin must be
    declared inside it and no other pin may be. Pins are unioned across
    duplicate declarations of the same macro (the duplicate itself is
    already a finding)."""
    findings: list[dict[str, Any]] = []
    declared: dict[str, dict[str, int]] = {}
    macro_line: dict[str, int] = {}
    for decl in macros:
        macro_line.setdefault(decl["name"], decl["line"])
        pins = declared.setdefault(decl["name"], {})
        for pin, line in decl["pins"].items():
            pins.setdefault(pin, line)
    for macro in sorted(set(expected) & set(declared)):
        wanted = set(expected[macro])
        have = declared[macro]
        for pin in sorted(wanted - set(have)):
            findings.append(
                _finding(
                    "missing_pin",
                    f"declared pin {pin} is missing from MACRO {macro}",
                    line=macro_line[macro],
                    macro=macro,
                    pin=pin,
                )
            )
        for pin in sorted(set(have) - wanted):
            findings.append(
                _finding(
                    "unexpected_pin",
                    f"PIN {pin} (line {have[pin]}) is not a declared pin of "
                    f"MACRO {macro}",
                    line=have[pin],
                    macro=macro,
                    pin=pin,
                )
            )
    return findings


def check_lef_structure(
    text: str, expected_pins: dict[str, list[str]]
) -> dict[str, Any]:
    """Structurally check LEF ``text`` (the subset ``klt lef-abstract``
    emits) against ``expected_pins`` -- a ``{macro_name: [pin_name, ...]}``
    mapping of exactly the macros the file must declare, each with exactly
    the pins it must contain.

    Returns the additive ``self_check`` report block::

        {
            "status": "pass" | "fail",
            "findings": [
                {"code", "message", "line", "macro", "pin"}, ...
            ],
            "missing_macros": [<sorted names>],
            "unexpected_macros": [<sorted names>],
        }

    ``line``/``macro``/``pin`` are ``null`` when not applicable. Every check
    runs regardless of earlier findings. ``status`` is ``"pass"`` iff
    ``findings`` is empty. Checks: a library-level ``VERSION``; MACRO/PIN/
    PORT/OBS nesting with named ``END <name>`` for MACRO/PIN, anonymous
    ``END`` for PORT/OBS and a final ``END LIBRARY`` (orphan, mismatched,
    unnamed and unclosed ENDs, truncation, unterminated statements, content
    after ``END LIBRARY``); duplicate MACRO/PIN declarations; exact macro
    name set; every expected pin inside its own macro and no unexpected pin.
    Geometry and non-structural LEF semantics are not checked.
    """
    parser = _LefStructureParser(_lef_tokens(text))
    parser.run()
    macro_findings, missing, unexpected = _macro_identity_findings(
        parser.macros, expected_pins
    )
    findings = (
        parser.findings
        + macro_findings
        + _pin_identity_findings(parser.macros, expected_pins)
    )
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "missing_macros": missing,
        "unexpected_macros": unexpected,
    }


def _read_back_pin_uses(text: str) -> dict[str, str]:
    """``{pin_name: USE value}`` as actually written in LEF ``text`` (issue
    #2839) -- the ``USE`` statement inside each ``PIN <name>`` ... ``END
    <name>`` block. A pin with no ``USE`` statement is absent."""
    uses: dict[str, str] = {}
    tokens = _lef_tokens(text)
    current: str | None = None
    for index, (token, line, first) in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token == "PIN" and first and following and following[1] == line:
            current = following[0]
        elif token == "END" and current is not None and following:
            if following[1] == line and following[0] == current:
                current = None
        elif token == "USE" and first and current is not None and following:
            uses.setdefault(current, following[0])
    return uses


def check_power_pins(pins: list[dict[str, Any]], lef_text: str) -> dict[str, Any]:
    """The LEF abstract's supply-pin verdict (issue #2839, T1 item 11 port
    accessibility -- provisional until 2AMLogic/product#134 ratifies the
    amendment). ``pins`` are this command's own resolved pins (``use``,
    ``geometry_source``, ``lef_layer``); ``lef_text`` is the file read back
    from disk, whose ``USE`` statements are what is checked.

    Returns::

        {
            "status": "pass" | "fail",
            "findings": [{"code", "pin", "message"}, ...],
            "pins": [{"name", "use", "use_read_back", "layer",
                      "geometry_source"}, ...],   # supply pins only
        }

    Finding codes: ``no_power_pin``/``no_ground_pin`` (no pin is ``USE
    POWER``/``USE GROUND``; ``pin`` is ``null``), ``use_not_written`` (the
    file's own ``USE`` for a supply pin is absent or differs), and
    ``port_not_drawn`` (a supply pin's ``PORT`` is a synthesized placeholder
    or missing, not drawn layout geometry). ``status`` is ``"pass"`` iff
    there are no findings.
    """
    read_back = _read_back_pin_uses(lef_text)
    supply = [pin for pin in pins if pin["use"] in ("POWER", "GROUND")]
    findings: list[dict[str, Any]] = []
    uses = {pin["use"] for pin in supply}
    for use, code in (("POWER", "no_power_pin"), ("GROUND", "no_ground_pin")):
        if use not in uses:
            findings.append(
                {
                    "code": code,
                    "pin": None,
                    "message": f"no PIN with USE {use} in the abstract",
                }
            )
    for pin in supply:
        if read_back.get(pin["name"]) != pin["use"]:
            findings.append(
                {
                    "code": "use_not_written",
                    "pin": pin["name"],
                    "message": (
                        f"PIN {pin['name']}: expected USE {pin['use']} in the "
                        f"written file, found {read_back.get(pin['name'])!r}"
                    ),
                }
            )
        if pin["geometry_source"] != "drawn":
            findings.append(
                {
                    "code": "port_not_drawn",
                    "pin": pin["name"],
                    "message": (
                        f"PIN {pin['name']}: PORT geometry is "
                        f"{pin['geometry_source']!r}, not drawn layout geometry"
                    ),
                }
            )
    return {
        "status": "fail" if findings else "pass",
        "findings": findings,
        "pins": [
            {
                "name": pin["name"],
                "use": pin["use"],
                "use_read_back": read_back.get(pin["name"]),
                "layer": pin["lef_layer"],
                "geometry_source": pin["geometry_source"],
            }
            for pin in supply
        ],
    }


__all__ = [
    "SCHEMA_VERSION",
    "LefAbstractError",
    "check_lef_structure",
    "check_power_pins",
    "run_lef_abstract",
]
