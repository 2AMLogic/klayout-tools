"""Cell-level (black-box + pins) abstraction for ``klt extract --abstract-cells``.

Split out of ``extract.py`` (issue #1303) as a self-contained subsystem: given
a set of ``--abstract-cells`` glob patterns, this module finds every matching
cell instance (:func:`_collect_abstract_instances`), erases its interior
geometry so extraction never descends into it
(:func:`_abstract_cell_mask_layers` / :func:`_erase_abstracted_cell_geometry`),
resolves each of its declared LEF-macro pins to a real point in the abstracted
instance's footprint (:func:`_load_abstract_cell_lefs` /
:func:`_local_pin_candidate_points` / :func:`_resolve_abstract_cell_pins`),
and wires that black box back into the surrounding extracted netlist as a
``kdb.SubCircuit`` (:func:`_wire_abstract_cells`) -- the whole "cell-level
(black-box + pins) abstraction" feature from issue #620. It also carries the
independent ``--def-net-names`` net-renaming pass
(:func:`_def_net_name_probes` / :func:`_apply_def_net_name_overrides`, issue
#951), which shares this module's DEF shape-property scanning but is
otherwise unrelated to cell abstraction; it stayed alongside these functions
in ``extract.py`` before this split and is moved verbatim rather than split
again.

This is the same shape as the SPEF-export split (``extract_spef.py``, issue
#1195): a self-contained, single-purpose subsystem that used to live inline
in the 8800-line extraction engine, with only two call sites, both in
``extract.py`` (``run_extract_klayout_engine``'s pre-extraction erase/mask
setup and ``_extract_netlist``'s pin resolution + wiring). Every function
here takes the caller's ``layout``/``top_cell``/``deck`` as explicit
parameters -- nothing closes over ``extract.py``'s module state.

``_sanitize_instance_name`` lives here (rather than beside the rest of the
parasitics-injection code in ``extract.py``, which also uses it) purely to
keep the dependency one-directional: ``extract.py`` imports it back from
here, the same way it already imports ``ExtractError``/``_write_spef`` from
``extract_spef.py``, rather than this module reaching back into
``extract.py``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import TYPE_CHECKING, Any

from ._layout import region as _region
from .decks import ExtractionDeck
from .extract_spef import ExtractError
from .lef_header import read_lef_macro_pin_ports

if TYPE_CHECKING:
    from collections.abc import Iterable

    import klayout.db as kdb

#: The GDS shape-property id under which KLayout's own LEF/DEF reader records
#: each routed-net (``NETS``/``SPECIALNETS``) shape's **DEF net name** --
#: ``LEFDEFReaderConfiguration.net_property_name``, whose KLayout default is
#: ``1``. ``klayout_tools.place_and_route._merge_def_to_gds`` never overrides
#: it, and GDS ``PROPATTR``/``PROPVALUE`` round-trips it, so a GDS produced by
#: ``klt place-and-route``'s ``"route"`` stage already carries the design's
#: own (Verilog/DEF/OpenSTA) net names -- verified directly against the
#: committed ``tests/corpus/place_and_route/gcd.gds.gz`` fixture, which
#: carries all 458 of that design's DEF net names on its routed metal
#: (issue #951, no fixture regeneration required).
#:
#: This is what ``--def-net-names`` (:func:`run_extract`'s ``def_net_names``)
#: reads. It matters because extraction's *default* naming source -- GDS text
#: labels -- cannot see those names: the DEF->GDS merge emits label texts for
#: top-level pins only, so an internal routed net is named either by KLayout's
#: synthesised ``$<id>`` placeholder or by joining whatever standard-cell pin
#: labels happen to touch it (``A,X``), and neither is what OpenSTA calls that
#: net (``_019_``, ``req_msg[3]``, ...).
_DEF_NET_NAME_PROPERTY_ID = 1

#: How many independent probe points :func:`_def_net_name_probes` keeps per
#: DEF net name -- see :func:`_apply_def_net_name_overrides` for why more than
#: one.
_DEF_NET_NAME_PROBE_CANDIDATES = 4

#: Every character SPICE's own instance-name grammar does not admit bare --
#: see :func:`_sanitize_instance_name`.
_INSTANCE_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_instance_name(name: str) -> str:
    """Map every character outside ``[A-Za-z0-9_]`` in a parasitic *device
    instance* name to ``_`` (issue #312).

    Device instance names are cosmetic handles -- nothing downstream matches
    an ``R``/``C`` card's instance name against the raw net name it was
    derived from (the ``devices[]``/``nets[]`` JSON and the netlist's ``*
    device instance ...`` comment carry the real identity; *node* names are
    escaped separately, by KLayout's own ``NetlistSpiceWriter``). Left
    unsanitized, a net's ``expanded_name()`` can carry characters a SPICE
    reader treats as syntax rather than an opaque token: ``$`` (KLayout's
    anonymous/unlabelled-net placeholder, e.g. ``$12``) and ``,`` (KLayout's
    join character when multiple text labels land on one net, e.g.
    ``Y,Y2``). ngspice does not reject either -- it silently splits the
    comma-joined form into extra tokens, corrupting the card's arity and
    surfacing a confusing error against an unrelated node.
    """
    return _INSTANCE_NAME_UNSAFE_RE.sub("_", name)


def _matches_abstract_pattern(name: str, patterns: tuple[str, ...]) -> bool:
    """``True`` when the cell ``name`` matches any of the ``--abstract-cells``
    glob ``patterns``.

    Case-sensitive (``fnmatch.fnmatchcase``): GDSII/OASIS cell names are
    case-sensitive, and ``fnmatch.fnmatch`` would otherwise fold case on a
    case-insensitive filesystem only -- a platform-dependent match is exactly
    the kind of surprise a layout-processing contract must not have.
    """
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _collect_abstract_instances(
    layout: kdb.Layout, top_cell: kdb.Cell, patterns: tuple[str, ...]
) -> list[tuple[int, kdb.ICplxTrans]]:
    """Every instance of a pattern-matched cell type under ``top_cell``, as
    ``[(cell_index, transform-into-top-cell-coordinates), ...]``.

    Walks the instance tree explicitly rather than through
    ``begin_shapes_rec``, because the per-instance transform is exactly what
    a pin footprint has to be resolved through (acceptance criterion 6):
    ``CellInstArray.each_cplx_trans()`` yields one ``ICplxTrans`` per array
    element, already carrying that element's rotation/mirror *and* its
    displacement within the array, so a mirrored or rotated instance of the
    same abstracted cell type resolves its pins at the right places for free.

    A matched branch is **never descended into**: an abstracted cell that
    itself instantiates another matched cell type is abstracted as a single
    outermost black box, not twice. The top cell itself is never abstracted
    even if a pattern matches its name -- ``klt extract`` extracts *from* the
    top cell, so black-boxing it would produce an empty netlist rather than a
    hierarchical one.

    Returned in a deterministic order: sorted by ``(cell name, displacement
    x, displacement y, transform string)``, so the ``X<instance>`` cards the
    caller emits carry stable names across runs.
    """
    import klayout.db as kdb

    found: list[tuple[int, kdb.ICplxTrans]] = []

    def walk(cell: kdb.Cell, trans: kdb.ICplxTrans) -> None:
        for inst in cell.each_inst():
            target = layout.cell(inst.cell_index)
            matched = _matches_abstract_pattern(target.name, patterns)
            for element in inst.cell_inst.each_cplx_trans():
                composed = trans * element
                if matched:
                    found.append((inst.cell_index, composed))
                else:
                    walk(target, composed)

    walk(top_cell, kdb.ICplxTrans())

    found.sort(
        key=lambda entry: (
            layout.cell(entry[0]).name,
            entry[1].disp.x,
            entry[1].disp.y,
            str(entry[1]),
        )
    )
    return found


def _abstract_cell_mask_layers(deck: ExtractionDeck) -> set[tuple[int, int]]:
    """Every deck-read layer that must be erased from an abstracted cell's
    own definition so no device is recognised inside it (issue #620).

    Defined as ``ExtractionDeck.connectivity_layers`` (every layer this
    deck's connectivity graph reads at all -- already the curated set behind
    the ``ignored_layers`` diagnostic, so it automatically covers every
    device-recognition/marker layer any current or future deck declares,
    including resistor/bipolar/diode markers and capacitor plates) *minus*:

    - the routing/interconnect layers (``contact``, ``metals``, ``vias``) --
      a parent's connection to a black-boxed cell's pin must still reach
      through the cell's own local interconnect, so these are left intact;
    - the label layers (``well_label``, ``poly_label``, ``metal_labels``) --
      :func:`_resolve_abstract_cell_pins` reads a pin's name and access point
      directly off these, in the cell's own (otherwise-erased) definition.

    A resistor/capacitor whose recognition layer happens to be one of the
    deck's own ``metals`` (a real but rare deck configuration) is a known,
    documented gap: that layer is never erased (it is routing), so such a
    device could still be recognised inside an abstracted cell. Every
    curated deck shipped in this repo declares resistor/capacitor bodies on
    ``poly``/``active``/a dedicated plate layer, never directly on
    ``metals``, so this does not affect ``sky130``/``gf180mcu`` today.
    """
    routing = {deck.contact, *deck.metals, *deck.vias}
    labels = {deck.well_label, deck.poly_label, *deck.metal_labels}
    return {
        layer
        for layer in deck.connectivity_layers
        if layer not in routing and layer not in labels
    }


def _erase_abstracted_cell_geometry(
    layout: kdb.Layout,
    cell_indices: Iterable[int],
    mask_layers: set[tuple[int, int]],
) -> None:
    """Erase every ``mask_layers`` shape from each of ``cell_indices`` and
    every cell it (transitively) calls -- issue #620's black-box
    abstraction.

    Mutates ``layout`` in place: every downstream ``_region()``/``_texts()``
    call (which always re-derives its ``Region``/``Texts`` fresh from
    ``layout``) then sees these cells as if they carried no
    device-recognition geometry at all, with no changes needed anywhere else
    in :func:`_extract_netlist`'s device-recognition passes -- including
    ones (bipolar/diode/capacitor) that re-read straight from ``layout``
    rather than through a locally-masked ``Region`` variable. Safe because
    ``layout`` is loaded fresh, once, for this one extraction run
    (:func:`extract_netlist_from_layout`) and never shared or cached across
    calls.

    A cell type matched by ``--abstract-cells`` is assumed to be used
    *exclusively* as a black box wherever it is instantiated in this stream
    -- erasing its own definition directly affects every instance, not just
    the ones reachable from the extraction top cell, and that is fine (there
    is no cheaper way to erase "this instance only" for a shared cell
    definition, and abstracting the same standard cell/macro differently in
    different places is not a meaningful operation LVS could compare against
    anyway).

    A **called** cell (one of ``cell_indices``' children, transitively) makes
    no such promise, though: KLayout cell definitions are shared across every
    place they are instantiated, and a cell used inside a matched macro may
    also be instantiated independently *outside* every matched subtree (e.g.
    a standard cell reused both inside an abstracted macro and directly at
    top level). Erasing a called cell's definition in place would silently
    destroy that unrelated instance's devices too. So each matched cell's
    *own* instances of children are instead repointed at a private,
    per-child-cell "shadow" duplicate (:meth:`kdb.Cell.copy_tree` -- a fresh,
    unshared copy of the child's entire subtree, at every depth) before
    erasing -- the shadow is never referenced by anything outside a matched
    cell's own hierarchy, so erasing it can never affect a sibling instance
    of the same cell type used elsewhere.
    """

    def clear_mask_layers(cell_index: int) -> None:
        cell = layout.cell(cell_index)
        for layer in mask_layers:
            layer_index = layout.find_layer(*layer)
            if layer_index is not None:
                cell.shapes(layer_index).clear()

    # Original called-cell index -> private, unshared duplicate of its whole
    # subtree. Shared across every matched cell that calls the same child
    # cell type -- both sides of that sharing are themselves matched (about
    # to be erased), so reusing one shadow between them is safe.
    shadow_cells: dict[int, int] = {}

    def shadow_for(child_index: int) -> int:
        shadow_index = shadow_cells.get(child_index)
        if shadow_index is not None:
            return shadow_index
        child_cell = layout.cell(child_index)
        shadow = layout.create_cell(
            layout.unique_cell_name(f"{child_cell.name}$abstract")
        )
        shadow.copy_tree(child_cell)
        shadow_index = shadow.cell_index()
        shadow_cells[child_index] = shadow_index
        for ci in {shadow_index, *shadow.called_cells()}:
            clear_mask_layers(ci)
        return shadow_index

    seen: set[int] = set()
    for cell_index in cell_indices:
        if cell_index in seen:
            continue
        seen.add(cell_index)
        cell = layout.cell(cell_index)
        clear_mask_layers(cell_index)
        for inst in list(cell.each_inst()):
            inst.cell_index = shadow_for(inst.cell_index)


def _texts_excluding_abstract_cells(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    layer: tuple[int, int] | None,
    patterns: tuple[str, ...],
) -> kdb.Texts:
    """:func:`_texts` restricted to labels drawn *outside* every abstracted
    cell type (issue #620).

    An abstracted cell's own in-cell labels are its **pin names**, not the
    parent's net names: left in the flat label collection they would rename
    (or comma-merge into) whatever top-level net the pin happens to touch --
    e.g. every instance of an inverter contributing its internal ``A`` label
    to a different routing net. Stripping them keeps the un-abstracted
    portion's net naming exactly what it would be if the cell were a hard
    macro with no drawn labels at all.
    """
    import klayout.db as kdb

    texts = kdb.Texts()
    if layer is None:
        return texts
    layer_index = layout.find_layer(*layer)
    if layer_index is None:
        return texts

    def walk(cell: kdb.Cell, trans: kdb.ICplxTrans) -> None:
        for text in kdb.Texts(cell.shapes(layer_index)).each():
            texts.insert(text.transformed(trans))
        for inst in cell.each_inst():
            target = layout.cell(inst.cell_index)
            if _matches_abstract_pattern(target.name, patterns):
                continue
            for element in inst.cell_inst.each_cplx_trans():
                walk(target, trans * element)

    walk(top_cell, kdb.ICplxTrans())
    return texts


def _load_abstract_cell_lefs(
    lef_paths: tuple[str, ...],
) -> dict[str, tuple[str, dict[str, list[dict[str, Any]]]]]:
    """Read every ``--abstract-cell-lef`` file into ``{<MACRO name>: (<lef
    path>, {<pin name>: [port box, ...]})}``.

    A path may be a LEF file or a directory (every ``*.lef``/``*.tlef`` file
    directly inside it is read, sorted by name for determinism). A macro
    declared by more than one LEF resolves to the **first** path given, so
    the flag's order is the precedence order -- an explicit block abstract
    passed ahead of a PDK's merged standard-cell LEF wins, rather than the
    result depending on directory iteration order.

    Raises :class:`ExtractError` for an unreadable path -- a mistyped LEF
    path must not silently degrade to "this cell has no LEF fallback".
    """
    macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]] = {}
    for path in lef_paths:
        if os.path.isdir(path):
            entries = sorted(
                os.path.join(path, name)
                for name in os.listdir(path)
                if name.endswith((".lef", ".tlef"))
            )
        else:
            entries = [path]
        for entry in entries:
            try:
                parsed = read_lef_macro_pin_ports(entry)
            except OSError as exc:
                raise ExtractError(
                    f"could not read --abstract-cell-lef '{entry}': {exc}"
                ) from exc
            for macro_name, pins in parsed.items():
                macros.setdefault(macro_name, (entry, pins))
    return macros


def _local_pin_candidate_points(
    layout: kdb.Layout,
    cell: kdb.Cell,
    deck: ExtractionDeck,
) -> dict[str, list[kdb.Point]]:
    """Extra local (cell-frame) candidate access points for every in-cell
    metal-label pin of ``cell``, discovered from the cell's own *pre-erasure*
    internal routing -- issue #1183.

    Must run *before* :func:`_erase_abstracted_cell_geometry` mutates
    ``layout`` (the caller -- :func:`extract_netlist_from_layout` -- already
    guarantees this: it calls this function first, over every matched cell
    type, then erases). :func:`_resolve_abstract_cell_pins`'s
    ``"in_cell_labels"`` path resolves a pin to exactly the one point where
    its name label sits -- but a real standard cell's pin can legally carry
    *several* geometrically disjoint metal shapes for one electrical node
    (the same "one LEF pin, several PORT rectangles" shape #1181/#1182
    already fixed for the ``"lef_abstract"`` path), and nothing guarantees
    the label sits on the specific rectangle that ends up externally routed
    -- the label is drawn once, at design-library time, long before any
    particular instance's place-and-route decides which rectangle a via
    actually lands on. When the label's own rectangle is a different,
    internally-isolated fragment (tied to the routed one only through the
    cell's own poly/diffusion -- exactly the connectivity
    :func:`_erase_abstracted_cell_geometry` deliberately severs for
    black-box abstraction), the label-only point resolves onto that isolated
    island instead of the routed net (this issue's reported failure,
    confirmed against the real ``gf180-trng`` ``clkload13``/``I`` case this
    issue cites: the label's own local point resolves to an isolated net
    while a probe at the DEF-verified via location resolves to the correctly
    merged, externally-routed net).

    This computes a *local*, cell-scoped ``LayoutToNetlist`` connectivity
    pass -- deliberately narrower than :func:`_extract_netlist`'s full
    connectivity graph -- restricted to ordinary-conductor connectivity only:
    ``active``/``poly`` split into gate (``active & poly``) and source/drain
    (``active - poly``) regions exactly as :func:`_extract_netlist` computes
    its own default (unflavoured) split, then ``contact``/``metals``/
    ``vias``, using the same per-level connect chain. Deliberately omits
    ``nwell``/``tap``/``mos_flavours``/``substrate_isolation``/dummy-marker
    handling -- none of those affect *signal*-pin metal connectivity (they
    scope device recognition and well/substrate-tie nets, which this
    function is not concerned with), so skipping them only ever
    *under*-connects relative to :func:`_extract_netlist`'s full graph, never
    over-connects: every extra candidate point this discovers is reachable
    through a connectivity subset :func:`_extract_netlist` itself already
    honours, so it can never introduce a false merge that the real
    extraction pass would not also recognise.

    Returns ``{<pin label text>: [<extra local dbu point>, ...]}`` -- built
    only from labels drawn on one of ``deck.metal_labels`` (the
    overwhelmingly common case for a standard cell's signal pins);
    ``well_label``/``poly_label`` pins are left with their original
    single-point resolution, unchanged. A label whose own point resolves to
    no net at all (nothing drawn there) or to a net with no other shapes on
    its own metal layer contributes no extra points -- the caller merges
    these into (never replaces) the original label point, so this is
    strictly additive: a design with no disjoint-metal-fragment pins is
    unaffected (every label's own point was already the sole candidate, and
    still is).
    """
    import klayout.db as kdb

    active = _region(layout, cell, deck.active)
    poly = _region(layout, cell, deck.poly)
    contact = _region(layout, cell, deck.contact)
    metals = [_region(layout, cell, layer) for layer in deck.metals]
    vias = [_region(layout, cell, layer) for layer in deck.vias]

    gate = active & poly
    source_drain = active - poly

    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, cell, []))
    l2n.connect(source_drain)
    l2n.connect(gate)
    l2n.connect(poly)
    l2n.connect(gate, poly)
    l2n.connect(contact)
    l2n.connect(source_drain, contact)
    l2n.connect(poly, contact)
    if metals:
        l2n.connect(contact, metals[0])
        l2n.connect(metals[0])
        for index in range(len(vias)):
            l2n.connect(metals[index], vias[index])
            l2n.connect(vias[index])
            l2n.connect(vias[index], metals[index + 1])
            l2n.connect(metals[index + 1])
    l2n.extract_netlist()

    extra_points: dict[str, list[kdb.Point]] = {}
    for metal_index, layer in enumerate(deck.metal_labels):
        if layer is None or metal_index >= len(metals):
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        target_region = metals[metal_index]
        for text in kdb.Texts(cell.shapes(layer_index)).each():
            point = kdb.Point(text.x, text.y)
            net = l2n.probe_net(target_region, point)
            if net is None:
                continue
            shapes = kdb.Shapes()
            l2n.shapes_of_net(net, target_region, True, shapes)
            points = extra_points.setdefault(text.string, [])
            for polygon in kdb.Region(shapes).merged().each():
                candidate = _polygon_interior_point(polygon)
                if candidate not in points:
                    points.append(candidate)
    return extra_points


def _polygon_interior_point(polygon: kdb.Polygon) -> kdb.Point:
    """A point *guaranteed* to lie inside ``polygon`` -- never merely inside
    its bounding box (issue #1296).

    ``polygon.bbox()``'s centre only lies on the polygon itself when the
    polygon *is* its own bounding box (a plain rectangle). For anything else
    -- an "L"/"T"/"+"-shaped island formed when
    :func:`_local_pin_candidate_points` merges several disjoint same-layer
    fragments of one locally-connected net, exactly the common case this
    function serves -- the bounding-box centre can land in a concave notch
    the polygon never draws at all. Probing that point later does not fail
    safely: it runs against the *global*, whole-design connectivity graph
    (not this function's cell-local one), so ``LayoutToNetlist.probe_net``
    answers with whatever real geometry another, unrelated instance happens
    to have drawn at that exact coordinate -- e.g. an abutting neighbour's
    own local routing, since routing layers are deliberately left un-erased
    for abstracted cells (see :func:`_abstract_cell_mask_layers`).
    :func:`_probe_abstract_pin_net`'s scoring then prefers that unrelated,
    already-named net over the pin's own correct (often still-unnamed)
    resolution, merging two electrically separate nets into one and
    splitting the net that should have kept both -- confirmed as this
    issue's root cause both by direct reproduction (``bbox().center()``
    computed for a two-rectangle "L" island landed outside the drawn
    geometry, and probing it globally resolved to an unrelated, already-named
    net drawn by a different abstracted-cell instance placed at that
    coordinate) and against the real ``gf180-trng`` digital-section LVS run
    this issue reports (``mismatch_count`` 8 -> 1503).

    Decomposes ``polygon`` into trapezoid pieces
    (``Polygon.decompose_trapezoids``, ``TD_htrapezoids`` -- for the
    Manhattan geometry every conductor layer in this codebase's decks draws,
    each piece is a plain rectangle) and returns the arithmetic mean of the
    *largest* piece's own vertices. A trapezoid piece is convex by
    construction, and the mean of a convex polygon's vertices is a convex
    combination of them -- always inside the polygon, by definition of
    convexity. The *largest* piece is used only so a single, deterministic,
    reasonably-central candidate is chosen from a shape that decomposes into
    several pieces.

    Falls back to the exact bounding-box-centre behaviour when ``polygon``
    ``is_box()`` (the overwhelmingly common case: a single-rectangle
    fragment, or several fragments that happen to merge back into one) --
    for a box, the bounding-box centre already *is* an interior point, so no
    decomposition is needed and every existing single-rectangle-pin design
    resolves exactly as it did before this issue's fix.
    """
    import klayout.db as kdb

    box = polygon.bbox()
    if polygon.is_box():
        return kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2)
    pieces = polygon.decompose_trapezoids(kdb.Polygon.TD_htrapezoids)
    if not pieces:
        # Degenerate/empty polygon -- no worse than this function's
        # pre-#1296 bounding-box-centre behaviour for whatever edge case
        # produced this.
        return kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2)
    largest = max(pieces, key=lambda piece: piece.area())
    piece_points = list(largest.each_point())
    x = sum(vertex.x for vertex in piece_points) // len(piece_points)
    y = sum(vertex.y for vertex in piece_points) // len(piece_points)
    return kdb.Point(x, y)


def _resolve_abstract_cell_pins(
    layout: kdb.Layout,
    cell: kdb.Cell,
    deck: ExtractionDeck,
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]],
    local_candidates: dict[str, list[kdb.Point]] | None = None,
) -> tuple[
    list[tuple[str, list[kdb.Point], str | None]], str | None, str | None, list[str]
]:
    """Resolve one abstracted cell type's pins (issue #620).

    Returns ``(pins, resolution_source, lef_path, warnings)`` where ``pins`` is
    ``[(pin name, access points in **cell-local dbu**, probe layer role or
    ``None``), ...]`` sorted by pin name (the stable ``.subckt`` pin order).
    ``access points`` is a *list* rather than a single point (issue #1181): a
    LEF ``PIN`` may legally declare multiple disjoint same-layer ``PORT``
    rectangles for one electrical node (and an in-cell label may likewise be
    drawn more than once for the same pin name), and in a routed design only
    a subset of those rectangles/labels may receive external routing -- the
    rest still belong to the same logical pin but sit on the cell's own
    isolated, unrouted conductor. Every candidate point is carried through so
    :func:`_probe_abstract_pin_net` can probe all of them and pick whichever
    one actually lands on routed geometry, instead of this function silently
    committing to the first candidate (LEF source order / label scan order)
    regardless of whether it is the one the design actually routes.
    ``resolution_source`` is:

    - ``"in_cell_labels"`` -- the cell draws text on one of the deck's own
      label layers (``metal_labels[i]``/``well_label``/``poly_label``)
      **directly in the cell** (``cell.shapes``, never
      ``begin_shapes_rec``): a label promoted up from a nested sub-cell
      belongs to that sub-cell's interface, not this one's, and the whole
      point of this mode is to stop at *this* cell's boundary. Each label
      carries its own layer role, so the pin is probed on exactly the
      conductor its label names.
    - ``"lef_abstract"`` -- no such label, but a ``--abstract-cell-lef`` LEF
      declares a ``MACRO`` of this cell's name with ``PORT`` geometry. Each
      port's bounding-box centre is the access point, read directly as
      cell-local micrometres converted to dbu -- the standard LEF/GDS
      convention every real PDK standard-cell library follows: a macro's
      ``ORIGIN`` is ``0 0`` and its ``PORT`` coordinates already sit in the
      same local frame as the cell's own drawn GDS geometry (no additional
      shift). A LEF whose macro declares a non-zero ``ORIGIN`` relative to
      its cell's drawn geometry is a known, out-of-scope gap -- not expected
      for a standard-cell/hard-macro LEF generated by (or compatible with)
      real PDK tooling. The LEF layer name is not translated to a GDS layer
      (that would need a PDK layer map ``klt extract`` does not resolve), so
      these pins carry no layer role and are probed against the deck's
      conductor layers bottom-up instead.
    - ``None`` -- neither source resolved anything; the caller turns this
      into an :class:`ExtractError` when the cell type actually has
      instances.

    ``warnings`` (issue #624) is only ever populated on the ``"lef_abstract"``
    path: a LEF-declared ``PIN`` whose :func:`~klayout_tools.lef_header.
    parse_lef_macro_pin_ports` entry resolves zero port boxes (e.g. a pin
    drawn only via ``PATH``/``VIA`` geometry, or a malformed ``POLYGON`` --
    both statement shapes that module's own "declarative header + pin-access-
    point only" scope deliberately never reads, see its module docstring) is
    dropped from the returned pin list rather than raising, mirroring every
    other malformed/partial-LEF tolerance in this codebase -- but *this*
    silent drop is otherwise invisible to a caller, since it never prevents
    the macro overall from resolving (the acceptance criterion only requires
    *some* pin to resolve). One ``warnings`` entry per dropped pin names the
    macro and pin so the gap has a caller-visible signal instead of a
    silently incomplete black-box ``.SUBCKT``. Always ``[]`` on the
    ``"in_cell_labels"``/``None`` paths, where every resolved pin always
    carries at least one concrete point (an in-cell label is never
    geometry-less).

    ``local_candidates`` (issue #1183) is
    :func:`_local_pin_candidate_points`'s ``{<pin name>: [<extra local
    point>, ...]}`` result, computed by the caller *before* this cell type's
    device-recognition geometry was erased for abstraction -- ``None`` (the
    default) or an empty dict behaves exactly as before this issue's fix.
    Only consulted on the ``"in_cell_labels"`` path (a ``"lef_abstract"``
    pin's points come from an external LEF file, not this cell's own
    drawing, so there is no matching pre-erasure geometry to have computed
    this from): each named pin's extra points are appended to its label-only
    point list, deduplicated, so :func:`_probe_abstract_pin_net` can probe
    every candidate exactly as it already does for a multi-rectangle LEF pin
    (#1181/#1182) and pick whichever one actually lands on routed geometry.
    """
    import klayout.db as kdb

    label_roles: list[tuple[tuple[int, int] | None, str]] = [
        (deck.well_label, "nwell"),
        (deck.poly_label, "poly"),
    ]
    label_roles += [
        (layer, f"metal{index}") for index, layer in enumerate(deck.metal_labels)
    ]

    in_cell: dict[str, tuple[list[kdb.Point], str]] = {}
    for layer, role in label_roles:
        if layer is None:
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        for text in kdb.Texts(cell.shapes(layer_index)).each():
            # Every label for a repeated name (a pin drawn with two access
            # points, issue #1181) is kept, not just the first: nothing
            # guarantees both labels' points land on the same probed net --
            # e.g. one point could sit on an externally-routed rectangle and
            # the other on an isolated, unrouted one, exactly the failure
            # mode this issue reports for LEF-declared multi-rectangle pins.
            # `_probe_abstract_pin_net` probes every candidate and picks
            # whichever one actually resolves to routed geometry, so keeping
            # every point here is what makes that possible. The role
            # recorded is the first-seen layer's (a real pin's labels should
            # all share one layer role; `label_roles`'s fixed order still
            # makes this deterministic if they don't).
            points, _role = in_cell.setdefault(text.string, ([], role))
            points.append(kdb.Point(text.x, text.y))

    if in_cell:
        for name, (points, _role) in in_cell.items():
            for extra_point in (local_candidates or {}).get(name, []):
                if extra_point not in points:
                    points.append(extra_point)
        pins = [(name, points, role) for name, (points, role) in in_cell.items()]
        pins.sort(key=lambda entry: entry[0])
        return pins, "in_cell_labels", None, []

    entry = lef_macros.get(cell.name)
    if entry is not None:
        lef_path, lef_pins = entry
        dbu = layout.dbu
        lef_resolved: list[tuple[str, list[kdb.Point], str | None]] = []
        lef_warnings: list[str] = []
        for pin_name in sorted(lef_pins):
            boxes = lef_pins[pin_name]
            if not boxes:
                lef_warnings.append(
                    f"--abstract-cell-lef macro '{cell.name}' pin "
                    f"'{pin_name}' declared no PORT geometry this parser "
                    "reads (RECT/POLYGON only -- PATH/VIA and malformed "
                    "POLYGON statements are skipped) -- pin dropped from "
                    "the abstracted cell's resolved pin list"
                )
                continue
            # Every disjoint PORT box's bounding-box centre is kept as its
            # own candidate access point (issue #1181), not just the first
            # in LEF source order: a LEF pin may legally declare several
            # disjoint same-layer rectangles for one electrical node, and
            # only a subset may receive external routing in any given
            # placement. Committing to `boxes[0]` here silently discarded
            # every rectangle after the first, so if the externally-routed
            # one was not first, the whole pin resolved onto an isolated
            # island net instead of the routed net. `_probe_abstract_pin_net`
            # probes every candidate point and keeps whichever one actually
            # lands on routed geometry.
            points = [
                kdb.Point(
                    round(((x0 + x1) / 2) / dbu),
                    round(((y0 + y1) / 2) / dbu),
                )
                for x0, y0, x1, y1 in (box["bbox_um"] for box in boxes)
            ]
            lef_resolved.append((pin_name, points, None))
        if lef_resolved:
            return lef_resolved, "lef_abstract", lef_path, lef_warnings

    return [], None, None, []


def _def_net_name_probes(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    metal_layers: tuple[tuple[int, int] | None, ...],
) -> dict[str, list[tuple[int, kdb.Point]]]:
    """Per distinct DEF net name (:data:`_DEF_NET_NAME_PROPERTY_ID`), up to
    :data:`_DEF_NET_NAME_PROBE_CANDIDATES` ``(metal_index, point)`` probe
    candidates taken from ``top_cell``'s own routed-metal shapes carrying that
    name (issue #951). ``metal_layers`` is ``deck.metals`` -- the same
    ``(layer, datatype)`` list :func:`_extract_netlist` builds its flattened
    ``metals[]`` regions from, so ``metal_index`` indexes straight into them.

    Shape properties must be read off the raw ``kdb.Shape`` objects, not off
    the flattened ``Region``s the deck builds from them (a ``Region`` merge
    discards per-shape properties) -- hence the direct
    ``top_cell.shapes(...)`` scan here. Only the top cell is scanned, not a
    recursive flatten: a DEF->GDS merge draws the DEF's ``NETS``/
    ``SPECIALNETS`` routed geometry directly in the top cell, and a *sub*-cell
    shape carrying property 1 would be a standard cell's own internal
    annotation, not a top-level net name.

    Several candidates rather than one because
    :func:`_apply_def_net_name_overrides` resolves each name through
    ``LayoutToNetlist.probe_net``, which needs a point landing *inside* the
    net's geometry: a bounding-box centre satisfies that for the boxes and
    paths KLayout's DEF reader emits, but not necessarily for a non-convex
    polygon. Trying a handful of independent shapes costs one extra
    ``probe_net`` call in the rare failing case and avoids losing a whole
    net's name to one awkward shape.
    """
    # Imported lazily, matching every other function in this module.
    import klayout.db as kdb

    probes: dict[str, list[tuple[int, kdb.Point]]] = {}
    for metal_index, layer in enumerate(metal_layers):
        if layer is None:
            continue
        layer_index = layout.find_layer(*layer)
        if layer_index is None:
            continue
        for shape in top_cell.shapes(layer_index).each():
            if shape.is_text():
                continue
            properties = shape.properties()
            if not properties:
                continue
            net_name = properties.get(_DEF_NET_NAME_PROPERTY_ID)
            if not isinstance(net_name, str) or not net_name:
                continue
            candidates = probes.setdefault(net_name, [])
            if len(candidates) >= _DEF_NET_NAME_PROBE_CANDIDATES:
                continue
            box = shape.bbox()
            candidates.append(
                (
                    metal_index,
                    kdb.Point((box.left + box.right) // 2, (box.bottom + box.top) // 2),
                )
            )
    return probes


def _apply_def_net_name_overrides(
    l2n: kdb.LayoutToNetlist,
    metals: list[kdb.Region],
    probes: dict[str, list[tuple[int, kdb.Point]]],
) -> tuple[int, list[str]]:
    """Rename each probed extracted net to its DEF net name, overriding
    whatever ``extract_netlist()`` derived from text labels. Returns
    ``(renamed_count, unresolved_names)`` -- ``unresolved_names`` being the
    DEF names no probe candidate resolved to an extracted net (geometry that
    joins nothing, e.g. an isolated fill-like fragment).

    Ordering is load-bearing: this must run *after* ``l2n.extract_netlist()``
    (``probe_net`` needs the live ``LayoutToNetlist``) and *before*
    ``netlist.make_top_level_pins()`` and the ``purge`` passes, both of which
    read net names to decide what to promote and what to keep.

    A name is only ever written onto **one** extracted net -- the first
    candidate that resolves. A DEF net that extraction splits into several
    electrically disconnected islands (unstrapped power geometry, dead metal)
    therefore names one island and leaves the rest as they were, rather than
    minting duplicate net names into the SPICE/SPEF output.
    """
    renamed = 0
    unresolved: list[str] = []
    for def_name, candidates in probes.items():
        net = None
        for metal_index, point in candidates:
            if metal_index >= len(metals):
                continue
            net = l2n.probe_net(metals[metal_index], point)
            if net is not None:
                break
        if net is None:
            unresolved.append(def_name)
            continue
        if net.name != def_name:
            net.name = def_name
            renamed += 1
    return renamed, sorted(unresolved)


def _probe_single_abstract_pin_point(
    l2n: kdb.LayoutToNetlist,
    point: kdb.Point,
    role: str | None,
    probe_layers: list[tuple[str, kdb.Region]],
) -> kdb.Net | None:
    """The extracted net at exactly one candidate ``point``, via
    ``LayoutToNetlist.probe_net(<layer>, <dbu point>)``.

    ``role`` (present for a label-resolved pin) names the conductor the pin's
    own label was drawn on, so that layer is probed first and its answer is
    authoritative. A pin with no role (the LEF fallback, whose LEF layer name
    is not translated to a GDS layer) falls back to probing every conductor
    in ``probe_layers`` order -- metals bottom-up, then poly/nwell/tap -- and
    takes the first hit, since a standard cell's pins land on the lowest
    metal available. Returns ``None`` when no conductor carries geometry at
    that point at all.

    This is the single-candidate core :func:`_probe_abstract_pin_net` calls
    once per access-point candidate for a pin (issue #1181) -- kept separate
    so that per-point probing logic and the multi-point "pick the best
    result" policy stay independently readable.
    """
    if role is not None:
        for name, region in probe_layers:
            if name == role:
                net = l2n.probe_net(region, point)
                if net is not None:
                    return net
                break
    for _name, region in probe_layers:
        net = l2n.probe_net(region, point)
        if net is not None:
            return net
    return None


def _abstract_pin_net_score(net: kdb.Net) -> tuple[int, int]:
    """Ranking key for choosing among several nets one pin's candidate
    access points resolve to (issue #1181): ``(has_name, connectivity)``,
    compared lexicographically so a named net always outranks an unnamed
    one regardless of connectivity, and among same-name-ness, richer
    connectivity wins.

    - **Named over unnamed.** By the time :func:`_wire_abstract_cells` runs,
      ``extract_netlist()`` and (if requested) ``--def-net-names``
      (:func:`_apply_def_net_name_overrides`) have already assigned every
      *real*, routed net its name (from a drawn label or an explicit DEF
      net-name override); a net probed off a LEF pin rectangle with no
      external routing landing on it is a fresh, disconnected island that
      never earns a name from either source. A name is therefore strong,
      already-available evidence of "this is the routed net", independent of
      whether other terminals on it have been wired into the netlist yet.
    - **Richer connectivity as the tiebreak.** ``net.terminal_count()
      + net.subcircuit_pin_count() + net.pin_count()`` is the same "is this
      net actually connected to anything" signal
      :func:`_purge_truly_floating_nets` and :func:`_promote_orphan_named_nets`
      already use elsewhere in this module to distinguish a real net from a
      floating one; a bare single-rectangle island scores 0 on all three,
      while a net carrying other devices/subcircuits/top-level pins scores
      higher.

    A single-candidate pin (the overwhelmingly common case: one LEF box, one
    label) never has anything to compare against, so this function's choice
    is moot for it -- behaviour for every existing single-rectangle-pin
    design is unchanged.
    """
    connectivity = net.terminal_count() + net.subcircuit_pin_count() + net.pin_count()
    return (1 if net.name else 0, connectivity)


def _probe_abstract_pin_net(
    l2n: kdb.LayoutToNetlist,
    points: list[kdb.Point],
    role: str | None,
    probe_layers: list[tuple[str, kdb.Region]],
) -> kdb.Net | None:
    """The best-resolved net across every candidate access point for one
    abstracted-cell pin (issue #1181).

    A LEF-declared pin may legally carry several disjoint same-layer ``PORT``
    rectangles for one electrical node (and an in-cell label may likewise
    repeat), of which only a subset may receive external routing in any
    given placement -- the rest sit on the cell's own isolated conductor.
    Probing only the first candidate (as this function used to) is routing-
    blind: if the externally-routed rectangle/label is not first, the pin
    resolves onto an isolated single-shape island net instead of the net the
    design actually routes it to (the bug this issue reports).

    Every point in ``points`` is probed independently via
    :func:`_probe_single_abstract_pin_point`; among every point that
    resolves to a net at all, the one with the highest
    :func:`_abstract_pin_net_score` wins, with ties broken by ``points``'
    own order (so a single-candidate pin's behaviour is unchanged). Returns
    ``None`` only when *no* candidate point resolves to any net -- the "pin
    lands on no conductor anywhere" case the caller already reports as a
    per-instance warning.
    """
    best_net: kdb.Net | None = None
    best_score: tuple[int, int] | None = None
    for point in points:
        net = _probe_single_abstract_pin_point(l2n, point, role, probe_layers)
        if net is None:
            continue
        score = _abstract_pin_net_score(net)
        if best_score is None or score > best_score:
            best_net = net
            best_score = score
    return best_net


def _wire_abstract_cells(
    layout: kdb.Layout,
    deck: ExtractionDeck,
    l2n: kdb.LayoutToNetlist,
    netlist: kdb.Netlist,
    top_circuit: kdb.Circuit,
    instances: list[tuple[int, kdb.ICplxTrans]],
    lef_macros: dict[str, tuple[str, dict[str, list[dict[str, Any]]]]],
    probe_layers: list[tuple[str, kdb.Region]],
    local_candidates_by_cell: dict[int, dict[str, list[kdb.Point]]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Wire every ``--abstract-cells``-matched instance into ``netlist`` as a
    black-box ``kdb.SubCircuit`` (issue #620), and return the JSON response's
    ``abstracted_cells[]`` field alongside any ``warnings`` this pass itself
    generates.

    For each *distinct* matched cell type (grouped from ``instances``, first-
    seen order): resolves its pins once (:func:`_resolve_abstract_cell_pins`,
    raising :class:`ExtractError` if neither source resolves any pin -- the
    acceptance criterion that a matched-but-unresolvable cell type must fail
    loudly, not silently drop pins or emit an unconnected instance), then
    creates one pin-only ``kdb.Circuit`` for that cell type (no devices --
    exactly the shell ``NetlistSpiceWriter`` already emits as an empty
    ``.SUBCKT ... .ENDS`` block for a *device-free* circuit; verified
    directly against ``klayout.db``). Every occurrence of that cell type then
    becomes one ``kdb.SubCircuit`` in ``top_circuit``, with each pin
    connected to the net probed at its instance-transformed access point
    (:func:`_probe_abstract_pin_net`) -- the same net object the flat,
    un-abstracted portion of the layout already resolved via ordinary
    ``l2n.connect()`` wiring (this cell's own conductor/contact geometry was
    deliberately left un-erased for exactly this reason, see
    :func:`_abstract_cell_mask_layers`).

    Must run on the *live* ``netlist``/``l2n`` pair, before
    ``Netlist.make_top_level_pins()``/``purge()`` -- connecting a
    ``SubCircuit`` pin to a net gives that net a non-zero
    ``subcircuit_pin_count()``, which is what keeps an otherwise-bare routing
    stub (the abstracted cell's own metal, touching no device outside it)
    from being purged as floating before this pass has a chance to use it.

    A pin whose resolved access point lands on no conductor at all (e.g. an
    unrouted design, or a LEF-fallback coordinate that does not land inside
    the drawn footprint) does not fail the whole run: a fresh, otherwise
    disconnected net is created for it instead, and a ``warnings[]`` entry
    names the instance/pin -- mirroring every other per-instance geometric-
    miss diagnostic this module already reports (e.g.
    ``unbiased_pmos_body_nets``) rather than a hard error for a single
    instance's placement issue.

    A LEF-fallback pin the ``--abstract-cell-lef`` parser could not resolve
    any ``PORT`` geometry for (issue #624 -- see
    :func:`_resolve_abstract_cell_pins`'s own ``warnings`` docs) is dropped
    from the black-box ``.SUBCKT`` entirely rather than wired in; its own
    ``warnings[]`` entry (one per macro/pin, not per instance -- pin
    resolution happens once per cell *type*) is folded into this function's
    returned ``warnings`` alongside the per-instance geometric-miss entries
    above.

    Instance/subckt naming is deterministic: cell types are visited in
    ``instances``'s own order (already sorted by
    :func:`_collect_abstract_instances`); each occurrence is named
    ``"<cell type>_<n>"`` (0-based, per cell type), sanitized for SPICE via
    :func:`_sanitize_instance_name`.

    ``local_candidates_by_cell`` (issue #1183) is ``{<cell index>:
    <_local_pin_candidate_points() result>}`` for every matched cell type,
    computed by the caller *before* :func:`_erase_abstracted_cell_geometry`
    ran -- ``None`` (the default) disables the extra-candidate lookup
    entirely, matching this function's pre-#1183 behaviour exactly. Passed
    straight through to :func:`_resolve_abstract_cell_pins` per cell type.
    """
    import klayout.db as kdb

    grouped: dict[int, list[kdb.ICplxTrans]] = {}
    for cell_index, trans in instances:
        grouped.setdefault(cell_index, []).append(trans)

    report: list[dict[str, Any]] = []
    warnings: list[str] = []

    for cell_index, transforms in grouped.items():
        cell = layout.cell(cell_index)
        local_candidates = (local_candidates_by_cell or {}).get(cell_index)
        pins, source, lef_path, pin_warnings = _resolve_abstract_cell_pins(
            layout, cell, deck, lef_macros, local_candidates
        )
        warnings.extend(pin_warnings)
        if source is None:
            raise ExtractError(
                f"--abstract-cells matched cell type '{cell.name}' "
                f"({len(transforms)} instance(s)), but no pins could be "
                "resolved for it: it draws no label directly in its own "
                "definition on any of this deck's well_label/poly_label/"
                "metal_labels layers, and no --abstract-cell-lef declares a "
                f"MACRO named '{cell.name}' -- pass at least one pin source "
                "for this cell type, or narrow --abstract-cells to exclude it"
            )

        black_box_circuit = kdb.Circuit()
        black_box_circuit.name = cell.name
        pin_ids: dict[str, int] = {}
        for pin_name, _points, _role in pins:
            pin = black_box_circuit.create_pin(pin_name)
            net = black_box_circuit.create_net(pin_name)
            black_box_circuit.connect_pin(pin, net)
            pin_ids[pin_name] = pin.id()
        netlist.add(black_box_circuit)

        for index, trans in enumerate(transforms):
            instance_name = _sanitize_instance_name(f"{cell.name}_{index}")
            subcircuit = top_circuit.create_subcircuit(black_box_circuit, instance_name)
            for pin_name, points, role in pins:
                global_points = [trans * point for point in points]
                net = _probe_abstract_pin_net(l2n, global_points, role, probe_layers)
                if net is None:
                    net = top_circuit.create_net(f"{instance_name}__{pin_name}")
                    warnings.append(
                        f"--abstract-cells instance '{instance_name}' (cell "
                        f"'{cell.name}') pin '{pin_name}': no conductor found "
                        "at its resolved access point -- left unconnected to "
                        "any parent net"
                    )
                subcircuit.connect_pin(pin_ids[pin_name], net)

        report.append(
            {
                "cell": cell.name,
                "instance_count": len(transforms),
                "pin_count": len(pins),
                "resolution_source": source,
                "lef_path": lef_path,
            }
        )

    report.sort(key=lambda entry: entry["cell"])
    return report, warnings
