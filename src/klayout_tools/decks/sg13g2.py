"""sg13g2 DRC/LVS deck: a curated starter subset transcribed directly from
IHP-Open-PDK's own machine-readable rule source (Epic #711 Phase 3b, issue
#905 -- "the second PDK-generality proof" for `klt deck`'s provenance-first
compiler methodology, following sky130 (Phases 0-2, #747/#867/#868/#869) and
gf180mcu (Phase 3a, #904)).

Unlike sky130/gf180mcu -- both of which already had a hand-curated deck
*before* Epic #711 existed, so Phases 0-2 backfilled `RuleProvenance`
citations and golden pairs onto an already-shipping deck -- **no hand-written
`decks/sg13g2.py` existed anywhere in this repo before this issue**. Issue
#524 ("Curated SG13G2 (IHP-Open-PDK) DRC/LVS deck for `klt drc`/`klt lvs`")
was filed to build that hand-curated deck the traditional way, but Champion
twice rejected it as too large a single-PR scope (a full sky130/gf180mcu-
sized deck built in one pass, rather than the rule-group-by-rule-group
history those two decks actually grew through) and it remains open,
unmerged, as of this deck's authoring. Per this issue's own acceptance
criteria, that means every rule below is transcribed *with* its
`RuleProvenance` citation from the very first commit -- there is no
"curate first, backfill provenance later" two-step available here the way
Phases 0-2 had -- and there is no existing hand-written `sg13g2` deck to
cross-check against (see "No #524 cross-check" below).

Source (Apache-2.0, IHP-GmbH/IHP-Open-PDK, the same v0.3.0 release tag
`scripts/fetch-ihp-sg13g2.sh` pins -- verified via a real fetched install,
`pdks/ihp-open-pdk/ihp-sg13g2/` -- and resolved to git commit
``5cccb161f7492697cfa52eb14dc03beb00bdca9e`` via the GitHub API
``git/refs/tags/v0.3.0`` lookup, since the release tarball itself carries no
``.git`` history):

- ``ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/{feol,beol}/*.drc`` --
  the DRC-DSL rule scripts (KLayout's Ruby-flavoured DRC runner, the same
  engine sky130's ``.lydrc``/gf180mcu's ``.drc`` files use). Each curated
  rule below cites the exact file and official rule id (e.g. ``"Act.a"``)
  it was transcribed from.
- ``ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/sg13g2_tech_default.json``
  -- unlike sky130/gf180mcu (whose thresholds are literal numeric constants
  inline in the DRC-DSL script itself), sg13g2's ``.drc`` files read every
  threshold indirectly through a ``drc_rules['<Key>']`` lookup into this
  JSON table (e.g. ``drc_rules['Act_a']``). This module's rules transcribe
  the *value* from this JSON table and the *rule definition* (layer, check
  kind, official id, description) from the ``.drc`` file that reads it --
  cross-verified against each other (every value below was confirmed to
  match both the JSON table's own numeric entry and the ``.drc`` file's own
  inline prose comment, e.g. ``"Rule Act.a: Min. Activ width is 0.15um"``,
  which line up exactly for every rule this module transcribes).
- ``ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/{mos_extraction,
  mos_derivations,general_derivations}.lvs`` -- the companion KLayout LVS
  deck's MOSFET device-recognition rules, for ``EXTRACTION_DECK`` below.
  Issue #1231 adds the thick-oxide ("-HV") MOS flavour from the same files
  and the drawn poly-resistor rules from
  ``lvs/rule_decks/{res_derivations,res_extraction,res_connections}.lvs``.
- ``ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib/sg13g2_tech.json``
  -- the PDK's own device-parameter table (the one its KLayout PyCell device
  generators read), source of the poly resistors' sheet resistances
  (``rsilG2_rspec``/``rppdG2_rspec``); its ``.lvs`` rule decks extract those
  devices through a custom Ruby extractor that carries no sheet-rho constant
  of its own, so the value has to come from this table (see
  ``EXTRACTION_DECK``'s resistor note below).

## Scope guard -- curated starter subset, not a full transcription

Mirrors sky130's own "Scope guard" (see ``sky130.py``'s module docstring):
this is **not** a full transcription of sg13g2's official rule set (the real
DRM spans dozens of ``.drc`` rule-deck files across FEOL/BEOL/forbidden/
antenna/density categories, hundreds of individual rule ids). Issue #905's
original increment covered a single, connected FEOL-to-BEOL2 stack -- Activ,
GatPoly, Cont, Metal1, Via1, Metal2, Via2 -- wide enough to draw a real
two-terminal NMOS/PMOS device and route it out to a second metal level,
proving the compiler generalizes to a PDK whose rule source is
JSON-table-driven rather than inline-literal (sg13g2) as well as one whose
thresholds are inline literals (sky130) or a published DRM CSV table
(gf180mcu). Issue #1243 (sg13g2's own equivalent of sky130's #619) then
extended that stack up through Metal3, Metal4, Metal5, TopVia1, TopMetal1,
TopVia2, and TopMetal2 -- the prerequisite issue #1233 (MIM capacitors) and
issue #1235 (metal resistors) both independently blocked on (see
"Device-class coverage" and "MIM capacitors" below). Coverage is expected to
keep growing incrementally in follow-on issues, exactly as sky130/gf180mcu's
own coverage did (see e.g. ``sky130.py``'s met2/met3/met4/met5 extension
notes, each its own numbered issue).

Not modelled in this increment, for the same "no compound/derived-layer
evaluation" reason ``sky130.py``'s own docstring documents for its own
approximations (this engine's ``Region`` check primitives check one drawn
layer, or one drawn layer against one other drawn layer -- never an
arbitrary boolean/derived expression):

- ``Gat.a1``/``Gat.a2`` (channel-length-specific GatPoly width, scoped to
  ``ngate``/``pgate`` -- themselves compound expressions of GatPoly, Activ,
  NWell, and the thick-gate-oxide marker) and ``Gat.g`` (45-degree bent
  GatPoly width) -- ``Act.a``/``Gat.a`` above already cover the
  layer-general minimum-width floor these refine.
- ``Cnt.c``/``Cnt.c.digibnd``/``Cnt.c.SRAM``/``Cnt.e``/``Cnt.g1``/``Cnt.g2``
  -- each scoped to a compound derivation (``act_nsram.join(activ_mask)
  .not(digibnd_drw)``, SRAM/DigiBnd-region carve-outs, pSD-vs-nSD Activ
  splits) this curated deck does not otherwise model; ``Cnt.c``/``Cnt.d``
  below transcribe the *general-case* Activ/GatPoly enclosure-of-Cont floor
  each of those refines.
- ``M1.e``/``M1.f``/``M1.g``/``M1.i`` and their per-metal-level ``Mn.*``
  analogues (wide-line/45-degree-bend spacing refinements, ``Metal2``
  through ``Metal5``) and ``TopMetal2``'s own ``TM2.bR`` (a
  ``RECOMMENDED``-gated wide-line refinement) -- the same class of
  refinement ``sky130.py`` already documents skipping for its own
  ``m1.3ab``-style wide-metal exceptions.
- No ``"area"``/``"density"``/``"antenna"`` rules (sg13g2's own
  ``density.drc``/``rule_decks/antenna.drc`` files) -- out of this
  DRC-deck-compiler epic's scope, the same carve-out sky130/gf180mcu make.

## Device-class coverage (``EXTRACTION_DECK``)

The same incremental discipline applies to LVS device recognition. Issue
#905 curated MOS only; issue #1231 added the thick-oxide MOS flavour and two
poly resistors. Recognised today:

- **MOS** -- thin-oxide ``sg13_lv_nmos``/``sg13_lv_pmos`` (the default
  ``active``/``nwell`` split) and thick-oxide ``sg13_hv_nmos``/
  ``sg13_hv_pmos`` (``mos_flavours``, scoped to ``ThickGateOx`` 44/0).
- **Drawn poly resistors** -- ``rsil`` and ``rppd``.

Still unrecognised, each tracked as its own follow-on issue rather than left
a silent gap (a device class this deck cannot recognise extracts as ordinary
interconnect -- i.e. a *short* a ``klt lvs`` run reports as an unmatched
device, never a wrong device with a plausible value):

- ``rhigh`` (the third poly-resistor flavour) -- see ``EXTRACTION_DECK``'s
  resistor note for why its sheet resistance is not confidently derivable
  from the PDK's own data -- and the metal resistors ``res_metal1``..
  ``res_topmetal2``. Both tracked by issue #1235; the metal resistors were
  independently blocked on the same ``metals``/``vias`` stack extension
  (issue #1243) MIM capacitors below needed -- #1243 has since landed, so
  #1235's metal-resistor flavours (up through ``res_topmetal2``, now that
  the stack reaches ``TopMetal2``) are a standalone follow-on rather than a
  blocked one.
- Diodes -- ``diode_extraction.lvs``'s antenna diodes (``dantenna``/
  ``dpantenna``) and the three-terminal ``schottky_nbl1`` (extracted
  upstream as a ``bjt3``, not a ``diode``) -- issue #1234 -- plus the
  ``isolbox`` isolation device, the ``sg13_hv_svaricap`` varactor,
  inductors, ESD devices, and the RF MOS/RF MIM variants, none of which is
  tracked yet.

### MIM capacitors -- investigated/deferred (#1233), prerequisite landed (#1243)

Unlike the plain "not curated yet" gaps above, issue #1233 *investigated*
populating ``EXTRACTION_DECK.capacitors`` for ``cap_cmim``/``rfcmim``
(``cap_derivations.lvs``, verified against a real fetched IHP-Open-PDK
v0.3.0 install: both flavours derive from ``mim_drw`` (36/0) over
``metal5_con``, with a ``vmim_drw``/``topvia1_drw`` via up to
``topmetal1_con``) and made a deliberate "defer" call rather than declaring
the entry at that time:

- :class:`~klayout_tools.decks.CapacitorDevice`'s own docstring permits a
  ``bottom_plate`` that does not match one of the deck's tracked
  ``metals[]`` -- it just stays its own self-connected node, isolated from
  the rest of the extracted netlist ("Known limitation"). So populating
  ``capacitors=`` was already *possible* at issue #1233's own time, but both
  of ``cap_cmim``'s plates (Metal5 bottom, TopMetal1-via top) sat entirely
  above this deck's then-current Metal1/Via1/Metal2 stack -- the recognised
  device would have carried the right capacitance value between two nets
  nothing else in the extracted graph touched, never matchable against a
  real design's routing.
- The two closest worked examples in this codebase answer the same
  "recognise now vs. extend the stack first" question oppositely, and both
  point the same way for sg13g2: gf180mcu's ``FuseTop``/``Metal4`` MiM cap
  set ``top_plate_via``/``top_plate_via_metal`` from the start because that
  deck's ``metals`` stack already reached ``Metal4`` when it was curated;
  sky130's ``met3``/``met4`` MiM caps did **not** set them until issue #619
  first extended sky130's own ``metals``/``vias`` stack far enough to reach
  ``met3``/``met4`` (issue #775 then wired the via). sg13g2's then-Metal2-
  capped stack was squarely in the sky130-pre-#619 situation, not the
  gf180mcu-already-there one.

**Decision (issue #1233):** defer ``cap_cmim``/``rfcmim`` recognition until
the deck's ``metals``/``vias`` stack itself is extended up through
Metal5/TopMetal1 -- filed as its own prerequisite, issue #1243, shared with
#1235's metal resistors (``res_metal1``..``res_topmetal2``), which
independently hit the identical Metal2 ceiling.

**Prerequisite landed (issue #1243):** ``EXTRACTION_DECK.metals``/``.vias``
now reach ``TopMetal2`` (see "Scope guard" above and ``EXTRACTION_DECK``'s
own ``metals``/``vias`` comment below) -- ``cap_cmim``/``rfcmim`` recognition
itself is **still not declared** here; #1243 only extends the connectivity
stack those plates would land on. Recognising ``cap_cmim``/``rfcmim`` is now
a standalone follow-on (issue #1233, reopened against the extended stack)
that can set ``top_plate_via``/``top_plate_via_metal`` correctly on the
first pass, mirroring #775 rather than repeating sky130's two-step
history.

### SiGe HBTs -- investigated, declined (issue #1232)

Unlike every other gap in this list, SiGe HBTs (``npn13G2``/``npn13G2l``/
``npn13G2v``, the lateral ``pnpMPA``) are not simply "not curated yet": issue
#1232 investigated populating ``EXTRACTION_DECK.bipolars`` (a tuple of
:class:`~klayout_tools.decks.BipolarDevice`, the mechanism sky130's/
gf180mcu's own curated ``pnp``/bipolar entries use) and concluded the model
cannot faithfully express what SG13G2's own LVS deck does, against a real
fetched IHP-Open-PDK v0.3.0 install (``bjt_derivations.lvs``/
``bjt_extraction.lvs``/``bjt_connections.lvs``/``custom_bjt_extractor.lvs``):

- **Different extractor class entirely.** SG13G2's deck calls
  ``extract_devices(CustomBJTExtractor.new('npn13G2', false), {...})`` --
  a hand-written Ruby ``RBA::GenericDeviceExtractor`` subclass
  (``custom_bjt_extractor.lvs``), not KLayout's stock
  ``DeviceExtractorBJT3Transistor`` that :class:`BipolarDevice` (and this
  engine's own ``extract.py`` bipolar-recognition block) wires up. That
  custom extractor iterates each device-marker instance individually and
  computes device parameters (``we``/``le``/``Nx``/``m`` for NPN, ``A``/``P``/
  ``m`` for PNP) from the *drawn bounding box* of the first recognised
  emitter polygon -- there is no equivalent "per-instance geometric
  measurement" step in this engine's ``BipolarDevice``/
  ``DeviceExtractorBJT3Transistor`` wiring at all.
- **The marker is itself a 3-layer compound derivation, not a single drawn
  layer.** ``npn_mk = trans_drw.and(pwell).and(ptap_holes)`` --
  :class:`BipolarDevice.marker` is a single ``(layer, datatype)`` pair, and
  ``pwell`` is not even a literal drawn layer here (``pwell =
  pwell_allowed.not(nwell_drw).not(digisub_gap)`` in
  ``general_derivations.lvs`` -- the same "sg13g2 draws no separate
  PWell layer" gap this module's MOS-recognition note above already
  documents for the NMOS body/``substrate_net`` fallback).
- **Collector/base/emitter pins are shape-filtered, not boolean-filtered.**
  E.g. ``npn13G2_e_pin = npn13G2_e_.with_bbox_min(0.07.um)
  .with_bbox_max(0.9.um).with_area(0.063.um)`` -- ``npn13G2``,
  ``npn13G2l``, and ``npn13G2v`` are distinguished from each other almost
  *entirely* by their emitter windows' drawn bounding-box/area filters
  (``with_bbox_min``/``with_bbox_max``/``with_area``), not by a distinct
  layer combination this engine could key a separate ``BipolarDevice``
  entry off. ``extract.py``'s own bipolar wiring only ever intersects
  layers (``base & marker``, ``emitter & base [& requires - excludes]``) --
  it has no size-filtered-region primitive at all.
- **npn13G2/npn13G2l/npn13G2v are 4-terminal devices with a genuinely
  distinct substrate node** (``'S' => npn_sub`` alongside ``'C'``/``'B'``/
  ``'E'`` in ``bjt_extraction.lvs``, ``npn_sub = npn_mk.not(npn_exclude)``,
  connected to ``pwell`` in ``bjt_connections.lvs``) -- not the "collector
  formed by substrate, ``collector=None`` folds it into the deck's
  ``substrate_net`` global" 3-terminal case :class:`BipolarDevice`'s own
  docstring describes (and sky130's/gf180mcu's populated entries use).
  ``pnpMPA`` is 3-terminal, but its own terminals
  (``pnp_mpa_b``/``pnp_mpa_c``) are each restricted to the subset
  *dynamically* ``interacting`` with the *already-computed* emitter region's
  extents (``pnp_b.interacting(pnp_b.extents.interacting(pnp_mpa_e))``) --
  a per-instance geometric relationship, not a static per-terminal
  ``requires``/``excludes`` layer set.

Forcing a same-shaped approximation onto :class:`BipolarDevice` here (e.g.
``base=(31, 0)`` NWell-alike, a single-layer ``marker``, no ``collector``)
would produce a self-consistent golden pair that still does not match SG13G2's
real connectivity or device count -- exactly the failure mode this issue's own
acceptance criteria warned against, so this deck declines to populate
``bipolars`` rather than ship a plausible-looking wrong mapping. A drawn
SiGe HBT continues to extract as ordinary unconnected geometry (a
``klt lvs`` ``device.unmatched``/short, never a wrong device) until either
this engine grows a size-filtered/dynamic-relationship device-recognition
primitive general enough for SG13G2's own custom extractor, or a future
issue revisits this with a narrower, explicitly-approximate scope.

## No #524 cross-check

Epic #711's Phase 1 (sky130, #747) and the sky130 LVS phases (#867-#869)
each additionally cross-checked their compiled rules against a real,
independently-authored reference: sky130's own upstream ``.lydrc``/``.lvs``
scripts run through KLayout's native DRC-DSL runner (``--engine klayout``).
This module's DRC/LVS *values* are already transcribed directly from that
same class of reference (sg13g2's own ``.drc``/``.lvs`` scripts, cited per
rule below) -- but issue #905's own acceptance criteria ask specifically for
a cross-check against **issue #524's hand-written `decks/sg13g2.py`**, a
second, independently-curated deck this repo would maintain in parallel (the
same relationship sky130's compiled ``DrcRule``/``ExtractionDeck`` entries
have with sky130's own hand-transcribed values, which predate Epic #711).
**#524 remains open and unmerged as of this deck's authoring** (Champion
rejected it twice as an oversized single-PR scope -- see that issue's own
comment history) -- there is no second `sg13g2` deck in this repo to diff
against. Per this issue's own instructions, this module proceeds with
golden-pair validation only (every rule below ships a golden violate/pass
pair validated against *this* module's own values, plus a golden
layout->netlist pair for each `EXTRACTION_DECK` device rule -- see
``tests/test_sg13g2_deck.py``) and states this gap explicitly here rather
than silently omitting the cross-check criterion. Once #524 lands (if ever),
a follow-on issue can diff the two decks' rule-by-rule values the way #869
diffed sky130's compiled deck against ``sky130.lvs``.
"""

from __future__ import annotations

from . import (
    DrcRule,
    ExtractionDeck,
    MOSFlavour,
    ParasiticsDeck,
    ResistorDevice,
    RuleProvenance,
)

# `RuleProvenance.source_repo`/`.commit` shared by every rule in this module
# -- the same "one repo/commit pin, per-rule source_path/rule_id" shape
# `sky130.py`'s `_sky130_provenance`/`_sky130_lvs_provenance` helpers use.
_IHP_OPEN_PDK_REPO = "IHP-GmbH/IHP-Open-PDK"
_IHP_OPEN_PDK_COMMIT = "5cccb161f7492697cfa52eb14dc03beb00bdca9e"  # v0.3.0 tag


def _sg13g2_drc_provenance(source_path: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a sg13g2 DRC rule -- `source_path`
    is the `.drc` rule-deck file that *defines* the rule (layer, check kind,
    official id); the rule's numeric threshold is additionally cross-verified
    against `sg13g2_tech_default.json`'s own entry for the same key (see the
    module docstring)."""
    return RuleProvenance(
        source_repo=_IHP_OPEN_PDK_REPO,
        source_path=source_path,
        rule_id=rule_id,
        commit=_IHP_OPEN_PDK_COMMIT,
    )


_LVS_RULE_DECKS = "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks"


def _sg13g2_lvs_provenance(source_file: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a sg13g2 *device-recognition*
    rule -- `source_file` is the `.lvs` rule-deck file under
    `libs.tech/klayout/tech/lvs/rule_decks/` whose `extract_devices(...)`
    call defines the device, `rule_id` its official device-class name (the
    string that call passes, e.g. `"sg13_lv_nmos"`). The LVS sibling of
    :func:`_sg13g2_drc_provenance`, mirroring `gf180mcu.py`'s own
    `_gf180mcu_lvs_provenance`."""
    return RuleProvenance(
        source_repo=_IHP_OPEN_PDK_REPO,
        source_path=f"{_LVS_RULE_DECKS}/{source_file}",
        rule_id=rule_id,
        commit=_IHP_OPEN_PDK_COMMIT,
    )


_FEOL = "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol"
_BEOL = "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol"

# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001), matching sky130.py/gf180mcu.py's own
# authoring convention -- see `DrcRule`'s docstring for how `run_drc()`
# rescales this against a layout's own actual dbu at run time.
NOMINAL_DBU_UM = 0.001

DECK: list[DrcRule] = [
    # --- 5.5 Activ (feol/5_5_activ.drc) -------------------------------
    DrcRule(
        id="activ.width.1",
        description="minimum Activ width",
        layer=(1, 0),  # Activ.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # 5_5_activ.drc rule "Act.a": activ_drw.width(0.15um, euclidian)
        # -> "5.5. Act.a : Min. Activ width : 0.15 um"
        # (sg13g2_tech_default.json: drc_rules.Act_a == 0.15)
        scope="Act",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_5_activ.drc", "Act.a"),
    ),
    DrcRule(
        id="activ.space.1",
        description="minimum Activ space or notch",
        layer=(1, 0),  # Activ.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_5_activ.drc rule "Act.b": activ_drw.space(0.21um, euclidian)
        # -> "5.5. Act.b : Min. Activ space or notch : 0.21 um"
        # (sg13g2_tech_default.json: drc_rules.Act_b == 0.21)
        scope="Act",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_5_activ.drc", "Act.b"),
    ),
    # --- 5.8 GatPoly (feol/5_8_gatpoly.drc) ---------------------------
    DrcRule(
        id="gatpoly.width.1",
        description="minimum GatPoly width",
        layer=(5, 0),  # GatPoly.drawing
        check="width",
        threshold_dbu=130,  # 0.13 um
        # 5_8_gatpoly.drc rule "Gat.a": gatpoly_drw.width(0.13um, euclidian)
        # -> "5.8. Gat.a : Min. GatPoly width : 0.13 um"
        # (sg13g2_tech_default.json: drc_rules.Gat_a == 0.13)
        scope="Gat",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_8_gatpoly.drc", "Gat.a"),
    ),
    DrcRule(
        id="gatpoly.space.1",
        description="minimum GatPoly space or notch",
        layer=(5, 0),  # GatPoly.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # 5_8_gatpoly.drc rule "Gat.b": gatpoly_drw.space(0.18um, euclidian)
        # -> "5.8. Gat.b : Min. GatPoly space or notch : 0.18 um"
        # (sg13g2_tech_default.json: drc_rules.Gat_b == 0.18)
        scope="Gat",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_8_gatpoly.drc", "Gat.b"),
    ),
    DrcRule(
        id="gatpoly.separation.activ.1",
        description=(
            "minimum GatPoly space to Activ (field poly clearance from "
            "unrelated active area; a GatPoly shape gating over its own "
            "Activ is not flagged -- overlapping/interacting shapes are "
            "outside this engine's separation_check, matching the real "
            "rule's own .sep() semantics)"
        ),
        layer=(5, 0),  # GatPoly.drawing
        other_layer=(1, 0),  # Activ.drawing
        check="separation",
        threshold_dbu=70,  # 0.07 um
        # 5_8_gatpoly.drc rule "Gat.d":
        # gatpoly_drw.sep(activ_drw, 0.07um, euclidian)
        # -> "5.8. Gat.d : Min. GatPoly space to Activ : 0.07 um"
        # (sg13g2_tech_default.json: drc_rules.Gat_d == 0.07)
        scope="Gat",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_8_gatpoly.drc", "Gat.d"),
    ),
    # --- 5.14 Cont (feol/5_14_cont.drc) -------------------------------
    DrcRule(
        id="cont.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Cont width -- "
            "approximated here as a minimum-width floor only; this "
            "engine's width_check has no upper bound, the same "
            "min-size-only approximation sky130.py's via.width.1/"
            "gf180mcu.py's contact.width.1 already document for their own "
            "fixed-size-cut rules"
        ),
        layer=(6, 0),  # Cont.drawing
        check="width",
        threshold_dbu=160,  # 0.16 um
        # 5_14_cont.drc rule "Cnt.a":
        # cont_sq.without_bbox_width(0.16um)  (min AND max bbox width)
        # -> "5.14. Cnt.a : Min. and max. Cont width : 0.16 um"
        # (sg13g2_tech_default.json: drc_rules.Cnt_a == 0.16)
        scope="Cnt",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_14_cont.drc", "Cnt.a"),
    ),
    DrcRule(
        id="cont.space.1",
        description="minimum Cont spacing",
        layer=(6, 0),  # Cont.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # 5_14_cont.drc rule "Cnt.b": cont_sq.space(0.18um, euclidian)
        # -> "5.14. Cnt.b : Min. Cont space : 0.18 um"
        # (sg13g2_tech_default.json: drc_rules.Cnt_b == 0.18)
        scope="Cnt",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_14_cont.drc", "Cnt.b"),
    ),
    DrcRule(
        id="activ.enclosing.cont.1",
        description=(
            "minimum Activ enclosure of Cont (approximates the official "
            "rule's SRAM/DigiBnd-scoped compound-layer derivation as a "
            "plain, unconditional Activ-encloses-Cont floor -- the same "
            "class of approximation sky130.py's diff.enclosing.licon.1 "
            "makes for its own compound-layer official rule)"
        ),
        layer=(1, 0),  # Activ.drawing
        other_layer=(6, 0),  # Cont.drawing
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # 5_14_cont.drc rule "Cnt.c":
        # cont_nsvaricap.enclosed(act_nsram.join(activ_mask).not(digibnd_drw),
        #                         0.07um, euclidian)
        # -> "5.14. Cnt.c : Min. Activ enclosure of Cont : 0.07 um"
        # (sg13g2_tech_default.json: drc_rules.Cnt_c == 0.07; the SRAM
        # (0.006um)/DigiBnd (0.05um) region-specific refinements -- Cnt.c.SRAM/
        # Cnt.c.digibnd -- are not modelled here, see the module docstring's
        # "Scope guard" section)
        scope="Cnt",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_14_cont.drc", "Cnt.c"),
    ),
    DrcRule(
        id="gatpoly.enclosing.cont.1",
        description="minimum GatPoly enclosure of Cont",
        layer=(5, 0),  # GatPoly.drawing
        other_layer=(6, 0),  # Cont.drawing
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # 5_14_cont.drc rule "Cnt.d": cont_sq.enclosed(gatpoly_drw, 0.07um,
        # euclidian) -> "5.14. Cnt.d : Min. GatPoly enclosure of Cont : 0.07 um"
        # (sg13g2_tech_default.json: drc_rules.Cnt_d == 0.07)
        scope="Cnt",
        provenance=_sg13g2_drc_provenance(f"{_FEOL}/5_14_cont.drc", "Cnt.d"),
    ),
    # --- 5.16 Metal1 (beol/5_16_metal1.drc) ---------------------------
    DrcRule(
        id="metal1.width.1",
        description="minimum Metal1 width",
        layer=(8, 0),  # Metal1.drawing
        check="width",
        threshold_dbu=160,  # 0.16 um
        # 5_16_metal1.drc rule "M1.a": metal1_drw.width(0.16um, euclidian)
        # -> "5.16. M1.a: Min. Metal1 width: 0.16 um."
        # (sg13g2_tech_default.json: drc_rules.M1_a == 0.16)
        scope="M1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_16_metal1.drc", "M1.a"),
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum Metal1 space or notch",
        layer=(8, 0),  # Metal1.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # 5_16_metal1.drc rule "M1.b": metal1_drw.space(0.18um, euclidian)
        # -> "5.16. M1.b: Min. Metal1 space or notch: 0.18 um."
        # (sg13g2_tech_default.json: drc_rules.M1_b == 0.18)
        scope="M1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_16_metal1.drc", "M1.b"),
    ),
    # --- 5.19 Via1 (beol/5_19_via1.drc) --------------------------------
    DrcRule(
        id="via1.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via1 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation cont.width.1 above makes"
        ),
        layer=(19, 0),  # Via1.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_19_via1.drc rule "V1.a": via1_nseal.without_bbox_min/max(0.19um)
        # -> "5.19. V1.a : Min. and max. Via1 width: 0.19 um"
        # (sg13g2_tech_default.json: drc_rules.V1_a == 0.19)
        scope="V1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_19_via1.drc", "V1.a"),
    ),
    DrcRule(
        id="via1.space.1",
        description="minimum Via1 spacing",
        layer=(19, 0),  # Via1.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_19_via1.drc rule "V1.b": via1_nseal.space(0.22um, euclidian)
        # -> "5.19. V1.b : Min. Via1 space: 0.22 um"
        # (sg13g2_tech_default.json: drc_rules.V1_b == 0.22)
        scope="V1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_19_via1.drc", "V1.b"),
    ),
    DrcRule(
        id="metal1.enclosing.via1.1",
        description="minimum Metal1 enclosure of Via1",
        layer=(8, 0),  # Metal1.drawing
        other_layer=(19, 0),  # Via1.drawing
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # 5_19_via1.drc rule "V1.c": via1_nseal.enclosed(metal1_drw, 0.01um,
        # euclidian) -> "5.19. V1.c : Min. Metal1 enclosure of Via1 is 0.01 um"
        # (sg13g2_tech_default.json: drc_rules.V1_c == 0.01)
        scope="V1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_19_via1.drc", "V1.c"),
    ),
    # --- 5.17 Metaln, Metal2 instance (beol/5_17_metaln.drc) -----------
    DrcRule(
        id="metal2.width.1",
        description="minimum Metal2 width",
        layer=(10, 0),  # Metal2.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M2.a" (met_no=2 instance of the templated
        # "Mn.a" rule): metal2_drw.width(0.20um, euclidian)
        # -> "5.17. M2.a: Min. Metal2 width: 0.20 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_a == 0.2, shared by every
        # Metal2-Metal5 level)
        scope="M2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M2.a"),
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum Metal2 space or notch",
        layer=(10, 0),  # Metal2.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M2.b": metal2_drw.space(0.21um, euclidian)
        # -> "5.17. M2.b: Min. Metal2 space or notch: 0.21 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_b == 0.21)
        scope="M2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M2.b"),
    ),
    # --- 5.20 Vian, Via2 instance (beol/5_20_vian.drc) ------------------
    DrcRule(
        id="via2.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via2 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1 above makes"
        ),
        layer=(29, 0),  # Via2.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_20_vian.drc rule "V2.a" (via_no=2 instance of the templated
        # "Vn.a" rule): via2_nseal.without_bbox_min/max(0.19um)
        # -> "5.20. V2.a : Min. and max. Via2 width: 0.19 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_a == 0.19, shared by
        # Via2-Via4)
        scope="V2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V2.a"),
    ),
    DrcRule(
        id="via2.space.1",
        description="minimum Via2 spacing",
        layer=(29, 0),  # Via2.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_20_vian.drc rule "V2.b": via2_nseal.space(0.22um, euclidian)
        # -> "5.20. V2.b : Min. Via2 space: 0.22 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_b == 0.22)
        scope="V2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V2.b"),
    ),
    DrcRule(
        id="metal2.enclosing.via2.1",
        description="minimum Metal2 enclosure of Via2",
        layer=(10, 0),  # Metal2.drawing
        other_layer=(29, 0),  # Via2.drawing
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # 5_20_vian.drc rule "V2.c": via2_nseal.enclosed(metal2_drw, 0.005um,
        # euclidian) -> "5.20. V2.c : Min. Metal2 enclosure of Via2 is 0.005 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_c == 0.005)
        scope="V2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V2.c"),
    ),
    # --- 5.17 Metaln, Metal3 instance (beol/5_17_metaln.drc) -----------
    # Issue #1243: extending the connectivity/DRC stack past Metal2, up
    # through Metal5/TopMetal1/TopMetal2 -- the prerequisite #1233 (MIM
    # capacitors) and #1235 (metal resistors) both block on (see the module
    # docstring's own "MIM capacitors" section). `metaln.drc`'s templated
    # "Mn.a"/"Mn.b" rule iterates `mets_lay = [metal2_drw, metal3_drw,
    # metal4_drw, metal5_drw]` from `metal_start_index = 2` -- Metal2's own
    # instance is transcribed above; Metal3/Metal4/Metal5 below share the
    # identical `Mn_a`/`Mn_b` JSON-table values (0.20/0.21 um) that
    # `metal2.width.1`/`metal2.space.1` already cite.
    DrcRule(
        id="metal3.width.1",
        description="minimum Metal3 width",
        layer=(30, 0),  # Metal3.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M3.a" (met_no=3 instance of "Mn.a"):
        # metal3_drw.width(0.20um, euclidian)
        # -> "5.17. M3.a: Min. Metal3 width: 0.20 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_a == 0.2)
        scope="M3",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M3.a"),
    ),
    DrcRule(
        id="metal3.space.1",
        description="minimum Metal3 space or notch",
        layer=(30, 0),  # Metal3.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M3.b": metal3_drw.space(0.21um, euclidian)
        # -> "5.17. M3.b: Min. Metal3 space or notch: 0.21 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_b == 0.21)
        scope="M3",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M3.b"),
    ),
    # --- 5.20 Vian, Via3 instance (beol/5_20_vian.drc) ------------------
    # `vian.drc`'s templated table iterates `vias_lay = [via2_drw, via3_drw,
    # via4_drw]` zipped with `metals_lay = [metal2_drw, metal3_drw,
    # metal4_drw]` from `via_start_index = 2` -- so Via3's own "Vn.c"
    # enclosure instance checks against **Metal3** (the metal *below* it,
    # the same "enclosure checked against the lower metal only" convention
    # `via1.width.1`'s docstring documents; Via3 physically also lands on
    # Metal4 above, but the upstream rule only constrains the landing pad
    # below it, exactly like Via1/Via2 above).
    DrcRule(
        id="via3.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via3 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1/via2.width.1 above make"
        ),
        layer=(49, 0),  # Via3.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_20_vian.drc rule "V3.a" (via_no=3 instance of "Vn.a"):
        # via3_nseal.without_bbox_min/max(0.19um)
        # -> "5.20. V3.a : Min. and max. Via3 width: 0.19 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_a == 0.19)
        scope="V3",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V3.a"),
    ),
    DrcRule(
        id="via3.space.1",
        description="minimum Via3 spacing",
        layer=(49, 0),  # Via3.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_20_vian.drc rule "V3.b": via3_nseal.space(0.22um, euclidian)
        # -> "5.20. V3.b : Min. Via3 space: 0.22 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_b == 0.22)
        scope="V3",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V3.b"),
    ),
    DrcRule(
        id="metal3.enclosing.via3.1",
        description="minimum Metal3 enclosure of Via3",
        layer=(30, 0),  # Metal3.drawing
        other_layer=(49, 0),  # Via3.drawing
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # 5_20_vian.drc rule "V3.c": via3_nseal.enclosed(metal3_drw, 0.005um,
        # euclidian) -> "5.20. V3.c : Min. Metal3 enclosure of Via3 is 0.005 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_c == 0.005)
        scope="V3",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V3.c"),
    ),
    # --- 5.17 Metaln, Metal4 instance (beol/5_17_metaln.drc) -----------
    DrcRule(
        id="metal4.width.1",
        description="minimum Metal4 width",
        layer=(50, 0),  # Metal4.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M4.a" (met_no=4 instance of "Mn.a"):
        # metal4_drw.width(0.20um, euclidian)
        # -> "5.17. M4.a: Min. Metal4 width: 0.20 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_a == 0.2)
        scope="M4",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M4.a"),
    ),
    DrcRule(
        id="metal4.space.1",
        description="minimum Metal4 space or notch",
        layer=(50, 0),  # Metal4.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M4.b": metal4_drw.space(0.21um, euclidian)
        # -> "5.17. M4.b: Min. Metal4 space or notch: 0.21 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_b == 0.21)
        scope="M4",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M4.b"),
    ),
    # --- 5.20 Vian, Via4 instance (beol/5_20_vian.drc) ------------------
    DrcRule(
        id="via4.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via4 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1/via2.width.1/"
            "via3.width.1 above make"
        ),
        layer=(66, 0),  # Via4.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_20_vian.drc rule "V4.a" (via_no=4 instance of "Vn.a"):
        # via4_nseal.without_bbox_min/max(0.19um)
        # -> "5.20. V4.a : Min. and max. Via4 width: 0.19 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_a == 0.19)
        scope="V4",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V4.a"),
    ),
    DrcRule(
        id="via4.space.1",
        description="minimum Via4 spacing",
        layer=(66, 0),  # Via4.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_20_vian.drc rule "V4.b": via4_nseal.space(0.22um, euclidian)
        # -> "5.20. V4.b : Min. Via4 space: 0.22 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_b == 0.22)
        scope="V4",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V4.b"),
    ),
    DrcRule(
        id="metal4.enclosing.via4.1",
        description="minimum Metal4 enclosure of Via4",
        layer=(50, 0),  # Metal4.drawing
        other_layer=(66, 0),  # Via4.drawing
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # 5_20_vian.drc rule "V4.c": via4_nseal.enclosed(metal4_drw, 0.005um,
        # euclidian) -> "5.20. V4.c : Min. Metal4 enclosure of Via4 is 0.005 um"
        # (sg13g2_tech_default.json: drc_rules.Vn_c == 0.005)
        scope="V4",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_20_vian.drc", "V4.c"),
    ),
    # --- 5.17 Metaln, Metal5 instance (beol/5_17_metaln.drc) -----------
    # Metal5 is the top of `metaln.drc`'s own templated table
    # (`mets_lay = [metal2_drw, metal3_drw, metal4_drw, metal5_drw]`) --
    # everything above it (TopVia1/TopMetal1/TopVia2/TopMetal2) is its own,
    # differently-thresholded "Top*" rule-deck file below.
    DrcRule(
        id="metal5.width.1",
        description="minimum Metal5 width",
        layer=(67, 0),  # Metal5.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M5.a" (met_no=5 instance of "Mn.a"):
        # metal5_drw.width(0.20um, euclidian)
        # -> "5.17. M5.a: Min. Metal5 width: 0.20 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_a == 0.2)
        scope="M5",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M5.a"),
    ),
    DrcRule(
        id="metal5.space.1",
        description="minimum Metal5 space or notch",
        layer=(67, 0),  # Metal5.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M5.b": metal5_drw.space(0.21um, euclidian)
        # -> "5.17. M5.b: Min. Metal5 space or notch: 0.21 um."
        # (sg13g2_tech_default.json: drc_rules.Mn_b == 0.21)
        scope="M5",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_17_metaln.drc", "M5.b"),
    ),
    # --- 5.21 TopVia1 (beol/5_21_topvia1.drc) ---------------------------
    # TopVia1 connects Metal5 to TopMetal1 -- the level issue #1233's
    # `cap_cmim`/`rfcmim` MIM caps land their `vmim_drw`/`topvia1_drw` via
    # on (see the module docstring's own "MIM capacitors" section). Unlike
    # Via1-Via4's single below-only enclosure rule, TopVia1 carries *two*
    # official enclosure rules -- "TV1.c" (Metal5, below) and "TV1.d"
    # (TopMetal1, above) -- both transcribed here rather than only the
    # lower one, since the upstream source genuinely defines both.
    DrcRule(
        id="topvia1.width.1",
        description=(
            "minimum (and, on the real rule, maximum) TopVia1 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1 above makes"
        ),
        layer=(125, 0),  # TopVia1.drawing
        check="width",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.a":
        # topvia1_nseal.without_bbox_min/max(0.42um)
        # -> "5.21. TV1.a : Min. and max. TopVia1 width: 0.42 um"
        # (sg13g2_tech_default.json: drc_rules.TV1_a == 0.42)
        scope="TV1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_21_topvia1.drc", "TV1.a"),
    ),
    DrcRule(
        id="topvia1.space.1",
        description="minimum TopVia1 spacing",
        layer=(125, 0),  # TopVia1.drawing
        check="space",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.b": topvia1_nseal.space(0.42um, euclidian)
        # -> "5.21. TV1.b : Min. TopVia1 space: 0.42 um"
        # (sg13g2_tech_default.json: drc_rules.TV1_b == 0.42)
        scope="TV1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_21_topvia1.drc", "TV1.b"),
    ),
    DrcRule(
        id="metal5.enclosing.topvia1.1",
        description="minimum Metal5 enclosure of TopVia1",
        layer=(67, 0),  # Metal5.drawing
        other_layer=(125, 0),  # TopVia1.drawing
        check="enclosing",
        threshold_dbu=100,  # 0.10 um
        # 5_21_topvia1.drc rule "TV1.c":
        # topvia1_nseal.enclosed(metal5_drw, 0.10um, euclidian)
        # -> "5.21. TV1.c : Min. Metal5 enclosure of TopVia1 is 0.10 um"
        # (sg13g2_tech_default.json: drc_rules.TV1_c == 0.1)
        scope="TV1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_21_topvia1.drc", "TV1.c"),
    ),
    DrcRule(
        id="topmetal1.enclosing.topvia1.1",
        description="minimum TopMetal1 enclosure of TopVia1",
        layer=(126, 0),  # TopMetal1.drawing
        other_layer=(125, 0),  # TopVia1.drawing
        check="enclosing",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.d":
        # topvia1_nseal.enclosed(topmetal1_drw, 0.42um, euclidian)
        # -> "5.21. TV1.d : Min. TopMetal1 enclosure of TopVia1 is 0.42 um"
        # (sg13g2_tech_default.json: drc_rules.TV1_d == 0.42)
        scope="TV1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_21_topvia1.drc", "TV1.d"),
    ),
    # --- 5.22 TopMetal1 (beol/5_22_topmetal1.drc) -----------------------
    # The level #1233's MIM caps' `topmetal1_con` top plate lands on. Note
    # the much coarser threshold (1.64 um, vs. Metal2-Metal5's 0.20 um) --
    # verified against both `5_22_topmetal1.drc`'s own inline prose comment
    # and `sg13g2_tech_default.json`'s `TM1_a`/`TM1_b` entries, not a
    # transcription error.
    DrcRule(
        id="topmetal1.width.1",
        description="minimum TopMetal1 width",
        layer=(126, 0),  # TopMetal1.drawing
        check="width",
        threshold_dbu=1640,  # 1.64 um
        # 5_22_topmetal1.drc rule "TM1.a":
        # topmetal1_drw.width(1.64um, euclidian)
        # -> "5.22. TM1.a: Min. TopMetal1 width: 1.64 um."
        # (sg13g2_tech_default.json: drc_rules.TM1_a == 1.64)
        scope="TM1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_22_topmetal1.drc", "TM1.a"),
    ),
    DrcRule(
        id="topmetal1.space.1",
        description="minimum TopMetal1 space or notch",
        layer=(126, 0),  # TopMetal1.drawing
        check="space",
        threshold_dbu=1640,  # 1.64 um
        # 5_22_topmetal1.drc rule "TM1.b":
        # topmetal1_drw.space(1.64um, euclidian)
        # -> "5.22. TM1.b: Min. TopMetal1 space or notch: 1.64 um."
        # (sg13g2_tech_default.json: drc_rules.TM1_b == 1.64)
        scope="TM1",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_22_topmetal1.drc", "TM1.b"),
    ),
    # --- 5.24 TopVia2 (beol/5_24_topvia2.drc) ---------------------------
    # TopVia2 connects TopMetal1 to TopMetal2 -- the level issue #1235's
    # `res_topmetal2` metal resistor flavour needs (see the module
    # docstring's own resistor note). Like TopVia1 above, both of its
    # official enclosure rules ("TV2.c" TopMetal1-below, "TV2.d"
    # TopMetal2-above) are transcribed.
    DrcRule(
        id="topvia2.width.1",
        description=(
            "minimum (and, on the real rule, maximum) TopVia2 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1/topvia1.width.1 above "
            "make"
        ),
        layer=(133, 0),  # TopVia2.drawing
        check="width",
        threshold_dbu=900,  # 0.90 um
        # 5_24_topvia2.drc rule "TV2.a":
        # topvia2_nseal.without_bbox_min/max(0.90um)
        # -> "5.24. TV2.a : Min. and max. TopVia2 width: 0.90 um"
        # (sg13g2_tech_default.json: drc_rules.TV2_a == 0.9)
        scope="TV2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_24_topvia2.drc", "TV2.a"),
    ),
    DrcRule(
        id="topvia2.space.1",
        description="minimum TopVia2 spacing",
        layer=(133, 0),  # TopVia2.drawing
        check="space",
        threshold_dbu=1060,  # 1.06 um
        # 5_24_topvia2.drc rule "TV2.b": topvia2_nseal.space(1.06um, euclidian)
        # -> "5.24. TV2.b : Min. TopVia2 space: 1.06 um"
        # (sg13g2_tech_default.json: drc_rules.TV2_b == 1.06)
        scope="TV2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_24_topvia2.drc", "TV2.b"),
    ),
    DrcRule(
        id="topmetal1.enclosing.topvia2.1",
        description="minimum TopMetal1 enclosure of TopVia2",
        layer=(126, 0),  # TopMetal1.drawing
        other_layer=(133, 0),  # TopVia2.drawing
        check="enclosing",
        threshold_dbu=500,  # 0.50 um
        # 5_24_topvia2.drc rule "TV2.c":
        # topvia2_nseal.enclosed(topmetal1_drw, 0.50um, euclidian)
        # -> "5.24. TV2.c : Min. TopMetal1 enclosure of TopVia2 is 0.50 um"
        # (sg13g2_tech_default.json: drc_rules.TV2_c == 0.5)
        scope="TV2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_24_topvia2.drc", "TV2.c"),
    ),
    DrcRule(
        id="topmetal2.enclosing.topvia2.1",
        description="minimum TopMetal2 enclosure of TopVia2",
        layer=(134, 0),  # TopMetal2.drawing
        other_layer=(133, 0),  # TopVia2.drawing
        check="enclosing",
        threshold_dbu=500,  # 0.50 um
        # 5_24_topvia2.drc rule "TV2.d":
        # topvia2_nseal.enclosed(topmetal2_drw, 0.50um, euclidian)
        # -> "5.24. TV2.d : Min. TopMetal2 enclosure of TopVia2 is 0.50 um"
        # (sg13g2_tech_default.json: drc_rules.TV2_d == 0.5)
        scope="TV2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_24_topvia2.drc", "TV2.d"),
    ),
    # --- 5.25 TopMetal2 (beol/5_25_topmetal2.drc) -----------------------
    # The top of the curated stack as of issue #1243 -- the level #1235's
    # `res_topmetal2` metal resistor flavour needs. `TM2.bR` (the
    # `RECOMMENDED`-gated wide-line spacing refinement) is not transcribed,
    # the same "wide-line/45-degree-bend refinement" class of omission the
    # module docstring's "Scope guard" section already documents for
    # `M1.e`/`M1.f`/`M1.g`/`M1.i` and their `Mn.*` analogues.
    DrcRule(
        id="topmetal2.width.1",
        description="minimum TopMetal2 width",
        layer=(134, 0),  # TopMetal2.drawing
        check="width",
        threshold_dbu=2000,  # 2.00 um
        # 5_25_topmetal2.drc rule "TM2.a":
        # topmetal2_drw.width(2.00um, euclidian)
        # -> "5.25. TM2.a: Min. TopMetal2 width: 2.00 um."
        # (sg13g2_tech_default.json: drc_rules.TM2_a == 2.0)
        scope="TM2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_25_topmetal2.drc", "TM2.a"),
    ),
    DrcRule(
        id="topmetal2.space.1",
        description="minimum TopMetal2 space or notch",
        layer=(134, 0),  # TopMetal2.drawing
        check="space",
        threshold_dbu=2000,  # 2.00 um
        # 5_25_topmetal2.drc rule "TM2.b":
        # topmetal2_drw.space(2.00um, euclidian)
        # -> "5.25. TM2.b: Min. TopMetal2 space or notch: 2.00 um."
        # (sg13g2_tech_default.json: drc_rules.TM2_b == 2.0)
        scope="TM2",
        provenance=_sg13g2_drc_provenance(f"{_BEOL}/5_25_topmetal2.drc", "TM2.b"),
    ),
]

LAYER_NAMES: dict[tuple[int, int], str] = {
    (1, 0): "Activ.drawing",
    (5, 0): "GatPoly.drawing",
    (6, 0): "Cont.drawing",
    (8, 0): "Metal1.drawing",
    (19, 0): "Via1.drawing",
    (10, 0): "Metal2.drawing",
    (29, 0): "Via2.drawing",
    (31, 0): "NWell.drawing",
    (44, 0): "ThickGateOx.drawing",
    # Metal3-TopMetal2 (issue #1243, extending the stack past Metal2):
    # layer numbers verified against `layers_def.drc`'s own
    # `get_polygons(layer, datatype)`/`labels(layer, datatype)`
    # declarations (`metal3_drw = get_polygons(30, 0)`, `via3_drw =
    # get_polygons(49, 0)`, `metal4_drw = get_polygons(50, 0)`, `via4_drw =
    # get_polygons(66, 0)`, `metal5_drw = get_polygons(67, 0)`,
    # `topvia1_drw = get_polygons(125, 0)`, `topmetal1_drw =
    # get_polygons(126, 0)`, `topvia2_drw = get_polygons(133, 0)`,
    # `topmetal2_drw = get_polygons(134, 0)`).
    (30, 0): "Metal3.drawing",
    (49, 0): "Via3.drawing",
    (50, 0): "Metal4.drawing",
    (66, 0): "Via4.drawing",
    (67, 0): "Metal5.drawing",
    (125, 0): "TopVia1.drawing",
    (126, 0): "TopMetal1.drawing",
    (133, 0): "TopVia2.drawing",
    (134, 0): "TopMetal2.drawing",
    # Poly-resistor recognition layers (issue #1231) -- names transcribed
    # from the PDK's own KLayout layer-properties file
    # (`libs.tech/klayout/tech/sg13g2.lyp`).
    (128, 0): "PolyRes.drawing",
    (111, 0): "EXTBlock.drawing",
    (14, 0): "pSD.drawing",
    (7, 0): "nSD.drawing",
    (7, 21): "nSD.block",
    (24, 0): "RES.drawing",
    (28, 0): "SalBlock.drawing",
    (8, 25): "Metal1.text",
    (10, 25): "Metal2.text",
    # Metal3-TopMetal2 label/text layers (issue #1243), same
    # `labels(layer, 25)` convention as Metal1.text/Metal2.text above --
    # `layers_def.drc`'s `metal3_text = labels(30, 25)` / `metal4_text =
    # labels(50, 25)` / `metal5_text = labels(67, 25)` / `topmetal1_text =
    # labels(126, 25)` / `topmetal2_text = labels(134, 25)`. TopVia1/TopVia2
    # (cut layers, not conductors) have no text layer of their own, the same
    # "vias carry no label layer" convention Via1/Via2 above establish.
    (30, 25): "Metal3.text",
    (50, 25): "Metal4.text",
    (67, 25): "Metal5.text",
    (126, 25): "TopMetal1.text",
    (134, 25): "TopMetal2.text",
}

# Voltage-domain marker layer this deck draws but does not *fully* model the
# DRC/extraction scoping of (issue #552's `decks.get_unmodeled_voltage_markers`,
# the same mechanism gf180mcu.py registers its own `Dualgate` marker under).
# sg13g2's `general_derivations.lvs` splits every MOS device recognition rule
# into a "-LV" (thin, default gate oxide) and "-HV" (thick gate oxide) pair
# keyed off `thickgateox_drw` (44/0, see `ngate_lv_base`/`ngate_hv_base` in
# that file): `ngate_lv_base = ngate.not(thickgateox_drw)`, `ngate_hv_base =
# ngate.and(thickgateox_drw)` (and the PMOS analogues).
#
# What issue #1231 closed: `EXTRACTION_DECK` below now declares one
# `mos_flavours` entry keyed on this same marker (see `MOSFlavour`'s own
# docstring in `decks/__init__.py`), so a transistor drawn inside ThickGateOx
# is recognised in its own extraction pass and binds the real
# `sg13_hv_nmos`/`sg13_hv_pmos` models under `--pdk` instead of silently
# taking the thin-oxide ("-LV") ones -- and `extract.py`'s own MOS
# `voltage_domain_warnings` stops firing for this marker (it only ever warned
# about that *MOS binding* gap). This mirrors exactly what #1111 did for
# gf180mcu's `Dualgate`.
#
# What is NOT modelled, and is what this entry still warns about: every DRC
# rule in `DECK` above applies the general-case FEOL thresholds regardless of
# ThickGateOx's presence -- sg13g2's own DRM scopes several FEOL rules to the
# thick-oxide domain (e.g. `Gat.a1`/`Gat.a2`'s channel-length-specific GatPoly
# widths, themselves compound `ngate`/`pgate` derivations of GatPoly, Activ,
# NWell *and* this marker -- see the module docstring's "Scope guard"), and
# none of that is transcribed here. So the registry entry stays, with its
# description narrowed to the DRC-rule residue.
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {
    (44, 0): (
        "ThickGateOx (44/0) marks sg13g2's thick-gate-oxide ('-HV') MOS "
        "voltage domain (see general_derivations.lvs's ngate_hv_base/"
        "pgate_hv_base). EXTRACTION_DECK models it for MOS device "
        "recognition/model binding (issue #1231: sg13_hv_nmos/sg13_hv_pmos "
        "under --pdk); this curated deck's DRC rules still apply the "
        "general-case ('-LV') thresholds to geometry regardless of "
        "ThickGateOx's presence (e.g. the Gat.a1/Gat.a2 channel-length-"
        "specific GatPoly widths, not transcribed in this deck yet)."
    ),
}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #
#
# MOS device recognition (issue #905, mirroring sky130.py's/gf180mcu.py's own
# `active`/`poly`/`nwell` "NMOS is active-minus-nwell, PMOS is
# active-and-nwell" idiom -- see `ExtractionDeck`'s own docstring). sg13g2's
# real `mos_extraction.lvs`/`mos_derivations.lvs` instead key NMOS/PMOS off
# an explicit *PWell* region (`ngate`/`pgate` restricted by a compound
# `mos_exclude` boolean derivation, with the NMOS body wired to `pwell`
# rather than a bare "not nwell" complement) -- a compound-layer, PWell-aware
# derivation this engine's plain `active`/`poly`/`nwell` fields cannot
# express (the same class of approximation sky130.py's own NMOS-body
# handling documents: "sky130 draws no separate substrate/pwell layer" and
# falls back to a synthesized `substrate_net` global). This curated deck
# makes the identical choice: NMOS body falls back to `substrate_net`
# (`"vsubs"`, the default) rather than the real drawn PWell region, and no
# `tap` layer is declared -- sg13g2 derives well ties (`ntap`/`ptap` in
# `general_derivations.lvs`) from the *same* Activ layer as transistor
# source/drain (`nactiv`/`pactiv` split by NWell/PWell containment, minus the
# gate), the same "no distinct tap layer, shared with transistor active"
# case gf180mcu's own `tap=None` (`Comp`-only) already documents.
#
# `active`/`poly`/`nwell`/`contact` layer numbers verified against
# `layers_def.drc`'s own `get_polygons(layer, datatype)` declarations
# (`activ_drw = get_polygons(1, 0)`, `gatpoly_drw = get_polygons(5, 0)`,
# `nwell_drw = get_polygons(31, 0)`, `cont_drw = get_polygons(6, 0)`).
#
# `metals`/`vias`: Metal1 (8/0) is the level `contact` (Cont, 6/0) lands
# devices on. Issue #1243 extends the stack past its original Metal1/Via1/
# Metal2 ceiling up through Metal5/TopMetal1/TopMetal2 -- the prerequisite
# #1233's MIM caps (Metal5 bottom plate, TopVia1/TopMetal1 top plate) and
# #1235's metal resistors (`res_metal1`..`res_topmetal2`) both block on (see
# the module docstring's own "MIM capacitors" section, and mirroring
# sky130's own #619 staged extension the module docstring's "Precedent"
# section describes). Via1 (19/0) connects Metal1 to Metal2 (10/0); Via2
# (29/0) connects Metal2 to Metal3 (30/0); Via3 (49/0) connects Metal3 to
# Metal4 (50/0); Via4 (66/0) connects Metal4 to Metal5 (67/0); TopVia1
# (125/0) connects Metal5 to TopMetal1 (126/0); TopVia2 (133/0) connects
# TopMetal1 to TopMetal2 (134/0) -- the same stack this module's DECK above
# now carries DRC coverage for (every level's width/space rules, plus each
# via's own enclosure rule(s)).
# `metal_labels` (8/25, 10/25, 30/25, 50/25, 67/25, 126/25, 134/25) are
# `labels(<n>, 25)` in `layers_def.drc` (`metal1_text`/`metal2_text`/
# `metal3_text`/`metal4_text`/`metal5_text`/`topmetal1_text`/
# `topmetal2_text`) -- genuine KLayout text layers (unlike the `_pin`
# datatype-2 layers in that same file, which are *polygon* pin-shape
# regions, not text -- so sg13g2 has no `well_label`/`poly_label` analogue
# to sky130's datatype-5 `.pin` text-layer convention; both stay `None`, the
# same "this curated deck declines to model a pin text layer it has no
# clean single-layer candidate for" default `ExtractionDeck`'s own
# docstring describes). TopVia1/TopVia2 are cut layers, not conductors, so
# -- like Via1-Via4 -- they carry no `metal_labels` entry of their own.
EXTRACTION_DECK = ExtractionDeck(
    active=(1, 0),  # Activ.drawing
    poly=(5, 0),  # GatPoly.drawing
    nwell=(31, 0),  # NWell.drawing
    contact=(6, 0),  # Cont.drawing
    metals=(
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (30, 0),  # Metal3.drawing
        (50, 0),  # Metal4.drawing
        (67, 0),  # Metal5.drawing
        (126, 0),  # TopMetal1.drawing
        (134, 0),  # TopMetal2.drawing
    ),
    vias=(
        (19, 0),  # Via1.drawing (Metal1 <-> Metal2)
        (29, 0),  # Via2.drawing (Metal2 <-> Metal3)
        (49, 0),  # Via3.drawing (Metal3 <-> Metal4)
        (66, 0),  # Via4.drawing (Metal4 <-> Metal5)
        (125, 0),  # TopVia1.drawing (Metal5 <-> TopMetal1)
        (133, 0),  # TopVia2.drawing (TopMetal1 <-> TopMetal2)
    ),
    metal_labels=(
        (8, 25),  # Metal1.text
        (10, 25),  # Metal2.text
        (30, 25),  # Metal3.text
        (50, 25),  # Metal4.text
        (67, 25),  # Metal5.text
        (126, 25),  # TopMetal1.text
        (134, 25),  # TopMetal2.text
    ),
    tap=None,
    well_label=None,
    poly_label=None,
    nfet_class="nfet",
    pfet_class="pfet",
    substrate_net="vsubs",
    # MOS device-recognition provenance (issue #905, mirroring sky130.py's
    # issue #868 citation shape): `mos_extraction.lvs`'s thin-oxide ("-LV")
    # devices --
    #   extract_devices(mos4('sg13_lv_nmos'),
    #     { 'SD' => nsd_fet, 'G' => ngate_lv, 'tS' => nsd_fet,
    #       'tD' => nsd_fet, 'tG' => poly_con, 'W' => pwell })
    #   extract_devices(mos4('sg13_lv_pmos'),
    #     { 'SD' => psd_fet, 'G' => pgate_lv, 'tS' => psd_fet,
    #       'tD' => psd_fet, 'tG' => poly_con, 'W' => nwell_drw })
    # These stay the *default* pair, cited for every transistor drawn outside
    # ThickGateOx; the thick-oxide ("-HV") pair is the `mos_flavours` entry
    # below (issue #1231).
    nfet_provenance=_sg13g2_lvs_provenance("mos_extraction.lvs", "sg13_lv_nmos"),
    pfet_provenance=_sg13g2_lvs_provenance("mos_extraction.lvs", "sg13_lv_pmos"),
    # Thick-oxide ("-HV") MOS flavour (issue #1231, the mechanism issue #1111
    # added for gf180mcu's `Dualgate`): a transistor whose Activ island
    # overlaps ThickGateOx (44/0) is sg13g2's thick-gate-oxide device, whose
    # real upstream device-class names are `sg13_hv_nmos`/`sg13_hv_pmos` --
    #   extract_devices(mos4('sg13_hv_nmos'), { ..., 'G' => ngate_hv, ... })
    #   extract_devices(mos4('sg13_hv_pmos'), { ..., 'G' => pgate_hv, ... })
    # in the same `mos_extraction.lvs`, keyed off `general_derivations.lvs`'s
    # `ngate_hv_base = ngate.and(thickgateox_drw)` / `pgate_hv_base` (and,
    # symmetrically, `ngate_lv_base = ngate.not(thickgateox_drw)` -- so the
    # default pair above is genuinely the *complement* of this flavour, which
    # is exactly the split `MOSFlavour` implements).
    # `MOSFlavour.flavour="hv"` is the key `pdk_models.py`'s
    # `_MOS_MODEL_FLAVOURS[("sg13g2", "sg13g2")]` table binds to those two
    # real subcircuit names under `--pdk` (`.subckt sg13_hv_nmos d g s b` in
    # `libs.tech/ngspice/models/sg13g2_moshv_mod.lib` -- see that module's
    # own verified-provenance note).
    mos_flavours=(
        MOSFlavour(
            marker=(44, 0),  # ThickGateOx.drawing
            flavour="hv",
            description="sg13g2 thick-gate-oxide domain (ThickGateOx 44/0)",
            nfet_provenance=_sg13g2_lvs_provenance(
                "mos_extraction.lvs", "sg13_hv_nmos"
            ),
            pfet_provenance=_sg13g2_lvs_provenance(
                "mos_extraction.lvs", "sg13_hv_pmos"
            ),
        ),
    ),
    # Drawn precision poly resistors (issue #1231), transcribed from
    # `res_derivations.lvs`/`res_extraction.lvs`/`res_connections.lvs`:
    #
    #   polyres_mk = polyres_drw.and(extblock_drw).interacting(gatpoly)
    #                  .not(polyres_exclude)
    #   rsil_res   = polyres_mk.and(res_drw)
    #                  .not(psd_drw.join(salblock_drw).join(nsd_drw)
    #                       .join(nsd_block))
    #   rppd_res   = polyres_mk.and(psd_drw).and(salblock_drw)
    #                  .not(nsd_block).not(nsd_drw)
    #   extract_devices(GeneralNTerminalExtractor.new('rsil', 2),
    #     { 'core' => rsil_res, 'ports' => rsil_ports, ... })   (ditto rppd)
    #
    # Mapping onto this engine's `ResistorDevice` model (`body & marker &
    # requires - excludes`, terminals = `body - segment`): `body` is GatPoly,
    # `marker` is `polyres` (128/0), and each flavour's distinguishing
    # implant/block masks become `requires`/`excludes`. This reproduces
    # upstream's own terminal derivation exactly -- `rsil_ports`/`rppd_ports`
    # are `gatpoly.interacting(core).not(core)`, i.e. the drawn poly heads on
    # either side of the marked segment, which is what `terminal=None`
    # (default: `body` minus the segment) already yields.
    #
    # Documented approximations, in this deck's usual "known-unmodelled beats
    # silently wrong" style:
    #
    # - Upstream's `core` is the *marker* region (`polyres & extblock`, merely
    #   `interacting` GatPoly); this engine intersects it with the body layer.
    #   For a real device cell -- where `polyres` is drawn coincident with the
    #   poly bar it marks -- the two are the same region.
    # - `polyres_exclude` is a 14-layer join. Only its two members this deck
    #   otherwise declares layers for are subtracted below (`Activ`, so a
    #   marked *gate* is never mistaken for a resistor -- the same guard
    #   gf180mcu's own `excludes=((22, 0), ...)` Comp term provides -- and
    #   `ThickGateOx`); the rest (pwell_block, nBuLay, TRANS, EmWind, EmWiHV,
    #   Activ_mask, RecogDiode, RecogESD, Ind, Ind_pin, Substrate) are layers
    #   this curated deck does not model at all, and are not transcribed.
    # - Upstream additionally connects each device's `*_sub` region (the
    #   segment sized by 5 nm) to `pwell`/`iso_pwell`/`nwell_drw`
    #   (`res_connections.lvs`), i.e. a real bulk terminal. `bulk_to_substrate`
    #   below wires the equivalent `W` terminal to this deck's `substrate_net`
    #   global, the same fallback its NMOS body already uses (this deck models
    #   no drawn PWell -- see the MOS note above).
    # - The PDK's own resistance model is more than sheet-rho: `sg13g2_tech
    #   .json`'s `CbResCalc` composes `l/weff*(b+1)*rspec + ... + 2/w*rzspec`
    #   with a per-flavour line-width delta (`*_lwd`) and a width-dependent
    #   contact/transition term (`*_rzspec`). Neither is expressible in this
    #   engine's `sheet_rho_ohm_sq` (+ optional fixed `fixed_offset_ohm`,
    #   which is *not* width-dependent), so `R = L/W * rspec` is a
    #   first-order transcription of the body term only -- stated here rather
    #   than silently implied.
    # - `rhigh` (the third poly-resistor flavour) is deliberately **not**
    #   declared: its sheet resistance is ambiguous in the PDK's own data --
    #   `rhigh_code.py` reads `rhigh_rspec` (1300 ohm/sq, with its `G2`-suffix
    #   branch commented out) while the shared `CbResCalc` helper that
    #   computes the same PyCell's default resistance hardcodes the `G2`
    #   suffix and so reads `rhighG2_rspec` (1360 ohm/sq). A segment this deck
    #   cannot value confidently is left as ordinary connected poly (today's
    #   short) rather than extracted with one of two contradictory
    #   coefficients.
    resistors=(
        ResistorDevice(
            name="rsil",  # upstream LVS device-class name
            body=(5, 0),  # GatPoly.drawing
            marker=(128, 0),  # polyres.drawing
            # `rsilG2_rspec` in sg13g2_tech.json (`techName == "SG13G2"`;
            # `rsil_code.py` reads the `G2` key unconditionally, and the
            # non-G2 key carries the same 7.0 value).
            sheet_rho_ohm_sq=7.0,
            requires=(
                (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
                (24, 0),  # Res      -- the silicided-resistor marker itself
            ),
            excludes=(
                (14, 0),  # pSD       -\
                (28, 0),  # SalBlock   |- upstream's `rsil_exc`
                (7, 0),  # nSD        |
                (7, 21),  # nSD_block -/
                (1, 0),  # Activ       -\ `polyres_exclude` (a marked gate is
                (44, 0),  # ThickGateOx -/ a transistor, not a resistor)
            ),
            bulk_to_substrate=True,  # upstream connects `rsil_sub` to pwell
            provenance=_sg13g2_lvs_provenance("res_extraction.lvs", "rsil"),
        ),
        ResistorDevice(
            name="rppd",  # upstream LVS device-class name
            body=(5, 0),  # GatPoly.drawing
            marker=(128, 0),  # polyres.drawing
            # `rppdG2_rspec` in sg13g2_tech.json -- `rppd_code.py` selects the
            # `G2` suffix for `techName == "SG13G2"` (260.0, vs the non-G2
            # 250.0 of the older SG13 flavour).
            sheet_rho_ohm_sq=260.0,
            requires=(
                (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
                (14, 0),  # pSD      -- p+ doped poly
                (28, 0),  # SalBlock -- unsalicided (vs. rsil's 7 ohm/sq)
            ),
            excludes=(
                (7, 0),  # nSD        -\ upstream's own `.not(nsd_drw)`/
                (7, 21),  # nSD_block -/ `.not(nsd_block)` (-> rhigh)
                (1, 0),  # Activ       -\ `polyres_exclude`, see rsil above
                (44, 0),  # ThickGateOx -/
            ),
            bulk_to_substrate=True,  # upstream connects `rppd_sub` to pwell
            provenance=_sg13g2_lvs_provenance("res_extraction.lvs", "rppd"),
        ),
    ),
)

# `klt extract --parasitics` RC coefficients (issue #216's addendum,
# `ParasiticsDeck`) are **not curated for sg13g2 in this issue** -- Epic
# #711 Phase 3b's own acceptance criteria scope this deck to DRC geometric
# rules and LVS device-extraction rules only, mirroring gf180mcu's own
# original (pre-parasitics-curation) state. An empty `ParasiticsDeck()`
# registers cleanly (rather than leaving `sg13g2` entirely absent from
# `get_parasitics_deck`, which would raise `UnknownExtractionDeckError` --
# a *different*, misleading failure mode than "no RC coefficients curated
# yet" for a deck that otherwise fully supports `klt extract`/`klt lvs`):
# `klt extract --deck sg13g2 --parasitics` runs cleanly and reports every
# conductor role as an uncalibrated gap, exactly as an unpopulated
# `LayerRC` field already does for any deck (see
# `klayout_tools.extract._describe_parasitics_overlap_gaps` and
# `ParasiticsDeck`'s own docstring).
PARASITICS = ParasiticsDeck()
