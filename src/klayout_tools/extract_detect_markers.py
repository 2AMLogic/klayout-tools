"""``klt extract`` unmodelled-device / flavour-marker geometry diagnostics
(issue #5736).

Split out of `extract.py` to keep it under its file-size ceiling. A move, not
a rewrite: the code below is byte-for-byte what lived inline there. These
detectors read ``layout`` / ``top_cell`` / ``deck`` geometry after the
extraction pass and only append to ``warnings[]`` or fill an optional
out-parameter; none changes a net, a device, a bound model or a rule
threshold. They are the layout-geometry counterpart of
`extract_detect_block.py`.

``_bbox_um_rounded`` and ``_resolve_mos_capacitors`` still live in
`extract.py` and are imported inside the function bodies that call them:
`extract.py` imports this module at its top level, so a module-level
``from .extract import ...`` would be a circular import (the idiom
`extract_detect_block.py` documents).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._layout import region as _region
from .decks import ExtractionDeck
from .extract_flavour import _flavour_split_island

if TYPE_CHECKING:
    import klayout.db as kdb


#: Minimum number of geometrically separate `contact` clusters a candidate
#: poly component must touch to be flagged by `_detect_unmodelled_poly_bodies`
#: -- the "resistor-body signature": a two-terminal conductor segment
#: contacted at *each* end, rather than routing with a single landing pad.
_UNMODELLED_POLY_MIN_CONTACT_CLUSTERS = 2


def _detect_unmodelled_poly_bodies(
    poly: kdb.Region,
    contact: kdb.Region,
    nfet_gate: kdb.Region,
    pfet_gate: kdb.Region,
    resistor_markers: kdb.Region | None = None,
    resistor_bodies: kdb.Region | None = None,
    dbu: float = 1.0,
    interconnect_markers: kdb.Region | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag ``poly`` connected components that no MOS gate extractor claims
    and that touch ``contact`` at two or more geometrically separate
    locations -- the resistor-body signature (issue #288).

    This deck's device extractors are a fixed, curated subset (today:
    ``nfet``/``pfet``, an optional declared drawn resistor/bipolar/
    capacitor). Geometry drawn for any device class the deck does not
    (yet) recognise is still built out of ordinary connectivity layers
    (``poly``, ``contact``, ...), so :func:`_extract_netlist`'s blanket
    ``l2n.connect(poly, contact)`` (and friends) absorbs it into ordinary
    interconnect -- silently shorting two terminals a schematic keeps
    distinct, with zero signal in ``warnings`` today. This is a narrowly
    scoped *diagnostic* heuristic, not a device extractor: it identifies
    the shape, not the missing device class, and is deliberately
    conservative to avoid false positives on real layouts (see
    ``docs/cli/extract.md``'s "Known limitation: unmodelled device
    geometry").

    Called with ``poly`` as it stands *after* :func:`_resolve_resistors`
    has already subtracted out every resistor body this deck's own
    ``ResistorDevice`` declarations recognise -- so a properly marked
    drawn resistor never reaches this heuristic at all. What is left is
    only genuinely unrecognised geometry: a resistor drawn without (or
    excluded from) the deck's marker layer, or any other not-yet-modelled
    device class that happens to share poly + contact.

    A component is skipped unconditionally if it touches ``nfet_gate``/
    ``pfet_gate`` anywhere -- a real MOS gate (with or without a
    legitimate poly-contacted gate strap) or ordinary poly routing between
    two recognised gates (poly needs no via to route on itself, so both
    ends of such a run are one merged polygon together with the gates
    themselves), never a candidate unmodelled-device body. It is likewise
    skipped unconditionally if it touches ``resistor_bodies`` (issue #324) --
    the recognised body regions ``_resolve_resistors`` already cut out of
    ``poly`` -- since a poly component *abutting* one of those bodies is that
    resistor's own terminal head, by construction: the head survives as a
    separate connected component once its body is subtracted, and legitimately
    carries a normal (2+) contact array with no device-recognition gap behind
    it at all.

    ``resistor_markers`` (issue #299) is the union of every ``ResistorDevice``
    marker layer this deck declares on ``poly`` (regardless of any
    ``requires``/``excludes`` narrowing) -- the raw resistor-ID geometry, not
    the narrower recognised-body region ``_resolve_resistors`` already cut
    out. It distinguishes two different reasons a flagged component reaches
    this heuristic at all: a component overlapping it carries a resistor
    marker this deck *knows about* but whose ``requires``/``excludes``
    conditions this specific segment did not satisfy (a **deck-coverage
    gap** -- e.g. gf180mcu's ``RES_MK`` present without the ``SAB``/``Pplus``
    combination any declared entry needs), versus a component that carries
    none of this deck's resistor markers at all (an **unmarked** shape, the
    original #288 case -- some other, entirely undeclared device class, or a
    resistor with no marker drawn). ``None`` (the default) treats every
    flagged component as unmarked, matching this function's behaviour before
    #299.

    ``resistor_bodies`` (issue #324) is the union of every *recognised*
    ``ResistorDevice`` body region ``_resolve_resistors`` returned for this
    layout (already narrowed by each entry's own ``requires``/``excludes``) --
    distinct from ``resistor_markers`` above, which is the raw, unnarrowed
    marker geometry. ``None`` (the default) skips no component on this basis,
    matching this function's behaviour before #324.

    ``dbu`` converts each flagged component's bounding box from database
    units to micrometres for ``unmodelled_poly[]`` (see below); defaults to
    ``1.0`` (i.e. no conversion) for callers that only need the warning
    strings.

    ``interconnect_markers`` (issue #1425) is the union of every shape drawn
    on the deck's optional ``ExtractionDeck.poly_interconnect`` marker layer
    -- a caller-drawn annotation saying "this poly is intentional
    interconnect" (most commonly a poly underpass: a strip contacted to
    metal at each end, routing one net beneath another). A component
    overlapping it at all is skipped unconditionally, the same way a
    component touching ``gate_regions``/``resistor_bodies`` is skipped above
    -- it never reaches the contact-cluster count or the resistor-marker
    split, and so is never counted, flagged, or warned about. ``None`` (the
    default) skips no component on this basis, matching this function's
    behaviour before #1425.

    Returns ``(warnings, unmodelled_poly)``. ``warnings`` has up to two
    strings (empty when nothing is flagged) -- at most one for unmarked
    shapes and one for marked-but-unrecognised ones -- each naming how many
    components were flagged and pointing at the documented limitation rather
    than guessing a device name. ``unmodelled_poly`` (issue #324) is one
    entry per flagged component -- ``{"bbox_um": {"left", "bottom", "right",
    "top"}, "reason": "unmarked" | "marked_unrecognised"}`` -- sorted by
    ``(left, bottom)`` for deterministic output, so a consumer can enumerate
    and triage the exact flagged shapes instead of re-deriving them by
    re-implementing this heuristic against the stream. Always a list, empty
    when ``warnings`` is empty.
    """
    import klayout.db as kdb

    from .extract import _bbox_um_rounded

    gate_regions = nfet_gate + pfet_gate
    bodies = resistor_bodies if resistor_bodies is not None else kdb.Region()
    markers = resistor_markers if resistor_markers is not None else kdb.Region()
    interconnect = (
        interconnect_markers if interconnect_markers is not None else kdb.Region()
    )
    unmarked = 0
    marked_unrecognised = 0
    unmodelled_poly: list[dict[str, Any]] = []
    for component in poly.merged().each():
        candidate = kdb.Region(component)
        if not candidate.interacting(gate_regions).is_empty():
            continue
        if not candidate.interacting(bodies).is_empty():
            continue
        if not candidate.interacting(interconnect).is_empty():
            continue
        contact_clusters = (candidate & contact).merged().count()
        if contact_clusters < _UNMODELLED_POLY_MIN_CONTACT_CLUSTERS:
            continue
        if candidate.interacting(markers).is_empty():
            unmarked += 1
            reason = "unmarked"
        else:
            marked_unrecognised += 1
            reason = "marked_unrecognised"
        box = component.bbox()
        unmodelled_poly.append(
            {
                "bbox_um": _bbox_um_rounded(box, dbu),
                "reason": reason,
            }
        )

    unmodelled_poly.sort(
        key=lambda entry: (entry["bbox_um"]["left"], entry["bbox_um"]["bottom"])
    )

    warnings: list[str] = []
    if unmarked:
        shape_word = "shape" if unmarked == 1 else "shapes"
        warnings.append(
            f"{unmarked} poly-layer {shape_word} not part of any recognised nfet/pfet "
            "gate touch contact at 2+ separate points (the resistor-body "
            "signature) and carry no resistor-marker layer at all; this deck "
            "may not model the device class drawn here, and its terminals "
            "have been absorbed into ordinary interconnect as an unintended "
            "short -- see docs/cli/extract.md's 'Known limitation: unmodelled "
            "device geometry'."
        )
    if marked_unrecognised:
        shape_word = "shape" if marked_unrecognised == 1 else "shapes"
        warnings.append(
            f"{marked_unrecognised} poly-layer {shape_word} not part of any "
            "recognised nfet/pfet gate touch contact at 2+ separate points "
            "(the resistor-body signature) and carry a resistor-marker layer, "
            "but do not match any of this deck's declared ResistorDevice "
            "requires/excludes conditions (a deck-coverage gap, not unmarked "
            "geometry); their terminals have been absorbed into ordinary "
            "interconnect as an unintended short -- see docs/cli/extract.md's "
            "'Known limitation: unmodelled device geometry'."
        )
    return warnings, unmodelled_poly


def _detect_missing_flavour_markers(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    deck_name: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag each ``deck.mos_flavours`` marker layer with zero shapes anywhere
    in the layout (issue #2417).

    With no marker geometry, flavour selection never fires and every MOS
    device silently binds to the deck's default flavour. This is distinct
    from the marker being present but not overlapping active (a legitimate
    layout), which is *not* flagged.

    Returns ``(warnings, entries)``; each entry is ``{"deck": str,
    "flavour": str, "marker": "<layer>/<datatype>"}``. Both empty when the
    deck declares no flavour or every marker has geometry.
    """
    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    for flavour in deck.mos_flavours:
        if flavour.and_markers:
            # A combination flavour (issue #3599) shares its base marker with
            # a plain flavour that already reports it; a missing refining
            # layer is a legitimate layout, not a gap.
            continue
        if not _region(layout, top_cell, flavour.marker).is_empty():
            continue
        marker_label = f"{flavour.marker[0]}/{flavour.marker[1]}"
        warnings.append(
            f"deck '{deck_name}' declares MOS flavour '{flavour.flavour}' "
            f"scoped by marker layer {marker_label}, but that layer has no "
            "shapes in the layout; every MOS device binds to the deck's "
            "default flavour"
        )
        entries.append(
            {
                "deck": deck_name,
                "flavour": flavour.flavour,
                "marker": marker_label,
            }
        )
    return warnings, entries


def _flavour_recognised_islands(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    marker: tuple[int, int],
) -> kdb.Region:
    """Union of the gated ``deck.active`` islands claimed by a
    ``deck.mos_flavours`` entry whose ``and_markers`` include ``marker``
    (issue #4587): such a device is extracted, so its marker is not an
    unsupported device. Empty when no flavour names ``marker``."""
    import klayout.db as kdb

    flavours = [f for f in deck.mos_flavours if marker in f.and_markers]
    recognised = kdb.Region()
    if not flavours:
        return recognised
    active = _region(layout, top_cell, deck.active)
    poly = _region(layout, top_cell, deck.poly)
    nwell = _region(layout, top_cell, deck.nwell)
    for flavour in flavours:
        marker_region = _region(layout, top_cell, flavour.marker)
        and_regions = [_region(layout, top_cell, m) for m in flavour.and_markers]
        not_regions = [_region(layout, top_cell, m) for m in flavour.not_markers]
        for component in active.merged().each():
            island = kdb.Region(component)
            if (island & poly).is_empty():
                continue
            claimed_part, _ = _flavour_split_island(
                island, poly, flavour, marker_region, and_regions, not_regions, nwell
            )
            recognised += claimed_part
    return recognised


def _detect_unsupported_devices(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    entries_out: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Flag each ``deck.unsupported_device_markers`` layer that carries shapes
    in the extracted cell (issue #2919).

    The deck cannot extract that device class, so the devices are missing
    from the netlist even though the run reports ``status: extracted``.

    Returns the prose warnings and extends ``entries_out`` (when given) with
    one entry per flagged marker: ``{"kind": str, "marker":
    "<layer>/<datatype>", "count": int, "bbox_um": {...}, "description":
    str}`` where ``count`` is the number of connected marker components and
    ``bbox_um`` bounds all of them. Nothing is returned or appended when the
    deck declares no such marker or none has shapes.

    A marker component that touches an island ``deck.mos_capacitors``
    recognises (:func:`_resolve_mos_capacitors`, re-run here on the drawn
    layers) is a device the netlist *does* hold, so it is not reported --
    only the residue the deck could not classify stays loud.
    """
    import klayout.db as kdb

    from .extract import _bbox_um_rounded, _resolve_mos_capacitors

    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    recognised = kdb.Region()
    if deck.mos_capacitors and deck.unsupported_device_markers:
        recognised = _resolve_mos_capacitors(
            layout,
            top_cell,
            deck,
            _region(layout, top_cell, deck.active),
            _region(layout, top_cell, deck.poly),
            _region(layout, top_cell, deck.nwell),
            kdb.Region(),
        )[2]
    for spec in deck.unsupported_device_markers:
        region = _region(layout, top_cell, spec.marker).merged()
        if not recognised.is_empty():
            region = region.not_interacting(recognised)
        flavour_islands = _flavour_recognised_islands(
            layout, top_cell, deck, spec.marker
        )
        if not flavour_islands.is_empty():
            region = region.not_interacting(flavour_islands)
        if region.is_empty():
            continue
        count = region.count()
        marker_label = f"{spec.marker[0]}/{spec.marker[1]}"
        warnings.append(
            f"unsupported device class '{spec.kind}': marker layer "
            f"{marker_label} carries {count} shape(s) ({spec.description}), "
            "but this deck does not extract that device, so it is absent "
            "from the netlist"
        )
        entries.append(
            {
                "kind": spec.kind,
                "marker": marker_label,
                "count": count,
                "bbox_um": _bbox_um_rounded(region.bbox(), layout.dbu),
                "description": spec.description,
            }
        )
    if entries_out is not None:
        entries_out.extend(entries)
    return warnings


def _detect_unmodeled_device_markers(
    layout: kdb.Layout, top_cell: kdb.Cell, deck_name: str
) -> list[str]:
    """One prose warning per device-recognition marker layer the deck
    registers via :func:`~klayout_tools.decks.get_unmodeled_device_markers`
    that carries shapes in the extracted cell (issue #3858).

    Prose only, no structured field; empty when the deck registers no marker
    or none has shapes, so such layouts' output is unchanged.
    """
    from .decks import get_unmodeled_device_markers

    warnings: list[str] = []
    for marker, description in sorted(get_unmodeled_device_markers(deck_name).items()):
        region = _region(layout, top_cell, marker)
        if region.is_empty():
            continue
        warnings.append(
            f"layer {marker[0]}/{marker[1]} carries {region.merged().count()} "
            f"shape(s) with no native device recognition: {description}"
        )
    return warnings


def _detect_voltage_domain_overlap(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    deck_name: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Flag extracted MOS device geometry (``deck.active``) drawn inside a
    voltage-domain marker layer this deck does not model the scoping of
    (issue #552).

    Mirrors :func:`_detect_unmodelled_poly_bodies`'s "device carries a
    marker this deck doesn't model" shape, but for a *different* gap: not a
    device class this deck fails to recognise at all, but a device class
    (MOS) it recognises and extracts with the *wrong* model. Some decks
    (today, sg13g2's ``ThickGateOx`` 44/0, registered via
    :func:`~klayout_tools.decks.get_unmodeled_voltage_markers`) draw a
    marker selecting a second gate-oxide/voltage domain -- e.g. a thick-
    oxide flavour -- with its own correct MOS model, which this deck's
    ``ExtractionDeck.nfet_class``/``pfet_class`` derivation (well layer
    alone) does not read, so a transistor drawn entirely inside the marker
    still extracts bound to the default model name with no signal that
    anything is off.

    A marker this deck's ``ExtractionDeck.mos_flavours`` *also* declares
    (issue #1111 -- today, gf180mcu's ``Dualgate`` 55/0) is skipped
    entirely: MOS recognition for that marker is flavour-aware, so a
    transistor drawn inside it already extracts bound to the flavour's own
    real model (under ``--pdk``) -- the gap this function exists to flag
    does not apply to it, and flagging it anyway would be a stale warning
    about an already-closed gap.

    A marker/description pair is only flagged when the marker's geometry
    actually *interacts* with ``deck.active`` (the MOS device-recognition
    footprint, evaluated for the whole layout, both flavours combined) --
    not merely present somewhere in the stream -- so a marker shape drawn
    only over, say, an ESD diode this deck's ``DiodeDevice`` entries already
    scope correctly to it produces no false-positive warning here.

    Returns ``(warnings, voltage_domain_warnings)``: ``warnings`` has one
    prose string per flagged marker (empty when this deck registers no
    marker, every registered marker is covered by ``mos_flavours``, or none
    of the remainder overlaps ``deck.active``); ``voltage_domain_warnings``
    is the matching structured view -- one ``{"marker": "<layer>/<datatype>",
    "description": str}`` entry per flagged marker, mirroring ``klt drc``'s
    ``coverage.voltage_domain_warnings`` shape (same registry, same
    description text) so a caller correlating the two commands' output for
    the same layout sees the identical wording. Always a list, empty when
    nothing is flagged.
    """
    from .decks import get_unmodeled_voltage_markers

    unmodeled_markers = get_unmodeled_voltage_markers(deck_name)
    flavour_markers = {flavour.marker for flavour in deck.mos_flavours}
    unmodeled_markers = {
        marker: description
        for marker, description in unmodeled_markers.items()
        if marker not in flavour_markers
    }
    if not unmodeled_markers:
        return [], []

    active = _region(layout, top_cell, deck.active)
    if active.is_empty():
        return [], []

    warnings: list[str] = []
    voltage_domain_warnings: list[dict[str, Any]] = []
    for marker, description in sorted(unmodeled_markers.items()):
        marker_region = _region(layout, top_cell, marker)
        if marker_region.is_empty():
            continue
        if active.interacting(marker_region).is_empty():
            continue
        marker_label = f"{marker[0]}/{marker[1]}"
        warnings.append(
            f"MOS device geometry overlaps the '{marker_label}' "
            f"voltage-domain marker: {description}"
        )
        voltage_domain_warnings.append(
            {"marker": marker_label, "description": description}
        )
    return warnings, voltage_domain_warnings
