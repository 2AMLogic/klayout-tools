"""Registry of DRC rule decks.

A "deck" is our own declarative rule table (see :class:`DrcRule`) that drives
``klayout.db.Region``'s native check primitives (``width_check``,
``space_check``, ``separation_check``, ``enclosing_check``, etc.) — the same
C++ polygon-processing engine that backs KLayout's higher-level DRC-DSL
scripts, invoked directly instead of through the script runner. This keeps
``klt drc`` headless with zero new runtime dependency (see
``docs/cli/drc.md`` for the engine-choice rationale).

Deck data lives in per-PDK sibling modules (``sky130.py``, ``gf180mcu.py``);
this module only aggregates them into a name -> deck registry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrcRule:
    """One rule in a DRC deck.

    ``layer`` and ``other_layer`` are ``(layer, datatype)`` pairs. ``check``
    selects which ``klayout.db.Region`` check primitive to run:
    ``"width"`` / ``"space"`` / ``"notch"`` are single-layer checks;
    ``"separation"`` / ``"enclosing"`` / ``"enclosed"`` / ``"overlap"`` are
    two-layer checks and require ``other_layer``. ``threshold_dbu`` is the
    rule's distance threshold expressed in database units of the deck's own
    *nominal* dbu (see each deck module's ``NOMINAL_DBU_UM`` constant — e.g.
    sky130 and gf180mcu are both authored against ``dbu_um = 0.001``, i.e.
    1 nm per unit).

    ``threshold_dbu`` is **not** used directly against a layout's shapes:
    ``run_drc()`` scales it by the ratio of the deck's ``NOMINAL_DBU_UM`` to
    the layout's actual ``dbu`` before passing it to the ``Region.*_check()``
    primitives, so a deck's rules give identical results regardless of the
    database unit the input stream happens to be written at (see
    ``docs/cli/drc.md``).

    Rule ``id`` values are a stable, public contract once shipped — never
    renumber or repurpose one (see ``docs/cli/drc.md``).
    """

    id: str
    description: str
    layer: tuple[int, int]
    check: str
    threshold_dbu: int
    other_layer: tuple[int, int] | None = None


class UnknownDeckError(Exception):
    """Raised by :func:`get_deck` / :func:`get_layer_names` for an unknown deck name."""


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
    segment's own geometry, so **this number is the whole accuracy of the
    extracted resistance**. Every deck that sets it must cite the PDK/DRM
    source it came from inline, the same way the DRC decks cite rule ids.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and the model token on the written ``R`` card -- a
    consumer simulating the netlist supplies a matching ``.model``, exactly
    as it already must for the ``nfet``/``pfet`` ``M`` cards).

    ``bulk_to_substrate`` selects ``DeviceExtractorResistorWithBulk`` (a
    third ``W`` terminal tied to the deck's ``substrate_net`` global)
    instead of the plain two-terminal ``DeviceExtractorResistor``, for a
    device whose PDK LVS deck models a bulk terminal (e.g. gf180mcu's
    ``ppolyf_u``, extracted upstream with ``'W' => sub``). It inherits the
    same documented approximation as the NMOS body terminal: there is no
    drawn substrate-tap geometry in these curated decks, so the bulk is the
    global net, not a real extracted tap.
    """

    name: str
    body: tuple[int, int]
    marker: tuple[int, int]
    sheet_rho_ohm_sq: float
    requires: tuple[tuple[int, int], ...] = ()
    excludes: tuple[tuple[int, int], ...] = ()
    terminal: tuple[int, int] | None = None
    bulk_to_substrate: bool = False


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
    "dummy" devices (issue #295): matched-pair/array edge fill whose gate and
    diffusions are tied off to a rail so the device contributes nothing to the
    circuit, yet must be *drawn* on the real device layers next to the devices
    it protects. Any MOS gate lying under a shape on this layer is dropped
    before device recognition (``extract.py`` subtracts it from the
    NMOS/PMOS gate regions), so the dummy never appears in the extracted
    netlist and ``klt lvs`` no longer reports a spurious ``device.unmatched``
    for it -- while the dummy's diffusions still extract as ordinary
    interconnect (they tie to the rail as drawn). ``None`` (the default) is
    fully backward-compatible: a deck that declares no ``dummy`` layer
    extracts exactly as it did before the field existed. Whether a given PDK
    draws a native dummy-marker layer is left to the deck author to declare.

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
    is the global net name the NMOS body terminal is tied to when no drawn
    substrate-tap geometry exists to derive one from (KLayout
    ``connect_global``) -- see the family deck's docstring for why this is a
    documented approximation, not a real substrate-tap extraction.

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

    ``resistors`` declares the deck's *drawn* precision-resistor device
    classes (see :class:`ResistorDevice`), each recognised by KLayout's
    ``DeviceExtractorResistor``/``...WithBulk``. Optional and empty by
    default: a deck that declares none extracts exactly as it did before the
    field existed (#222), and a conductor that carries no declared resistor
    marker is never reclassified as a resistor.
    """

    active: tuple[int, int]
    poly: tuple[int, int]
    nwell: tuple[int, int]
    contact: tuple[int, int]
    metals: tuple[tuple[int, int], ...]
    tap: tuple[int, int] | None = None
    well_label: tuple[int, int] | None = None
    poly_label: tuple[int, int] | None = None
    dummy: tuple[int, int] | None = None
    metal_labels: tuple[tuple[int, int] | None, ...] = ()
    vias: tuple[tuple[int, int], ...] = ()
    nfet_class: str = "nfet"
    pfet_class: str = "pfet"
    substrate_net: str = "vsubs"
    bipolars: tuple[BipolarDevice, ...] = ()
    capacitors: tuple[CapacitorDevice, ...] = ()
    resistors: tuple[ResistorDevice, ...] = ()

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
        deck declares.
        """
        classes = ["nfet", "pfet"]
        for bipolar in self.bipolars:
            if bipolar.class_name not in classes:
                classes.append(bipolar.class_name)
        for capacitor in self.capacitors:
            if capacitor.name not in classes:
                classes.append(capacitor.name)
        if self.resistors and "resistor" not in classes:
            classes.append("resistor")
        return tuple(classes)

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
        ``contact``, plus optional ``tap``), the ``metals``/``vias`` stack and
        every label layer (``well_label``/``poly_label``/``metal_labels``),
        and each ``bipolars``/``capacitors``/``resistors`` entry's own
        recognition layers (base/emitter/marker/collector; plate +
        requires/excludes; body/marker/requires/excludes plus optional
        terminal). ``None`` entries (an absent optional layer) are skipped.
        """
        layers: set[tuple[int, int]] = {
            self.active,
            self.poly,
            self.nwell,
            self.contact,
        }
        for optional in (self.tap, self.well_label, self.poly_label, self.dummy):
            if optional is not None:
                layers.add(optional)
        layers.update(self.metals)
        layers.update(self.vias)
        layers.update(label for label in self.metal_labels if label is not None)
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
        for resistor in self.resistors:
            layers.add(resistor.body)
            layers.add(resistor.marker)
            layers.update(resistor.requires)
            layers.update(resistor.excludes)
            if resistor.terminal is not None:
                layers.add(resistor.terminal)
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
    """

    base: tuple[int, int]
    emitter: tuple[int, int]
    marker: tuple[int, int]
    collector: tuple[int, int] | None = None
    emitter_requires: tuple[tuple[int, int], ...] = ()
    emitter_excludes: tuple[tuple[int, int], ...] = ()
    class_name: str = "bjt"


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
    need to be one of the owning deck's own ``metals`` -- and even when it
    *is* (e.g. gf180mcu's bottom plate is ``Metal4``, which the deck now
    tracks as part of its full Metal1-Metal5 stack since #220), the two plate
    regions are still recognised as their own new, self-connected connectivity
    nodes, derived from a *separately* registered ``top_plate``/``bottom_plate``
    clip rather than the ``metals[]`` region itself, and so are not wired into
    the rest of the deck's contact/via/metal connectivity graph -- see "Known
    limitation" below.

    Geometry (all layer fields are ``(layer, datatype)`` pairs):

    - ``top_plate`` -- the purpose-drawn top-plate conductor. Narrowed by
      ``top_plate_requires`` (all of which must also cover it) and
      ``top_plate_excludes`` (subtracted), the same requires/excludes idiom
      :class:`ResistorDevice` uses (#222) -- e.g. gf180mcu's ``FuseTop``
      additionally requires both ``CAP_MK``/``MIM_L_MK`` and excludes
      ``efuse_mk``/``plfuse``, mirroring the PDK's own official derivation.
    - ``bottom_plate`` -- the conductor the bottom plate is drawn on, with
      its own optional ``bottom_plate_requires``/``bottom_plate_excludes``.
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

    ``area_cap_f_um2`` is the device's capacitance per square micrometre of
    plate *overlap* area, in **Farads**. KLayout's
    ``kdb.DeviceExtractorCapacitor`` computes ``C = A * area_cap`` from the
    two plates' actual geometric overlap, so **this number is the whole
    accuracy of the extracted capacitance** -- exactly the role
    ``sheet_rho_ohm_sq`` plays for a resistor device (#222). Every deck that
    sets it must cite the PDK/DRM source it came from inline.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and one of the values :attr:`ExtractionDeck.device_classes`
    reports for a deck that declares this entry).

    Known limitation: because the plate regions are registered as their own
    separate connectivity nodes (not the deck's ``metals[]`` layers, even
    where a plate happens to be drawn on a tracked metal), a recognised
    capacitor's two terminal nets are only as large as the plate shapes
    ``klt extract`` recognises as this device -- multiple plate polygons that
    touch each other merge into one net (e.g. a shared bottom plate across
    several caps), but neither plate's net extends into whatever real
    upper-metal routing the deck's metal-stack connectivity would otherwise
    connect it to. The *device* itself (a
    capacitor of the correct value between two correctly-shaped plates) is
    still correctly recognised; only the *net names/connectivity* of its two
    terminals is a documented approximation -- the same "curated starter
    subset, not the full metal stack" scope guard the rest of this deck
    already carries (see ``docs/cli/extract.md`` -> "Coverage").
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


@dataclass(frozen=True)
class LayerRC:
    """First-order lumped-RC parasitic coefficients for one conductor role.

    A curated, per-PDK numeric table for ``klt extract --parasitics`` -- the
    parasitics analogue of :class:`DrcRule`'s curated width/space table and
    the SPICE model-binding table (``klayout_tools.pdk_models``), sourced
    from each PDK's *public* process/DRM data (never NDA'd, matching this
    repo's open-PDK-only rule). Three coefficients per conductor role:

    - ``sheet_res_ohm_sq`` -- sheet resistance, ohms per square. Combined
      with a per-net square count (estimated from the net's per-layer area
      and perimeter, see ``klayout_tools.extract._n_squares``) to give one
      lumped series resistance per net.
    - ``cap_area_ff_um2`` -- parallel-plate capacitance to substrate,
      femtofarads per square micrometre of the net's area on this layer.
    - ``cap_perim_ff_um`` -- sidewall/fringe capacitance to substrate,
      femtofarads per micrometre of the net's perimeter on this layer.

    **These are representative, uncalibrated, order-of-magnitude starter
    values**, exactly like the DRC decks' "curated starter subset" scope
    (see ``docs/cli/drc.md`` -> "Coverage"). Parasitic-extraction accuracy
    tuning/calibration against silicon is an explicit non-goal of the first
    cut (issue #216's "Non-goals"); each family deck's ``PARASITICS``
    docstring cites the public source its numbers are drawn from.
    """

    sheet_res_ohm_sq: float
    cap_area_ff_um2: float
    cap_perim_ff_um: float


@dataclass(frozen=True)
class ParasiticsDeck:
    """Per-PDK-family first-order lumped-RC coefficient set for
    ``klt extract --parasitics`` (see ``docs/cli/extract.md`` -> "Parasitic
    (RC) extraction" and ``docs/design/lvs-extraction-spike.md`` -> "Addendum
    (#216)").

    One :class:`LayerRC` per conductor role the extraction pass tracks
    geometry for. ``metals`` is index-aligned with the matching
    :class:`ExtractionDeck`'s ``metals`` stack (so ``metals[0]``'s
    coefficients apply to whatever layer that deck's ``metals[0]`` is -- e.g.
    sky130's local-interconnect ``li1``, gf180mcu's ``Metal1``), which is why
    the coefficients are per-deck and per-metal-index rather than keyed by a
    universal role name. A ``None`` role (or a ``metals`` entry that is
    ``None``) contributes no parasitics for that role.
    """

    diffusion: LayerRC | None = None
    poly: LayerRC | None = None
    metals: tuple[LayerRC | None, ...] = ()


class UnknownExtractionDeckError(Exception):
    """Raised by :func:`get_extraction_deck` for an unknown deck name."""


def deck_source_path(name: str) -> str | None:
    """Absolute path to the Python module source that defines deck ``name``.

    Our decks are declarative Python tables in per-PDK sibling modules
    (``sky130.py``, ``gf180mcu.py``) rather than standalone rule-deck files,
    so the "deck file" whose content hash pins a run's rule set (see
    :func:`klayout_tools._provenance.build_provenance`) is that module's
    source. Deck names map 1:1 to those sibling module names (see the
    registries below). Returns ``None`` when the module can't be located.
    """
    from importlib import import_module

    try:
        module = import_module(f"{__package__}.{name}")
    except ImportError:
        return None
    return getattr(module, "__file__", None)


def _registry() -> dict[str, list[DrcRule]]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.DECK, "gf180mcu": gf180mcu.DECK}


def _layer_name_registry() -> dict[str, dict[tuple[int, int], str]]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.LAYER_NAMES, "gf180mcu": gf180mcu.LAYER_NAMES}


def get_deck(name: str) -> list[DrcRule]:
    """Return the rule list for a registered deck name.

    Raises :class:`UnknownDeckError` (which the caller in ``drc.py`` turns
    into a :class:`~klayout_tools.drc.DrcError`) if ``name`` is not a
    registered deck.
    """
    decks = _registry()
    try:
        return decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None


def get_layer_names(name: str) -> dict[tuple[int, int], str]:
    """Return the ``(layer, datatype) -> "name.purpose"`` map for a deck.

    Used only for human-readable JSON output; unrecognised decks return an
    empty map rather than raising (callers already validated the deck name
    via :func:`get_deck` before reaching this point).
    """
    return _layer_name_registry().get(name, {})


def _nominal_dbu_registry() -> dict[str, float]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.NOMINAL_DBU_UM, "gf180mcu": gf180mcu.NOMINAL_DBU_UM}


def get_nominal_dbu(name: str) -> float:
    """Return the database unit (in micrometres) that ``name``'s rule
    thresholds were authored against.

    Every ``DrcRule.threshold_dbu`` value in a deck is transcribed assuming
    this dbu; ``run_drc()`` uses it to rescale thresholds to the actual
    layout's ``dbu`` before running any ``Region.*_check()`` (see
    :class:`DrcRule`). Raises :class:`UnknownDeckError` for an unregistered
    deck name, mirroring :func:`get_deck`.
    """
    registry = _nominal_dbu_registry()
    try:
        return registry[name]
    except KeyError:
        available = ", ".join(sorted(registry))
        raise UnknownDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None


def _extraction_registry() -> dict[str, ExtractionDeck]:
    from . import gf180mcu, sky130

    return {
        "sky130": sky130.EXTRACTION_DECK,
        "gf180mcu": gf180mcu.EXTRACTION_DECK,
    }


def get_extraction_deck(name: str) -> ExtractionDeck:
    """Return the registered :class:`ExtractionDeck` for ``name``.

    Raises :class:`UnknownExtractionDeckError` (which ``extract.py`` turns
    into an :class:`~klayout_tools.extract.ExtractError`) if ``name`` is not
    a registered deck. Deliberately a *separate* registry/name lookup from
    :func:`get_deck` (the DRC deck registry) even though the deck names
    overlap (``"sky130"``/``"gf180mcu"``) -- DRC and extraction decks are
    different rule tables that happen to share PDK-family names, not the
    same object reused for two purposes.
    """
    decks = _extraction_registry()
    try:
        return decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownExtractionDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None


def _parasitics_registry() -> dict[str, ParasiticsDeck]:
    from . import gf180mcu, sky130

    return {
        "sky130": sky130.PARASITICS,
        "gf180mcu": gf180mcu.PARASITICS,
    }


def get_parasitics_deck(name: str) -> ParasiticsDeck:
    """Return the registered :class:`ParasiticsDeck` for ``name``.

    Raises :class:`UnknownExtractionDeckError` (which ``extract.py`` turns
    into an :class:`~klayout_tools.extract.ExtractError`) if ``name`` is not a
    registered deck -- same name lookup and error type as
    :func:`get_extraction_deck`, since every extraction deck also carries a
    parasitics table.
    """
    decks = _parasitics_registry()
    try:
        return decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownExtractionDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None
