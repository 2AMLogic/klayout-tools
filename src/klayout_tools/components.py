"""Report geometric connected components across a caller-selected
conductor/via stack -- no PDK deck, no device recognition.

Pure library: :func:`run_components_report` returns plain Python data (a
``dict`` of JSON-serialisable primitives) and never prints, mirroring
``ring_check.py``/``layers.py``. Serialisation and human-readable formatting
live in the CLI command module (``cli/components_cmd.py``).

Headless invariant: uses only the pip ``klayout`` package's batch database
API (``klayout.db``) -- no GUI, no Qt, and no dependency on the standalone
``klayout`` application binary.

Why this exists (issue #674): ``klt extract`` answers a device/netlist
question using a curated, PDK-specific ``ExtractionDeck`` -- it is not a
convenient fit for inspecting unnamed, device-free metal, annotation
geometry, an incomplete layout, or a deliberately limited subset of layers.
Today that requires a hand-rolled KLayout ``Region``/``LayoutToNetlist``
script with hand-coded conductor/via interaction semantics. This module
generalises the same connectivity engine ``extract.py``'s ``_extract_netlist``
already uses internally (``l2n.connect(metal, via)`` / ``l2n.connect(via)`` /
``l2n.connect(via, next_metal)`` -- see that module's docstring) behind a
caller-supplied, ad-hoc layer/via mapping instead of a hardcoded
``ExtractionDeck``, so no device recognition and no named PDK deck is ever
required.

The load-bearing distinction this engine makes -- the reason a hand-rolled
``Region`` union isn't enough -- is that it tells apart two shapes that
merely **overlap in XY on different layers** from two shapes that are
**electrically joined through an explicitly declared via**. ``kdb.
LayoutToNetlist``'s ``connect()`` graph is purely declarative: two regions
that are never named in a ``connect()`` call together never merge into the
same net, no matter how much their polygons overlap in the XY plane. Only a
same-layer ``connect(region)`` (touching shapes on the same conductor merge)
or an explicit via-mediated ``connect(conductor_a, via)`` +
``connect(via, conductor_b)`` pair joins two different conductor levels. This
module never calls ``extract_devices()`` and never calls ``Netlist.purge()``,
so every connected cluster -- including an isolated, unnamed, device-free
island -- survives as its own reported component; nothing here recognises or
requires a device, a named net, or a pin.
"""

from __future__ import annotations

from typing import Any

from ._layout import load_layout
from ._layout import select_top_cells as _select_top_cells_shared

#: Floating-point-noise cleanup applied to every micrometre value this module
#: reports -- same precision ``extract.py``'s ``black_box_regions`` uses.
_PRECISION_UM = 6


class ComponentsError(Exception):
    """Raised when a components report cannot even be attempted: a
    missing/unreadable layout file, a malformed conductor/via/label-layer
    spec, or a named ``top`` cell that is not present in the stream.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback.
    """


def run_components_report(
    layout_path: str,
    conductors: list[dict[str, Any]],
    vias: list[dict[str, Any]] | None = None,
    label_layers: list[dict[str, Any]] | None = None,
    region_um: tuple[float, float, float, float] | None = None,
    top: str | None = None,
) -> dict[str, Any]:
    """Report every geometric connected component across ``conductors`` +
    ``vias``, joined only where an explicit via lands, per top cell.

    ``conductors`` is a non-empty list of ``{"name": str, "layer": [layer,
    datatype]}`` entries -- one entry per conductor level (e.g. a routing
    metal, local interconnect, or any other drawn layer the caller wants
    treated as a conductor). ``name`` must be unique across ``conductors``.
    Same-layer touching shapes on a single conductor always merge into one
    component, regardless of ``vias``.

    ``vias`` (optional, default none) is a list of ``{"name": str, "layer":
    [layer, datatype], "between": [conductor_name_a, conductor_name_b]}``
    entries -- one entry per via/contact layer that electrically joins two
    named conductor levels wherever its shapes actually land on both. ``name``
    must be unique across ``vias``; ``between`` must name two *different*,
    already-declared ``conductors`` entries. A via shape that only touches one
    side (or neither) contributes no join -- see this module's docstring for
    why mere XY overlap between un-via'd conductors never joins two
    components.

    ``label_layers`` (optional, default none) is a list of ``{"name": str,
    "layer": [layer, datatype]}`` entries naming GDS text layers to scan for
    names/labels/pins. Any text whose location touches a component's geometry
    (on any of its conductor/via layers) is reported in that component's
    ``labels`` list -- purely a spatial "does this text sit on this
    component" test, independent of the connectivity graph above (texts are
    never registered with the connectivity engine, so they can never
    themselves join two components together).

    ``region_um``, when given, is a ``(left, bottom, right, top)`` crop window
    in **micrometres**: every conductor/via/label-layer shape is clipped to
    this window before components are computed, mirroring ``klt
    ring-check``'s ``--region``. A component whose (clipped) bounding box
    touches the crop window's edge is flagged ``touches_crop_boundary: True``
    so a caller knows it may continue outside the cropped view -- omitted
    (always ``False``) when ``region_um`` is not given.

    ``top``, when given, restricts the report to that single top cell
    (required only to disambiguate a multi-top stream); omitted, every top
    cell in the stream is reported.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/components.md``)::

        {
            "schema_version": 1,
            "file": <path as provided>,
            "conductors": [{"name": str, "layer": [layer, datatype]}, ...],
            "vias": [{"name": str, "layer": [layer, datatype],
                       "between": [str, str]}, ...],
            "label_layers": [{"name": str, "layer": [layer, datatype]}, ...],
            "region_um": [left, bottom, right, top] | None,
            "top": <str> | None,
            "dbu_um": <database unit in micrometres, float>,
            "component_count": <int>,
            "components": [
                {
                    "id": <stable string identifier, "<cell>:<index>">,
                    "cell": <top cell name>,
                    "bbox_um": {"left": float, "bottom": float,
                                "right": float, "top": float},
                    "conductors": [
                        {"name": str, "layer": [layer, datatype],
                         "shape_count": int, "area_um2": float}, ...
                    ],
                    "vias": [
                        {"name": str, "layer": [layer, datatype],
                         "shape_count": int, "area_um2": float}, ...
                    ],
                    "labels": [str, ...],
                    "touches_crop_boundary": bool,
                },
                ...
            ],
        }

    ``components`` is sorted deterministically (by cell name, then bounding
    box) so ``id`` and output ordering are stable across runs regardless of
    the underlying engine's internal cluster-enumeration order (which is not
    guaranteed stable across KLayout builds -- the same caveat ``ring_check.py``
    documents). Only conductor/via layers that actually contribute at least
    one shape to a component are listed in that component's ``conductors``/
    ``vias`` entries -- an unconnected conductor with no geometry on it never
    appears.

    Raises :class:`ComponentsError` if the file is missing/unreadable, the
    conductor/via/label-layer specs are malformed or reference an unknown
    conductor name, or a named ``top`` cell is absent from the stream.
    """
    conductor_specs = _validate_conductors(conductors)
    via_specs = _validate_vias(vias or [], conductor_specs)
    label_specs = _validate_label_layers(label_layers or [])

    layout = load_layout(layout_path, ComponentsError)

    # Imported lazily (after load_layout, which already paid this cost),
    # matching ring_check.py's/extract.py's own ordering.
    import klayout.db as kdb

    top_cells = _select_top_cells(layout, top)
    dbu = layout.dbu
    clip_box = _clip_box(kdb, region_um, dbu) if region_um is not None else None

    components: list[dict[str, Any]] = []
    for cell in top_cells:
        components.extend(
            _components_for_cell(
                kdb,
                layout,
                cell,
                conductor_specs,
                via_specs,
                label_specs,
                clip_box,
                dbu,
            )
        )

    # Deterministic, diff-clean ordering (mirrors ring_check.py/drc.py): the
    # engine's own cluster-enumeration order is not guaranteed stable across
    # builds.
    components.sort(
        key=lambda c: (
            c["cell"],
            c["bbox_um"]["left"],
            c["bbox_um"]["bottom"],
            c["bbox_um"]["right"],
            c["bbox_um"]["top"],
        )
    )
    for index, component in enumerate(components):
        component["id"] = f"{component['cell']}:{index}"

    return {
        "schema_version": 1,
        "file": layout_path,
        "conductors": [
            {"name": spec["name"], "layer": list(spec["layer"])}
            for spec in conductor_specs
        ],
        "vias": [
            {
                "name": spec["name"],
                "layer": list(spec["layer"]),
                "between": list(spec["between"]),
            }
            for spec in via_specs
        ],
        "label_layers": [
            {"name": spec["name"], "layer": list(spec["layer"])} for spec in label_specs
        ],
        "region_um": list(region_um) if region_um is not None else None,
        "top": top,
        "dbu_um": dbu,
        "component_count": len(components),
        "components": components,
    }


def _components_for_cell(
    kdb: Any,
    layout: Any,
    cell: Any,
    conductor_specs: list[dict[str, Any]],
    via_specs: list[dict[str, Any]],
    label_specs: list[dict[str, Any]],
    clip_box: Any | None,
    dbu: float,
) -> list[dict[str, Any]]:
    """Compute every connected component for one top ``cell``."""
    conductor_regions: dict[str, Any] = {}
    for spec in conductor_specs:
        region = _region(kdb, layout, cell, spec["layer"])
        if clip_box is not None:
            region &= kdb.Region(clip_box)
        conductor_regions[spec["name"]] = region

    via_regions: dict[str, Any] = {}
    for spec in via_specs:
        region = _region(kdb, layout, cell, spec["layer"])
        if clip_box is not None:
            region &= kdb.Region(clip_box)
        via_regions[spec["name"]] = region

    label_texts: dict[str, Any] = {}
    for spec in label_specs:
        texts = _texts(kdb, layout, cell, spec["layer"])
        if clip_box is not None:
            texts = texts.interacting(kdb.Region(clip_box))
        label_texts[spec["name"]] = texts

    l2n = kdb.LayoutToNetlist(cell.name, dbu)
    conductor_index = {
        name: l2n.register(region, name) for name, region in conductor_regions.items()
    }
    via_index = {
        name: l2n.register(region, name) for name, region in via_regions.items()
    }

    # Same-layer merge: touching shapes on one conductor/via always join,
    # regardless of any via mapping.
    for region in conductor_regions.values():
        l2n.connect(region)
    for region in via_regions.values():
        l2n.connect(region)

    # Via-mediated joins only -- never a direct conductor<->conductor
    # connect(), so bare XY overlap between un-via'd conductors is never
    # treated as a connection (see this module's docstring).
    for spec in via_specs:
        conductor_a, conductor_b = spec["between"]
        via_region = via_regions[spec["name"]]
        l2n.connect(conductor_regions[conductor_a], via_region)
        l2n.connect(via_region, conductor_regions[conductor_b])

    # No `extract_devices()` call (nothing here recognises a device) and no
    # `Netlist.purge()` call afterwards -- every connected cluster, including
    # an isolated island with no via/label, survives as its own net below.
    l2n.extract_netlist()
    netlist = l2n.netlist()
    circuit = netlist.circuit_by_name(cell.name)
    if circuit is None:
        return []

    components: list[dict[str, Any]] = []
    for net in circuit.each_net():
        component = _describe_net(
            kdb,
            net,
            l2n,
            cell.name,
            conductor_specs,
            conductor_index,
            via_specs,
            via_index,
            label_texts,
            clip_box,
            dbu,
        )
        if component is not None:
            components.append(component)
    return components


def _describe_net(
    kdb: Any,
    net: Any,
    l2n: Any,
    cell_name: str,
    conductor_specs: list[dict[str, Any]],
    conductor_index: dict[str, int],
    via_specs: list[dict[str, Any]],
    via_index: dict[str, int],
    label_texts: dict[str, Any],
    clip_box: Any | None,
    dbu: float,
) -> dict[str, Any] | None:
    """Build one component entry for ``net``, or ``None`` if it carries no
    geometry at all (can happen for a net KLayout creates with zero shapes)."""
    total_region = kdb.Region()

    conductors_present: list[dict[str, Any]] = []
    for spec in conductor_specs:
        region = l2n.polygons_of_net(net, conductor_index[spec["name"]])
        shape_count = region.count()
        if shape_count == 0:
            continue
        total_region += region
        conductors_present.append(
            {
                "name": spec["name"],
                "layer": list(spec["layer"]),
                "shape_count": shape_count,
                "area_um2": round(region.area() * dbu * dbu, _PRECISION_UM),
            }
        )

    vias_present: list[dict[str, Any]] = []
    for spec in via_specs:
        region = l2n.polygons_of_net(net, via_index[spec["name"]])
        shape_count = region.count()
        if shape_count == 0:
            continue
        total_region += region
        vias_present.append(
            {
                "name": spec["name"],
                "layer": list(spec["layer"]),
                "shape_count": shape_count,
                "area_um2": round(region.area() * dbu * dbu, _PRECISION_UM),
            }
        )

    if total_region.is_empty():
        return None

    labels: set[str] = set()
    for texts in label_texts.values():
        for text in texts.interacting(total_region).each():
            labels.add(text.string)

    box = total_region.bbox()
    touches_crop_boundary = False
    if clip_box is not None:
        touches_crop_boundary = (
            box.left == clip_box.left
            or box.right == clip_box.right
            or box.bottom == clip_box.bottom
            or box.top == clip_box.top
        )

    return {
        "id": "",  # assigned after the deterministic sort in the caller
        "cell": cell_name,
        "bbox_um": {
            "left": round(box.left * dbu, _PRECISION_UM),
            "bottom": round(box.bottom * dbu, _PRECISION_UM),
            "right": round(box.right * dbu, _PRECISION_UM),
            "top": round(box.top * dbu, _PRECISION_UM),
        },
        "conductors": conductors_present,
        "vias": vias_present,
        "labels": sorted(labels),
        "touches_crop_boundary": touches_crop_boundary,
    }


def _select_top_cells(layout: Any, top: str | None) -> list[Any]:
    """Return the top cells to report on, honouring an optional ``top`` filter.

    Delegates to the shared ``_layout.select_top_cells`` helper (issue #554),
    the same "optional scope, default to every top cell" pattern
    ``ring_check.py``/``drc.py``/``precheck.py`` already share.
    """
    return _select_top_cells_shared(layout, top, ComponentsError)


def _region(kdb: Any, layout: Any, cell: Any, layer: tuple[int, int]) -> Any:
    """A flattened ``Region`` for ``layer`` under ``cell``, or empty when the
    layer is absent from the stream."""
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Region()
    return kdb.Region(cell.begin_shapes_rec(layer_index))


def _texts(kdb: Any, layout: Any, cell: Any, layer: tuple[int, int]) -> Any:
    """A flattened ``Texts`` collection for ``layer`` under ``cell``, or empty
    when the layer is absent from the stream."""
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return kdb.Texts()
    return kdb.Texts(cell.begin_shapes_rec(layer_index))


def _clip_box(
    kdb: Any, region_um: tuple[float, float, float, float], dbu: float
) -> Any:
    """Convert a micrometre ``(left, bottom, right, top)`` window into an
    integer-dbu ``kdb.Box`` for clipping -- same rounding ``ring_check.py``
    applies."""
    left, bottom, right, top = region_um
    return kdb.Box(
        round(left / dbu),
        round(bottom / dbu),
        round(right / dbu),
        round(top / dbu),
    )


def _validate_conductors(conductors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not conductors:
        raise ComponentsError("conductors must contain at least one entry")

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in conductors:
        name = _require_name(entry, "conductors")
        if name in seen_names:
            raise ComponentsError(f"duplicate conductor name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "conductors", name)
        specs.append({"name": name, "layer": layer})
    return specs


def _validate_vias(
    vias: list[dict[str, Any]], conductor_specs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    conductor_names = {spec["name"] for spec in conductor_specs}

    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in vias:
        name = _require_name(entry, "vias")
        if name in seen_names:
            raise ComponentsError(f"duplicate via name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "vias", name)

        between = entry.get("between")
        if (
            not isinstance(between, list)
            or len(between) != 2
            or not all(isinstance(v, str) for v in between)
        ):
            raise ComponentsError(
                f"via {name!r} 'between' must be a two-element list of "
                f"conductor names, got {between!r}"
            )
        conductor_a, conductor_b = between
        if conductor_a == conductor_b:
            raise ComponentsError(
                f"via {name!r} 'between' must name two different conductors, "
                f"got {between!r}"
            )
        for conductor_name in (conductor_a, conductor_b):
            if conductor_name not in conductor_names:
                raise ComponentsError(
                    f"via {name!r} 'between' references unknown conductor "
                    f"{conductor_name!r}"
                )
        specs.append(
            {"name": name, "layer": layer, "between": (conductor_a, conductor_b)}
        )
    return specs


def _validate_label_layers(label_layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in label_layers:
        name = _require_name(entry, "label_layers")
        if name in seen_names:
            raise ComponentsError(f"duplicate label layer name: {name!r}")
        seen_names.add(name)
        layer = _require_layer(entry, "label_layers", name)
        specs.append({"name": name, "layer": layer})
    return specs


def _require_name(entry: Any, field: str) -> str:
    if not isinstance(entry, dict):
        raise ComponentsError(f"each {field} entry must be an object, got {entry!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ComponentsError(f"each {field} entry requires a non-empty 'name' string")
    return name


def _require_layer(entry: dict[str, Any], field: str, name: str) -> tuple[int, int]:
    layer = entry.get("layer")
    if (
        not isinstance(layer, list)
        or len(layer) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in layer)
    ):
        raise ComponentsError(
            f"{field} entry {name!r} 'layer' must be a [layer, datatype] pair "
            f"of integers, got {layer!r}"
        )
    return (layer[0], layer[1])
