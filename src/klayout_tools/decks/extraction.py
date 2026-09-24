"""Extraction device domain for the ``decks`` package: the device-recognition
dataclasses an :class:`ExtractionDeck` composes.

Split out of ``decks/__init__.py`` (issue #1821) as a self-contained
domain: :class:`ResistorDevice`/:class:`ResistorFlavour`,
:class:`MOSFlavour`, :class:`ExtractionDeck` itself,
:class:`BipolarDevice`, :class:`CapacitorDevice`/:class:`CapacitorFlavour`,
:class:`MomCapacitorDevice`, and :class:`DiodeDevice` -- the curated device
tables ``klt extract`` matches drawn geometry against to recognise
precision resistors, MOS transistors, bipolar junction transistors, MiM/MOM
capacitors, and diodes, per PDK family.

Several of these dataclasses carry a ``provenance``/``nfet_provenance``/
``pfet_provenance`` field typed :class:`~klayout_tools.decks.rules.RuleProvenance`
-- the same structured DRM-citation type the DRC rule domain
(``rules.py``) uses for :class:`~klayout_tools.decks.rules.DrcRule`. This
module imports it from there rather than duplicating it; no type here is
referenced back from ``rules.py``, so the dependency is one-directional.

The registry/lookup functions that use these types (:func:`get_extraction_deck`,
:func:`_resolve_device_flavours`, etc.) stay in ``decks/__init__.py``, which
re-exports every public name below so existing
``from klayout_tools.decks import X`` call sites are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import RuleProvenance


@dataclass(frozen=True)
class ResistorDevice:
    """One *drawn* precision-resistor device class an :class:`ExtractionDeck`
    can recognise (issue #222).

    A drawn resistor is a deliberately-marked segment of an ordinary
    conductor (poly, diffusion, metal): the designer draws the conductor,
    then covers the resistive part of it with the PDK's resistor-ID
    ("marker") layer. Without this declaration that segment extracts as a
    plain conductor -- i.e. a **short** between the resistor's two heads --
    so a resistor drawn at the wrong length/width passes LVS silently. This
    is the device-recognition analogue of :class:`ExtractionDeck`'s
    ``nfet_class``/``pfet_class`` MOS wiring, driving KLayout's native
    ``klayout.db.DeviceExtractorResistor`` /
    ``...DeviceExtractorResistorWithBulk``.

    Geometry (all fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering):

    - ``body`` -- the drawn conductor layer the resistor lives on. **Must**
      be one of the owning deck's own conductor layers (``poly``,
      ``active``, or one of ``metals``), because the recognised resistor
      body is *subtracted* from that layer's connectivity region: leaving it
      in would short the two terminals together through the conductor and
      defeat the whole point.
    - ``marker`` -- the resistor-ID layer. The resistive segment is
      ``body & marker`` (further narrowed by ``requires``/``excludes``); the
      **terminals** are the rest of that conductor layer (``body`` minus the
      recognised segment), i.e. the contacted heads on either side. This
      mirrors both PDKs' own KLayout LVS decks, which derive the terminal
      layer the same way (sky130's ``poly_con = poly.not(poly_res)``,
      gf180mcu's ``poly2_con = poly2.not(res_mk)``).
    - ``requires`` -- additional layers that must **all** cover the segment
      for it to be this device (e.g. gf180mcu's ``Pplus`` + ``SAB``, which
      are what distinguish a 350 ohm/sq *unsalicided* p+ poly resistor from
      a 7.3 ohm/sq salicided one).
    - ``excludes`` -- layers that disqualify the segment (subtracted from
      it), e.g. sky130's ``rpm``/``urpm`` precision-resistor implant masks,
      which mark the *other*, much-higher-sheet-rho poly resistor flavours.
      A segment excluded here is left as ordinary connected conductor (it
      keeps today's short) rather than extracted with the wrong resistance
      -- a wrong value passing LVS with high confidence is worse than a
      known-unmodelled device.
    - ``terminal`` -- optional override naming a *different* deck conductor
      layer to take the terminals from (defaults to ``body``). Like
      ``body``, it must be one of the deck's own conductor layers.

    ``sheet_rho_ohm_sq`` is the device's sheet resistance in ohms per
    square; KLayout computes ``R = L / W * sheet_rho`` from the recognised
    segment's own geometry -- exactly the role ``area_cap_f_um2`` plays for
    a capacitor device (#512). Every deck that sets it must cite the
    PDK/DRM source it came from inline, the same way the DRC decks cite
    rule ids.

    ``fixed_offset_ohm`` (issue #518) is an optional second coefficient: a
    per-instance resistance added on top of ``L / W * sheet_rho_ohm_sq``,
    for a precision-resistor flavour whose real PDK model composes a
    length-scaling *body* term with a fixed-length *head*/end-effect term
    at the metal-to-resistor contact (sky130's ``res_high_po``'s SPICE
    ``.subckt`` wires an ``rhead`` sub-resistor -- a constant contact
    resistance, independent of the segment's own drawn length -- in series
    with an ``rbody`` sub-resistor that scales with drawn length). KLayout's
    ``kdb.DeviceExtractorResistor``/``...ResistorWithBulk`` constructors
    only take one sheet-rho coefficient, so -- mirroring
    ``CapacitorDevice.perim_cap_f_um``'s post-extraction correction using
    the already-computed ``A``/``P`` device parameters -- this is *not*
    threaded into that constructor call: ``extract.py``'s
    ``_apply_device_parameter_corrections`` reads the already-computed ``R``
    device parameter back and rewrites it in place to ``L / W *
    sheet_rho_ohm_sq + fixed_offset_ohm`` after extraction, before the
    netlist reaches the SPICE writer or ``klt lvs``'s comparer (issue
    #521). Defaults to ``0.0``, which
    reproduces ``R = L / W * sheet_rho_ohm_sq`` only -- today's behaviour,
    bit-for-bit -- for every deck entry that does not set it. A deck that
    transcribes (or, as for sky130's ``res_high_po``, measures via ngspice
    against) its PDK's own composite head-plus-body resistor model should
    set this rather than leave the fixed head/end term silently dropped.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and the model token on the written ``R`` card -- a
    consumer simulating the netlist supplies a matching ``.model``, exactly
    as it already must for the ``nfet``/``pfet`` ``M`` cards).

    ``bulk_to_substrate`` selects ``DeviceExtractorResistorWithBulk`` (a
    third ``W`` terminal tied to the deck's ``substrate_net`` global)
    instead of the plain two-terminal ``DeviceExtractorResistor``, for a
    device whose PDK LVS deck models a bulk terminal (e.g. gf180mcu's
    ``ppolyf_u``, extracted upstream with ``'W' => sub``). It shares the
    same ``W`` terminal region as the NMOS body (``extract.py``'s
    ``nfet_body``), so it inherits that terminal's behaviour: on a deck that
    draws a distinct ``tap`` layer (issue #490), a substrate-tie ring drawn
    outside every ``nwell`` and contacted up to a named net resolves the
    bulk terminal to that real net; only a layout with no such ring falls
    back to the deck's synthesized ``substrate_net`` global.

    ``flavour_option``/``flavours`` (issue #595) declare an optional,
    caller-selectable **sheet-rho flavour set** for a resistor family whose
    members share *identical* recognition geometry -- the same
    ``body``/``marker``/``requires``/``excludes`` region, disambiguated only
    by a build-time deck variable in the official PDK LVS deck this is
    transcribed from (e.g. gf180mcu's ``POLY_RES``, which selects one of
    ``'1k'``/``'2k'``/``'3k'`` for the *same* drawn ``ppolyf_u_h`` region --
    see #299's own note on why wiring all three as separate
    :class:`ResistorDevice` entries would instead recognise the *same* drawn
    shape three times over). This is a different problem from
    ``requires``/``excludes`` (which tell two *geometrically distinguishable*
    flavours apart): there is no drawn layer here a deck could key off, so
    the deck itself keeps recognising and wiring exactly one entry -- this
    entry's own ``name``/``sheet_rho_ohm_sq`` are simply *which* flavour that
    is -- and a caller who knows their design draws a different flavour of
    the same geometry selects it via ``get_extraction_deck``'s
    ``deck_options`` (``klt extract --deck-option <flavour_option>=<value>``).

    ``flavour_option`` is the deck-option key this entry's flavour is chosen
    by (``None``, the default, means this entry has no caller-selectable
    flavour -- today's behaviour, unaffected either way). ``flavours`` is the
    tuple of every :class:`ResistorFlavour` selectable through that key,
    typically including one entry that matches this ``ResistorDevice``'s own
    default ``name``/``sheet_rho_ohm_sq`` (the value used when no
    ``deck_options`` override is given) plus the previously-unmodelled
    siblings. :func:`get_extraction_deck` validates a given ``deck_options``
    value against this list and raises :class:`InvalidDeckOptionError` for an
    unrecognised key or value rather than silently keeping the default or
    guessing -- the same "known-unmodelled short beats a silently wrong
    value" discipline ``excludes`` already applies. A deck entry that leaves
    ``flavours`` at its empty default has no selectable flavour: passing
    ``deck_options`` naming a key no entry declares is itself an
    :class:`InvalidDeckOptionError`, not a silent no-op.

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's ``sheet_rho_ohm_sq`` (and, when
    set, ``fixed_offset_ohm``) was transcribed/measured from -- the
    device-recognition analogue of :class:`DrcRule.provenance` (see
    :class:`RuleProvenance`'s own docstring for the shared type and how it
    generalises beyond DRC). ``None`` (the default) means no structured
    provenance has been backfilled for this entry yet -- the prose citation
    in the deck module's own inline comment remains the only record, exactly
    as for every entry before this field existed.
    """

    name: str
    body: tuple[int, int]
    marker: tuple[int, int]
    sheet_rho_ohm_sq: float
    requires: tuple[tuple[int, int], ...] = ()
    excludes: tuple[tuple[int, int], ...] = ()
    terminal: tuple[int, int] | None = None
    bulk_to_substrate: bool = False
    fixed_offset_ohm: float = 0.0
    flavour_option: str | None = None
    flavours: tuple[ResistorFlavour, ...] = ()
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class ResistorFlavour:
    """One caller-selectable ``(value, name, sheet_rho_ohm_sq)`` choice for a
    :class:`ResistorDevice` whose ``flavours`` field declares more than one
    sheet-rho interpretation of the *same* recognised geometry (issue #595).

    ``value`` is the string a caller passes via ``deck_options`` (e.g.
    gf180mcu's ``"1k"``/``"2k"``/``"3k"``, matching the PDK's own upstream
    ``POLY_RES`` deck-variable spelling so a record can cite it directly).
    ``name``/``sheet_rho_ohm_sq`` are the ``ResistorDevice.name``/
    ``sheet_rho_ohm_sq`` the owning entry is rewritten to when this flavour is
    selected -- everything else about the entry (``body``, ``marker``,
    ``requires``, ``excludes``, ``terminal``, ``bulk_to_substrate``,
    ``fixed_offset_ohm``) is unchanged, since flavour selection never changes
    *which* geometry is recognised, only what device it is reported as.
    """

    value: str
    name: str
    sheet_rho_ohm_sq: float


@dataclass(frozen=True)
class MOSFlavour:
    """One additional, marker-scoped MOS voltage/gate-oxide flavour an
    :class:`ExtractionDeck` recognises via its optional ``mos_flavours``
    field (issue #1111, option 2 of #552) -- e.g. gf180mcu's ``Dualgate``
    (55/0) selecting its 5V/6V thick-oxide domain, whose real device models
    (``nfet_06v0``/``pfet_06v0``) differ from the deck's default 3.3V
    ``nfet_03v3``/``pfet_03v3``.

    Before this field existed, a deck derived NMOS/PMOS purely from
    ``active``/``nwell`` (see :class:`ExtractionDeck`'s own docstring) and
    bound every recognised transistor to the same model regardless of any
    other marker layer drawn over it -- ``decks.get_unmodeled_voltage_markers``
    (issue #577) only *warned* about the resulting mismatch, it never fixed
    it. Each ``MOSFlavour`` entry narrows that split for the geometry drawn
    inside ``marker``: ``extract.py`` computes this deck's ordinary
    ``nfet``/``pfet`` regions from ``active`` with every declared flavour's
    marker-interacting geometry first set aside (so the default split is
    unaffected outside every flavour marker -- no regression for the common
    case), then re-derives an *additional* ``nfet``/``pfet`` split from just
    that set-aside geometry per flavour, extracted with its own
    ``kdb.DeviceExtractorMOS4Transistor`` pass.

    Deliberately does **not** introduce a new ``devices[].class`` label for
    flavoured transistors: every MOS device -- flavoured or not -- still
    extracts under the deck's ordinary ``nfet_class``/``pfet_class`` (e.g.
    plain ``"nfet"``/``"pfet"``), so this has no effect on ``device_counts``,
    ``klt lvs`` device-class matching, or any other consumer of the
    structural netlist. Instead, ``extract.py`` tags each flavoured device
    with a KLayout device *property* (``pdk_models.MOS_FLAVOUR_PROPERTY``,
    keyed by :attr:`flavour`) that only the ``--pdk`` SPICE model-binding
    writer (:mod:`klayout_tools.pdk_models`) reads, to select the flavour's
    own real subcircuit instead of the deck's default one -- the property is
    invisible to every other consumer (JSON response, LVS comparison,
    unbound ``M``-card output). See ``pdk_models``'s module docstring for the
    matching ``(deck_name, pdk_variant_family) -> {flavour: {"nfet": ...,
    "pfet": ...}}`` binding table this ties into.

    ``marker`` should be a layer this deck also registers via
    ``get_unmodeled_voltage_markers`` describing what remains unmodelled
    beyond MOS recognition (e.g. gf180mcu's DRC rules that still do not read
    ``Dualgate``) -- reusing that same marker, not a new deck-specific
    hardcode, is what makes a flavour declaration here also suppress
    ``extract.py``'s own MOS ``voltage_domain_warnings`` for exactly the
    geometry this entry now correctly models (issue #577's diagnostic is a
    *residual*-gap signal; once a gap is closed here, the warning for it
    should stop firing).

    ``flavour`` is a short caller-invisible identifier (e.g. ``"06v0"``) that
    must match a key in ``pdk_models``'s per-deck flavour binding table --
    the single point of contact between this deck-geometry declaration and
    that PDK-model-name table (kept in separate modules, matching this
    codebase's existing "geometry vs. model binding" module split).

    A transistor's *whole* active-diffusion island (both source and drain,
    not just the gate -- gf180mcu draws a MOS device's active mask as one
    continuous polygon spanning source-gate-drain) is classified against
    ``marker`` as a single unit: an island that ``interacting`` overlaps the
    marker *at all* is entirely this flavour's, never split mid-island. This
    is a deliberate, documented choice for the marker-straddling case (the
    DRM does not contemplate a transistor legally straddling a voltage-
    domain boundary at all -- every real device is either fully inside or
    fully outside the marker), and matches the "any overlap counts" idiom
    ``_detect_voltage_domain_overlap`` (issue #552/#577) already uses for its
    own marker-interaction gate.

    ``description`` is a short human-readable label for this flavour
    (currently unused by any consumer beyond documentation -- a placeholder
    for a future per-flavour JSON surface).

    ``nfet_provenance``/``pfet_provenance`` (issue #1231) are machine-readable
    citations of the upstream PDK-LVS-deck source lines *this flavour's* two
    MOS recognition rules were transcribed from -- the per-flavour siblings of
    :attr:`ExtractionDeck.nfet_provenance`/``pfet_provenance``, which cite the
    deck's *default* (unflavoured) pair. A flavour genuinely is two upstream
    device rules (an NMOS one and a PMOS one, e.g. sg13g2's ``sg13_hv_nmos``/
    ``sg13_hv_pmos``), so it carries a pair rather than the single
    ``provenance`` field :class:`ResistorDevice` and friends use. Both default
    to ``None`` -- no structured provenance backfilled for this flavour, the
    same default every other ``provenance`` field in this module carries, so a
    deck that leaves them unset (e.g. gf180mcu's ``Dualgate`` entry, whose
    prose comment remains the record) is unaffected.
    """

    marker: tuple[int, int]
    flavour: str
    description: str = ""
    nfet_provenance: RuleProvenance | None = None
    pfet_provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class ExtractionDeck:
    """Connectivity + device-extraction rule set for ``klt extract`` (see
    ``docs/design/lvs-extraction-spike.md`` and ``docs/cli/extract.md``).

    A curated, per-PDK-family layer-role table for KLayout's
    ``klayout.db.LayoutToNetlist`` -- the extraction analogue of
    :class:`DrcRule`'s curated width/space/enclosure table, covering a
    two-terminal-well CMOS stack (NMOS/PMOS via one drawn well layer,
    contact/local-interconnect up through the PDK's declared metal stack --
    an arbitrary number of ``metals`` levels joined by ``vias``, e.g.
    gf180mcu's full ``Metal1``-``Metal5``) rather than a full PDK's device
    zoo. All layer fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering.

    ``active``/``poly``/``nwell`` are the device-recognition layers: NMOS is
    ``active - nwell``, PMOS is ``active & nwell`` (KLayout's standard
    "well marks the flip side" MOS-splitting idiom). ``tap`` is an optional,
    *distinct* substrate/well-tie diffusion layer (present when a PDK draws
    taps on a separate layer from transistor active, e.g. sky130's
    ``tap.drawing``; ``None`` when taps share the active layer, e.g.
    gf180mcu's ``Comp`` -- see the family deck's own docstring for which
    case applies and why a blanket "connect the well to every contact
    inside it" rule is wrong (it shorts every transistor terminal in the
    well together; only a genuinely distinct tap region is safe to tie to
    the well directly).

    ``tap_nplus``/``tap_pplus`` (issue #1084) are optional companion fields
    for exactly the ``tap is None`` case above -- a PDK family that draws no
    dedicated tap mask at all, but *does* draw a well/substrate tie as
    ordinary ``active`` diffusion covered by an implant layer the deck
    already declares for other purposes (e.g. gf180mcu's ``Nplus``/
    ``Pplus``, used elsewhere in that deck's diode/resistor/bipolar
    recognition). When ``tap`` is ``None`` and at least one of these is set,
    ``extract.py`` *derives* an equivalent tap region rather than requiring
    a literal drawn tap layer: ``tap_nplus & active & nwell`` is a well tie
    (n+ diffusion tied to the well from *inside* it -- opposite doping from
    an ordinary PMOS source/drain, which is p+ inside the well, so the two
    never collide), ``tap_pplus & active - nwell`` is a substrate tie (p+
    diffusion tied to the substrate from *outside* every well -- opposite
    doping from an ordinary NMOS source/drain, which is n+ outside the
    well). The derived region then feeds the exact same tap/``tap_substrate``
    connectivity mechanism a directly-drawn ``tap`` layer already uses
    (issue #490) -- ties the PMOS body (via ``nwell``) and the NMOS body
    (via the ``substrate_net`` global) to whatever real, routed net the tie
    reaches, instead of a second, parallel mechanism. Either field left
    ``None`` simply contributes nothing to the derived region (a deck may
    declare only one side). Both default to ``None``: a deck that declares
    neither -- every deck as of this field's introduction, until gf180mcu's
    own module sets them -- derives no tap at all, byte-for-byte the same
    behaviour as before these fields existed. Ignored outright when ``tap``
    itself is set: a genuinely distinct drawn tap layer always wins over a
    derived one.

    ``contact`` connects ``active``/``poly``/``tap`` to the first metal
    level. ``metals`` is the ordered metal-stack layer list (index 0 is the
    one ``contact`` lands on); ``metal_labels`` is the matching list of
    optional text/label layers used to name nets/pins (``None`` where a
    level has no label layer in this curated deck); ``vias`` connects
    ``metals[i]`` to ``metals[i + 1]`` and has ``len(metals) - 1`` entries.

    ``well_label`` is an optional text/label layer read directly off
    ``nwell`` (for a PDK that labels the well/body pin on the well layer
    itself, e.g. sky130's ``VPB``) -- distinct from ``metal_labels``, which
    label metal-level pins/power straps.

    ``dummy`` is an optional marker layer declaring drawn-but-non-functional
    "dummy" devices (issue #295, extended to drawn resistors and bipolars in
    #462): matched-pair/array edge fill whose terminals are tied off to a
    rail so the device contributes nothing to the circuit, yet must be
    *drawn* on the real device layers next to the devices it protects. Any
    MOS gate, drawn-resistor body, or bipolar unit lying under a shape on
    this layer is dropped before device recognition (``extract.py``
    subtracts it from the NMOS/PMOS gate regions, each drawn resistor's
    candidate body, and each bipolar's base region alike), so the dummy
    never appears in the extracted netlist and ``klt lvs`` no longer reports
    a spurious ``device.unmatched`` for it -- while the dummy's remaining
    geometry still extracts as ordinary interconnect (it ties to the rail as
    drawn). ``None`` (the default) is fully backward-compatible: a deck that
    declares no ``dummy`` layer extracts exactly as it did before the field
    existed. Whether a given PDK draws a native dummy-marker layer is left
    to the deck author to declare.

    ``poly_label`` is an optional text/label layer read directly off ``poly``
    (mirroring ``well_label``'s "label a pin on the drawn layer itself"
    pattern) so a gate/poly node can be named without a metal landing pad on
    it -- a device gate that ``klt gen`` draws as bare poly (no contact/metal,
    see ``gen.py``'s ``_mos_unit_layout``) still has no ``metals[]`` shape for
    a ``metal_labels[]`` text to attach to, so a poly-level label layer is the
    only way ``klt gen-compose``'s ``pins[]`` can promote such a port to a
    named ``.SUBCKT`` pin (#210). ``None`` where a family's curated deck
    declares no poly-label convention.

    ``nfet_class``/``pfet_class`` name the extracted ``DeviceClassMOS4Transistor``
    device classes (``devices[].class`` in the JSON response). ``substrate_net``
    is the global net name (KLayout ``connect_global``) the NMOS body
    terminal falls back to when a deck draws a distinct ``tap`` layer but no
    tap shape ends up outside every ``nwell`` in a given layout, or when
    ``tap`` is ``None`` entirely (e.g. gf180mcu's shared ``Comp`` layer, no
    per-purpose split possible). When a deck *does* declare ``tap`` and a
    layout draws a substrate-tie ring on it (outside every ``nwell``,
    contacted up to a named net -- issue #490), ``extract.py`` wires that
    real geometry to the NMOS body terminal instead, so the body terminal
    resolves to the ring's own real net rather than this synthesized global
    -- see the family deck's docstring for the tap/nwell-containment split
    that makes this possible.

    ``substrate_isolation`` (issue #1128) is an optional isolation/deep-well
    layer (e.g. gf180mcu's ``DNWELL``) that, when declared, splits the NMOS
    body/``substrate_net`` identity above into more than one synthesized
    net. An NMOS device's recognised active-diffusion island, or a
    substrate-tie tap's slice of geometry (drawn or derived -- ``tap``/
    ``tap_nplus``/``tap_pplus`` above), that overlaps a connected component
    of this layer resolves to a *per-island* identity (named
    ``f"{substrate_net}_iso{n}"``, with ``n`` assigned in deterministic
    bounding-box order, not raw ``Region`` iteration order -- see
    :func:`~klayout_tools.extract._partition_region_by_islands`) instead of
    the single, deck-wide ``substrate_net`` global -- letting two physically
    separate, isolated NMOS domains (e.g. deep-nwell/isolated-p-well tubs)
    extract as two genuinely distinct nets rather than always collapsing
    onto one. Geometry that does not overlap any component of this layer at
    all keeps today's single shared ``substrate_net`` identity, matching
    real silicon (an un-isolated p-substrate genuinely is one continuous
    node) -- this is a per-*isolated-region* split, not a full per-instance
    one. ``None`` (the default -- every deck as of this field's
    introduction, until gf180mcu's own module sets it) disables this
    scoping entirely: a deck that declares no ``substrate_isolation``
    extracts exactly as it did before this field existed, with every NMOS
    body/substrate-tie sharing one identity regardless of any well/deep-well
    geometry drawn in the layout. Applies only to this deck's *default*
    (non-``mos_flavours``) NMOS device recognition and to the ``tap``/
    derived-tap substrate-tie slice -- a flavoured NMOS device
    (``mos_flavours``), a ``bulk_to_substrate`` resistor's ``W`` terminal, or
    a collector-less :class:`BipolarDevice`'s collector, whose own geometry
    happens to sit inside an isolated region, still resolves to the single
    global ``substrate_net`` identity: a known, documented residual gap left
    for a follow-up rather than this issue's scope.

    ``bipolars`` is an optional tuple of :class:`BipolarDevice` entries (empty
    by default) declaring this deck's drawn vertical-BJT device-recognition
    layers -- the resistor/bipolar/capacitor extension point #221's own
    docstring anticipates (see :attr:`device_classes` below). Empty for a
    deck with no curated bipolar recognition; non-empty decks may declare
    more than one entry (e.g. distinct NPN and PNP device families).

    ``capacitors`` is an optional tuple of :class:`CapacitorDevice` entries
    (empty by default) declaring this deck's drawn MiM (Metal-Insulator-Metal)
    capacitor device-recognition layers (issue #225), the capacitor sibling of
    ``bipolars`` above. Empty for a deck with no curated capacitor
    recognition; non-empty decks may declare more than one entry (e.g.
    sky130's two independent MiM stacks, one per metal level it is drawn on).

    ``mom_capacitors`` is an optional tuple of :class:`MomCapacitorDevice`
    entries (empty by default) declaring this deck's drawn MoM (Metal-oxide-
    Metal) capacitor device-recognition layers (issue #1466) -- a structural
    sibling of ``capacitors`` for a device family (single marker + per-metal
    multi-port, topologically matched, no computed capacitance value)
    :class:`CapacitorDevice` cannot represent; see that class's own
    docstring for the full rationale. Empty for a deck with no curated MoM
    capacitor recognition; non-empty decks may declare more than one entry
    (e.g. sg13g2's ``cap_cmomi``/``cap_cmomf`` pair, told apart only by
    their own distinct marker layers).

    ``resistors`` declares the deck's *drawn* precision-resistor device
    classes (see :class:`ResistorDevice`), each recognised by KLayout's
    ``DeviceExtractorResistor``/``...WithBulk``. Optional and empty by
    default: a deck that declares none extracts exactly as it did before the
    field existed (#222), and a conductor that carries no declared resistor
    marker is never reclassified as a resistor.

    ``diodes`` is an optional tuple of :class:`DiodeDevice` entries (empty by
    default) declaring this deck's drawn junction-diode device-recognition
    layers (issue #542), the diode sibling of ``bipolars`` above -- what lets
    a discrete PN/ESD-clamp diode extract as a ``D`` element instead of
    vanishing from the netlist as unrecognised diffusion geometry. Empty for
    a deck with no curated diode recognition; non-empty decks may declare
    more than one entry (e.g. gf180mcu's n+/p-substrate and p+/Nwell
    junction flavours).

    ``nfet_provenance``/``pfet_provenance`` (issue #868) are machine-readable
    citations of the exact upstream PDK-LVS-deck source line each MOS
    recognition rule was transcribed from -- the device-recognition analogue
    of :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring), applied to the two MOS device classes this deck *always*
    recognises via its own ``active``/``poly``/``nwell`` fields above. A deck
    still declares exactly one *default* NMOS and one *default* PMOS
    recognition rule this way -- ``mos_flavours`` below is the (optional,
    additive) per-entry list for every *additional*, marker-scoped MOS
    voltage flavour a deck also recognises, rather than a change to this
    default pair. Both default to ``None`` -- no structured provenance
    backfilled yet, the same "prose comment remains the record" default every
    other ``provenance`` field in this module carries.

    ``mos_flavours`` (issue #1111, option 2 of #552) is an optional tuple of
    :class:`MOSFlavour` entries (empty by default) declaring additional,
    marker-scoped MOS voltage/gate-oxide flavours this deck recognises beyond
    the default ``active``/``nwell`` split above -- see :class:`MOSFlavour`'s
    own docstring for the full derivation and why it does not introduce a new
    ``devices[].class`` label. Empty for a deck with no such flavour (every
    deck as of this field's introduction, until gf180mcu's own module sets
    it) extracts exactly as it did before the field existed.

    ``poly_interconnect`` (issue #1425) is an optional marker layer declaring
    that a ``poly``-layer shape it covers is *intentional* interconnect (most
    commonly a poly underpass -- a poly strip contacted to metal at each end,
    used to route one net beneath another on a deck with too few metal levels
    to stay planar) rather than an unrecognised device body. Without it, such
    a shape matches ``extract.py``'s ``_detect_unmodelled_poly_bodies``
    resistor-body signature (issue #288: not part of a recognised MOS gate,
    touches ``contact`` at 2+ separate points, carries no resistor marker)
    and is reported in ``unmodelled_poly[]``/``warnings[]`` as though its
    terminals were an unintended short -- a false positive for correct,
    deliberate geometry that also crowds out genuine unmodelled-device
    findings in the same report. A caller who draws this marker over such a
    strip (any ``(layer, datatype)`` the deck's PDK does not otherwise use --
    it never participates in ordinary connectivity, only in this exclusion
    check) keeps the shape out of ``unmodelled_poly[]`` entirely; it still
    extracts as ordinary poly interconnect exactly as it does today, this
    only silences the diagnostic. ``None`` (the default -- every deck as of
    this field's introduction) disables the exclusion: a poly shape matching
    the signature is flagged exactly as it was before this field existed, no
    behaviour change for a deck/layout that declares or draws no marker.
    """

    active: tuple[int, int]
    poly: tuple[int, int]
    nwell: tuple[int, int]
    contact: tuple[int, int]
    metals: tuple[tuple[int, int], ...]
    tap: tuple[int, int] | None = None
    tap_nplus: tuple[int, int] | None = None
    tap_pplus: tuple[int, int] | None = None
    well_label: tuple[int, int] | None = None
    poly_label: tuple[int, int] | None = None
    dummy: tuple[int, int] | None = None
    poly_interconnect: tuple[int, int] | None = None
    metal_labels: tuple[tuple[int, int] | None, ...] = ()
    vias: tuple[tuple[int, int], ...] = ()
    nfet_class: str = "nfet"
    pfet_class: str = "pfet"
    substrate_net: str = "vsubs"
    substrate_isolation: tuple[int, int] | None = None
    bipolars: tuple[BipolarDevice, ...] = ()
    capacitors: tuple[CapacitorDevice, ...] = ()
    mom_capacitors: tuple[MomCapacitorDevice, ...] = ()
    resistors: tuple[ResistorDevice, ...] = ()
    diodes: tuple[DiodeDevice, ...] = ()
    mos_flavours: tuple[MOSFlavour, ...] = ()
    nfet_provenance: RuleProvenance | None = None
    pfet_provenance: RuleProvenance | None = None

    @property
    def device_classes(self) -> tuple[str, ...]:
        """The device-class *roles* (not the ``devices[].class`` label
        strings ``nfet_class``/``pfet_class``/``BipolarDevice.class_name``/
        ``CapacitorDevice.name`` provide) this deck is structurally capable
        of recognising -- independent of whether a given layout actually
        contains any devices of that class (see ``device_counts`` in
        ``docs/cli/extract.md`` for the "what was found" counterpart of this
        "what can be found" declaration).

        Every registered deck extracts two-terminal-well MOS (``"nfet"``,
        ``"pfet"``); a deck that also declares one or more ``bipolars``
        entries (#223) appends each entry's ``class_name`` (in declaration
        order, deduplicated), and a deck that declares one or more
        ``capacitors`` entries (#225) likewise appends each entry's ``name``
        after that. Finally, a deck that declares one or more ``resistors``
        entries (#222) appends the ``"resistor"`` role after those -- a single
        role token regardless of how many drawn-resistor device classes the
        deck declares. Last, a deck that declares one or more ``diodes``
        entries (#542) appends each entry's ``name`` (in declaration order,
        deduplicated) after that -- one token per declared junction-diode
        flavour, matching how ``bipolars``/``capacitors`` name theirs. A deck
        that declares one or more ``mom_capacitors`` entries (#1466) appends
        each entry's ``name`` right after ``capacitors``' own -- the two
        capacitor families are structurally distinct (see
        :class:`MomCapacitorDevice`'s docstring) but share the same "device-
        class role" position in this list.
        """
        classes = ["nfet", "pfet"]
        for bipolar in self.bipolars:
            if bipolar.class_name not in classes:
                classes.append(bipolar.class_name)
        for capacitor in self.capacitors:
            if capacitor.name not in classes:
                classes.append(capacitor.name)
        for mom_capacitor in self.mom_capacitors:
            if mom_capacitor.name not in classes:
                classes.append(mom_capacitor.name)
        if self.resistors and "resistor" not in classes:
            classes.append("resistor")
        for diode in self.diodes:
            if diode.name not in classes:
                classes.append(diode.name)
        return tuple(classes)

    @property
    def resolved_option_values(self) -> dict[str, str]:
        """Every caller-selectable deck-option key this deck declares
        (``ResistorDevice.flavour_option``/``CapacitorDevice.flavour_option``)
        mapped to the flavour ``value`` **currently wired into this deck
        object** -- sorted by key (issue #2394).

        Read off the deck itself rather than off the caller's
        ``deck_options`` mapping, so it answers the same question either way
        round: on the *registered* deck (nothing overridden) each key maps to
        the flavour the deck silently defaults to, and on a deck already
        resolved by :func:`~klayout_tools.decks.get_extraction_deck` each key
        maps to the flavour that call actually selected. That is what makes a
        provenance echo of this mapping honest for the defaulted case --
        ``provenance.deck.options`` previously recorded only keys the caller
        passed, so a run that silently took gf180mcu's ``poly_res='1k'``
        default was indistinguishable in its own record from a deck with no
        selectable options at all (issue #2394's "a wrong default reads as
        fact").

        The wired value is recovered by matching each entry's own ``name``
        against its declared ``flavours`` -- both families' flavour objects
        carry the ``name`` the owning entry is rewritten to when that flavour
        is selected (:class:`ResistorFlavour`/:class:`CapacitorFlavour`), and
        :func:`~klayout_tools.decks.get_extraction_deck` performs exactly
        that rewrite, so the match is exact in both directions. An entry that
        declares a ``flavour_option`` but no matching ``flavours`` entry
        contributes nothing: there is no value to report, and a fabricated
        one would be worse than an absent key.

        Empty for every deck that declares no ``flavour_option`` at all (e.g.
        sky130) -- the caller is expected to omit the field entirely in that
        case rather than record an empty mapping, keeping "this deck has no
        selectable options" distinguishable from "it has some".
        """
        values: dict[str, str] = {}
        for device in (*self.resistors, *self.capacitors):
            key = device.flavour_option
            if key is None or key in values:
                continue
            wired = next((f for f in device.flavours if f.name == device.name), None)
            if wired is not None:
                values[key] = wired.value
        return dict(sorted(values.items()))

    @property
    def merge_layers(self) -> frozenset[tuple[int, int]]:
        """The ``(layer, datatype)`` pairs whose shapes actually *merge* two
        nets together -- exactly ``metals`` plus ``vias``, this deck's
        net-connectivity stack (issue #619).

        This is a strict subset of :attr:`connectivity_layers`: a layer can
        be *read* by extraction (e.g. a ``capacitors[].bottom_plate`` device-
        recognition role) without ever being one of these merge levels -- see
        :attr:`device_recognition_only_layers` for exactly that gap, the one
        that let sky130's own met3/met4 (read as MiM-cap bottom plates, but
        not yet a ``metals`` level) hide a routing-connectivity gap from
        ``ignored_layers`` before this deck's ``metals`` stack reached them
        too.
        """
        return frozenset(self.metals) | frozenset(self.vias)

    @property
    def device_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """Every ``(layer, datatype)`` this deck reads for
        ``bipolars``/``capacitors``/``mom_capacitors``/``resistors``/
        ``diodes`` device recognition -- base/emitter/marker/collector; plate
        + requires/excludes; marker + per-metal port layers; body/marker/
        requires/excludes plus optional terminal; anode/cathode/marker +
        requires/excludes -- regardless of whether the same layer is also one
        of this deck's ``metals``/``vias`` connectivity levels (see
        :attr:`merge_layers`). ``None`` entries (an absent optional layer)
        are skipped.

        A layer can legitimately serve both roles at once (sky130's met3/met4
        are simultaneously a ``metals`` connectivity level and a
        ``capacitors[].bottom_plate`` -- issue #619); this property reports
        the device-recognition role on its own, independent of connectivity
        status, so :attr:`device_recognition_only_layers` can subtract
        :attr:`merge_layers` from it to find the layers that are read but
        never merge a net.
        """
        layers: set[tuple[int, int]] = set()
        for bipolar in self.bipolars:
            layers.add(bipolar.base)
            layers.add(bipolar.emitter)
            layers.add(bipolar.marker)
            layers.update(bipolar.emitter_requires)
            layers.update(bipolar.emitter_excludes)
            if bipolar.collector is not None:
                layers.add(bipolar.collector)
        for capacitor in self.capacitors:
            layers.add(capacitor.top_plate)
            layers.add(capacitor.bottom_plate)
            layers.update(capacitor.top_plate_requires)
            layers.update(capacitor.top_plate_excludes)
            layers.update(capacitor.bottom_plate_requires)
            layers.update(capacitor.bottom_plate_excludes)
            if capacitor.top_plate_via is not None:
                layers.add(capacitor.top_plate_via)
        for mom_capacitor in self.mom_capacitors:
            layers.add(mom_capacitor.marker)
            layers.update(pin for pin in mom_capacitor.metal_pins if pin is not None)
        for resistor in self.resistors:
            layers.add(resistor.body)
            layers.add(resistor.marker)
            layers.update(resistor.requires)
            layers.update(resistor.excludes)
            if resistor.terminal is not None:
                layers.add(resistor.terminal)
        for diode in self.diodes:
            layers.add(diode.marker)
            layers.update(diode.anode_requires)
            layers.update(diode.anode_excludes)
            layers.update(diode.cathode_requires)
            layers.update(diode.cathode_excludes)
            if diode.anode is not None:
                layers.add(diode.anode)
            if diode.cathode is not None:
                layers.add(diode.cathode)
        for mos_flavour in self.mos_flavours:
            layers.add(mos_flavour.marker)
        return frozenset(layers)

    @property
    def _structural_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """The MOS-core and label layers this deck reads for a role other
        than ``metals``/``vias`` connectivity or bipolar/capacitor/resistor/
        diode device recognition -- ``active``/``poly``/``nwell``/
        ``contact``, the optional ``tap``/``tap_nplus``/``tap_pplus``/
        ``well_label``/``poly_label``/``dummy``, and every ``metal_labels``
        entry. ``None`` optionals are skipped.

        Subtracted from :attr:`device_recognition_only_layers` (issue #619):
        a bipolar device can legitimately reuse the deck's own MOS-core
        layers as its recognition geometry (sky130's vertical PNP is built
        from the *same* ``nwell``/``diff`` layers as an ordinary MOS body/
        active region -- see ``sky130.py``'s ``bipolars`` comment), so
        without this exclusion, *any* routine PMOS/NMOS layout -- with no
        bipolar device drawn at all -- would trigger a spurious "device
        recognition only" report from the coincidental layer-number overlap
        alone, defeating this diagnostic's only-fire-on-a-real-gap intent.
        """
        layers: set[tuple[int, int]] = {
            self.active,
            self.poly,
            self.nwell,
            self.contact,
        }
        for optional in (
            self.tap,
            self.tap_nplus,
            self.tap_pplus,
            self.well_label,
            self.poly_label,
            self.dummy,
        ):
            if optional is not None:
                layers.add(optional)
        layers.update(label for label in self.metal_labels if label is not None)
        return frozenset(layers)

    @property
    def device_recognition_only_layers(self) -> frozenset[tuple[int, int]]:
        """:attr:`device_recognition_layers` minus :attr:`merge_layers` and
        minus :attr:`_structural_recognition_layers` -- layers this deck
        reads *only* for a bipolar/capacitor/resistor/diode device-
        recognition role that plays no other connectivity or MOS-core role
        (issue #619).

        Consumed by ``klt extract`` to compute the response's
        ``device_recognition_only_layers`` field, the "read but not merged"
        counterpart to ``ignored_layers``'s "never read at all". Without this
        distinction, a layer that is read for device recognition looks
        identical to a genuine connectivity level in
        :attr:`connectivity_layers` -- ``ignored_layers`` alone cannot tell
        "this layer is fully ignored" from "this layer is read, but shapes on
        it never merge two nets" -- exactly the gap that let sky130's met3/
        met4 (drawn as MiM-cap bottom plates) hide a routing-connectivity gap
        from ``ignored_layers`` with a clean-looking (but wrong) empty
        report, before this deck's ``metals`` stack grew to cover them too.

        The :attr:`_structural_recognition_layers` subtraction keeps this
        low-noise: a bipolar device's ``base``/``emitter`` can coincide with
        the deck's own MOS ``nwell``/``active`` layers (sky130's vertical
        PNP does exactly this), and those layers were never candidates for a
        routing-connectivity gap in the first place -- they are structural
        MOS-recognition geometry, not metal.
        """
        return (
            self.device_recognition_layers
            - self.merge_layers
            - self._structural_recognition_layers
        )

    @property
    def connectivity_layers(self) -> frozenset[tuple[int, int]]:
        """Every ``(layer, datatype)`` this deck actually reads during
        extraction -- the device-recognition, connectivity, and label layers
        ``extract.py``'s ``_extract_netlist`` loads a ``Region``/``Texts`` for.

        Consumed by ``klt extract`` to compute the response's
        ``ignored_layers`` field (issue #220): shapes drawn on a layer *not*
        in this set are invisible to the connectivity graph, so a block routed
        on such a layer silently extracts as disconnected nets. Reporting the
        set difference against what the stream actually carries turns that
        silent mis-extraction into a diagnostic (the extraction-side analogue
        of ``klt drc``'s ``coverage.layers_in_stream_without_rules``).

        Includes the MOS-recognition layers (``active``/``poly``/``nwell``/
        ``contact``, plus optional ``tap``/``tap_nplus``/``tap_pplus``), the
        ``metals``/``vias`` stack
        (:attr:`merge_layers`) and every label layer (``well_label``/
        ``poly_label``/``metal_labels``), and each ``bipolars``/
        ``capacitors``/``resistors``/``diodes`` entry's own recognition
        layers (:attr:`device_recognition_layers`). ``None`` entries (an
        absent optional layer) are skipped.

        Note this does *not* distinguish a ``metals``/``vias`` connectivity
        level from a layer read for device recognition only -- see
        :attr:`device_recognition_only_layers` for that distinction (issue
        #619).
        """
        layers: set[tuple[int, int]] = {
            self.active,
            self.poly,
            self.nwell,
            self.contact,
        }
        for optional in (
            self.tap,
            self.tap_nplus,
            self.tap_pplus,
            self.well_label,
            self.poly_label,
            self.dummy,
        ):
            if optional is not None:
                layers.add(optional)
        layers.update(self.merge_layers)
        layers.update(label for label in self.metal_labels if label is not None)
        layers.update(self.device_recognition_layers)
        return frozenset(layers)


@dataclass(frozen=True)
class BipolarDevice:
    """One drawn vertical-BJT device-recognition entry for an
    :class:`ExtractionDeck`'s optional ``bipolars`` field (issue #223),
    consumed by ``extract.py``'s ``kdb.DeviceExtractorBJT3Transistor``
    wiring -- the bipolar analogue of :class:`ExtractionDeck`'s own
    ``active``/``poly``/``nwell`` MOS-recognition layers.

    ``base``/``emitter`` reuse the *same* curated layers the deck already
    declares for MOS recognition (typically ``nwell`` and ``active``
    respectively -- a vertical PNP/NPN's base/emitter are drawn on the same
    physical well/diffusion masks an ordinary MOS transistor uses, just in a
    different geometric arrangement) rather than introducing dedicated
    bipolar-only masks this curated deck does not otherwise model.

    ``marker`` is the PDK's dedicated bipolar device-recognition mark layer
    (drawn by the bipolar device library cell over itself; not consumed for
    connectivity otherwise, e.g. sky130's ``pnp.drawing`` 82/44 or
    gf180mcu's ``DRC_BJT`` 127/5). It disambiguates "this specific patch of
    well/diffusion is a real bipolar device" from the many unrelated
    nwell/diffusion regions a layout draws for ordinary PMOS/tap purposes --
    ``extract.py`` intersects ``base`` with ``marker`` before extraction, so
    only nwell area actually inside a marked device cell becomes a base
    region, and only diffusion inside *that* scoped base region becomes an
    emitter; an ordinary PMOS-only nwell drawn elsewhere in the layout is
    never misrecognised as a bipolar base.

    ``collector`` is ``None`` when the PDK's vertical bipolar has no drawn
    collector layer of its own (collector formed by the substrate -- true
    for both curated decks this issue populates). KLayout's
    ``DeviceExtractorBJT3Transistor`` handles that case itself: an empty
    ``C`` input makes it output the base region's own footprint onto the
    collector terminal, which ``extract.py``'s wiring then ties to the
    deck's ``substrate_net`` global -- the same ``connect_global`` pattern
    :class:`ExtractionDeck`'s NMOS body/``substrate_net`` wiring already
    uses. When a future deck's bipolar has a genuinely distinct drawn
    collector layer (e.g. a lateral device), set this instead.

    ``emitter_requires``/``emitter_excludes`` narrow the recognised emitter
    region the same requires/excludes idiom :class:`ResistorDevice` (#222)
    and :class:`CapacitorDevice` (#225) already use: after ``extract.py``
    scopes the emitter to diffusion inside the marked base
    (``emitter & base & marker``), every layer in ``emitter_requires`` must
    *also* cover it (intersected in) and every layer in ``emitter_excludes``
    is subtracted. This exists to disambiguate a genuine emitter diffusion
    from a **base-contact ring** drawn on the *same* diffusion layer inside
    the *same* well and *same* device mark (issue #302): without narrowing,
    that ring is a second shape on the emitter layer inside the base, so
    ``DeviceExtractorBJT3Transistor`` recognises it as a second, artefact
    device sharing the one base net (its "emitter" is really the base tie).
    A deck that models the implant masks can positively identify the emitter
    (e.g. gf180mcu's p+ emitter has ``Pplus`` and the n+ base tie has
    ``Nplus``, so ``emitter_excludes=(Nplus,)`` drops the ring). A deck that
    models *no* implant layers (e.g. sky130's curated deck) has no such
    disambiguator for a ring drawn on the literal emitter layer -- a
    documented residual limitation, see that deck's docstring. Both default
    to ``()`` (no narrowing), so a deck that declares neither extracts
    exactly as it did before the fields existed.

    ``class_name`` names the extracted ``DeviceClassBJT3Transistor`` device
    class (``devices[].class`` in the JSON response, and one of the values
    :attr:`ExtractionDeck.device_classes` reports for a deck that declares
    this entry).

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from -- the device-recognition analogue of
    :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring). ``None`` (the default) means no structured provenance has
    been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    base: tuple[int, int]
    emitter: tuple[int, int]
    marker: tuple[int, int]
    collector: tuple[int, int] | None = None
    emitter_requires: tuple[tuple[int, int], ...] = ()
    emitter_excludes: tuple[tuple[int, int], ...] = ()
    class_name: str = "bjt"
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class CapacitorDevice:
    """One drawn MiM (Metal-Insulator-Metal) capacitor device-recognition
    entry for an :class:`ExtractionDeck`'s optional ``capacitors`` field
    (issue #225), consumed by ``extract.py``'s ``kdb.DeviceExtractorCapacitor``
    wiring -- the capacitor sibling of :class:`BipolarDevice`.

    A MiM cap is two conductor plates separated by a thin dielectric, drawn
    as *two independent layers* rather than one marked-up conductor the way
    :class:`BipolarDevice` reuses the deck's own MOS layers: ``top_plate`` is
    the PDK's purpose-drawn top-plate layer (e.g. gf180mcu's ``FuseTop``,
    sky130's ``capm``/``capm2`` "MiM cap plate" mark layers) and
    ``bottom_plate`` is the conductor the bottom plate is drawn on (e.g.
    gf180mcu's ``Metal4``, sky130's ``met3``/``met4``).

    Unlike :class:`ResistorDevice`-shaped fields elsewhere in this codebase
    (there is no such class yet -- see #222), ``bottom_plate`` does **not**
    need to be one of the owning deck's own ``metals``. When it *is* (e.g.
    gf180mcu's bottom plate is ``Metal4``, which the deck now tracks as part
    of its full Metal1-Metal5 stack since #220), ``extract.py`` ties the
    recognised bottom-plate region directly into that ``metals[]`` node
    (issue #314), so ordinary contact/via/metal routing to that metal reaches
    the capacitor's bottom terminal. When it is *not* (e.g. sky130's
    ``met3``/``met4`` bottom plates, which sit above this curated deck's
    ``metals`` stack -- ``li1``/``met1`` only), the bottom plate stays its
    own new, self-connected connectivity node, isolated from the rest of the
    deck's graph -- see "Known limitation" below.

    Geometry (all layer fields are ``(layer, datatype)`` pairs):

    - ``top_plate`` -- the purpose-drawn top-plate conductor. Narrowed by
      ``top_plate_requires`` (all of which must also cover it) and
      ``top_plate_excludes`` (subtracted), the same requires/excludes idiom
      :class:`ResistorDevice` uses (#222) -- e.g. gf180mcu's ``FuseTop``
      additionally requires both ``CAP_MK``/``MIM_L_MK`` and excludes
      ``efuse_mk``/``plfuse``, mirroring the PDK's own official derivation.
    - ``bottom_plate`` -- the conductor the bottom plate is drawn on, with
      its own optional ``bottom_plate_requires``/``bottom_plate_excludes``.
      When this layer matches one of the owning deck's own ``metals`` (by
      ``(layer, datatype)`` value), ``extract.py`` connects the recognised
      bottom-plate region into that ``metals[]`` connectivity node (#314) --
      see "Known limitation" below for when it does not match.
    - ``bottom_plate_oversize_um`` -- when nonzero, the bottom plate is not
      the raw (filtered) ``bottom_plate`` region but the PDK's derived
      "virtual bottom plate": the subset of that region already touching the
      *unsized* ``top_plate`` region, clipped to ``top_plate`` sized
      (oversized) by this many micrometres -- gf180mcu's own official
      derivation for its MiM stack (``FuseTop.sized(1.06um) &
      Metal4.interacting(FuseTop)``, the gf180mcu DRM's "10.4.2 MIM Option
      B" footnote 1). Zero (the default) means the bottom plate is simply
      the filtered ``bottom_plate`` region, unfiltered by any sizing step --
      sky130's own official derivation, where the bottom plate is "whatever
      conductor the purpose-built top-plate mark layer sits over," no
      virtual-plate derivation needed.
    - ``top_plate_via``/``top_plate_via_metal`` -- optional declaration of
      the via layer that lands directly on the top plate and the ``metals[]``
      layer it connects up to (#314). When both are set, ``extract.py`` reads
      the raw ``top_plate_via`` layer as its own region, connects it to the
      recognised top-plate region wherever the two geometrically touch, and
      connects it on to the ``metals[]`` entry matching
      ``top_plate_via_metal`` -- the top-plate analogue of ``bottom_plate``
      matching a tracked metal above. Both default to ``None`` (no top-plate
      connectivity beyond the plate's own self-merge); ``top_plate_via_metal``
      must be one of the deck's own ``metals`` when ``top_plate_via`` is set
      (``extract.py`` raises :class:`~klayout_tools.extract.ExtractError` for
      a deck that sets one without the other, or sets
      ``top_plate_via_metal`` to a layer the deck does not track -- a
      deck-authoring mistake, the same class of check
      :func:`~klayout_tools.extract._resolve_resistors` already performs for
      a resistor's ``body``/``terminal`` layers). Left unset for a PDK/deck
      combination where the top plate's real via either does not exist or
      lands on a metal this curated deck's own ``metals`` stack does not
      track (e.g. gf180mcu's MiM stack sets both -- see ``gf180mcu.py``;
      sky130's two MiM stacks likewise set both as of issue #775, once #619
      extended sky130's own ``metals``/``vias`` to the full li1-met5 stack
      their real ``via3``/``via4`` land on) -- documented, not silently
      claimed as fixed for a deck whose ``metals`` stack genuinely does not
      reach that far. When ``top_plate_via`` *is* one of the owning deck's own
      ``vias`` layers, ``extract.py`` also excludes the geometric overlap
      between the via and this capacitor's recognised ``bottom_plate``
      region from that ``vias[]`` layer before the deck's generic per-layer
      connectivity loop runs (#364) -- otherwise a via placed per the PDK's
      own minimum-overlap rule for it (which requires the bottom plate to
      enclose/overlap the via, not clear it) would be read by that generic
      loop as an ordinary via shorting the two plates together, even though
      the plate wiring above already connects the via to the top plate
      correctly.

    ``area_cap_f_um2`` is the device's capacitance per square micrometre of
    plate *overlap* area, in **Farads**. KLayout's
    ``kdb.DeviceExtractorCapacitor`` computes ``C = A * area_cap`` from the
    two plates' actual geometric overlap -- exactly the role
    ``sheet_rho_ohm_sq`` plays for a resistor device (#222). Every deck that
    sets it must cite the PDK/DRM source it came from inline.

    ``perim_cap_f_um`` (issue #512) is an optional second coefficient: the
    device's *fringe/sidewall* capacitance per micrometre of plate-overlap
    *perimeter*, in Farads, added on top of the area term so the reported
    ``c_f`` becomes ``area_cap_f_um2 * A + perim_cap_f_um * P`` -- ``A``/``P``
    are the same overlap area/perimeter KLayout's own
    ``DeviceClassCapacitor`` already computes and exposes as its ``A``/``P``
    device parameters (``extract.py``'s
    ``_apply_device_parameter_corrections`` reads both back and rewrites
    ``C`` in place after extraction, before the netlist reaches the SPICE
    writer or ``klt lvs``'s comparer -- issue #521; KLayout's
    ``DeviceExtractorCapacitor`` constructor itself takes only one
    coefficient, so this is *not* threaded into that call). Defaults to
    ``0.0``, which reproduces ``C = area_cap_f_um2 * A`` only -- today's
    behaviour, bit-for-bit -- for every deck that does not set it. A MiM
    capacitor's real model is two-term (area *and* perimeter/fringe), the
    same shape :class:`LayerRC` below already uses for
    ``--parasitics``'s substrate capacitance (``cap_area_ff_um2``/
    ``cap_perim_ff_um``); a deck that transcribes its PDK's own two-term MiM
    model card (e.g. gf180mcu's/sky130's own SPICE ``.model`` cards'
    ``c_cox``/``c_capsw`` or ``camimc``/``cpmimc`` coefficients) should set
    this rather than leave the perimeter term silently dropped.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and one of the values :attr:`ExtractionDeck.device_classes`
    reports for a deck that declares this entry).

    Known limitation: a plate whose declared layer does not resolve to one
    of the deck's tracked ``metals[]`` entries (``bottom_plate`` that is not
    one of ``metals``, or a ``top_plate`` with no ``top_plate_via`` declared)
    is still registered as its own new, self-connected connectivity node --
    multiple plate polygons that touch each other merge into one net (e.g. a
    shared bottom plate across several caps), but that plate's net does not
    extend into whatever real routing the deck's metal-stack connectivity
    would otherwise connect it to. The *device* itself (a capacitor of the
    correct value between two correctly-shaped plates) is still correctly
    recognised in every case; only an unwired plate's *net name/connectivity*
    carries this documented approximation -- the same "curated starter
    subset, not the full metal stack" scope guard the rest of this deck
    already carries (see ``docs/cli/extract.md`` -> "Coverage").

    ``flavour_option``/``flavours`` (issue #1151) declare an optional,
    caller-selectable **area/perimeter-coefficient flavour set** for a
    capacitor family whose members share *identical* recognition geometry --
    the same ``top_plate``/``bottom_plate``/requires/excludes region,
    disambiguated only by a build-time deck variable in the official PDK LVS
    deck this is transcribed from (e.g. gf180mcu's ``MIM_CAP``, which selects
    one of ``'1'``/``'1.5'``/``'2'`` -- 1.0/1.5/2.0 fF/um² dielectric
    densities -- for the *same* drawn ``FuseTop``-over-``Metal4`` region: the
    density options share byte-identical drawn mask geometry, so no
    ``requires``/``excludes`` split could ever tell them apart). This is the
    capacitor sibling of :class:`ResistorDevice`'s own
    ``flavour_option``/``flavours`` (see that docstring for the full
    rationale -- the two fields exist for the same reason, one per device
    family): the deck itself keeps recognising and wiring exactly one entry
    -- this entry's own ``name``/``area_cap_f_um2``/``perim_cap_f_um`` are
    simply *which* flavour that is -- and a caller who knows their design
    draws a different flavour of the same geometry selects it via
    :func:`get_extraction_deck`'s ``deck_options`` (``klt extract
    --deck-option <flavour_option>=<value>``).

    ``flavour_option`` is the deck-option key this entry's flavour is chosen
    by (``None``, the default, means this entry has no caller-selectable
    flavour -- today's behaviour, unaffected either way). ``flavours`` is the
    tuple of every :class:`CapacitorFlavour` selectable through that key,
    typically including one entry that matches this ``CapacitorDevice``'s
    own default ``name``/``area_cap_f_um2``/``perim_cap_f_um`` (the value
    used when no ``deck_options`` override is given) plus the
    previously-unmodelled siblings. :func:`get_extraction_deck` validates a
    given ``deck_options`` value against this list and raises
    :class:`InvalidDeckOptionError` for an unrecognised key or value rather
    than silently keeping the default or guessing -- the same
    "known-unmodelled short beats a silently wrong value" discipline
    ``excludes`` already applies, and the same validation
    :class:`ResistorDevice.flavours` already gets. A deck entry that leaves
    ``flavours`` at its empty default has no selectable flavour: passing
    ``deck_options`` naming a key no entry (resistor or capacitor) declares
    is itself an :class:`InvalidDeckOptionError`, not a silent no-op.

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's ``area_cap_f_um2`` (and, when
    set, ``perim_cap_f_um``) was transcribed from -- the device-recognition
    analogue of :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s
    own docstring). ``None`` (the default) means no structured provenance
    has been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    name: str
    top_plate: tuple[int, int]
    bottom_plate: tuple[int, int]
    area_cap_f_um2: float
    top_plate_requires: tuple[tuple[int, int], ...] = ()
    top_plate_excludes: tuple[tuple[int, int], ...] = ()
    bottom_plate_requires: tuple[tuple[int, int], ...] = ()
    bottom_plate_excludes: tuple[tuple[int, int], ...] = ()
    bottom_plate_oversize_um: float = 0.0
    top_plate_via: tuple[int, int] | None = None
    top_plate_via_metal: tuple[int, int] | None = None
    perim_cap_f_um: float = 0.0
    flavour_option: str | None = None
    flavours: tuple[CapacitorFlavour, ...] = ()
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class CapacitorFlavour:
    """One caller-selectable ``(value, name, area_cap_f_um2,
    perim_cap_f_um)`` choice for a :class:`CapacitorDevice` whose
    ``flavours`` field declares more than one area/perimeter-coefficient
    interpretation of the *same* recognised geometry (issue #1151) -- the
    capacitor sibling of :class:`ResistorFlavour` (issue #595).

    ``value`` is the string a caller passes via ``deck_options`` (e.g.
    gf180mcu's ``"cap_mim_1f0_m4m5_noshield"`` / ``"cap_mim_1f5_m4m5_noshield"``
    / ``"cap_mim_2f0_m4m5_noshield"``, matching the PDK's own official LVS
    device-class name for each density option so a record can cite it
    directly -- unlike :class:`ResistorFlavour`'s short ``"1k"``/``"2k"``/
    ``"3k"`` spelling, there is no shorter upstream token for a MiM density
    option to mirror; the LVS device-class name itself is the PDK's own
    per-flavour identifier). ``name``/``area_cap_f_um2``/``perim_cap_f_um``
    are the ``CapacitorDevice.name``/``area_cap_f_um2``/``perim_cap_f_um``
    the owning entry is rewritten to when this flavour is selected --
    everything else about the entry (``top_plate``, ``bottom_plate``, every
    requires/excludes field, ``bottom_plate_oversize_um``,
    ``top_plate_via``/``top_plate_via_metal``) is unchanged, since flavour
    selection never changes *which* geometry is recognised, only what device
    it is reported as and what capacitance it reports.
    """

    value: str
    name: str
    area_cap_f_um2: float
    perim_cap_f_um: float


@dataclass(frozen=True)
class MomCapacitorDevice:
    """One drawn MoM (Metal-oxide-Metal) capacitor device-recognition entry
    for an :class:`ExtractionDeck`'s optional ``mom_capacitors`` field (issue
    #1466), consumed by ``extract.py``'s own ``kdb.GenericDeviceExtractor``
    subclass -- a structurally distinct sibling of :class:`CapacitorDevice`
    for a device family that mechanism cannot represent.

    IHP's ``cap_cmomi`` (interdigitated) and ``cap_cmomf`` (metal fringe/
    finger) MoM capacitors -- the family this entry was introduced for --
    are recognised from a **single marker layer** covering the whole device
    footprint (upstream's ``Recog.mom``/``Recog.momf``), containing exactly
    two ``MkPin``-style per-metal port shapes, rather than from two
    independently-drawn plate layers the way a MiM stack is. The two ports
    can land on the *same* metal level (side by side) or on *adjacent*
    metal levels (stacked) -- there is no fixed "top plate over bottom
    plate" relationship to derive two ``kdb.Region`` inputs from the way
    :class:`CapacitorDevice`'s ``top_plate``/``bottom_plate`` split does.
    Terminals are told apart by *position* within the marker (sorted by
    ``(x-center, y-center, metal index)``, first/last mapped to the two
    terminal ids), not by which per-metal layer they are on, and the real
    device's compact model (``C_total = density[N]*active_area + Cfeed``,
    supplied by the SPICE/Verilog-A model) is not something the LVS
    extractor computes at all -- unlike a MiM cap's plate-overlap
    ``area_cap_f_um2``/``perim_cap_f_um`` coefficients, there is no
    capacitance formula to transcribe here. This entry therefore reports
    the device's drawn dimensions (``W``/``L``, read off the marker's
    own bounding box) and its finger stack's metal-index range
    (``MMIN``/``MMAX``, issue #2435) as matched parameters, the same
    "dimension-matched, not value-computed" shape a MOSFET already uses --
    see ``docs/json-contract.md``'s "MoM capacitor devices" note for the
    resulting ``devices[].params`` shape (no ``c_f``/``area_um2``/
    ``perimeter_um`` keys; ``w_um``/``l_um`` plus ``mmin``/``mmax``).

    ``marker`` is the PDK's dedicated MoM device-recognition mark layer
    (e.g. sg13g2's ``Recog.mom`` 99/39 for ``cap_cmomi``, ``Recog.momf``
    99/40 for ``cap_cmomf``) -- unlike :class:`CapacitorDevice`, there is no
    separate ``top_plate``/``bottom_plate`` pair, since the marker itself
    *is* the whole recognised footprint.

    ``metal_pins`` is a tuple of per-metal port layers, **index-aligned
    with the owning deck's own ``metals`` field** (``metal_pins[i]``
    corresponds to ``metals[i]``): the PDK's own per-metal ``MkPin``
    drawing layer (e.g. sg13g2's ``Metal1.pin``/.../``Metal5.pin``, 8/2
    through 67/2 -- distinct GDS numbers from ``metal_labels``' *text*
    layers, which label a net rather than mark a device port) restricted to
    shapes falling inside ``marker``. ``None`` at a given index means this
    device has no port on that metal level -- either a level above the
    device's own thin-metal stack ceiling (sg13g2's ``TopMetal1``/
    ``TopMetal2``, which upstream's own extraction call never wires a pin
    layer for) or a level the deck's own stack reaches by a different route
    (sg13cmos5l's ``TopMetal1``, sitting where sg13g2 has ``Metal5``: cmos5l
    forbids ``Metal5`` outright, so its instances can only ever populate
    ``m1p``..``m4p``). ``extract.py`` raises
    :class:`~klayout_tools.extract.ExtractError` for a deck whose entry's
    ``metal_pins`` length does not match ``len(metals)`` exactly -- a
    deck-authoring mistake, the same class of check
    :func:`~klayout_tools.extract._resolve_resistors` and the main
    capacitor loop already perform for their own layer fields. Each
    non-``None`` per-metal port region is wired into that ``metals[i]``
    connectivity node (mirroring upstream's own ``cap_cmomi_connections
    .lvs``/``cap_cmomf_connections.lvs``, which tie each per-metal pin
    *only* to its own metal's routed conductor -- never to every metal at
    once, which would bridge two ports the real device keeps electrically
    independent), so ordinary contact/via/metal routing to that metal
    reaches this device's matching terminal.

    ``metal_pins`` additionally decides **which metal levels the finger-stack
    measurement reads** (issue #2435). For each index it leaves non-``None``,
    ``extract.py`` hands the extractor that level's own drawn conductor
    geometry -- the owning deck's ``metals[i]`` layer, *not* a second
    declared field, since ``MomCapacitorDevice`` already index-aligns with
    it -- narrowed to the polygons lying entirely inside ``marker``, and
    ``MMIN``/``MMAX`` become the lowest/highest such level (unioned with the
    two recognised ports' own levels, which always lie inside the drawn
    range). A level left ``None`` is therefore never read as a finger level:
    that is what stops an ordinary route running across the capacitor on a
    level this device family cannot reach (cmos5l's ``TopMetal1``, sg13g2's
    ``TopMetal1``/``TopMetal2``) from being counted into the stack. No new
    field is needed on this dataclass for the feature.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and one of the values :attr:`ExtractionDeck.device_classes`
    reports for a deck that declares this entry).

    ``provenance`` (issue #1466, the ``MomCapacitorDevice`` sibling of
    :attr:`CapacitorDevice.provenance`) is a machine-readable citation of the
    exact upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from (see :class:`RuleProvenance`'s own docstring). ``None``
    (the default) means no structured provenance has been backfilled for
    this entry yet -- the prose citation in the deck module's own inline
    comment remains the only record, exactly as for every entry before this
    field existed.
    """

    name: str
    marker: tuple[int, int]
    metal_pins: tuple[tuple[int, int] | None, ...]
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class DiodeDevice:
    """One drawn junction-diode device-recognition entry for an
    :class:`ExtractionDeck`'s optional ``diodes`` field (issue #542),
    consumed by ``extract.py``'s ``kdb.DeviceExtractorDiode`` wiring -- the
    diode sibling of :class:`BipolarDevice`.

    A junction diode is an ordinary doped-diffusion/well overlap the PDK
    marks with a dedicated device-recognition layer, exactly like a vertical
    BJT: without this declaration that geometry is either dropped from the
    netlist entirely (nothing recognises it) or, worse, silently merged into
    surrounding interconnect -- so any diode-based ESD clamp is unverifiable
    by ``klt lvs``. This is the device-recognition analogue of
    :class:`ExtractionDeck`'s ``nfet_class``/``pfet_class`` MOS wiring,
    driving KLayout's native ``klayout.db.DeviceExtractorDiode``: the
    recognised device is the **geometric overlap** of the two terminal
    regions, and its extracted parameters are that overlap's area ``A``
    (square micrometres) and perimeter ``P`` (micrometres) -- reported as
    ``params.area_um2``/``params.perimeter_um`` in the JSON response.

    Deliberately *not* modelled (issue #542's own "Non-goals"): the device's
    saturation-current/area-scaling I-V model. A recognised diode is emitted
    as a schematic-equivalent ``D`` card whose model token is this entry's
    ``name``, the same fidelity level the MOS/BJT recognisers already
    provide -- a consumer simulating the netlist supplies a matching
    ``.model``.

    Geometry (all layer fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering):

    - ``anode`` -- the p-doped side's drawn layer (e.g. gf180mcu's ``Comp``
      for a p+ diffusion diode, or its ``Nwell`` when the *cathode* is the
      diffusion). ``extract.py`` scopes it to ``anode & marker`` before
      extraction.
    - ``cathode`` -- the n-doped side's drawn layer, scoped the same way.
    - ``marker`` -- the PDK's dedicated diode device-recognition mark layer
      (e.g. gf180mcu's ``diode_mk`` 115/5), drawn by the diode device
      library cell over itself. It is what disambiguates "this patch of
      diffusion inside a well is a real diode device" from the many
      unrelated diffusion/well overlaps an ordinary MOS layout draws --
      ``extract.py`` intersects *both* terminal layers with it before
      extraction, the same guard the bipolar block applies to its base, so
      an ordinary PMOS's p+-in-Nwell source/drain is never misrecognised as
      a diode.
    - ``anode_requires``/``anode_excludes`` and
      ``cathode_requires``/``cathode_excludes`` -- narrow the corresponding
      terminal region with the same requires/excludes idiom
      :class:`ResistorDevice` (#222), :class:`CapacitorDevice` (#225) and
      :class:`BipolarDevice` (#302) already use: every layer in ``requires``
      must *also* cover the region (intersected in) and every layer in
      ``excludes`` is subtracted. This is how a deck tells one junction
      flavour apart from another drawn on the same masks -- e.g. gf180mcu's
      n+/p-substrate ``diode_nd2ps_06v0`` requires ``Nplus`` + ``Dualgate``
      and excludes ``Nwell``, versus the p+/Nwell ``diode_pd2nw_06v0``'s
      ``Pplus`` + ``Dualgate``. All four default to ``()`` (no narrowing).

    Exactly one of ``anode``/``cathode`` may be ``None``, meaning "this
    terminal is formed by the substrate rather than by a drawn layer" -- the
    n+-in-p-substrate case (gf180mcu's ``diode_nd2ps_*``), whose anode is
    the p-substrate the PDK never draws a mask for. That terminal's region
    is then the device's own ``marker`` footprint (narrowed by its
    ``requires``/``excludes`` the same way a declared layer would be, e.g.
    ``anode_excludes=(Nwell, DNWELL)`` to keep the substrate side genuinely
    *outside* every well), and ``extract.py`` ties it to the deck's
    ``substrate_net`` global with ``connect_global`` -- exactly the pattern
    :class:`BipolarDevice`'s collector-less case and
    :class:`ExtractionDeck`'s NMOS body already use. Using the marker
    footprint rather than an empty region is load-bearing:
    ``DeviceExtractorDiode`` forms the device from the *overlap* of its two
    inputs, so an empty input yields no device at all.

    Joining the ``substrate_net`` global does **not** discard a real drawn
    net: ``connect_global`` unifies every region tied to that name into one
    node, so a layout that draws a substrate tie the deck's own tap
    mechanism claims (``tap``, or the derived ``tap_nplus``/``tap_pplus``
    pair -- issues #490/#1084) lands the tie and this terminal on one net,
    and KLayout names that net from the tie's own drawn label. A contacted,
    labelled tie inside the same device-mark footprint that the deck's tap
    mechanism does *not* claim is the one case where the two stay apart --
    ``extract.py`` reports that divergence in ``warnings[]`` rather than
    substituting the synthesized name silently (issue #1196).

    Terminal connectivity is derived from the declared layers rather than
    configured separately: a terminal whose layer is the owning deck's own
    ``nwell`` joins that well's connectivity node (so a well tap/label names
    it), a substrate-formed (``None``) terminal joins the deck's
    ``substrate_net`` global, and any other terminal layer (i.e. a
    diffusion) joins the deck's ``contact`` node, so ordinary
    contact/metal routing to the diffusion names it. This mirrors the
    bipolar block's fixed base->``nwell`` / emitter->``contact`` wiring.

    ``name`` is the extracted ``DeviceClassDiode`` device class
    (``devices[].class`` in the JSON response, the model token on the
    written ``D`` card, and one of the values
    :attr:`ExtractionDeck.device_classes` reports for a deck that declares
    this entry).

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from -- the device-recognition analogue of
    :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring). ``None`` (the default) means no structured provenance has
    been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    name: str
    marker: tuple[int, int]
    anode: tuple[int, int] | None = None
    cathode: tuple[int, int] | None = None
    anode_requires: tuple[tuple[int, int], ...] = ()
    anode_excludes: tuple[tuple[int, int], ...] = ()
    cathode_requires: tuple[tuple[int, int], ...] = ()
    cathode_excludes: tuple[tuple[int, int], ...] = ()
    provenance: RuleProvenance | None = None
