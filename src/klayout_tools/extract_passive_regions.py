"""Passive-device geometry and MoM-capacitor extraction for ``klt extract``.

Split out of ``extract.py`` (issue #2419) as a cohesive subsystem: the
deck-declared resistor / capacitor / diode terminal-region resolution
(:func:`_resistor_body_region`, :func:`_resolve_resistors`,
:func:`_capacitor_plate_regions`, :func:`_diode_terminal_region`), the
capacitor top-plate-via overlap exclusion (:func:`_capacitor_top_via_overlap_region`,
:func:`_capacitor_top_via_exclusions`, :func:`_exclude_capacitor_top_via_overlap`),
and the MoM (finger) capacitor device class and extractor
(:data:`MOM_FEED_TOKENS`, :func:`mom_capacitor_device_class`,
:func:`_build_mom_capacitor_extractor`, and their feed / metal-range helpers).

This is a move, not a rewrite: every function body is unchanged. Dependencies
are imported from their defining modules (deck types from
:mod:`klayout_tools.decks`, ``_region`` from :mod:`klayout_tools._layout`,
:class:`ExtractError` from :mod:`klayout_tools.extract_spef`) -- never back
through ``extract.py`` -- so the import graph stays one-directional.
``extract.py`` re-exports every moved name, so existing ``from
klayout_tools.extract import ...`` call sites keep working unchanged. The
diode near-miss diagnostics (``_collect_diode_near_miss_warnings`` and its
helpers) deliberately stay in ``extract.py``: they assemble warnings for the
main extraction/reporting flow rather than resolving device regions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from ._layout import region as _region
from .decks import CapacitorDevice, ExtractionDeck, ResistorDevice
from .extract_spef import ExtractError

if TYPE_CHECKING:
    import klayout.db as kdb


def _resistor_body_region(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    resistor: ResistorDevice,
    base: kdb.Region | None = None,
) -> kdb.Region:
    """The recognised device-body region for one :class:`ResistorDevice`
    entry against this layout: ``body & marker``, narrowed by ``requires``
    (every layer must also cover it) and ``excludes`` (each subtracted) --
    see :class:`ResistorDevice`'s own docstring.

    Factored out of :func:`_resolve_resistors`'s own inline computation
    (issue #2204) so ``erc.py``'s deck-driven device-body auto-detection
    computes the *exact same* region this deck's own extraction recognises
    a resistor body as, rather than a second, potentially drifting
    reimplementation -- the issue's own "Marker-to-role mapping" design
    note's explicit requirement ("the same one `klt extract` uses").

    ``base`` is the conductor region to intersect the marker against.
    :func:`_resolve_resistors` passes its own (possibly black-box-masked)
    ``poly``/``active``/metal region here, so this refactor changes nothing
    about its result. Omitted (the default, every caller outside the main
    extraction pipeline), this recomputes ``resistor.body`` fresh via
    :func:`_region` -- there is no black-box masking to inherit outside
    ``run_extract``'s own pipeline.

    Empty when the resistor's ``marker`` (or any ``requires`` layer) is not
    drawn anywhere on this layout -- matching :func:`_capacitor_plate_regions`'s
    own "no PDK marker drawn" convention.
    """
    if base is None:
        base = _region(layout, top_cell, resistor.body)
    body = base & _region(layout, top_cell, resistor.marker)
    for layer in resistor.requires:
        body = body & _region(layout, top_cell, layer)
    for layer in resistor.excludes:
        body = body - _region(layout, top_cell, layer)
    return body


def _resolve_resistors(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    poly: kdb.Region,
    active: kdb.Region,
    metals: list[kdb.Region],
    dummy: kdb.Region,
) -> tuple[
    list[tuple[ResistorDevice, kdb.Region, kdb.Region]],
    kdb.Region,
    kdb.Region,
    list[kdb.Region],
    kdb.Region,
    int,
]:
    """Resolve the deck's drawn-resistor declarations against this layout.

    Returns ``(resistors, poly, active, metals, poly_candidate_bodies,
    dummy_devices_dropped)`` where ``resistors`` is one ``(spec,
    body_region, terminal_region)`` triple per *recognised* device class and
    the three conductor regions are the deck's originals with every
    recognised resistor body **subtracted** -- so the caller's connectivity
    graph (and its MOS gate/source-drain split) sees the resistor's heads as
    ordinary conductor and the resistive segment as a hole, instead of one
    continuous short (see :class:`~klayout_tools.decks.ResistorDevice`).

    A resistor body is ``body_layer & marker & all(requires) - any(excludes)``.
    A spec whose body region comes out empty on this layout (the common case
    -- no PDK resistor marker drawn anywhere) is dropped entirely and
    subtracts nothing, so extraction of a resistor-free layout is bit-for-bit
    what it was before this feature existed.

    ``dummy`` is the deck's optional dummy-device marker region (see
    ``ExtractionDeck.dummy``, issue #295) -- possibly empty when the deck
    declares no ``dummy`` layer or the layout draws no such geometry.
    Mirroring the MOS gate-suppression idiom in ``_extract_netlist``, a
    resistor body's *candidate* region (after ``requires``/``excludes`` but
    before the ``dummy`` cut) has ``dummy`` subtracted before it is handed
    off as a recognised device: any *connected component* of the candidate
    fully consumed by ``dummy`` is dropped outright (counted into
    ``dummy_devices_dropped``, issue #462) rather than registered as a
    device, while a component only partially covered survives as a clean
    geometric cut -- the same all-or-nothing distinction the MOS path
    already makes. Whatever ``dummy`` removes from a candidate body is *not*
    subtracted from the caller's conductor region, so it stays present as
    ordinary conductor, exactly like a suppressed MOS gate's poly.

    ``poly_candidate_bodies`` is the union of every *candidate* body region
    (post ``requires``/``excludes``, but **before** the ``dummy`` cut) whose
    ``spec.body`` is the deck's ``poly`` layer -- used by
    :func:`_detect_unmodelled_poly_bodies` to recognise a fully
    dummy-suppressed poly resistor's own footprint as "claimed" (so it is
    never misflagged as an unmodelled-device gap, issue #462), distinct from
    the narrower post-dummy body carried in ``resistors`` itself.

    Raises :class:`ExtractError` for a deck-authoring mistake (a ``body``/
    ``terminal`` layer that is not one of the deck's own conductor layers),
    since the terminal region must be a layer the connectivity graph already
    carries.
    """
    import klayout.db as kdb

    if not deck.resistors:
        return [], poly, active, metals, kdb.Region(), 0

    # Keyed by drawn conductor layer so a resistor declared on `poly` is cut
    # out of the very same Region the connectivity graph uses.
    bases: dict[tuple[int, int], kdb.Region] = {deck.poly: poly, deck.active: active}
    for index, layer in enumerate(deck.metals):
        bases.setdefault(layer, metals[index])

    def _conductor(layer: tuple[int, int], field: str, name: str) -> kdb.Region:
        try:
            return bases[layer]
        except KeyError:
            raise ExtractError(
                f"resistor '{name}': {field} layer {layer[0]}/{layer[1]} is not one "
                "of the deck's conductor layers (active/poly/metals)"
            ) from None

    recognised: list[tuple[ResistorDevice, kdb.Region, tuple[int, int]]] = []
    poly_candidate_bodies = kdb.Region()
    dummy_devices_dropped = 0
    for spec in deck.resistors:
        base = _conductor(spec.body, "body", spec.name)
        terminal_layer = spec.terminal if spec.terminal is not None else spec.body
        _conductor(terminal_layer, "terminal", spec.name)

        body = _resistor_body_region(layout, top_cell, spec, base=base)
        if body.is_empty():
            continue
        if spec.body == deck.poly:
            poly_candidate_bodies += body
        if not dummy.is_empty():
            for component in body.merged().each():
                if (kdb.Region(component) - dummy).is_empty():
                    dummy_devices_dropped += 1
            body = body - dummy
        if body.is_empty():
            continue
        recognised.append((spec, body, terminal_layer))

    for spec, body, _terminal_layer in recognised:
        bases[spec.body] = bases[spec.body] - body

    resistors = [
        (spec, body, bases[terminal_layer]) for spec, body, terminal_layer in recognised
    ]
    return (
        resistors,
        bases[deck.poly],
        bases[deck.active],
        [bases[layer] for layer in deck.metals],
        poly_candidate_bodies,
        dummy_devices_dropped,
    )


def _capacitor_plate_regions(
    layout: kdb.Layout, top_cell: kdb.Cell, capacitor: CapacitorDevice
) -> tuple[kdb.Region, kdb.Region]:
    """The recognised ``(top_region, bottom_region)`` pair for one
    :class:`CapacitorDevice` entry against this layout.

    Shared between the main capacitor-recognition loop in
    ``_extract_netlist`` and :func:`_exclude_capacitor_top_via_overlap`
    below (issue #364), so the two never drift apart on what counts as
    "this capacitor's bottom plate" -- the overlap exclusion must be
    computed against exactly the same (possibly virtual-bottom-plate-
    clipped, requires/excludes-narrowed) region the capacitor device itself
    is later registered and extracted against.

    Either region comes back empty when the capacitor's markers are not
    drawn anywhere on this layout (the common case -- no PDK cap marker
    drawn at all).
    """
    top_region = _region(layout, top_cell, capacitor.top_plate)
    for layer in capacitor.top_plate_requires:
        top_region = top_region & _region(layout, top_cell, layer)
    for layer in capacitor.top_plate_excludes:
        top_region = top_region - _region(layout, top_cell, layer)

    bottom_conductor = _region(layout, top_cell, capacitor.bottom_plate)
    for layer in capacitor.bottom_plate_requires:
        bottom_conductor = bottom_conductor & _region(layout, top_cell, layer)
    for layer in capacitor.bottom_plate_excludes:
        bottom_conductor = bottom_conductor - _region(layout, top_cell, layer)

    if capacitor.bottom_plate_oversize_um:
        # "Virtual bottom plate" derivation (e.g. gf180mcu's MiM stack): only
        # bottom-conductor shapes that already touch the *unsized* top plate
        # count (`interacting`), then clipped to the top plate's oversized
        # outline for the exact overlap area -- the same two-step derivation
        # the PDK's own official KLayout LVS deck uses (see
        # `CapacitorDevice`'s docstring).
        oversize_dbu = int(round(capacitor.bottom_plate_oversize_um / layout.dbu))
        bottom_region = bottom_conductor.interacting(top_region) & (
            top_region.sized(oversize_dbu)
        )
    else:
        bottom_region = bottom_conductor

    return top_region, bottom_region


def _diode_terminal_region(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    marker: kdb.Region,
    layer: tuple[int, int] | None,
    requires: tuple[tuple[int, int], ...],
    excludes: tuple[tuple[int, int], ...],
) -> kdb.Region:
    """The recognised region for **one** terminal of a :class:`DiodeDevice`
    entry (issue #542), against this layout.

    ``layer`` is the terminal's declared drawn layer, scoped to the device's
    already-built ``marker`` region -- the same "intersect with the device
    mark before recognition" guard the bipolar block applies to its base, so
    an ordinary PMOS's p+-in-Nwell diffusion is never misrecognised as a
    diode. ``None`` means the terminal is formed by the substrate (the PDK
    draws no mask for it): the region is then the device's ``marker``
    footprint itself, which the caller ties to the deck's ``substrate_net``
    global. That footprint -- rather than an empty region -- is load-bearing:
    ``kdb.DeviceExtractorDiode`` forms the device from the *overlap* of its
    two inputs, so an empty input silently yields no device at all.

    ``requires``/``excludes`` then narrow the result the same way
    :func:`_capacitor_plate_regions` and ``_resolve_resistors`` narrow
    theirs: every ``requires`` layer must also cover the region, every
    ``excludes`` layer is subtracted. For a substrate-formed terminal this
    is how the deck keeps the substrate side genuinely *outside* every well
    (``anode_excludes=(Nwell, DNWELL)``).

    Always returns a freshly-owned ``Region``: both terminals of the same
    entry can derive from the one ``marker`` object, and each is registered
    into the connectivity graph separately.
    """
    # `&`/`-` below already return fresh regions; the `dup()` branch covers
    # the no-declared-layer, no-narrowing case.
    if layer is None:
        region = marker.dup()
    else:
        region = _region(layout, top_cell, layer) & marker
    for required in requires:
        region = region & _region(layout, top_cell, required)
    for excluded in excludes:
        region = region - _region(layout, top_cell, excluded)
    return region


def _capacitor_top_via_overlap_region(
    layout: kdb.Layout, top_cell: kdb.Cell, capacitor: CapacitorDevice
) -> kdb.Region:
    """The geometric overlap between one :class:`CapacitorDevice` entry's
    own ``top_plate_via`` footprint and its recognised bottom plate (issue
    #364) -- the region :func:`_exclude_capacitor_top_via_overlap` cuts from
    the deck's generic ``vias[]`` connectivity so a MiM/MOM cap's DRM-legal
    via-to-bottom-plate overlap does not extract as an ordinary via shorting
    the two plates together. See that function's own docstring for the full
    "why" (issue #364/#1388).

    Factored out (issue #2204) so ``erc.py``'s deck-driven device-body
    auto-detection subtracts the *same* region from a matching ``vias``
    role that this deck's own extraction excludes from its generic via
    connectivity, rather than a second, potentially drifting
    reimplementation.

    Empty when ``capacitor.top_plate_via`` is unset, when no shape is drawn
    on that layer anywhere in this layout, or when this capacitor's own
    plates are not drawn (``top_region``/the top-plate-scoped bottom region
    empty) -- matching :func:`_capacitor_plate_regions`'s own "no PDK marker
    drawn" convention.
    """
    import klayout.db as kdb

    if capacitor.top_plate_via is None:
        return kdb.Region()
    top_via_region = _region(layout, top_cell, capacitor.top_plate_via)
    if top_via_region.is_empty():
        return top_via_region
    top_region, bottom_region = _capacitor_plate_regions(layout, top_cell, capacitor)
    # See `_exclude_capacitor_top_via_overlap`'s own docstring (issue #1388)
    # for why `bottom_region` must be narrowed to `interacting(top_region)`
    # before intersecting it with the via footprint.
    scoped_bottom_region = bottom_region.interacting(top_region)
    if top_region.is_empty() or scoped_bottom_region.is_empty():
        return kdb.Region()
    return top_via_region & scoped_bottom_region


def _capacitor_top_via_exclusions(
    layout: kdb.Layout, top_cell: kdb.Cell, deck: ExtractionDeck
) -> dict[int, kdb.Region]:
    """Every capacitor's ``top_plate_via ∩ bottom_plate`` overlap against this
    layout, keyed by the **index into ``deck.vias``** of the via layer it must
    be cut from -- :func:`_exclude_capacitor_top_via_overlap`'s working set.

    Factored out of that function (issue #2396) so the *same* derivation can
    be run a second time, at a different moment in the pipeline: with
    ``--abstract-cells``,
    :func:`~klayout_tools.extract_abstract._erase_abstracted_cell_geometry`
    erases each black-boxed cell's capacitor ``top_plate`` (it is a
    device-recognition layer) but deliberately keeps its routing layers,
    including the ``top_plate_via``. Run against the post-erasure layout, the
    derivation below therefore sees an empty ``top_region`` inside every
    abstracted cell and returns no exclusion for it -- and every top-plate via
    in the black box falls back into the deck's generic ``vias[]``
    connectivity, shorting that capacitor's own top plate to its bottom plate
    (and merging whatever *parent* nets each plate reaches). The caller
    (:func:`extract_netlist_from_layout`) runs this once **before** erasure and
    threads the result back in, the same pre-erasure-capture pattern
    :func:`~klayout_tools.extract_abstract._abstract_cell_body_identity_cover`
    already uses for the well/substrate-isolation cover (issue #1911).

    A deck with no capacitor declaring ``top_plate_via``, or whose declared
    ``top_plate_via`` is not one of the deck's own ``vias`` layers (so it never
    reaches the generic connectivity loop in the first place), yields an empty
    mapping.
    """
    import klayout.db as kdb

    exclusions: dict[int, kdb.Region] = {}
    for capacitor in deck.capacitors:
        if capacitor.top_plate_via is None:
            continue
        if capacitor.top_plate_via not in deck.vias:
            # Not one of this deck's tracked via layers -- the generic loop
            # never touches it, so there is nothing to exclude (the
            # deck-authoring validation for a mismatched
            # `top_plate_via`/`top_plate_via_metal` pair is the main
            # capacitor loop's job, not this helper's).
            continue
        via_index = deck.vias.index(capacitor.top_plate_via)
        # For a deck whose `bottom_plate` is *not* clipped to the top
        # plate's own footprint (`bottom_plate_oversize_um == 0`, e.g.
        # sky130's MiM stacks), `_capacitor_plate_regions`'s zero-oversize
        # branch returns the bottom conductor's *entire* drawn region --
        # every shape on that metal layer anywhere in the layout, not just
        # this capacitor's own plate. `_capacitor_top_via_overlap_region`
        # narrows to `interacting(top_region)` (issue #1388) before
        # intersecting it with the via footprint, keeping only the
        # bottom-plate shape(s) that actually sit under *this* capacitor's
        # top-plate marker. This both restores the issue #775 guard (an
        # empty `top_region` -- no cap marker drawn anywhere -- makes the
        # scoped bottom region empty too, so a digital/macro layout that
        # only routes on the declared `bottom_plate` metal is untouched)
        # *and* fixes the case #775 didn't cover: a layout that draws both a
        # real capacitor and ordinary routing between the bottom-plate metal
        # and the metal above elsewhere on the chip. Without this
        # narrowing, `top_via_region` (every shape on the declared
        # `top_plate_via` layer, e.g. sky130's real `via3`/`via4` routing
        # vias used throughout ordinary signal routing) intersected against
        # the unscoped, chip-wide bottom region excludes every legitimate
        # via on that layer from the deck's generic `vias[]` connectivity --
        # a false disconnect across the whole design, not the narrow
        # false-short exclusion this function exists to apply.
        overlap = _capacitor_top_via_overlap_region(layout, top_cell, capacitor)
        if overlap.is_empty():
            continue
        exclusions[via_index] = exclusions.get(via_index, kdb.Region()) + overlap
    return exclusions


def _exclude_capacitor_top_via_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    vias: list[kdb.Region],
    pre_erasure_exclusions: dict[int, kdb.Region] | None = None,
) -> list[kdb.Region]:
    """Exclude each capacitor's own ``top_plate_via ∩ bottom_plate`` overlap
    from the deck's generic ``vias[]`` layers (issue #364), before those
    layers are registered into the connectivity graph and consumed by
    ``_extract_netlist``'s generic per-layer ``metals[i]``/``vias[i]`` loop.

    A capacitor's ``top_plate_via`` (#314) is wired directly to the
    recognised top-plate region and on to ``top_plate_via_metal`` -- but
    without this exclusion, that *same* via shape also reaches the deck's
    generic per-layer connectivity loop, which connects every ``vias[i]``
    shape to whichever ``metals[i]``/``metals[i + 1]`` conductor it
    geometrically touches. A top-plate via placed per the PDK's own
    minimum-overlap rule for that via (which *requires* the bottom plate to
    enclose/overlap it, not clear it) inevitably touches the bottom-plate
    conductor beneath it in plan view, so the generic loop reads that
    DRM-legal overlap as an ordinary via shorting the two plates together --
    a false short (the extraction engine has no notion of the dielectric
    that keeps the via from actually reaching the bottom plate in real
    silicon).

    Only the *geometric intersection* of the via footprint with the
    capacitor's own recognised bottom-plate region is cut, not the whole via
    shape/component: a via landing pad that only partially overlaps the
    bottom plate keeps the rest of its footprint in the generic connectivity
    graph, and any *other* via shape drawn on the same physical via layer
    elsewhere in the layout -- ordinary routing unrelated to this capacitor
    -- is left untouched. That last guarantee requires narrowing
    ``bottom_region`` to ``interacting(top_region)`` before intersecting it
    with the via footprint (issue #1388): for a deck with
    ``bottom_plate_oversize_um == 0`` (e.g. sky130's MiM stacks),
    :func:`_capacitor_plate_regions` hands back the bottom conductor's
    *entire* drawn region on that metal, not just the part under this
    capacitor's own top plate, so without the narrowing a single drawn
    capacitor would exclude every via on the shared via layer chip-wide --
    including ordinary routing vias nowhere near a capacitor.

    ``pre_erasure_exclusions`` (issue #2396) is the same mapping
    :func:`_capacitor_top_via_exclusions` derived from this layout **before**
    ``--abstract-cells`` erased each black-boxed cell's capacitor
    ``top_plate``, unioned into the freshly-derived one here. It is what keeps
    a MiM cap inside a black box from reading as a hard short between its own
    plates: the erasure removes the ``capm``-style top plate that explains the
    DRM-required ``via3``-on-``met3`` overlap, but not the via itself, so
    without the pre-erasure capture the exclusion silently switches off in
    exactly the cells the caller asked to treat as opaque -- and every parent
    net that reaches one plate of some capacitor in the macro merges with
    every net that reaches the other. Because the captured mapping is
    precisely what a *flat* (non-abstracted) extraction of the same stream
    would exclude, unioning it in can only restore connections flat extraction
    also lacks; it never cuts a via an unabstracted run would keep. ``None``
    (the default, and every non-abstracted run) leaves behaviour exactly as it
    was.

    Returns a new ``vias`` list (the input list/regions are not mutated); a
    deck with no capacitor declaring ``top_plate_via``, or whose declared
    ``top_plate_via`` is not one of the deck's own ``vias`` layers (so it
    never reaches the generic loop in the first place), returns the input
    list unchanged.
    """
    import klayout.db as kdb

    exclusions = _capacitor_top_via_exclusions(layout, top_cell, deck)
    for via_index, region in (pre_erasure_exclusions or {}).items():
        if region.is_empty():
            continue
        exclusions[via_index] = exclusions.get(via_index, kdb.Region()) + region

    if not exclusions:
        return vias
    return [
        region - exclusions[index] if index in exclusions else region
        for index, region in enumerate(vias)
    ]


#: ``FEED`` parameter codes (issue #2445) -- the ``cap_cmomi`` PCell's
#: feed-structure variant carried through a KLayout ``DeviceParameterDefinition``
#: (a ``double``) as a small integer enum, mapped back to the upstream
#: ``.subckt``'s own ``none``/``same``/``double`` token only when a card is
#: written (:data:`MOM_FEED_TOKENS`, consumed by ``pdk_models.py``) and when
#: ``devices[].feed`` is reported. ``0`` is **not** a variant: it is the
#: class's own default and means "this side never measured/stated the feed"
#: (the same sentinel role ``0`` plays for ``MMIN``/``MMAX``, issue #2435),
#: so an unrecognised port layout is left off the card rather than guessed
#: at. The codes are deliberately *not* the ``.lib``'s own ``none/same/double
#: -> 0/1/2`` numbering, precisely so ``0`` stays free for "unmeasured".
MOM_FEED_UNMEASURED = 0
MOM_FEED_NONE = 1
MOM_FEED_SAME = 2
MOM_FEED_DOUBLE = 3
MOM_FEED_TOKENS: dict[int, str] = {
    MOM_FEED_NONE: "none",
    MOM_FEED_SAME: "same",
    MOM_FEED_DOUBLE: "double",
}

#: Device-class names whose upstream ``.subckt`` declares a ``feed``
#: parameter. Exactly ``cap_cmomi`` -- ``cap_cmomf`` has no ``feed`` at all
#: (``IHP-GmbH/ihp-sg13cmos5l`` ``libs.tech/ngspice/models/cap_cmomf.lib:53``),
#: so it must never gain the ``FEED`` parameter or token.
_MOM_FEED_CLASS_NAMES = frozenset({"cap_cmomi"})


def mom_capacitor_has_feed(name: str) -> bool:
    """Whether the MoM-capacitor device class ``name`` carries the ``FEED``
    parameter (issue #2445) -- only ``cap_cmomi`` does."""
    return name in _MOM_FEED_CLASS_NAMES


def _mom_feed_variant(ports: list[tuple[Any, int]], marker_bbox: kdb.Box) -> int:
    """Classify the ``cap_cmomi`` PCell's ``feed`` variant (issue #2445) from
    the two recognised port polygons and the recognition marker's bounding
    box, returning a ``MOM_FEED_*`` code, or ``MOM_FEED_UNMEASURED`` when the
    port layout matches none of the three PCell signatures.

    What each ``feed`` value draws (IHP ``cap_cmomi_code.py`` at
    ``607e18d4bd9214a52575c194b4181ef449f9252f``, ``genLayout`` /
    ``_place_pins``), and so what is separable:

    * ``same`` -- two **stacked** pads, PLUS on ``mmax`` and MINUS on
      ``mmax-1``, with both pins placed at the *same* pad centre. The two
      ports sit on different metals and overlap in x/y.
    * ``double`` -- PLUS pad left, MINUS pad right, both on ``mmax``, both
      pins at the pad centre height. The two ports sit on one metal, at the
      same y (mid-height of the marker), far apart in x.
    * ``none`` -- no feed pads; pins sit inside the outer ``mmax`` bars, PLUS
      on the bottom bar and MINUS on the top bar. The two ports sit on one
      metal at *opposite* y edges of the marker.

    So the three are told apart by port metal levels and the ports' relative
    placement inside the marker, all of which this step already has. The
    thresholds are wide (``0.25``/``0.75`` of the marker height (the PCell's
    ``none`` pins are centred on the marker's own y edges, so a pin clipped to
    the marker lands slightly inside them), ``0.5`` of its
    width) because the PCell's own geometry sits at the extremes (``dy`` is
    exactly ``0`` or the whole marker height); a port layout in between --
    typically a hand-drawn abstraction, not this PCell's output -- is left
    unmeasured rather than forced into the nearest variant, the same "never
    guess" rule issue #2408 set.
    """
    (poly_a, metal_a), (poly_b, metal_b) = ports
    box_a, box_b = poly_a.bbox(), poly_b.bbox()
    if metal_a != metal_b:
        return MOM_FEED_SAME if box_a.overlaps(box_b) else MOM_FEED_UNMEASURED
    height, width = marker_bbox.height(), marker_bbox.width()
    if height <= 0 or width <= 0:
        return MOM_FEED_UNMEASURED
    dx = abs(box_a.center().x - box_b.center().x)
    dy = abs(box_a.center().y - box_b.center().y)
    if dx < 0.5 * width:
        return MOM_FEED_UNMEASURED
    if dy <= 0.25 * height:
        return MOM_FEED_DOUBLE
    if dy >= 0.75 * height:
        return MOM_FEED_NONE
    return MOM_FEED_UNMEASURED


def _set_mom_feed_parameter(
    device: kdb.Device,
    param_feed: int | None,
    ports: list[tuple[Any, int]],
    marker_bbox: kdb.Box,
) -> None:
    """Write the ``FEED`` parameter (issue #2445) onto one extracted MoM
    capacitor ``device`` -- a no-op when ``param_feed`` is ``None`` (the
    device class carries no ``FEED`` parameter; see
    :func:`mom_capacitor_has_feed`).

    Module-level rather than inline in the extractor's ``extract_devices``
    so the ``None`` guard does not count against
    ``_build_mom_capacitor_extractor``'s own baselined cyclomatic complexity
    (``complexity-baseline.json``).
    """
    if param_feed is None:
        return
    device.set_parameter(param_feed, float(_mom_feed_variant(ports, marker_bbox)))


def mom_capacitor_device_class(name: str) -> kdb.DeviceClass:
    """Build the ``kdb.DeviceClass`` one
    :class:`~klayout_tools.decks.MomCapacitorDevice` entry named ``name``
    registers -- two terminals ``A``/``B`` (declared
    EQUIVALENT, order is arbitrary -- see ``MomCapacitorDevice``'s
    docstring), plus ``W``/``L`` geometry parameters, ``MMIN``/``MMAX``
    finger-stack metal-index parameters, and deliberately no
    capacitance parameter at all (the real device's ``C`` is supplied by the
    SPICE/Verilog-A model, not computed here -- see ``docs/json-contract
    .md``'s "MoM capacitor devices" note for the resulting
    ``devices[].params`` shape). ``W``/``L`` (uppercase) match KLayout's own
    MOS convention so ``extract.py``'s ``_describe_devices`` reports them as
    ``w_um``/``l_um`` with no code change needed there.

    ``MMIN``/``MMAX`` (issue #2435) are the inclusive **1-based metal index
    range** the device's fingers are drawn on -- the upstream ``.subckt``'s
    own ``mmin``/``mmax`` parameters, whose layer count ``N = mmax - mmin +
    1`` keys the compact model's capacitance density, so they are a
    value-bearing measurement rather than bookkeeping. Measured from drawn
    geometry by :func:`_build_mom_capacitor_extractor` and reported as
    ``devices[].params``' ``mmin``/``mmax`` (see ``_describe_devices``).

    ``cap_cmomi`` alone additionally carries ``FEED`` (issue #2445): the
    PCell's feed variant, an integer enum (``MOM_FEED_*``) in the parameter's
    ``double``, ``0`` meaning unmeasured. ``cap_cmomf`` has no ``feed`` in its
    upstream ``.subckt`` and so never registers it
    (:func:`mom_capacitor_has_feed`).

    ``MMIN``/``MMAX`` (and ``FEED``) are declared **non-primary**
    (``is_primary=False``, KLayout's
    "secondary parameter" flag): they are extracted and written, but never
    compared by ``kdb.NetlistComparer``. A reference netlist's own
    ``cap_cmom*`` instantiation is free to leave ``mmin``/``mmax`` at the
    ``.subckt`` defaults, and a schematic-side card that does is not a
    different *device* from the drawn one -- turning a metal-range
    difference into an LVS device-parameter mismatch would report a
    modelling discrepancy as a connectivity failure. ``W``/``L`` stay
    primary exactly as before, so this addition cannot change any existing
    compare's verdict (verified by ``tests/test_lvs.py``'s own
    ``cap_cmomi`` round-trip tests, whose reference cards carry neither
    parameter).

    Shared by :func:`_build_mom_capacitor_extractor`'s own
    ``GenericDeviceExtractor.setup()`` (the layout-extraction side, which
    calls this once per fresh instance and reads the ids it needs back off
    the returned object's own terminal/parameter definitions) and
    :mod:`klayout_tools.netlist_capacitor_recovery`'s round-trip reader-side
    recognition (issue #1942) -- both sides register a structurally
    identical ``DeviceClass`` for the same ``name``, one call to build it, so
    they cannot silently drift apart.
    """
    import klayout.db as kdb

    device_class = kdb.DeviceClass()
    device_class.name = name
    terminal_a = kdb.DeviceTerminalDefinition("A", "Terminal A")
    device_class.add_terminal(terminal_a)
    terminal_b = kdb.DeviceTerminalDefinition("B", "Terminal B")
    device_class.add_terminal(terminal_b)
    device_class.equivalent_terminal_id(terminal_a.id(), terminal_b.id())
    param_w = kdb.DeviceParameterDefinition("W", "Width")
    device_class.add_parameter(param_w)
    param_l = kdb.DeviceParameterDefinition("L", "Length")
    device_class.add_parameter(param_l)
    # `is_primary=False` (the 4th positional argument): extracted and
    # written, never compared -- see this function's own docstring for why.
    param_mmin = kdb.DeviceParameterDefinition(
        "MMIN", "Lowest metal index carrying finger geometry", 0.0, False
    )
    device_class.add_parameter(param_mmin)
    param_mmax = kdb.DeviceParameterDefinition(
        "MMAX", "Highest metal index carrying finger geometry", 0.0, False
    )
    device_class.add_parameter(param_mmax)
    if mom_capacitor_has_feed(name):
        # Issue #2445: also non-primary, for the same reason -- a reference
        # card is free to leave `feed` at the `.subckt` default.
        param_feed = kdb.DeviceParameterDefinition(
            "FEED",
            "Feed variant code (1=none, 2=same, 3=double, 0=unmeasured)",
            0.0,
            False,
        )
        device_class.add_parameter(param_feed)
    return device_class


def _mom_extractor_metal_layer_specs(num_metals: int) -> list[tuple[str, str]]:
    """The ``(layer_name, description)`` pairs one MoM-capacitor extractor
    defines between its ``core`` and ``dev_mk`` marker layers, in the exact
    order :meth:`setup` must define them (issue #2435).

    The whole ``m1p``..``mNp`` *port* run comes first -- that is upstream's
    own ``define_layers`` order, and keeping it unbroken leaves upstream's
    ``core``/``m<n>p`` layer indices byte-for-byte what they were before the
    finger-stack measurement existed. The ``m1d``..``mNd``
    *drawn-conductor* run this issue adds follows it, so
    :meth:`get_connectivity` can address both with one contiguous
    ``layers[1 : 1 + 2 * num_metals]`` slice.

    Module-level rather than inline in :meth:`setup` so the two loops do not
    count against ``_build_mom_capacitor_extractor``'s own baselined
    cyclomatic complexity (``complexity-baseline.json``).
    """
    port_layers = [
        (f"m{metal_number}p", f"Metal{metal_number} pin ports")
        for metal_number in range(1, num_metals + 1)
    ]
    drawn_layers = [
        (
            f"m{metal_number}d",
            f"Metal{metal_number} drawn finger geometry inside the marker",
        )
        for metal_number in range(1, num_metals + 1)
    ]
    return port_layers + drawn_layers


def _mom_finger_stack_metal_range(
    port_metal_indices: Iterable[int],
    drawn_metals: Mapping[int, kdb.Region],
    *,
    device_name: str,
    report_gap: Callable[[str, Any], None],
    gap_context: Any,
) -> tuple[int, int]:
    """The inclusive, **1-based** metal-index range one recognised MoM
    capacitor's fingers are drawn on -- the upstream ``.subckt``'s own
    ``mmin``/``mmax`` (issue #2435).

    ``drawn_metals`` maps each 1-based metal level to that level's drawn
    conductor geometry lying **entirely inside** this device's recognition
    marker (the caller does the containment narrowing); a level the deck
    does not let this device family reach carries an empty region and is
    therefore never counted. ``port_metal_indices`` are the levels the two
    recognised ports sit on.

    The two are **unioned** rather than the drawn geometry being taken
    alone. Both pins always land on a level inside the drawn range (the
    PCell puts them on ``mmax``, or on ``mmax``/``mmax-1`` stacked for the
    ``same`` feed), so a port level can only ever confirm the drawn range,
    never widen it past what was drawn -- while keeping the measurement
    well-defined for a pin-only abstraction whose routing stubs all leave
    the marker, so that nothing at all is *enclosed* by it.

    A *gap* in the measured range is reported through ``report_gap`` (called
    as ``report_gap(message, gap_context)``, which is how the extractor
    binds its own ``self.error``/marker component to it) rather than
    resolved here: a real finger stack is contiguous by construction (the
    PCell paints every level in ``mmin..mmax``), so a hole means the marker
    encloses something this measurement cannot tell from a finger. The
    observed min/max is still returned, so the written card never silently
    regresses to a PDK default.
    """
    populated_levels = sorted(
        set(port_metal_indices)
        | {
            metal_index
            for metal_index, drawn_region in drawn_metals.items()
            if not drawn_region.is_empty()
        }
    )
    mmin, mmax = populated_levels[0], populated_levels[-1]
    missing = [
        f"Metal{level}"
        for level in range(mmin, mmax + 1)
        if level not in populated_levels
    ]
    if missing:
        report_gap(
            f"{device_name}: drawn finger geometry under its recognition "
            f"marker spans Metal{mmin}..Metal{mmax} but is missing on "
            f"{', '.join(missing)} -- a real finger stack is contiguous, so "
            "mmin/mmax may be measuring non-finger geometry enclosed by the "
            f"marker. Reported as mmin={mmin}, mmax={mmax} anyway.",
            gap_context,
        )
    return mmin, mmax


def _build_mom_capacitor_extractor(
    name: str, metal_count: int
) -> kdb.GenericDeviceExtractor:
    """Build a fresh ``kdb.GenericDeviceExtractor`` subclass instance that
    recognises one :class:`MomCapacitorDevice` entry (issue #1466) --
    IHP's ``cap_cmomi``/``cap_cmomf`` MoM (Metal-oxide-Metal) capacitors, and
    structurally any future device with the same "single marker, per-metal
    multi-port, position-split terminals, no computed value" shape.

    A **Python transcription of upstream's own ``CapMomExtractor``**
    (``custom_mom_extractor.lvs``, an ``RBA::GenericDeviceExtractor``
    subclass) -- kept as close to that source's structure and variable
    names as the Ruby/Python API difference allows, so the two can be
    diffed side by side. See :class:`~klayout_tools.decks.MomCapacitorDevice`
    for the device-recognition semantics this implements, and this
    function's own inline comments for the specific upstream lines each
    step mirrors.

    ``kdb.GenericDeviceExtractor`` is the Python-subclassable base KLayout's
    own built-in extractors (``DeviceExtractorCapacitor`` etc.) are written
    against internally -- the same "reimplement the C++ extension base
    class from script" mechanism this codebase already uses for
    ``kdb.NetlistSpiceWriterDelegate`` (``pdk_models.py``'s
    ``_ModelBindingSpiceWriterDelegate``) and ``kdb.GenericNetlistCompareLogger``
    (``lvs.py``'s ``_Logger``); this is the first such subclass for *device
    extraction* rather than netlist writing/comparison, because no built-in
    ``DeviceExtractor*`` class models this device's recognition shape (see
    ``MomCapacitorDevice``'s docstring for why).

    ``name`` becomes the extracted ``devices[].class`` value (and the
    device extractor/class name KLayout error/log messages cite).
    ``metal_count`` is the number of per-metal port layers to define --
    always ``len(deck.metals)`` for the owning :class:`MomCapacitorDevice`
    entry's deck, so a fresh instance's layer set lines up index-for-index
    with the caller's own ``metal_pins``/``metals`` regions (an empty
    ``kdb.Region`` at every index the entry's ``metal_pins`` left ``None``).

    **Finger-stack recovery (issue #2435) is this transcription's one
    deliberate addition to upstream's extractor.** Alongside the
    ``m1p``..``mNp`` *port* layers upstream defines, this instance defines
    ``m1d``..``mNd`` -- the deck's own drawn ``metals[i]`` conductor
    geometry lying **entirely inside** the recognition marker (the caller
    does that containment narrowing; see ``_extract_netlist``'s own
    ``mom_capacitors`` loop). ``MMIN``/``MMAX`` are then the lowest/highest
    1-based metal index populated by either a drawn finger shape or one of
    the two recognised ports, which is exactly the PCell's own
    ``mmin``/``mmax``: every metal level in ``mmin..mmax`` carries the same
    bar/tooth pattern (``cmomi_code.py``'s own "Every metal layer
    mmin..mmax carries the same pattern" comment), and both pins land on a
    level inside that range (``mmax``, or ``mmax``/``mmax-1`` stacked for
    the ``same`` feed) -- so unioning the port levels in can never widen the
    measured range beyond the drawn one, while keeping the measurement
    well-defined for a pin-only abstraction that draws no conductor inside
    the marker at all.

    Upstream's ``CapMomExtractor`` does **not** capture this (checked
    against ``custom_mom_extractor.lvs`` at the revision
    ``decks/sg13cmos5l.py`` pins): nothing there is being diverged *from*,
    the port half below still mirrors it line for line, and the addition is
    purely extra input layers plus two extra parameters.

    A fresh instance is required per device *name* (not just per deck):
    ``kdb.GenericDeviceExtractor.setup()`` sets this extractor's own
    ``name``/registers its own device class once, so reusing one instance
    across ``cap_cmomi`` and ``cap_cmomf`` would register a second device
    class under the first one's name instead of two independent ones --
    mirroring why upstream's own ``cap_extraction.lvs`` constructs a fresh
    ``CapMomExtractor.new(...)`` per device too, despite both sharing the
    same Ruby class.
    """
    import klayout.db as kdb

    class _MomCapacitorExtractor(kdb.GenericDeviceExtractor):
        def __init__(self, extractor_name: str, num_metals: int) -> None:
            super().__init__()
            self._extractor_name = extractor_name
            self._num_metals = num_metals
            # Terminal/parameter ids are read back off their own definition
            # objects in `setup()`, once the device class is registered --
            # mirrors upstream's own `@reg_dev.terminal_id('mim_top')`
            # lookups rather than hard-coding the ids `add_terminal()`
            # happens to hand out in declaration order.
            self._terminal_a = 0
            self._terminal_b = 1

        def setup(self) -> None:
            # Mirrors upstream's private `define_layers`: `core` (the
            # marker), one per-metal port layer per declared metal level,
            # then `dev_mk` (the same marker again, kept as its own layer
            # for 1:1 parity with upstream's `core`/`dev_mk` split -- see
            # `custom_mom_extractor.lvs`'s own `define_layers`). The
            # `m<n>d` drawn-conductor block between the ports and `dev_mk`
            # is issue #2435's own addition (see this builder's docstring);
            # keeping it *after* the whole `m<n>p` run leaves upstream's own
            # `core`/`m<n>p` layer indices unchanged.
            self.name = self._extractor_name
            self.define_layer("core", f"{self._extractor_name} recognition marker")
            for layer_name, description in _mom_extractor_metal_layer_specs(
                self._num_metals
            ):
                self.define_layer(layer_name, description)
            self.define_layer("dev_mk", "Device marker")

            # `DeviceCustomMIM`'s shape (`custom_mim_extractor.lvs`) --
            # built by `mom_capacitor_device_class` (shared with the
            # round-trip reader-side recognition `netlist_capacitor_
            # recovery.py` registers for the same name, issue #1942) so
            # both sides agree on the exact same terminal/parameter shape.
            # Terminal/parameter ids are read back off the returned
            # object's own definitions rather than assumed, mirroring
            # `mom_capacitor_device_class`'s own docstring note on why
            # (`add_terminal()`/`add_parameter()` write the id onto the
            # *argument* object, not the call's own return value).
            device_class = mom_capacitor_device_class(self._extractor_name)
            terminal_a, terminal_b = device_class.terminal_definitions()
            self._terminal_a = terminal_a.id()
            self._terminal_b = terminal_b.id()
            param_ids = {
                definition.name: definition.id()
                for definition in device_class.parameter_definitions()
            }
            self._param_w = param_ids["W"]
            self._param_l = param_ids["L"]
            self._param_mmin = param_ids["MMIN"]
            self._param_mmax = param_ids["MMAX"]
            # Issue #2445: `cap_cmomi` only (`mom_capacitor_has_feed`).
            self._param_feed = param_ids.get("FEED")
            self.register_device_class(device_class)

        def get_connectivity(
            self, layout: kdb.Layout, layers: list[int]
        ) -> kdb.Connectivity:
            # Mirrors upstream's own `get_connectivity`: the marker
            # self-merges, and every per-metal port layer is scoped to
            # shapes touching the (duplicate) device-marker layer -- purely
            # an *internal* clustering connectivity for this extractor's own
            # marker-to-ports grouping, entirely separate from (and never
            # wired into) the outer `LayoutToNetlist` connectivity graph the
            # caller builds with its own `l2n.connect()` calls.
            core = layers[0]
            # Every per-metal layer -- upstream's `m<n>p` ports plus issue
            # #2435's own `m<n>d` drawn-conductor layers -- is scoped to the
            # marker the same way, so one slice covers both. The drawn half
            # is safe to cluster on: the caller hands us only conductor
            # polygons lying *entirely inside* a marker, so a route crossing
            # over a MoM cap (or joining two of them) is never in this
            # geometry and cannot merge two markers' clusters into one --
            # the "found N != 2 ports, drop both devices" failure that
            # merging causes.
            marker_scoped_layers = layers[1 : 1 + 2 * self._num_metals]
            dev_mk = layers[-1]
            conn = kdb.Connectivity()
            conn.connect(core, core)
            conn.connect(core, dev_mk)
            for marker_scoped_layer in marker_scoped_layers:
                conn.connect(marker_scoped_layer, dev_mk)
            return conn

        def extract_devices(self, layer_geometry: list[kdb.Region]) -> None:
            # Mirrors upstream's own `extract_devices` body closely -- see
            # its inline comments (transcribed into this function's own
            # docstring) for the full rationale of each step.
            core = layer_geometry[0]
            metal_ports = {
                metal_index + 1: layer_geometry[1 + metal_index]
                for metal_index in range(self._num_metals)
            }
            # Issue #2435: the drawn-conductor half of the finger-stack
            # measurement, index-aligned with `metal_ports` above.
            drawn_metals = {
                metal_index + 1: layer_geometry[1 + self._num_metals + metal_index]
                for metal_index in range(self._num_metals)
            }
            dev_mk = layer_geometry[-1]

            for component in dev_mk.merged().each():
                ports: list[tuple[Any, int]] = []
                for metal_index, port_region in metal_ports.items():
                    for polygon in port_region.each():
                        ports.append((polygon, metal_index))

                if len(ports) != 2:
                    # A well-formed device places exactly two `MkPin`s under
                    # its marker (upstream's own guard) -- skip anything
                    # else rather than guessing which two of N ports belong
                    # together. The usual real-world cause is two device
                    # markers close enough to touch and merge into one
                    # cluster, which drops BOTH devices, not just the
                    # malformed one.
                    self.error(
                        f"{self._extractor_name}: expected exactly 2 port "
                        f"regions under its recognition marker, found "
                        f"{len(ports)}. The device is not extracted. Check "
                        "for markers of adjacent devices touching or "
                        "overlapping.",
                        component,
                    )
                    continue

                device = self.create_device()

                # `l` -> marker bounding-box WIDTH (X extent), `w` -> HEIGHT
                # (Y extent) -- upstream's own axis mapping, transcribed
                # verbatim (`custom_mom_extractor.lvs`'s "Parameter/axis
                # mapping" comment).
                bbox = core.merged().bbox()
                device.set_parameter(self._param_l, bbox.width() * self.dbu())
                device.set_parameter(self._param_w, bbox.height() * self.dbu())

                # Issue #2435: the drawn finger stack's inclusive 1-based
                # metal range -- the upstream `.subckt`'s own `mmin`/`mmax`,
                # whose layer count `N = mmax - mmin + 1` keys the compact
                # model's capacitance density. The measurement itself lives
                # in `_mom_finger_stack_metal_range` below; a non-contiguous
                # range is reported through the `report_gap` callback (which
                # is what binds this extractor's own `self.error`/`component`
                # to it) rather than being resolved there.
                mmin, mmax = _mom_finger_stack_metal_range(
                    (port_metal_index for _, port_metal_index in ports),
                    drawn_metals,
                    device_name=self._extractor_name,
                    report_gap=self.error,
                    gap_context=component,
                )
                device.set_parameter(self._param_mmin, float(mmin))
                device.set_parameter(self._param_mmax, float(mmax))

                # Issue #2445: the PCell's `feed` variant, from the two
                # ports' placement inside the marker (see
                # `_mom_feed_variant`). Left at the class default `0`
                # ("unmeasured") when the layout matches no PCell signature;
                # a no-op for classes without `FEED` (`cap_cmomf`).
                _set_mom_feed_parameter(device, self._param_feed, ports, bbox)

                # Deterministic pick of two ports (by x, then y, then metal
                # index) -- which of the two ends up on terminal A is
                # arbitrary, which is why the two terminals are declared
                # equivalent above; what matters is that each terminal is
                # defined on the layer that carries its own metal net, so
                # two ports stacked on adjacent metals (the `same`-feed
                # PCell configuration) stay on separate nets rather than
                # collapsing onto one.
                ports.sort(
                    key=lambda entry: (
                        entry[0].bbox().center().x,
                        entry[0].bbox().center().y,
                        entry[1],
                    )
                )
                a_polygon, a_metal_index = ports[0]
                b_polygon, b_metal_index = ports[-1]

                self.define_terminal(device, self._terminal_a, a_metal_index, a_polygon)
                self.define_terminal(device, self._terminal_b, b_metal_index, b_polygon)

    return _MomCapacitorExtractor(name, metal_count)
