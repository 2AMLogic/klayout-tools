"""Extraction device domain for the ``decks`` package: the device-recognition
dataclasses an :class:`ExtractionDeck` composes.

Split out of ``decks/__init__.py`` (issue #1821) as a self-contained
domain: :class:`ResistorDevice`/:class:`ResistorFlavour`,
:class:`MOSFlavour`, :class:`ExtractionDeck` itself,
:class:`BipolarDevice` (with its :class:`LayerExpr` derived-marker form) and
:class:`TerminalRelation`,
:class:`CapacitorDevice`/:class:`CapacitorFlavour`,
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

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import chain
from typing import Any

from .rules import RuleProvenance


class InvalidLabelLayerError(Exception):
    """Raised by :meth:`ExtractionDeck.with_label_layers` (and so by
    :func:`~klayout_tools.decks.get_extraction_deck`) for a label-layer
    override naming a role this deck has no label slot for, or carrying a
    malformed ``(layer, datatype)`` pair (issue #2656).

    ``extract.py`` turns this into an
    :class:`~klayout_tools.extract.ExtractError` (a clean exit-1 message, not
    a traceback), the same treatment it already gives
    :class:`~klayout_tools.decks.UnknownExtractionDeckError` and
    :class:`~klayout_tools.decks.InvalidDeckOptionError`.
    """


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

    ``end_term_ohm_um``/``width_offset_um`` (issue #2652) are two more
    optional coefficients covering a composite PDK resistor model
    ``fixed_offset_ohm`` cannot express: IHP's sg13g2 ``rsil``/``rppd``/
    ``rhigh`` poly resistors (``libs.tech/xschem/sg13g2_pr/<flavour>.sym``'s
    ``value=`` attribute) are each ``end_term / w + body_coeff * l / (w +-
    lwd)`` -- an end/contact term that, unlike ``res_high_po``'s constant
    ``rhead``, scales as **1/w** rather than being independent of width, plus
    a body term whose denominator is the drawn width adjusted by a fixed
    lithography bias (``lwd``), not the raw drawn width. Neither is
    expressible as a width-independent additive constant, so they are
    separate coefficients rather than folding into ``fixed_offset_ohm``:

    - ``end_term_ohm_um`` is an ohm-micrometre coefficient: ``R`` gains
      ``end_term_ohm_um / W`` (``W`` the already-computed device parameter,
      in micrometres like every other KLayout device geometry parameter --
      see ``CapacitorDevice.area_cap_f_um2``/``perim_cap_f_um``'s own units).
      A PDK model's own coefficient is typically quoted in ohm-*metres*
      against ``w`` in metres (e.g. sg13g2 rppd's ``70.0e-6 / w``); converting
      to ohm-micrometres against ``W`` in micrometres is a clean factor of
      1e6 (``70.0e-6`` ohm*m == ``70.0`` ohm*um), so deck entries transcribe
      the PDK coefficient directly without an awkward residual scale factor.
    - ``width_offset_um`` shifts the body term's denominator from ``W`` to
      ``W + width_offset_um`` (micrometres, so a PDK ``lwd`` quoted in metres
      is converted the same way): since the already-computed ``R = L / W *
      sheet_rho_ohm_sq`` is linear in ``L``, rescaling it by ``W / (W +
      width_offset_um)`` reproduces ``L / (W + width_offset_um) *
      sheet_rho_ohm_sq`` without needing to read ``L`` back separately. A
      negative value (sg13g2 rhigh's ``w - 0.04e-6``) narrows the effective
      width the same way a positive one (rppd's/rsil's ``w + lwd``) widens
      it.

    Both are applied in the same post-extraction correction (``extract.py``'s
    ``apply_resistor_fixed_offset_corrections``) and in the same order
    relative to series ``combine_devices()`` folding as ``fixed_offset_ohm``
    (applied once per surviving, possibly-combined device, using that
    device's own already-combined ``R``/``W`` -- correct because a uniform-
    width series string's combined ``R`` stays linear in the summed ``L``,
    and only the two true end contacts of the folded string get an end
    term, not one per drawn primitive). Both default to ``0.0``, the
    identity: ``end_term_ohm_um=0.0`` adds nothing, and
    ``width_offset_um=0.0`` leaves ``W / (W + 0.0) == 1.0``, so every deck
    entry that does not set them extracts ``R`` bit-for-bit as before this
    field existed.

    ``topology`` (issue #2679) selects how ``body``/``marker`` relate:

    - ``"overlap"`` (the default, every deck entry before this field
      existed) -- the marker lies *on* the body conductor and the segment is
      ``body & marker``, as described above.
    - ``"abutting_heads"`` -- the PDK draws the resistive body on the
      ``marker`` layer itself and the ``body`` layer is only the two contact
      heads, which *abut* (zero overlap) the marker. IHP sg13g2's
      ``rsil``/``rppd``/``rhigh`` PCells draw ``polyres`` (128/0) as the whole
      resistive body and ``GatPoly`` (5/0) only as the two head pads at either
      end, so ``body & marker`` is empty there. The segment is then
      ``marker & requires - excludes`` (mirroring upstream
      ``res_derivations.lvs``'s ``polyres & extblock ...``), kept only
      where it touches the ``terminal`` (default ``body``) conductor in **two
      or more** separate head regions -- a body with a missing head, or one
      separated from its heads by a gap, is not a resistor. The terminals
      are the touching heads (``terminal`` minus the segment), which
      KLayout's native extractor attaches directly to the abutting body
      edge; the segment is still cut out of the ``body`` conductor, so a
      layout that *does* overlap (a continuous bar marked by ``polyres``)
      keeps extracting as before.

    ``anode_marker`` (issue #4312) declares a **split-terminal** resistor whose
    two terminals are not the conductor on either side of the segment but two
    named regions of one conductor shape, told apart by a marker layer -- the
    gf180mcu eFuse, whose PDK LVS deck (``efuse_derivations.lvs``) draws one
    poly2 bar inside ``efuse_mk`` with ``plfuse`` as the link, ``lvs_source``
    over the anode end, and the rest as the cathode. ``requires`` is then the
    *scope*: only ``body`` shapes lying entirely inside every ``requires`` layer
    (``Region.inside``) take part. Within that scope the segment is
    ``scope & marker`` (less ``excludes``), the anode is ``scope &
    anode_marker`` (less the segment), and the cathode is ``scope`` less
    ``anode_marker`` and ``marker``. The whole scope is cut out of the
    ``body`` conductor's connectivity so anode and cathode, which abut one
    another around the link, stay distinct nets; each is then connected to
    ``contact`` on its own. A shape missing its anode, cathode or segment is
    not a device and is left as ordinary conductor (the marker is then
    reported as unclaimed). ``None`` (the default) is every other entry.

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
    end_term_ohm_um: float = 0.0
    width_offset_um: float = 0.0
    flavour_option: str | None = None
    flavours: tuple[ResistorFlavour, ...] = ()
    provenance: RuleProvenance | None = None
    topology: str = "overlap"
    anode_marker: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.topology not in RESISTOR_TOPOLOGIES:
            raise ValueError(
                f"ResistorDevice '{self.name}': unknown topology "
                f"{self.topology!r} (expected one of {RESISTOR_TOPOLOGIES})"
            )
        if self.anode_marker is not None and (
            self.topology != "overlap" or not self.requires
        ):
            raise ValueError(
                f"ResistorDevice '{self.name}': anode_marker needs "
                "topology='overlap' and a non-empty requires (the scope "
                "layer(s) the split conductor must lie inside)"
            )


#: Valid :attr:`ResistorDevice.topology` values.
RESISTOR_TOPOLOGIES: tuple[str, ...] = ("overlap", "abutting_heads")


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
    ``fixed_offset_ohm``, ``end_term_ohm_um``, ``width_offset_um``) is
    unchanged, since flavour selection never changes
    *which* geometry is recognised, only what device it is reported as.
    """

    value: str
    name: str
    sheet_rho_ohm_sq: float


@dataclass(frozen=True)
class UnsupportedDeviceMarker:
    """A marker layer whose drawn shapes mean the layout holds a device class
    this deck does not recognise (issue #2919) -- e.g. gf180mcu's
    ``MOS_CAP_MK`` (166/5) over a MOS capacitor.

    ``extract.py`` reports every such layer that carries shapes in the
    extracted cell in the response's ``unsupported_devices[]`` field and in
    ``warnings[]``, so a netlist that silently lacks those devices cannot read
    as a clean extraction. ``kind`` is a short machine-readable token (e.g.
    ``"mos_capacitor"``); ``description`` is the prose used in the warning.
    The marker takes no part in recognition or connectivity.
    """

    marker: tuple[int, int]
    kind: str
    description: str


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

    ``and_markers``/``not_markers``/``nfet_only`` (issue #3599) express a
    marker *combination*, for a flavour upstream derives as
    ``<base>.and(A).not(B)`` rather than from a single layer -- e.g. sky130's
    native-Vt NMOS (``hvi`` AND ``lvtn``, NOT ``areaid_ed``/``areaid_le``/
    ``vpp``). All three default to empty/``False``, in which case
    classification is exactly the single-``marker`` behaviour above:

    - ``and_markers``: the island must also ``interacting``-overlap *every*
      one of these layers to be claimed.
    - ``not_markers``: an island that ``interacting``-overlaps *any* of these
      layers is not claimed (it stays available to later flavours / the
      default split). Like ``marker``, this is island-level, not a geometric
      subtraction -- the same documented straddling policy.
    - ``nfet_only``: the flavour has no PMOS counterpart; an island
      ``interacting`` the deck's ``nwell`` is never claimed (no PMOS is
      extracted or bound for it).

    ``extract.py`` processes ``mos_flavours`` in declaration order and each
    flavour removes what it claims before the next runs, so a combination
    flavour must be declared *before* any flavour whose ``marker`` it
    refines (or that flavour must list the refining layer in ``not_markers``).

    ``drain_marker``/``pfet_only``/``body`` (issue #4570) express an
    *asymmetric* device -- gf180mcu's drain-extended LDMOS
    (``nfet_10v0_asym``/``pfet_10v0_asym``), whose source and drain are
    distinct terminals. All default to off, leaving every other flavour
    unchanged:

    - ``drain_marker``: a layer (``mvsd``/``mvpsd``) marking the drain side.
      The gate is cut back by it, the diffusion it covers becomes the drain
      terminal (``tD``) and the rest the source terminal (``tS``), so the
      extracted ``D``/``S`` terminals are stable rather than interchangeable.
    - ``pfet_only``: the claimed island is a PMOS regardless of ``nwell``
      (the LDPMOS sits in ``DNWELL`` with no ``Nwell`` over it); the mirror of
      ``nfet_only``.
    - ``body``: the layer used as the PMOS body ("W") terminal instead of
      ``nwell`` (``DNWELL`` for the LDPMOS); it is joined to ``nwell`` so a
      drawn ``Nwell`` tap ring ties it to its supply.
    """

    marker: tuple[int, int]
    flavour: str
    description: str = ""
    nfet_provenance: RuleProvenance | None = None
    pfet_provenance: RuleProvenance | None = None
    and_markers: tuple[tuple[int, int], ...] = ()
    not_markers: tuple[tuple[int, int], ...] = ()
    nfet_only: bool = False
    drain_marker: tuple[int, int] | None = None
    pfet_only: bool = False
    body: tuple[int, int] | None = None


@dataclass(frozen=True)
class MosCapacitorDevice:
    """One MOS-capacitor recognition entry for an :class:`ExtractionDeck`'s
    optional ``mos_capacitors`` field (issue #2919) -- gf180mcu's
    ``cap_nmos_*``/``cap_pmos_*`` family, drawn as an ordinary poly-over-COMP
    gate under a marker layer (``MOS_CAP_MK``, 166/5) rather than with a
    purpose-drawn plate layer the way :class:`CapacitorDevice` expects.

    Recognition mirrors the PDK's own LVS runset
    (``moscap_derivations.lvs``/``moscap_extraction.lvs``) at the granularity
    the MOS recogniser already uses for :class:`MOSFlavour`: an active
    (``COMP``) *island* that ``interacting``-overlaps ``marker`` and carries a
    poly gate is a capacitor, never a transistor -- the island is removed
    from the MOS source/drain/gate split before it runs, exactly as the PDK
    removes ``mos_cap_mk`` shapes from its MOS derivations. The two device
    terminals are the gate poly and the (merged) source/drain diffusion; the
    well/body is not a terminal. Capacitance parameters ``A``/``P`` are the
    gate region's area and perimeter, as the runset reports them.

    An island is claimed by the first entry (declaration order) for which
    every test below holds: it ``interacting``-overlaps ``marker`` and
    ``implant``; it overlaps *every* ``requires`` layer and *none* of the
    ``excludes`` layers; and ``in_well`` equals whether it overlaps the
    deck's ``nwell``. ``requires``/``excludes`` carry the voltage-domain
    (``Dualgate``) and deep-well (``DNWELL``) splits plus the PDK's
    ``moscap_exclude`` marker set; ``implant`` is the doping (``Nplus`` for an
    NMOS-style gate, ``Pplus`` for a PMOS-style one).

    ``name`` is the extracted device-class name and the PDK model name
    (e.g. ``cap_nmos_03v3_b``); ``area_cap_f_um2`` is the runset's
    ``capacitor(<name>, <area_cap>, MosCap)`` coefficient in F/um^2 -- the
    reported ``C`` is ``area_cap_f_um2 * A``.
    """

    name: str
    marker: tuple[int, int]
    implant: tuple[int, int]
    in_well: bool
    area_cap_f_um2: float
    requires: tuple[tuple[int, int], ...] = ()
    excludes: tuple[tuple[int, int], ...] = ()
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class CapacitorVariant:
    """One caller-selectable alternative **capacitor set** for an
    :class:`ExtractionDeck` (issue #2920) -- for a PDK whose capacitor family
    exists in several mutually-exclusive process options that draw
    *different geometry* (gf180mcu's MIM option A: ``Metal2`` bottom plate
    under ``Metal3``; option B: ``Metal(n-1)`` under the top metal), which
    :class:`CapacitorDevice`'s ``flavours`` (identical geometry, different
    coefficients) cannot express.

    ``option`` is the deck-option key (``--deck-option <option>=<value>``),
    ``value`` the string selecting this variant, ``capacitors`` the entries
    that replace the deck's ``capacitors`` when it is selected. The deck's
    own ``capacitors`` are the *default* variant, and must also be listed
    here (as the first variant) so that every variant's geometry is known.
    """

    option: str
    value: str
    capacitors: tuple[CapacitorDevice, ...]


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

    ``tap_nplus_complement`` (issue #2591) is a third, optional derivation
    input for the same ``tap is None`` case: a p+ implant layer whose
    *absence* marks n+ diffusion, for a PDK family whose own LVS deck treats
    n+ as the default doping and never requires a positive n-implant over a
    well tie (IHP SG13G2's ``general_derivations.lvs``: ``nactiv =
    activ.not(psd_drw.join(nsd_block))``, ``ntap = nactiv.and(nwell_drw)
    ...not(gatpoly)``). When set, ``extract.py`` additionally derives a well
    tie from ``(active & nwell) - tap_nplus_complement - poly``, unioned
    with the positive ``tap_nplus`` form above (so a layout drawn either way
    extracts the same tie). One deliberate, conservative restriction versus
    that upstream derivation: a candidate piece that *touches or overlaps a
    PMOS gate* (``active & nwell & poly``) is dropped, never promoted to a
    tie -- it is the source/drain of a PMOS drawn without its p+ implant,
    which this engine (unlike the foundry deck) still recognises as a PMOS,
    and reclassifying it as a tie would silently delete that device and
    short its terminals onto the well. A tie butted against a properly
    implanted PMOS source (the p+ region sits between it and the gate) is
    unaffected. ``None`` (the default) derives nothing from this field --
    every deck but sg13g2 as of its introduction, byte-for-byte the same
    behaviour as before it existed. Like ``tap_nplus``/``tap_pplus`` it is
    ignored outright when ``tap`` is set.

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

    Each of those three label fields is the deck's *curated default* for
    "which GDS purpose carries net/pin name text", and a caller whose stream
    wrote names on a different purpose of the same conductor can redirect it
    per run without a deck change: see :meth:`with_label_layers` and
    :attr:`label_layer_roles` (``klt extract --label-layer``, issue #2656).

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

    ``unsupported_device_markers`` (issue #2919) is an optional tuple of
    :class:`UnsupportedDeviceMarker` entries naming marker layers that flag a
    device class the deck cannot yet extract; shapes on one are reported in
    ``unsupported_devices[]``/``warnings[]``. Empty (the default) reports
    nothing. A marker component that an entry of ``mos_capacitors`` (below)
    recognised is *not* reported -- only the residue the deck could not
    classify stays loud.

    ``mos_capacitors`` (issue #2919) is an optional tuple of
    :class:`MosCapacitorDevice` entries recognising marker-scoped MOS
    capacitors (gf180mcu's ``cap_nmos_*``/``cap_pmos_*``). Empty (the
    default) recognises none, leaving MOS recognition untouched.
    """

    active: tuple[int, int]
    poly: tuple[int, int]
    nwell: tuple[int, int]
    contact: tuple[int, int]
    metals: tuple[tuple[int, int], ...]
    tap: tuple[int, int] | None = None
    tap_nplus: tuple[int, int] | None = None
    tap_pplus: tuple[int, int] | None = None
    tap_nplus_complement: tuple[int, int] | None = None
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
    unmodelled_families: tuple[UnmodelledDeviceFamily, ...] = ()
    mos_flavours: tuple[MOSFlavour, ...] = ()
    unsupported_device_markers: tuple[UnsupportedDeviceMarker, ...] = ()
    mos_capacitors: tuple[MosCapacitorDevice, ...] = ()
    nfet_provenance: RuleProvenance | None = None
    pfet_provenance: RuleProvenance | None = None
    capacitor_variants: tuple[CapacitorVariant, ...] = ()
    capacitor_variant_selected: str | None = None

    @property
    def unselected_capacitors(self) -> tuple[tuple[str, CapacitorDevice], ...]:
        """``(option value, entry)`` for every :attr:`capacitor_variants`
        capacitor that is *not* currently wired into :attr:`capacitors`
        (issue #2920) -- what ``extract.py`` checks to warn that a layout
        draws a capacitor option the active selection does not recognise.
        Empty for a deck that declares no variants.
        """
        if not self.capacitor_variants:
            return ()
        selected = self.capacitor_variant_selected or self.capacitor_variants[0].value
        return tuple(
            (variant.value, capacitor)
            for variant in self.capacitor_variants
            if variant.value != selected
            for capacitor in variant.capacitors
        )

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
        groups = (
            (bipolar.class_name for bipolar in self.bipolars),
            (capacitor.name for capacitor in self.capacitors),
            (mom_capacitor.name for mom_capacitor in self.mom_capacitors),
            (mos_capacitor.name for mos_capacitor in self.mos_capacitors),
            ("resistor",) if self.resistors else (),
            (diode.name for diode in self.diodes),
        )
        # `dict.fromkeys` keeps first-declared order and drops repeats.
        return tuple(dict.fromkeys(("nfet", "pfet", *chain.from_iterable(groups))))

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
        if self.capacitor_variant_selected is not None:
            # Issue #2920: the variant selector (`mim_option`) is echoed only
            # when a variant was actually selected, so a run that takes the
            # default keeps exactly the provenance/options hash it had before
            # the key existed.
            values[self.capacitor_variants[0].option] = self.capacitor_variant_selected
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
            layers.update(layer_operand_leaves(bipolar.marker))
            layers.update(bipolar.emitter_requires)
            layers.update(bipolar.emitter_excludes)
            # `filter(None, ...)` rather than two `is not None` branches so
            # this ratcheted function's complexity stays put (issue #2862
            # added the second optional layer).
            layers.update(filter(None, (bipolar.collector, bipolar.substrate)))
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
            # `filter(None, ...)` keeps this ratcheted function's complexity
            # put (issue #4312 added the second optional layer).
            layers.update(filter(None, (resistor.terminal, resistor.anode_marker)))
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
        for mos_capacitor in self.mos_capacitors:
            layers.add(mos_capacitor.marker)
            layers.add(mos_capacitor.implant)
            layers.update(mos_capacitor.requires)
            layers.update(mos_capacitor.excludes)
        for mos_flavour in self.mos_flavours:
            layers.add(mos_flavour.marker)
        return frozenset(layers)

    @property
    def _structural_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """The MOS-core and label layers this deck reads for a role other
        than ``metals``/``vias`` connectivity or bipolar/capacitor/resistor/
        diode device recognition -- ``active``/``poly``/``nwell``/
        ``contact``, the optional ``tap``/``tap_nplus``/``tap_pplus``/
        ``tap_nplus_complement``/``well_label``/``poly_label``/``dummy``,
        and every ``metal_labels`` entry. ``None`` optionals are skipped.

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
            self.tap_nplus_complement,
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
        ``contact``, plus optional ``tap``/``tap_nplus``/``tap_pplus``/
        ``tap_nplus_complement``), the
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
            self.tap_nplus_complement,
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

    @property
    def label_layer_roles(self) -> tuple[str, ...]:
        """The *role* token each of this deck's label slots can be addressed
        by from outside it (``klt extract --label-layer <role>=<L>/<D>``,
        issue #2656), in a stable order: ``"well"`` (:attr:`well_label`),
        ``"poly"`` (:attr:`poly_label`), then ``"metal0"`` ..
        ``"metal<len(metals) - 1>"`` (:attr:`metal_labels` ``[i]``, index-
        aligned with :attr:`metals` exactly as that field already is).

        Every metal level gets a role whether or not this deck declares a
        label layer for it today: a ``metal_labels`` entry that is ``None``
        (or absent entirely, when the tuple is shorter than ``metals``) is a
        slot the deck leaves empty, not a slot a caller may not fill -- see
        :meth:`with_label_layers`.
        """
        return ("well", "poly") + tuple(
            f"metal{index}" for index in range(len(self.metals))
        )

    def with_label_layers(
        self,
        overrides: Mapping[str, tuple[int, int] | None] | None,
        *,
        deck_name: str | None = None,
    ) -> ExtractionDeck:
        """This deck with each :attr:`label_layer_roles` entry named in
        ``overrides`` **replaced** by the given ``(layer, datatype)`` pair --
        or cleared, for a ``None`` value (issue #2656).

        The escape hatch for a layout whose net/pin name text sits on a
        different GDS *purpose* than the one this deck's curated default
        reads. A PDK layer map commonly declares more than one text purpose
        per conductor (IHP's shared ``.lyp`` declares both ``Metal2.pin``
        10/2 and ``Metal2.text`` 10/25, and ``sg13cmos5l.py`` picks ``.pin``
        where ``sg13g2.py`` picks ``.text`` for the same conceptual field), so
        a placed-and-routed stream from a flow that wrote names on the purpose
        the deck does not read names *nothing*: ``make_top_level_pins()``
        promotes only named nets, so the whole top-level pin interface
        silently comes out empty and ``klt lvs`` has no anchor to seed
        correspondence against a reference (issue #1385's warning fires, and
        issue #2656 is the reproduction that motivated this method).

        Replacement, not union: the overridden purpose is read *instead of*
        the deck's own, never in addition to it. That is deliberate -- the
        real case this exists for (a LibreLane/KLayout-style flow writing
        every top-level net name on ``.text`` while ``.pin`` carries only
        foundry-cell-*internal* pin text) wants the deck's default purpose
        dropped, since flattening those in-cell strings into top-level net
        names is exactly the mis-naming :func:`extract.py`'s below-top-label
        reconciliation exists to avoid. A deck that should read *both*
        purposes by default is a deck-data question, not a per-request one.

        ``overrides`` is keyed by :attr:`label_layer_roles` token;
        ``deck_name``, when given, is quoted in the error message. A role this
        deck has no slot for, or a value that is neither ``None`` nor a
        2-tuple of ints, raises :class:`InvalidLabelLayerError` rather than
        being silently ignored -- a typo'd role would otherwise read as "the
        override had no effect", indistinguishable from the zero-pin bug it
        was passed to fix. ``None``/an empty mapping (the default everywhere)
        returns ``self`` unchanged, so every call site that predates this
        method is byte-identical.

        A ``metal<i>`` override for a level this deck declares no
        ``metal_labels`` entry for extends the tuple with ``None`` padding up
        to that index -- the field is index-aligned with :attr:`metals`, and
        every consumer already tolerates a ``None`` slot (that is what "this
        level has no label layer in this curated deck" means).
        """
        if not overrides:
            return self
        where = f"deck '{deck_name}'" if deck_name else "this deck"
        roles = self.label_layer_roles
        unknown = [role for role in overrides if role not in roles]
        if unknown:
            raise InvalidLabelLayerError(
                f"{where} has no label-layer role(s) named "
                f"{', '.join(sorted(unknown))} (available: {', '.join(roles)})"
            )
        for role, layer in overrides.items():
            if layer is None:
                continue
            if (
                not isinstance(layer, tuple)
                or len(layer) != 2
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) for v in layer
                )
            ):
                raise InvalidLabelLayerError(
                    f"{where} label-layer role {role!r} override must be a "
                    f"(layer, datatype) pair of integers or None, got {layer!r}"
                )
        replacements: dict[str, Any] = {}
        if "well" in overrides:
            replacements["well_label"] = overrides["well"]
        if "poly" in overrides:
            replacements["poly_label"] = overrides["poly"]
        metal_overrides = {
            int(role.removeprefix("metal")): layer
            for role, layer in overrides.items()
            if role.startswith("metal")
        }
        if metal_overrides:
            width = max(len(self.metal_labels), max(metal_overrides) + 1)
            labels: list[tuple[int, int] | None] = list(self.metal_labels)
            labels.extend([None] * (width - len(labels)))
            for index, layer in metal_overrides.items():
                labels[index] = layer
            replacements["metal_labels"] = tuple(labels)
        return dataclasses.replace(self, **replacements)


#: The terminal names a :class:`TerminalRelation` may reference on a
#: :class:`BipolarDevice` -- also the order :meth:`BipolarDevice.terminal_
#: resolution_order` falls back to when nothing forces a different one.
BIPOLAR_TERMINALS: tuple[str, ...] = ("base", "emitter", "collector")

#: The geometric operations a :class:`TerminalRelation` may name (issue
#: #2863). Deliberately closed: each value is one KLayout ``Region`` method
#: ``extract.py`` applies verbatim, so an unknown op fails at deck
#: construction rather than silently selecting nothing.
TERMINAL_RELATION_OPS: tuple[str, ...] = ("interacting",)


@dataclass(frozen=True)
class TerminalRelation:
    """A per-terminal geometric relation to *another terminal of the same
    device* (issue #2863): "this terminal is the part of its candidate layer
    that is related by ``op`` to the already-resolved ``source`` terminal".

    A static ``requires``/``excludes`` layer set (what every device entry
    already supports) can only say "this layer, narrowed by those layers".
    It cannot say "the piece of this layer that touches *that instance's*
    emitter", which is how a PDK attributes a base ring to the one device it
    surrounds when several devices' rings sit side by side -- e.g. SG13G2's
    ``pnpMPA`` base, transcribed from its LVS deck as::

        pnp_mpa_b = pnp_b.interacting(pnp_b.extents.interacting(pnp_mpa_e))

    which is ``TerminalRelation(source="emitter", extents=True)`` on the
    base terminal.

    - ``source`` -- the terminal this one is selected against: one of
      :data:`BIPOLAR_TERMINALS` other than the terminal that declares the
      relation. It is resolved first (see
      :meth:`BipolarDevice.terminal_resolution_order`).
    - ``op`` -- the geometric selection; ``"interacting"`` (the only value
      today, see :data:`TERMINAL_RELATION_OPS`) keeps each whole polygon of
      the candidate region that touches or overlaps ``source``.
    - ``extents`` -- when ``False``, a candidate polygon is kept if *the
      polygon itself* interacts with ``source``
      (``candidate.interacting(source)``). When ``True``, it is kept if its
      own **bounding box** interacts with ``source``
      (``candidate.interacting(candidate.extents.interacting(source))``,
      the PDK idiom above) -- what selects a ring that *surrounds* its
      emitter without touching it.

    Pure data with no geometry of its own; ``extract.py`` applies it. Frozen
    and hashable, so two deck entries differing only in a relation compare
    unequal and project differently in ``klt deck devices``.
    """

    source: str
    op: str = "interacting"
    extents: bool = False

    def __post_init__(self) -> None:
        if self.source not in BIPOLAR_TERMINALS:
            raise ValueError(
                f"TerminalRelation: unknown source terminal {self.source!r} "
                f"(expected one of: {', '.join(BIPOLAR_TERMINALS)})"
            )
        if self.op not in TERMINAL_RELATION_OPS:
            raise ValueError(
                f"TerminalRelation: unknown op {self.op!r} "
                f"(expected one of: {', '.join(TERMINAL_RELATION_OPS)})"
            )


#: The two boolean operators a :class:`LayerExpr` node may apply (issue
#: #2860). ``"and"`` intersects every operand; ``"not"`` subtracts every
#: operand after the first from the first.
LAYER_EXPR_OPS = frozenset({"and", "not"})


@dataclass(frozen=True)
class LayerExpr:
    """A derived device-recognition layer: a boolean expression over drawn
    ``(layer, datatype)`` pairs and other :class:`LayerExpr` nodes (issue
    #2860).

    Some PDKs do not draw their device-recognition mark as one literal
    layer. IHP SG13G2's NPN marker is a three-operand boolean, one operand of
    which is itself derived (``bjt_derivations.lvs`` /
    ``general_derivations.lvs`` in the PDK's own KLayout LVS runset)::

        npn_mk = trans_drw.and(pwell).and(ptap_holes)
        pwell  = pwell_allowed.not(nwell_drw).not(digisub_gap)

    A ``LayerExpr`` tree expresses exactly that, as plain frozen data (so a
    deck stays hashable and comparable like every other deck dataclass)::

        pwell = LayerExpr.not_(PWELL_ALLOWED, NWELL, DIGISUB_GAP, name="pwell")
        npn_mk = LayerExpr.and_(TRANS, pwell, PTAP_HOLES, name="npn_mk")

    ``op`` is one of :data:`LAYER_EXPR_OPS`:

    - ``"and"`` -- the intersection of every operand.
    - ``"not"`` -- the first operand minus every later operand (``a - b - c``).

    A single-operand node of either kind is just that operand, so
    ``LayerExpr.and_((82, 44))`` recognises exactly what the bare ``(82, 44)``
    pair does. ``OR`` is deliberately not offered: no curated PDK marker needs
    it yet, and without it every leaf has one unambiguous polarity (see
    :meth:`required_layers` / :meth:`excluded_layers`).

    ``name`` is an optional human-readable label for the derived layer (the
    PDK runset's own variable name, e.g. ``"pwell"``/``"npn_mk"``). It has no
    effect on evaluation; ``klt deck devices`` reports it.

    This is distinct from :class:`~klayout_tools.decks.rules.DerivedLayer`,
    which is a fixed-mode, exactly-two-layer derivation for DRC rule scoping
    (sized intersection / whole-polygon selection) and cannot nest. Today
    only :attr:`BipolarDevice.marker` accepts a ``LayerExpr``; ``extract.py``
    evaluates it with :func:`klayout_tools._layout.region_expr`.
    """

    op: str
    operands: tuple[tuple[int, int] | LayerExpr, ...]
    name: str = ""

    def __post_init__(self) -> None:
        if self.op not in LAYER_EXPR_OPS:
            raise ValueError(
                f"LayerExpr op must be one of {sorted(LAYER_EXPR_OPS)}, got {self.op!r}"
            )
        if not self.operands:
            raise ValueError("LayerExpr needs at least one operand")
        for operand in self.operands:
            if isinstance(operand, LayerExpr):
                continue
            if (
                isinstance(operand, tuple)
                and len(operand) == 2
                and all(
                    isinstance(part, int) and not isinstance(part, bool)
                    for part in operand
                )
            ):
                continue
            raise ValueError(
                "LayerExpr operands must be (layer, datatype) int pairs or "
                f"LayerExpr nodes, got {operand!r}"
            )

    @classmethod
    def and_(cls, *operands: tuple[int, int] | LayerExpr, name: str = "") -> LayerExpr:
        """The intersection of ``operands``."""
        return cls("and", tuple(operands), name)

    @classmethod
    def not_(
        cls,
        base: tuple[int, int] | LayerExpr,
        *subtract: tuple[int, int] | LayerExpr,
        name: str = "",
    ) -> LayerExpr:
        """``base`` minus every layer in ``subtract``."""
        return cls("not", (base, *subtract), name)

    def _leaves(self, negated: bool) -> list[tuple[tuple[int, int], bool]]:
        """Every leaf in first-mention order, paired with whether it sits
        under an odd number of subtractions."""
        out: list[tuple[tuple[int, int], bool]] = []
        for index, operand in enumerate(self.operands):
            flipped = negated ^ (self.op == "not" and index > 0)
            if isinstance(operand, LayerExpr):
                out.extend(operand._leaves(flipped))
            else:
                out.append((operand, flipped))
        return out

    def leaf_layers(self) -> tuple[tuple[int, int], ...]:
        """Every drawn ``(layer, datatype)`` this expression reads, deduped,
        in first-mention order -- the set a deck must register as read
        (:attr:`ExtractionDeck.device_recognition_layers`), or extraction
        would evaluate the expression over layers it never loaded."""
        return tuple(dict.fromkeys(leaf for leaf, _ in self._leaves(False)))

    def required_layers(self) -> tuple[tuple[int, int], ...]:
        """The leaves that must be drawn for the expression to be non-empty
        (an even number of subtractions above them), in first-mention order."""
        return tuple(
            dict.fromkeys(leaf for leaf, negated in self._leaves(False) if not negated)
        )

    def excluded_layers(self) -> tuple[tuple[int, int], ...]:
        """The leaves whose presence removes area from the expression (an odd
        number of subtractions above them), in first-mention order."""
        return tuple(
            dict.fromkeys(leaf for leaf, negated in self._leaves(False) if negated)
        )


def layer_operand_leaves(
    operand: tuple[int, int] | LayerExpr,
) -> tuple[tuple[int, int], ...]:
    """The drawn layers ``operand`` reads: itself for a bare pair, or
    :meth:`LayerExpr.leaf_layers` for an expression."""
    if isinstance(operand, LayerExpr):
        return operand.leaf_layers()
    return (operand,)


@dataclass(frozen=True)
class SizeFilter:
    """Pure-data size restriction on a recognition or terminal region (issue
    #2861): bounding-box minimum / maximum side and polygon area, each an
    **inclusive** ``(lo, hi)`` range in microns (um) / square microns (um^2).
    An exact value is the degenerate range ``(v, v)``. A dimension left
    ``None`` is unconstrained; a polygon must satisfy *every* declared
    dimension. It is independent of any one device family so resistor and
    capacitor flavours can adopt it later (today only
    :attr:`BipolarDevice.emitter_size` consumes it).

    Dimensions, measured per merged polygon:

    - ``bbox_min_um`` -- the shorter side of the polygon's bounding box
      (KLayout ``Region.with_bbox_min``);
    - ``bbox_max_um`` -- the longer side (``with_bbox_max``);
    - ``area_um2`` -- the polygon area (``with_area``).

    Rounding rule (applied by ``extract.py``): bounds are converted to the
    layout's database unit by rounding to the nearest integer dbu (area to
    the nearest dbu^2), so a PDK value such as 0.063 um^2 is exact at
    ``dbu=0.001`` and tolerant of float noise at any other dbu; the range is
    then inclusive at both ends. One dbu outside either bound does not match.

    Construction rejects a negative bound, ``lo > hi``, and an *empty*
    filter (all three dimensions ``None``) -- an empty filter would be a
    silent no-op, so use ``emitter_size=None`` for "no size restriction".
    """

    bbox_min_um: tuple[float, float] | None = None
    bbox_max_um: tuple[float, float] | None = None
    area_um2: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if (
            self.bbox_min_um is None
            and self.bbox_max_um is None
            and self.area_um2 is None
        ):
            raise ValueError(
                "SizeFilter must constrain at least one of bbox_min_um, "
                "bbox_max_um, area_um2 (use None instead of an empty filter)"
            )
        for name in ("bbox_min_um", "bbox_max_um", "area_um2"):
            rng = getattr(self, name)
            if rng is None:
                continue
            if len(rng) != 2:
                raise ValueError(f"SizeFilter.{name} must be a (lo, hi) pair")
            lo, hi = rng
            if lo < 0 or hi < 0:
                raise ValueError(f"SizeFilter.{name} bounds must be non-negative")
            if lo > hi:
                raise ValueError(f"SizeFilter.{name} has lo > hi: {rng!r}")

    def overlaps(self, other: SizeFilter) -> bool:
        """True when some polygon could satisfy both filters: every
        dimension constrained by *both* has intersecting ranges (a dimension
        either leaves unconstrained cannot separate them)."""
        for name in ("bbox_min_um", "bbox_max_um", "area_um2"):
            a, b = getattr(self, name), getattr(other, name)
            if a is None or b is None:
                continue
            if a[1] < b[0] or b[1] < a[0]:
                return False
        return True


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

    ``marker`` is either a bare ``(layer, datatype)`` pair -- one drawn
    layer, the form both sky130 and gf180mcu use -- or a :class:`LayerExpr`
    (issue #2860) for a PDK whose recognition mark is itself a boolean of
    several drawn layers, possibly nested (IHP SG13G2's ``npn_mk = trans &
    (pwell_allowed - nwell - digisub_gap) & ptap_holes``). ``extract.py``
    evaluates the expression to a region and uses it exactly where it would
    use the drawn marker layer, and every leaf layer of the expression
    counts as read (:attr:`ExtractionDeck.device_recognition_layers`). A
    bare pair means exactly what it always has.

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

    ``emitter_size`` (issue #2861) is an optional :class:`SizeFilter` applied
    to the *merged* emitter region after ``emitter_requires``/
    ``emitter_excludes``, so entries sharing ``base``/``marker``/``emitter``
    can tell flavours apart by drawn emitter-window size (e.g. SG13G2's
    ``npn13G2``/``npn13G2l``/``npn13G2v``). A marked emitter matching no
    entry of such a group is reported in ``warnings[]`` (with its bbox) and
    yields no device; entries of a group whose size ranges overlap are
    rejected with an ``ExtractError``. ``None`` (default) means no size
    restriction and leaves extraction and the deck hash unchanged.

    ``class_name`` names the extracted ``DeviceClassBJT3Transistor`` device
    class (``devices[].class`` in the JSON response, and one of the values
    :attr:`ExtractionDeck.device_classes` reports for a deck that declares
    this entry).

    ``substrate`` (issue #2862) is the optional fourth terminal's drawn
    layer, for a PDK's bipolar whose substrate contact is a mask of its own,
    distinct from the collector (SG13G2's four-terminal NPN flavours are the
    motivating case). ``None`` (the default) keeps the entry a
    three-terminal device exactly as before this field existed --
    ``extract.py`` runs it through ``DeviceExtractorBJT3Transistor``
    unchanged, so every deck declared before this field extracts
    byte-identically. A deck that declares the layer instead gets KLayout's
    four-terminal ``DeviceExtractorBJT4Transistor`` (terminals ``C``/``B``/
    ``E``/``S``): the ``S`` region is ``substrate & base`` (the same
    marker-scoped-subset idiom the drawn ``collector`` uses -- the substrate
    mask has to be drawn inside the marked base footprint, a substrate ring
    *outside* it is not recognised as the terminal), the same dummy-marker
    cut the base already took propagates to it for free, and its connectivity
    is a drawn diffusion's -- it joins ``contact`` and picks its net up from
    ordinary contact/metal routing, never the ``substrate_net`` global (that
    global stays the collector-less collector's net). The JSON report's
    ``devices[].nets`` grows an ``"s"`` key for such devices only; a
    three-terminal entry's report is unchanged. ``substrate`` and a
    collector-less ``collector=None`` may be combined (KLayout's BJT4
    extractor handles an empty ``C`` input the way the three-terminal one
    does -- it outputs the base footprint onto it, which stays tied to
    ``substrate_net``) for a device whose collector is the substrate while
    its substrate contact is drawn.

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from -- the device-recognition analogue of
    :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring). ``None`` (the default) means no structured provenance has
    been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.

    ``base_relation``/``emitter_relation``/``collector_relation`` (issue
    #2863) each optionally define that terminal by a geometric relation to
    *another terminal of the same device* -- see :class:`TerminalRelation`.
    All three default to ``None``, and an entry declaring none of them is
    resolved by exactly the fixed base-first path described above
    (``base & marker``, then emitter/collector scoped to that base), so every
    existing deck extracts bit-for-bit as before. Once *any* relation is
    declared, each terminal is resolved from a marker-scoped **candidate**
    region (its own layer ``& marker``, minus the deck's ``dummy`` marker;
    the emitter's candidate is further narrowed by
    ``emitter_requires``/``emitter_excludes`` first), then:

    - a terminal that declares a relation keeps only the candidate polygons
      the relation selects against its (already-resolved) ``source``;
    - a terminal that declares none keeps the legacy scoping -- the emitter
      and collector are intersected with the resolved base -- *unless* the
      base itself is relation-defined, in which case they stay at their
      marker-scoped candidate (scoping them to a base that is itself derived
      from them would be circular).

    Terminals are resolved in :meth:`terminal_resolution_order`. An entry
    whose relations reference the declaring terminal itself, form a cycle,
    or reference / declare a relation on a ``collector`` that is ``None``
    is rejected at construction with a :class:`ValueError` naming the
    problem, never resolved to a silently-empty region.

    The device is still formed by KLayout's stock
    ``DeviceExtractorBJT3Transistor``, which only recognises an emitter
    lying *inside* a base polygon. A base selected with ``extents=True`` is
    typically a ring around its emitter, so ``extract.py`` hands the
    extractor each selected base polygon's bounding box as the device body
    while still registering the ring itself as the base terminal's
    connectivity layer. Likewise a drawn collector only becomes the ``C``
    terminal where it overlaps that body; a lateral collector ring lying
    wholly *outside* the base body is not attributable through the stock
    extractor (``extract.py`` then falls back to the vertical,
    collector-is-base case) -- a limit of the extractor, not of the relation.
    """

    base: tuple[int, int]
    emitter: tuple[int, int]
    marker: tuple[int, int] | LayerExpr
    collector: tuple[int, int] | None = None
    emitter_requires: tuple[tuple[int, int], ...] = ()
    emitter_excludes: tuple[tuple[int, int], ...] = ()
    class_name: str = "bjt"
    provenance: RuleProvenance | None = None
    substrate: tuple[int, int] | None = None
    base_relation: TerminalRelation | None = None
    emitter_relation: TerminalRelation | None = None
    collector_relation: TerminalRelation | None = None
    emitter_size: SizeFilter | None = None

    def __post_init__(self) -> None:
        # Validates eagerly so a malformed deck entry fails where it is
        # declared (at import of the deck module), not on the first layout
        # that happens to draw the device.
        self.terminal_resolution_order()

    def relation(self, terminal: str) -> TerminalRelation | None:
        """The :class:`TerminalRelation` ``terminal`` declares, or ``None``."""
        if terminal not in BIPOLAR_TERMINALS:
            raise ValueError(
                f"bipolar '{self.class_name}': unknown terminal {terminal!r} "
                f"(expected one of: {', '.join(BIPOLAR_TERMINALS)})"
            )
        relation: TerminalRelation | None = getattr(self, f"{terminal}_relation")
        return relation

    @property
    def has_relations(self) -> bool:
        """Whether any terminal is relation-defined -- ``False`` selects the
        legacy fixed base-first resolution path unchanged."""
        return any(self.relation(t) is not None for t in BIPOLAR_TERMINALS)

    def terminal_dependencies(self, terminal: str) -> tuple[str, ...]:
        """The terminals ``terminal``'s region is derived from: its declared
        relation's ``source``, else (legacy scoping) ``("base",)`` for the
        emitter/collector while the base is not itself relation-defined,
        else nothing."""
        relation = self.relation(terminal)
        if relation is not None:
            return (relation.source,)
        if terminal != "base" and self.base_relation is None:
            return ("base",)
        return ()

    def terminal_resolution_order(self) -> tuple[str, ...]:
        """The deterministic order ``extract.py`` resolves this entry's
        drawn terminals in: a topological order of
        :meth:`terminal_dependencies`, ties broken by
        :data:`BIPOLAR_TERMINALS` order. A ``None`` collector is omitted
        (it has no drawn geometry to resolve).

        Raises :class:`ValueError` for a self-referencing relation, a
        relation on or against a ``None`` collector, or a dependency cycle.
        """
        name = f"bipolar '{self.class_name}'"
        drawn = tuple(
            t
            for t in BIPOLAR_TERMINALS
            if t != "collector" or self.collector is not None
        )
        for terminal in BIPOLAR_TERMINALS:
            relation = self.relation(terminal)
            if relation is None:
                continue
            if terminal not in drawn:
                raise ValueError(
                    f"{name}: collector_relation is set but collector is None "
                    "-- a substrate-formed collector has no drawn geometry to "
                    "select from; declare a collector layer or drop the relation"
                )
            if relation.source == terminal:
                raise ValueError(
                    f"{name}: {terminal}_relation references {terminal} itself "
                    "-- a terminal cannot be selected against its own region"
                )
            if relation.source not in drawn:
                raise ValueError(
                    f"{name}: {terminal}_relation references the collector, "
                    "but collector is None (substrate-formed, no drawn "
                    "geometry to select against)"
                )

        order: list[str] = []
        remaining = list(drawn)
        while remaining:
            ready = [
                t
                for t in remaining
                if all(dep in order for dep in self.terminal_dependencies(t))
            ]
            if not ready:
                cycle = ", ".join(
                    f"{t} -> {'/'.join(self.terminal_dependencies(t))}"
                    for t in remaining
                )
                raise ValueError(
                    f"{name}: terminal relations form a dependency cycle "
                    f"({cycle}) -- at least one terminal must be resolvable "
                    "from its own layer alone"
                )
            order.append(ready[0])
            remaining.remove(ready[0])
        return tuple(order)


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
class UnmodelledDeviceFamily:
    """A device family this deck deliberately does **not** recognise (issue
    #3137), declared so a diagnostic can say so.

    Purely informational: it never changes connectivity or ``device_count``.
    ``extract.py``'s diode near-miss warning appends a one-sentence note
    naming ``name`` when a candidate region overlaps ``signature`` -- the
    geometry looks like a diode one marker short, but is really this family
    (e.g. SG13G2's SiGe HBT diffusion matches the antenna-diode predicates).

    ``signature`` is the drawn layer (or :class:`LayerExpr`) that identifies
    the family; ``reference`` is the pointer text the note ends with.
    """

    name: str
    signature: tuple[int, int] | LayerExpr
    reference: str


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
