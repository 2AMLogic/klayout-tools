"""DEF -> GDS merge subsystem for ``klt place-and-route`` (ported from
``def2stream.py`` onto ``klayout.db``, in-process).

Split out of ``place_and_route.py`` (issue #1820) as a self-contained
subsystem: parsing a DEF's own ``DIEAREA`` in real microns
(:func:`_parse_def_die_area_um`), merging a standard-cell/macro GDS view
into the DEF-derived layout at a consistent DBU (:func:`_merge_gds_view`),
measuring a merged cell library's own abutment-box overhang
(:func:`_cell_library_max_overhang_um`), resolving a LEF layer name to its
routed-net GDS ``(layer, datatype)`` pair (:func:`_lef_net_layer_gds_map`),
a region-based drawn-geometry coverage test
(:func:`_box_is_inside_drawn_geometry`), stamping unrouted single-pin DEF
nets with a recoverable name marker (:func:`_stamp_single_pin_def_net_names`,
issue #1488), and the merge's own top-level orchestration
(:func:`_merge_def_to_gds`). This mirrors the shape of the earlier
``extract.py``/``extract_abstract.py`` (#1303), ``gen.py``/``gen_pcells``
(#1698), ``gen_compose.py``/``gen_compose_routing.py`` (#1708), and this same
file's own STA split, ``place_and_route.py``/``place_and_route_sta.py``
(#1808/#1819): a self-contained, single-purpose subsystem relocated verbatim
out of a file that had grown past the point one module should hold stage
orchestration *and* this.

Only :func:`_merge_def_to_gds` crosses the boundary back into
``place_and_route.py`` -- called exactly once, from that module's own
:func:`~klayout_tools.place_and_route.run_place_and_route`. Everything else
in this module is section-internal (the two DEF-property-id constants,
``_DEF_INSTANCE_NAME_PROPERTY_ID``/``_SINGLE_PIN_NET_MARKER_HALF_DBU``, are
also re-exported back into ``place_and_route.py``'s own namespace, since the
test suite reaches them by that path from before this split -- see that
module's own re-export block).

Dependency surface, same discipline the STA split documents for its own
reverse dependency: ``write_layout`` (``_layout.py``),
``read_lef_macro_pin_ports`` (``lef_header.py``) and ``repair_min_area``
(``place_and_route_min_area.py``, issue #2139 -- this merge's own
post-route minimum-area repair pass, split off for the same reason this
module was) are imported at module scope here directly from where they are
actually defined -- none creates a cycle, since none of those modules
imports back from here or from ``place_and_route.py``, and none is ever
monkeypatched through ``place_and_route.<name>``. The handful of names still
defined in (or re-exported through) ``place_and_route.py`` itself
(``PlaceAndRouteError``,
``read_lef_header``, ``_resolve_gds_view``, ``_resolve_layer_map``) are
imported *inside* the function that uses them, deferred rather than at
module scope, both because ``place_and_route.py`` in turn imports this
module's own :func:`_merge_def_to_gds` back (module scope, no cycle -- this
module never imports ``place_and_route`` at its own module scope) and
because the test suite monkeypatches ``place_and_route.read_lef_header``
expecting :func:`_merge_def_to_gds` to observe it -- a deferred,
looked-up-by-name-at-call-time import is what makes that monkeypatch
visible here, exactly as it would if this code had never moved. This
purely preserves ``klayout_tools.place_and_route._merge_def_to_gds``/
``read_lef_header`` as working import/monkeypatch paths for callers and the
test suite that used them before this split.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ._layout import write_layout
from .lef_header import read_lef_macro_pin_ports
from .place_and_route_min_area import repair_min_area

#: ``UNITS DISTANCE MICRONS <n> ;`` -- mirrors ``congestion.py``'s own
#: ``_UNITS_RE`` (kept as a separate, module-local copy here rather than
#: imported, matching this file's existing small-targeted-scan convention
#: for DEF text, e.g. :func:`~klayout_tools.place_and_route_sta._def_pin_net_names`).
_DEF_UNITS_RE = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;")
#: ``DIEAREA ( x0 y0 ) ( x1 y1 ) ;`` -- DEF's ``DIEAREA`` is technically an
#: arbitrary rectilinear polygon, but every point pair this regex captures
#: is reduced to its bounding box below, which is exact for the common
#: rectangular case and a conservative (over-)approximation otherwise.
_DEF_DIEAREA_RE = re.compile(r"DIEAREA\s+((?:\(\s*-?\d+\s+-?\d+\s*\)\s*)+);")
_DEF_DIEAREA_POINT_RE = re.compile(r"\(\s*(-?\d+)\s+(-?\d+)\s*\)")

#: The user-property key KLayout's own LEF/DEF reader records each DEF
#: ``COMPONENTS`` entry's **instance name** under, on the ``kdb.Instance`` it
#: places for that entry -- ``LEFDEFReaderConfiguration.instance_property_name``,
#: whose KLayout default is ``1`` (verified against klayout 0.30.10).
#: :func:`_merge_def_to_gds` never overrides it, exactly as it never overrides
#: ``net_property_name`` (the *shape* property id, coincidentally also ``1``,
#: that ``klt extract --def-net-names`` reads -- a different object kind, so
#: the two never collide).
#:
#: This is what makes a DEF ``NETS`` connection (``( <instance> <pin> )``)
#: resolvable to **one placement**: a standard cell's own pin geometry lives
#: in the macro cell every instance of that cell type shares, so the pin name
#: alone identifies nothing. See
#: :func:`_stamp_single_pin_def_net_names` (issue #1488).
_DEF_INSTANCE_NAME_PROPERTY_ID = 1

#: Half-width, in database units, of the marker box
#: :func:`_stamp_single_pin_def_net_names` draws to carry an unrouted
#: single-pin net's DEF name. Deliberately tiny (a 2 dbu square, i.e. 2 nm on
#: a 0.001 um-dbu PDK) *and* only ever drawn where the merged layout already
#: has drawn conductor covering it, so the marker is a geometric no-op: every
#: consumer of the merged GDS -- DRC, LVS, extraction's own region merge --
#: sees exactly the same merged polygons it saw before, and the marker
#: contributes only its shape property.
_SINGLE_PIN_NET_MARKER_HALF_DBU = 1


def _parse_def_die_area_um(def_path: str) -> tuple[float, float, float, float] | None:
    """Parse ``def_path``'s own ``DIEAREA`` bounding box in real microns
    (``(x0_um, y0_um, x1_um, y1_um)``), or ``None`` when the DEF has no
    ``UNITS DISTANCE MICRONS``/``DIEAREA`` statement to parse -- an unusual
    but not necessarily invalid DEF, so the post-merge sanity check this
    backs (issue #1090's second, independent fix) is skipped rather than
    raising in that case, matching :func:`read_lef_header`'s own
    "malformed/absent input never raises" convention.
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None

    units_match = _DEF_UNITS_RE.search(text)
    die_match = _DEF_DIEAREA_RE.search(text)
    if units_match is None or die_match is None:
        return None

    scale = float(units_match.group(1))
    points = [
        (int(x) / scale, int(y) / scale)
        for x, y in _DEF_DIEAREA_POINT_RE.findall(die_match.group(1))
    ]
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _merge_gds_view(kdb: Any, main_layout: Any, gds_path: str) -> None:
    """Merge a GDS view (the standard-cell library GDS, or a macro's own
    declared ``gds``) into ``main_layout``, preserving ``main_layout``'s own
    DBU regardless of ``gds_path``'s own declared DBU (issue #1090, a
    follow-on to #1032's fix for the *first* DEF/LEF read's own DBU).

    A plain ``main_layout.read(gds_path)`` on an existing, non-empty
    ``Layout`` silently *adopts* the incoming GDS stream's own DBU whenever
    it differs from ``main_layout.dbu`` -- confirmed directly against the
    installed ``klayout`` build -- without rescaling any geometry already
    present. Every DEF-derived shape (instance placements, routed wires,
    vias -- correct at the DEF's own DBU, itself resolved from the tech LEF
    per #1032) then gets silently *reinterpreted* at the GDS's own DBU on
    this second read, doubling (or halving) every DEF-derived coordinate in
    real microns while the standard-cell/macro geometry that arrives with
    this same read stays correct -- a geometrically-plausible-looking but
    wrong merged GDS that passes the "empty (unmatched) cells"/"orphan
    cells" checks below.

    Reading the incoming view into its own scratch ``Layout`` first,
    rescaling it (only when its DBU actually differs) to
    ``main_layout.dbu``, and re-serializing it in-memory before the real
    merge keeps both sides in one coordinate system by construction: the
    DBU-adoption behavior above can then only ever apply to the scratch
    layout's own (already-rescaled) content, never to ``main_layout``'s
    existing geometry, regardless of which DBU either side started at.
    """
    scratch = kdb.Layout()
    scratch.read(gds_path)
    if scratch.dbu != main_layout.dbu:
        scale = scratch.dbu / main_layout.dbu
        scratch.transform(kdb.ICplxTrans(scale))
        scratch.dbu = main_layout.dbu
    save_opts = kdb.SaveLayoutOptions()
    save_opts.format = "GDS2"
    main_layout.read_bytes(scratch.write_bytes(save_opts), kdb.LoadLayoutOptions())


def _cell_library_max_overhang_um(
    top_only: Any, cell_lef_macros: list[dict[str, Any]]
) -> float:
    """The largest amount any standard-cell definition merged into
    ``top_only`` draws its own GDS-view geometry beyond its own LEF
    ``SIZE`` box, measured from that box's own origin -- LEF's own
    convention: a macro's ``(0, 0)`` is its ``SIZE`` box's lower-left
    corner.

    Real open_pdks standard-cell libraries deliberately draw geometry
    outside their own LEF abutment box so that abutting cells' wells/
    implants merge across a row (issue #1335): gf180mcu's own
    ``gf180mcu_fd_sc_mcu9t5v0__endcap`` overhangs by 2.93 um on its left,
    every other cell in that library by ~0.43/0.45 um. This is the
    *actual*, measured overhang for whichever cells this specific merge
    used -- :func:`_merge_def_to_gds`'s own DIEAREA guard bounds it against
    a technology-scaled ceiling (the tech LEF's own row height) before
    trusting it as a tolerance, so a corrupted/implausible measurement
    (e.g. from a cell GDS view that is itself the defect under test) can
    never mask a genuine doubling/halving-scale DBU-mismatch defect.

    Returns ``0.0`` when no merged cell's name matches a declared macro
    with a known ``SIZE`` (e.g. every placed cell is a via/fill cell, or
    the library declares no ``SIZE`` at all) -- callers fall back to the
    existing fixed tolerance in that case, matching this module's
    "malformed/absent input never raises" convention elsewhere.
    """
    macros_by_name = {
        macro["name"]: macro
        for macro in cell_lef_macros
        if macro["width_um"] is not None and macro["height_um"] is not None
    }
    max_overhang_um = 0.0
    for cell in top_only.each_cell():
        macro = macros_by_name.get(cell.name)
        if macro is None or cell.is_empty():
            continue
        bbox_um = cell.bbox().to_dtype(top_only.dbu)
        overhang_um = max(
            0.0,
            -bbox_um.left,
            -bbox_um.bottom,
            bbox_um.right - macro["width_um"],
            bbox_um.top - macro["height_um"],
        )
        max_overhang_um = max(max_overhang_um, overhang_um)
    return max_overhang_um


def _lef_net_layer_gds_map(map_path: str | None) -> dict[str, tuple[int, int]]:
    """``{<LEF layer name>: (<GDS layer>, <GDS datatype>)}`` for the ``NET``
    purpose of every entry in the open_pdks KLayout LEF/DEF layer-map file at
    ``map_path`` -- i.e. the exact ``(layer, datatype)`` KLayout's own DEF
    reader draws that LEF layer's *routed net* geometry on, which is also the
    ``(layer, datatype)`` an extraction deck lists in its own ``metals``
    (sky130: ``li1`` -> ``67/20``, ``met1`` -> ``68/20``, ...).

    File shape is the same whitespace-delimited
    ``<lef_layer_name> <comma-separated purposes> <gds_layer> <gds_datatype>``
    ``lef_abstract.py``'s own :func:`~klayout_tools.lef_abstract.
    _load_gds_to_lef_layer_map` parses -- this is the same file read in the
    opposite direction (LEF name -> GDS pair, restricted to the one purpose
    that names drawn routing), kept local here for the same
    "each verb module is self-contained" reason :func:`~klayout_tools.
    place_and_route._resolve_layer_map` is duplicated between the two
    modules.

    Returns ``{}`` for ``None`` (no map file resolved) or an unreadable/
    malformed file -- callers treat that as "cannot resolve", never as an
    error, matching :func:`~klayout_tools.place_and_route._resolve_layer_map`'s
    own degrade-gracefully posture.
    """
    if map_path is None:
        return {}
    mapping: dict[str, tuple[int, int]] = {}
    try:
        with open(map_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split("#", 1)[0].split()
                if len(parts) < 4 or parts[0] in ("NAME", "DIEAREA"):
                    continue
                if "NET" not in parts[1].split(","):
                    continue
                try:
                    mapping.setdefault(parts[0], (int(parts[-2]), int(parts[-1])))
                except ValueError:
                    continue
    except OSError:
        return {}
    return mapping


def _box_is_inside_drawn_geometry(
    kdb: Any, top_cell: Any, layer_index: int, box: Any
) -> bool:
    """Whether ``box`` lies entirely within geometry already drawn on
    ``layer_index`` somewhere under ``top_cell`` (recursively, i.e. including
    a placed standard cell's own shapes).

    Region-based rather than a per-shape point test so paths, polygons and
    boxes are all handled identically, and so a box straddling two abutting
    shapes still counts as covered. Bounded to the shapes actually touching
    ``box`` (a tiny marker square), so this is a cheap region query, not a
    full-layer flatten.
    """
    covering = kdb.Region(top_cell.begin_shapes_rec_touching(layer_index, box))
    return (kdb.Region(box) - covering).is_empty()


def _stamp_single_pin_def_net_names(
    kdb: Any,
    *,
    layout: Any,
    top_cell: Any,
    def_path: str,
    lef_paths: Iterable[str],
    layer_map_path: str | None,
) -> dict[str, Any]:
    """Give every **unrouted single-pin** DEF net a shape carrying its own DEF
    net name, so ``klt extract --def-net-names`` can recover it (issue #1488).

    KLayout's LEF/DEF reader stamps ``net_property_name`` (shape property
    ``1``) only onto the routed-metal geometry it draws *in the top cell*
    from a ``NETS``/``SPECIALNETS`` record's ``ROUTED``/``NEW`` wires. A net
    with exactly one instance pin and nothing to route to -- a tie cell's
    output, a synthesis-inserted constant driver -- has no such wire, so
    nothing anywhere carried its name and ``--def-net-names`` fell back to
    extraction's synthesized ``$<id>``. That is not cosmetic: several
    structurally identical unnamed nets are exactly what makes ``klt lvs``
    report an ambiguous-pairing ``topology`` warning per net.

    The net's only physical presence is its instance's **pin geometry**,
    which lives inside the standard cell's own macro cell (shared by every
    instance of that cell type) rather than in the top cell -- so no change
    to what property id ``extract_abstract._def_net_name_probes`` reads could
    have found it. This resolves the pin to a top-cell *point* instead, and
    draws the missing name carrier there:

    1. :func:`~klayout_tools.extract_spef.def_net_instance_pins` parses the
       DEF's own ``NETS`` section into ``{net: ((instance, pin), ...)}`` --
       already built (and tested) for issue #961's SPEF ``*CONN``
       correlation, and already covering wireless records.
    2. Nets a routed-metal shape *already* names are skipped, so the
       well-tested issue #951 path is untouched. (A single-pin net can still
       be routed: ``def_net_instance_pins`` drops top-level ``PIN`` design-port
       connections, so a port net with one instance pin looks single-pin here
       while genuinely carrying wires.)
    3. Each remaining net's one ``(instance, pin)`` is resolved to a placement
       via :data:`_DEF_INSTANCE_NAME_PROPERTY_ID`, and to macro-local pin
       geometry via :func:`~klayout_tools.lef_header.read_lef_macro_pin_ports`
       -- the same LEF-pin-plus-instance-transform technique
       ``extract_abstract.py``'s ``--abstract-cells`` pin resolution uses.
    4. A :data:`_SINGLE_PIN_NET_MARKER_HALF_DBU`-sized marker box carrying the
       DEF name under the same shape property id is drawn at that point, on
       the pin's own layer -- but **only** where the merged layout already has
       drawn conductor covering it, which is what makes the marker both a
       geometric no-op and a point ``LayoutToNetlist.probe_net`` resolves to
       the real net rather than to an isolated island.

    Returns ``{"single_pin_markers": <int>, "unresolved_single_pin_nets":
    [<net name>, ...]}``. A net lands in ``unresolved_single_pin_nets`` when
    any step above cannot be completed (no layer-map file to resolve the LEF
    ``PORT`` layer through, a macro/pin the LEF never declares, a pin whose
    centre is not covered by drawn geometry) -- never an error: the merge
    still produces its GDS and that net simply keeps today's synthesized
    ``$<id>`` fallback downstream.
    """
    # Local import, mirroring `_post_route_spef_metrics`'s own -- and the
    # module's `klayout.db` convention -- rather than a module-level one.
    from .extract import _DEF_NET_NAME_PROPERTY_ID, def_net_instance_pins

    report: dict[str, Any] = {
        "single_pin_markers": 0,
        "unresolved_single_pin_nets": [],
    }

    single_pin_nets = {
        net_name: pins[0]
        for net_name, pins in def_net_instance_pins(def_path).items()
        if len(pins) == 1
    }
    if not single_pin_nets:
        return report

    named_by_routing: set[str] = set()
    for layer_index in layout.layer_indexes():
        for shape in top_cell.shapes(layer_index).each():
            value = shape.property(_DEF_NET_NAME_PROPERTY_ID)
            if isinstance(value, str) and value:
                named_by_routing.add(value)
    pending = {
        net_name: connection
        for net_name, connection in single_pin_nets.items()
        if net_name not in named_by_routing
    }
    if not pending:
        return report

    placements: dict[str, tuple[str, Any]] = {}
    for inst in top_cell.each_inst():
        properties = inst.properties()
        component = (
            properties.get(_DEF_INSTANCE_NAME_PROPERTY_ID) if properties else None
        )
        if isinstance(component, str) and component:
            placements.setdefault(component, (inst.cell.name, inst.cplx_trans))

    pin_ports: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for lef_path in lef_paths:
        try:
            pin_ports.update(read_lef_macro_pin_ports(lef_path))
        except OSError:
            continue

    lef_to_gds = _lef_net_layer_gds_map(layer_map_path)
    dbu = layout.dbu
    half = _SINGLE_PIN_NET_MARKER_HALF_DBU
    unresolved: list[str] = []

    for net_name, (component, pin_name) in pending.items():
        placement = placements.get(component)
        if placement is None:
            unresolved.append(net_name)
            continue
        macro_cell, trans = placement
        stamped = False
        for port in pin_ports.get(macro_cell, {}).get(pin_name, ()):
            gds_layer = lef_to_gds.get(port["layer"])
            if gds_layer is None:
                continue
            layer_index = layout.find_layer(*gds_layer)
            if layer_index is None:
                continue
            x0, y0, x1, y1 = port["bbox_um"]
            point = trans * kdb.Point(
                round((x0 + x1) / 2 / dbu), round((y0 + y1) / 2 / dbu)
            )
            marker = kdb.Box(
                point.x - half, point.y - half, point.x + half, point.y + half
            )
            if not _box_is_inside_drawn_geometry(kdb, top_cell, layer_index, marker):
                continue
            shape = top_cell.shapes(layer_index).insert(marker)
            shape.set_property(_DEF_NET_NAME_PROPERTY_ID, net_name)
            stamped = True
            break
        if stamped:
            report["single_pin_markers"] += 1
        else:
            unresolved.append(net_name)

    report["unresolved_single_pin_nets"] = sorted(unresolved)
    return report


def _merge_def_to_gds(
    *,
    def_path: str,
    tech_lef: str,
    cell_lef: str,
    pdk_info: dict[str, Any],
    cell_library: str,
    hdl_toplevel: str,
    macros: list[dict[str, Any]],
    out_path: str,
) -> dict[str, Any]:
    """Merge the routed DEF with the resolved standard-cell GDS view into a
    single GDS -- ported directly from ORFS's ``def2stream.py`` (survey
    section 4) onto this repo's ``klayout.db`` package, in-process. Never
    shells out to a ``klayout`` binary (matching ``klt drc``'s existing
    in-process ``pya`` posture).

    ``macros`` (issue #438) additionally merges each hard macro's own GDS
    view (when its request entry declared one via ``gds``) alongside the
    standard-cell GDS view, keyed by the macro's own LEF ``MACRO`` name
    (``macro["cell_name"]``, resolved once at request-validation time via
    :func:`klayout_tools.lef_header.read_lef_header` -- the DEF reader
    creates one cell per distinct macro reference, named after its LEF
    ``MACRO``, exactly like a standard cell). A macro with no declared
    ``gds`` is exempted from the "missing (unmatched) cell" check below
    instead -- an abstract-only macro instance (this issue's own DEF-level
    placement/obstruction verification does not need a real GDS view) is
    expected to stay empty, not an error.

    Returns ``{"path": <str | None>, "resolution": <str>, "def_net_names":
    {...}, "min_area_repair": {...}}``. ``path``/``resolution`` describe the
    :func:`~klayout_tools.place_and_route._resolve_layer_map` result actually
    applied (or not) to this merge, so the caller can surface it in the
    response envelope's ``layer_map`` field (issue #1029) -- a caller has no
    other way to tell whether the merged GDS's routing shapes got a
    guaranteed-matching layer/datatype assignment. ``def_net_names`` is
    :func:`_stamp_single_pin_def_net_names`'s own report (issue #1488), which
    the caller surfaces as the sibling ``def_net_names`` response field.
    ``min_area_repair`` is
    :func:`~klayout_tools.place_and_route_min_area.repair_min_area`'s own
    report (issue #2139) -- what the post-route minimum-area repair pass
    drew, and what it could **not** repair safely -- surfaced as the sibling
    ``min_area_repair`` response field for the same reason: a caller has no
    other way to tell whether the merged GDS it just got back really clears
    the PDK's own ``*.area.*`` rules.
    """
    # Deferred import: `place_and_route.py` imports this function back at
    # module scope (to preserve `klayout_tools.place_and_route.
    # _merge_def_to_gds` as a working import/monkeypatch path), so importing
    # these names from there at *this* module's own top level would be a
    # circular import. Mirrors `place_and_route_sta.py`'s identical pattern
    # for the reverse dependency. `read_lef_header` is included here (rather
    # than imported directly from `lef_header.py`, like
    # `read_lef_macro_pin_ports` at this module's own top level) because the
    # test suite monkeypatches `place_and_route.read_lef_header` expecting
    # this function to observe it -- only a deferred, looked-up-by-name
    # import makes that visible.
    import klayout.db as kdb

    from .place_and_route import (
        PlaceAndRouteError,
        _resolve_gds_view,
        _resolve_layer_map,
        read_lef_header,
    )

    cell_gds = _resolve_gds_view(pdk_info, cell_library)
    layer_map_path, layer_map_resolution = _resolve_layer_map(pdk_info)

    opts = kdb.LoadLayoutOptions()
    lefdef_config = opts.lefdef_config
    lefdef_config.lef_files = [tech_lef, cell_lef, *(macro["lef"] for macro in macros)]
    if layer_map_path is not None:
        lefdef_config.map_file = layer_map_path

    # Issue #1032: KLayout's DEF reader silently proceeds when the target
    # layout DBU it is configured for does not match the DEF file's own
    # declared `UNITS DISTANCE MICRONS` value, producing wrong/lossy
    # via-cut geometry as a side effect (only a `Warning:` line, never a
    # raised error). Left unset, `lefdef_config.dbu` inherits KLayout's
    # compiled-in default (0.001, i.e. `DATABASE MICRONS 1000`), which
    # happens to match sky130 but is wrong for any PDK whose own tech LEF
    # declares a different `DATABASE MICRONS` value (e.g. gf180mcu's 2000).
    # Resolve the tech LEF's own declared value and set the reader's target
    # DBU to match; when the tech LEF doesn't declare one (or the
    # regex-based parser can't extract it), fall back to KLayout's existing
    # default DBU behavior -- `read_lef_header` never raises on malformed
    # input, so `database_microns is None` is a normal, expected case here.
    # Captured as the full header (not just `database_microns`) so the
    # post-merge DIEAREA guard below can reuse its own `sites` entry rather
    # than re-parsing `tech_lef` a second time.
    tech_lef_header = read_lef_header(tech_lef)
    database_microns = tech_lef_header["database_microns"]
    if database_microns is not None:
        lefdef_config.dbu = 1.0 / database_microns

    main_layout = kdb.Layout()
    try:
        main_layout.read(def_path, opts)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise PlaceAndRouteError(
            f"could not read DEF '{def_path}' for GDS merge: {exc}"
        ) from exc

    top_cell = main_layout.cell(hdl_toplevel)
    if top_cell is None:
        raise PlaceAndRouteError(
            f"DEF '{def_path}' does not define top cell '{hdl_toplevel}'"
        )
    top_cell_index = top_cell.cell_index()

    # Clear every non-top cell except LEF-via cells (KLayout prepends
    # "VIA_" when reading a DEF that instantiates a LEF via) and DEF fill
    # cells -- matching def2stream.py's own orphan-cell handling exactly.
    for cell in main_layout.each_cell():
        if cell.cell_index() != top_cell_index:
            if not cell.name.startswith("VIA_") and not cell.name.endswith("_DEF_FILL"):
                cell.clear()

    try:
        _merge_gds_view(kdb, main_layout, cell_gds)
    except Exception as exc:
        raise PlaceAndRouteError(
            f"could not read standard-cell GDS view '{cell_gds}' for GDS merge: {exc}"
        ) from exc

    for macro in macros:
        if macro["gds"] is None:
            continue
        try:
            _merge_gds_view(kdb, main_layout, macro["gds"])
        except Exception as exc:
            raise PlaceAndRouteError(
                f"could not read macro GDS view '{macro['gds']}' for GDS merge: {exc}"
            ) from exc

    top_only = kdb.Layout()
    top_only.dbu = main_layout.dbu
    top = top_only.create_cell(hdl_toplevel)
    top.copy_tree(main_layout.cell(hdl_toplevel))

    # Issue #1488: give each *unrouted single-pin* DEF net a shape carrying
    # its own DEF net name, since the reader only ever stamped
    # `net_property_name` onto routed geometry. Runs on the merged
    # `top_only`/`top` pair (not `main_layout`) because the coverage guard
    # inside needs the standard-cell GDS view's real drawn conductor, and
    # before the checks below so everything they verify covers the final,
    # as-written geometry.
    def_net_names_info = _stamp_single_pin_def_net_names(
        kdb,
        layout=top_only,
        top_cell=top,
        def_path=def_path,
        lef_paths=[cell_lef, *(macro["lef"] for macro in macros)],
        layer_map_path=layer_map_path,
    )

    # Abstract-only macro instances (no `gds` declared) are expected to stay
    # empty -- exempt their own LEF MACRO cell name from the missing-cell
    # check, mirroring the VIA_/_DEF_FILL exemption above.
    abstract_only_macro_cells = {
        macro["cell_name"] for macro in macros if macro["gds"] is None
    }
    missing = sorted(
        cell.name
        for cell in top_only.each_cell()
        if cell.is_empty() and cell.name not in abstract_only_macro_cells
    )
    if missing:
        raise PlaceAndRouteError(
            "DEF/GDS merge produced empty (unmatched) cells: "
            + ", ".join(missing[:10])
            + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
        )

    orphans = sorted(
        cell.name
        for cell in top_only.each_cell()
        if cell.name != hdl_toplevel and cell.parent_cells() == 0
    )
    if orphans:
        raise PlaceAndRouteError(
            "DEF/GDS merge produced orphan cells: " + ", ".join(orphans[:10])
        )

    # Post-merge sanity check (issue #1090's second, independent fix): a
    # merged top cell whose bounding box extends beyond the DEF's own
    # `DIEAREA` is a defect no valid design can produce on its own (OpenROAD
    # never places/routes outside the declared die boundary) -- exactly the
    # symptom a DBU mismatch between the DEF-derived geometry and a
    # subsequently-merged GDS view produces (a doubled/halved coordinate
    # system silently blows the merged extent past the die). This is a
    # regression guard, not just a check for *this* fix: it fires even if a
    # future edit reintroduces a bare, DBU-unsafe `main_layout.read(...)`
    # call in place of :func:`_merge_gds_view`. Skipped (rather than
    # raising) when the DEF carries no parseable `DIEAREA`, matching
    # `_parse_def_die_area_um`'s own "malformed/absent input never raises"
    # convention.
    die_area_um = _parse_def_die_area_um(def_path)
    if die_area_um is not None:
        die_x0, die_y0, die_x1, die_y1 = die_area_um
        bbox_um = top.bbox().to_dtype(top_only.dbu)
        # A small absolute tolerance (not a DBU-doubling-scale-sized one)
        # absorbs ordinary OFFGRID/rounding overhang at the die boundary
        # without masking the doubling/halving this check exists to catch.
        #
        # Issue #1335: that fixed 0.01 um tolerance rejected every gf180mcu
        # `request.power` run, even though it is the only gf180mcu
        # configuration that is actually DRC-clean. open_pdks standard-cell
        # libraries (gf180mcu in particular) deliberately draw geometry
        # beyond their own LEF abutment box so abutting cells' wells/
        # implants merge -- a legitimate overhang a fixed few-hundredths-of-
        # a-micron tolerance cannot tell apart from a DBU-doubling/halving
        # defect (which blows the merged extent past the die by an amount
        # on the order of the die's own size, not a single cell/row).
        # Expand the tolerance by the merged cell library's own *measured*
        # overhang (:func:`_cell_library_max_overhang_um`), but cap that
        # measurement at the tech LEF's own row (`SITE`) height: an
        # overhang beyond one row already means something else is wrong --
        # well/implant-merge geometry never spans multiple rows -- so the
        # tolerance stays physically bounded regardless of what the
        # measurement itself reports, and can never absorb a doubling/
        # halving-scale defect. Falls back to the fixed 0.01 um tolerance
        # (today's behavior, unchanged) when the tech LEF declares no
        # `SITE` to derive that ceiling from.
        tolerance_um = 0.01
        overhang_ceiling_um = max(
            (
                site["height_um"]
                for site in tech_lef_header["sites"]
                if site["height_um"] is not None
            ),
            default=None,
        )
        if overhang_ceiling_um is not None:
            cell_lef_macros = read_lef_header(cell_lef)["macros"]
            measured_overhang_um = _cell_library_max_overhang_um(
                top_only, cell_lef_macros
            )
            tolerance_um = max(
                tolerance_um, min(measured_overhang_um, overhang_ceiling_um)
            )
        if (
            bbox_um.left < die_x0 - tolerance_um
            or bbox_um.bottom < die_y0 - tolerance_um
            or bbox_um.right > die_x1 + tolerance_um
            or bbox_um.top > die_y1 + tolerance_um
        ):
            raise PlaceAndRouteError(
                "DEF/GDS merge produced a top cell bounding box "
                f"({bbox_um.left:.4f}, {bbox_um.bottom:.4f}) .. "
                f"({bbox_um.right:.4f}, {bbox_um.top:.4f}) um that extends "
                f"beyond the DEF's own DIEAREA ({die_x0:.4f}, {die_y0:.4f}) "
                f".. ({die_x1:.4f}, {die_y1:.4f}) um by more than this "
                f"merge's own {tolerance_um:.4f} um tolerance (the fixed "
                "0.01 um default, or the merged cell library's own measured "
                "abutment-box overhang when larger) -- possible DBU "
                "mismatch between the DEF-derived geometry and a merged GDS "
                "view"
            )

    # Issue #2139: floor every routed-metal polygon against its own layer's
    # minimum-*area* rule before writing. The tech LEF's own via cells
    # (sky130's `L1M1_PR_MR`/`M2M3_PR`) and OpenROAD's PDN via patches each
    # draw metal that is legal in isolation but below the layer's
    # `*.area.*` floor once it is the whole merged polygon -- the
    # `place-and-route` half of the gap #2075 fixed for `gen-compose`'s
    # landing pads and explicitly could not reproduce here. Runs last, on
    # the final `top_only`/`top` geometry: after the DIEAREA guard above
    # (which must judge the *merge's* own extent, not this pass's patches,
    # and whose parsed die box is reused here to keep every patch inside
    # it), and after the #1488 marker pass (whose markers are drawn inside
    # existing conductor and so change no merged polygon's area).
    min_area_info = repair_min_area(
        kdb,
        layout=top_only,
        top_cell=top,
        variant=pdk_info["variant"],
        routing_layers=set(_lef_net_layer_gds_map(layer_map_path).values()),
        die_area_um=die_area_um,
    )

    # write_layout() (see _layout.py, #320) always disables KLayout's default
    # wall-clock GDS2 BGNLIB/BGNSTR timestamps, so re-running an identical
    # place-and-route request twice produces a byte-identical merged GDS --
    # not just a byte-identical DEF (#1367).
    write_layout(top_only, out_path, PlaceAndRouteError)

    return {
        "path": layer_map_path,
        "resolution": layer_map_resolution,
        "def_net_names": def_net_names_info,
        "min_area_repair": min_area_info,
    }
