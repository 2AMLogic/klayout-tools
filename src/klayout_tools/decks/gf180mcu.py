"""gf180mcu DRC deck: a curated subset of the official rule set.

Rule thresholds below are transcribed from the published GlobalFoundries
180nm MCU ("gf180mcu") **Design Rule Manual** (DRM), fetched directly from
the canonical source repository for this issue:

- https://github.com/google/gf180mcu-pdk (Apache License 2.0), commit
  ``de3240d`` (2023-05-31) — the DRM lives at
  ``docs/physical_verification/design_manual/`` as reStructuredText
  sections with the numeric rule tables as CSV (``tables_clear/*.csv``).
  Sections/tables used below:

  - ``drm_07_05.rst`` / ``tables_clear/13_Nwell31.csv`` — 7.4 Nwell (``NW.*``)
  - ``drm_07_06.rst`` / ``tables_clear/14_COMP33_1.csv`` — 7.5 Comp (``DF.*``)
  - ``drm_07_08.rst`` / ``tables_clear/16_Poly2_42.csv`` — 7.7 Poly2 (``PL.*``)
  - ``drm_07_13.rst`` / ``tables_clear/21_Contact_56.csv`` — 7.12 Contact (``CO.*``)
  - ``drm_07_14.rst`` / ``tables_clear/22_Metaln_58.csv`` — 7.13 Metaln (``Mn.*``,
    ``n = 1 to 5``, i.e. ``Metal1``-``Metal5``)
  - ``drm_07_16.rst`` / ``tables_clear/24_MetalTop_61.csv`` — 7.15 MetalTop
    (``MT.*``) — **not** part of the 7.13 Metaln table above: ``MetalTop`` is
    a separate drawn layer with its own DRM section and its own rule ids/
    values (``MT.1``/``MT.2a``), not an ``n = 6`` row of ``Mn.*``.
  - ``drm_10_4_2.rst`` / ``tables_clear/35_MIM2_88.csv`` — 10.4.2 MIM
    (Metal-insulator-Metal) Capacitor, Option B (``MIMTM.*``), from the
    "10.4 MIM Capacitor" section (itself under the "10.0 Analog Device
    Related Rules" chapter)
  - ``drm_10_07.rst`` / ``tables_clear/38_DRC_BJT_103.csv`` — 10.7 DRC_BJT Mark
    Layer (``BJT.*``), from the "10.0 Analog Device Related Rules" chapter —
    the DRM's vertical NPN/PNP bipolar rule category.

Each rule's docstring below cites the exact source rule id (e.g. ``"DF.1a"``)
and its DRM description, so values can be re-verified against a fresh
checkout of that repo at any time. Unless noted otherwise, the **3.3V
column** value is used (this deck does not model the 5V/6V high-voltage
variants).

Provenance note (why this deck isn't transcribed from a ``.lydrc`` script
the way ``sky130.py`` was): the companion KLayout-runnable DRC deck lives in
https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv (also
Apache-2.0; commit ``999a6ff``, 2023-06-11), under
``klayout/drc/rule_decks/``. As open-sourced at that commit, ``main.drc``
only defines layer/connectivity setup (confirming the layer/datatype pairs
used below) and the per-feature rule files in that directory
(``drc_bjt.drc``, ``dualgate.drc``, ``hres.drc``, ``mcell.drc``, etc.) cover
specialised devices, not the core FEOL/BEOL width/space/enclosure checks
this deck curates — those aren't present as executable ``.output(...)``
statements in this snapshot of that repo. So, unlike sky130's rule ids
(transcribed verbatim from live ``.lydrc`` script lines), the values here
are transcribed from the DRM's published numeric tables instead, citing the
DRM's own rule ids (which the ``.lydrc`` deck would use for its own
``.output(...)`` calls once/if implemented).

This is *not* a full transcription of the official rule manual (hundreds of
rules across dozens of layers, plus 5V/6V variants, DFM guidelines, etc.) —
mirrors the "curated starter subset" scope guard sky130 documents:
width/space/enclosure checks across poly2/comp/contact/metal1 (extended, per
#188, to metal2/metal3/metal5/metaltop and the MiM capacitor stack), plus a
first increment of well/substrate-tap coverage (Nwell) and one bipolar
(BJT)-specific device rule, wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) for a second PDK. Coverage is
expected to grow incrementally in follow-on issues (e.g. Pplus/Nplus
implant-specific rules, LVPWELL/DNWELL, the remaining BJT rules that key off
DNWELL/LVPWELL, 5V/6V variants, DFM guidelines, and ``MIMTM.2``'s
sized/derived-layer need — see its own note below).

``klt extract``'s MiM-capacitor device recognition (``EXTRACTION_DECK.capacitors``
below, issue #225) is transcribed from a *different*, LVS-specific source
than the DRC rules above: the DRM's "10.4 MIM Capacitor" section publishes
only the DRC-checkable geometry rules (``MIMTM.*``, above), not a
device-recognition derivation or a capacitance-per-area coefficient. Both
come from ``globalfoundries-pdk-libs-gf180mcu_fd_pv``'s companion **KLayout
LVS** deck (a sibling of the DRC deck cited above, under ``klayout/lvs/``
rather than ``klayout/drc/``) — unlike the DRC deck, this was not present in
the ``999a6ff`` DRC-only snapshot cited above, so it is instead cited from a
real fetched PDK install: ``volare enable gf180mcu
c6d73a35f524070e85faff4a6a9eef49553ebc2b`` (the same commit ``open_pdks``
itself; its ``gf180mcu/Makefile.in`` copies ``${GF180MCU_PV_PATH}/klayout/lvs/*``
from that verification-library fetch), giving
``libs.tech/klayout/lvs/rule_decks/mimcap_derivations.lvs`` (the "virtual
bottom plate" geometry derivation), ``mimcap_extraction.lvs`` (the
``extract_devices(capacitor(name, area_cap, class), {...})`` call and its
``2.0e-15`` F/um² coefficient) and ``layers_definitions.lvs`` (the
``topmin1_metal``/``CAP_MK``/``mim_l_mk`` layer roles). Cross-checked
against two more independent sources in the same open_pdks build:
``gf180mcu-pdk``'s own ``docs/analog/model_parameters/LV/tables_clear/08_MIM.csv``
(a device-model table publishing the same three selectable MiM densities,
"1.0fF/um2 MIM" / "1.5fF/um2 MIM" / "2.0fF/um2 MIM"), and ``open_pdks``'
own ``gf180mcu/gf180mcu.json`` PDK-variant description string, which
literally reads ``"Global Foundries 0.18um MCU CMOS, 2fF MiM + 1k high
sheet rho poly"`` for this built variant (confirming ``2.0`` fF/um² -- not
``1.0``/``1.5`` -- is the one this specific PDK build ships).

Eight rules below approximate the official DRM rule in some way (each is
called out again in its own docstring below); the threshold *values* used
are always the real, unmodified DRM values:

- ``comp.space.1``: the official ``DF.3a`` text scopes to a substrate-tap
  butting context; approximated as a general COMP-to-COMP spacing check on
  the whole ``comp`` drawn layer.
- ``poly2.space.1``: the official ``PL.3a`` splits into "space on COMP" vs.
  "space on field" sub-cases (context our engine can't distinguish without
  a boolean layer expression); both sub-cases share the same 3.3V value
  (0.24um), so no threshold change is needed to unify them.
- ``poly2.width.1``: the official deck holds gate poly2 (``poly2 AND comp``,
  ``PL.2``, 0.28um channel length) to a stricter minimum than general
  interconnect poly2 (``PL.1``, 0.18um); approximated using the more
  permissive ``PL.1`` value across the whole ``poly2`` drawn layer, since
  isolating gate poly2 requires the same compound layer expression our
  engine does not evaluate.
- ``contact.width.1``: ``CO.1`` specifies contact as a fixed 0.22 x 0.22um
  square (a min **and** max bound); our ``width_check`` primitive only
  supports a minimum-width lower bound, so only the min half of the rule is
  enforced here.
- ``nwell.space.1``: the official ``NW.2a``/``NW.2b`` split Nwell-to-Nwell
  spacing by net-potential context (``NW.2a`` 0.6um for equipotential wells
  that may later be merged, ``NW.2b`` 1.4um for wells at different
  potentials); our engine has no connectivity/netlist information, only
  geometry, so it cannot distinguish the two contexts. Approximated using
  the less strict ``NW.2a`` (equipotential) value across the whole ``Nwell``
  drawn layer — this means two Nwell shapes at genuinely different
  potentials, spaced between 0.6um and 1.4um apart, will **not** be flagged
  even though the real DRM's ``NW.2b`` would flag them.
- ``nwell.enclosing.comp.1``: the official ``DF.4d`` scopes to NCOMP (the
  ``comp`` drawn layer where it overlaps ``Nplus``, i.e. specifically an
  Nwell tap) rather than all of ``comp``; approximated as Nwell enclosing
  the whole ``comp`` drawn layer, since isolating NCOMP requires the same
  boolean layer expression (``comp AND Nplus``) our engine does not
  evaluate.
- ``bjt.separation.comp.1``: the official ``BJT.3`` scopes to COMP
  "unrelated" to the BJT device (i.e. excludes COMP that is itself part of
  the same bipolar device, a connectivity/netlist notion); our engine has no
  connectivity information, only geometry, so it is approximated as a
  separation check against every ``comp`` shape on the layout, which may
  over-flag COMP that is legitimately part of the same BJT structure.
- ``mim.space.1``: the official ``MIMTM.1`` scopes to the MiM capacitor's
  "virtual bottom plate" (per the DRM's own note: ``FuseTop`` sized/oversized
  by 1.06um, intersected with ``Metal4``) versus nearby bottom-plate-or-
  routing ``Metal4``; approximated as a general ``Metal4``-to-``Metal4``
  spacing check across the whole drawn layer, since isolating the virtual
  bottom plate needs a sized/derived-layer primitive our engine does not
  have (see the ``MIMTM.2`` note below). This means ordinary ``Metal4``
  routing traces spaced between the deck's generic metal-spacing range and
  1.2um apart -- nothing to do with a MiM capacitor -- will be flagged even
  though they violate no real DRM rule. Threshold value unmodified.

``MIMTM.2`` ("min. MiM bottom-plate overlap of ``Via4``", 0.4um) is
**deliberately not transcribed** in this deck. Like ``MIMTM.1`` above, it is
keyed off the same "virtual bottom plate" derived layer (``FuseTop`` sized
by 1.06um AND ``Metal4`` intersecting ``FuseTop``) -- but here an *unscoped*
approximation would be actively wrong, not merely conservative: ordinary
``Metal4``-to-``Metal5`` routing vias use a much smaller enclosure than a
MiM cap's virtual bottom plate requires, so a blanket "``Metal4`` must
enclose every ``Via4`` by 0.4um" check would flag legitimate routing vias
throughout *any* layout using ``Metal4``/``Via4``/``Metal5`` at all, not
just genuine MiM structures -- unlike ``mim.space.1``'s over-flagging
(spurious, but at least confined to the already-narrow ``Metal4`` layer),
this one would make ordinary interconnect unusable. Implementing it
correctly needs a sized/derived-layer check primitive
:class:`~klayout_tools.decks.DrcRule` (``klayout_tools/decks/__init__.py``)
does not support today (only ``width``/``space``/``notch``/``separation``/
``enclosing``/``enclosed``/``overlap``, all single- or two-layer, none
derived/sized) -- tracked as a follow-on issue naming that missing
primitive as the blocker, per #188's own acceptance criteria.

The DRM's "10.4 MIM Capacitor" section defines two mutually-exclusive
process options (a PDK is wired for one or the other, never both): Option A
(``MIM.*``, bottom plate on ``Metal2``, for a 3-metal-layer process variant)
and Option B (``MIMTM.*`` above, bottom plate on ``Metal(n-1)`` of an
n-metal stack, for a 4-or-more-metal-layer process variant). This deck's
``Metal1``-``Metal5`` (``Mn.*``) coverage models the 5-metal-layer gf180mcu
variant -- confirmed by ``globalfoundries-pdk-libs-gf180mcu_fd_pv``'s
``main.drc``, whose connectivity (``topmin1_metal = metal4`` /
``top_via = via4`` when ``top_metal = metal5``) matches Option B's bottom
plate landing on ``Metal4`` for that stack, the same stack the issue's own
reproducer layout uses (``Metal4``/``FuseTop``/``Via4``/``Metal5``) -- so
only Option B (``MIMTM.*``) is transcribed above; Option A's ``MIM.*`` rule
set targets a different (3-metal-layer) process variant this deck does not
model and is not duplicated here.

Layer numbers (verified against ``google/globalfoundries-pdk-libs-gf180mcu_fd_pv``'s
``klayout/drc/rule_decks/main.drc`` layer derivations, e.g. ``comp =
get_polygons(22, 0)``, cross-checked against this repo's own gf180mcu
corpus fixtures in ``tests/corpus/golden/gf180mcu/*.layers.json`` where
present, and (for layers absent from both -- ``DRC_BJT``, and the
metal2-metal5/metaltop/MiM-stack layers added by #188) against the DRM's own
``tables_clear/06_Drawn_layer10.csv`` drawn-layer/GDS-number table; the
#188 additions are *also* independently confirmed present in ``main.drc``
itself, e.g. ``metaltop_drawn = get_polygons(53, 0)`` and ``mim_l_mk =
get_polygons(117, 10)``. The ``Via1``/``Via2``/``Via3`` numbers added by #220
(to populate ``EXTRACTION_DECK``'s inter-metal connectivity) come from the
same ``main.drc`` via-layer derivations -- ``via1 = get_polygons(35, 0)``,
``via2 = get_polygons(38, 0)``, ``via3 = get_polygons(40, 0)`` -- which sit
alongside the already-transcribed ``via4 = get_polygons(41, 0)``):

    Nwell     21/0
    Comp      22/0
    Pplus     31/0
    Nplus     32/0
    Poly2     30/0
    Contact   33/0
    Metal1    34/0
    Via1      35/0
    Metal2    36/0
    Via2      38/0
    Metal3    42/0
    Via3      40/0
    Metal4    46/0
    Via4      41/0
    Metal5    81/0
    MetalTop  53/0
    FuseTop   75/0
    Dualgate  55/0
    DRC_BJT   127/5
    MIM_L_MK  117/10
"""

from __future__ import annotations

from . import (
    BipolarDevice,
    CapacitorDevice,
    DrcRule,
    ExtractionDeck,
    LayerRC,
    ParasiticsDeck,
)

# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001, same as sky130), so a threshold in micrometres
# times 1000 gives threshold_dbu. `run_drc()` rescales threshold_dbu by
# NOMINAL_DBU_UM / layout.dbu at run time, so the deck still gives correct
# results against a layout written at a different dbu (see DrcRule's
# docstring).
NOMINAL_DBU_UM = 0.001

DECK: list[DrcRule] = [
    DrcRule(
        id="poly2.width.1",
        description="minimum poly2 interconnect width (3.3V)",
        layer=(30, 0),  # Poly2
        check="width",
        threshold_dbu=180,  # 0.18 um
        # DRM 7.7 Poly2, rule "PL.1": "Interconnect Width (outside PLFUSE)"
        # -> 0.18 (3.3V column). Approximation: the DRM holds gate poly2
        # (poly2 AND comp, rule "PL.2") to a stricter 0.28um minimum; using
        # the more permissive PL.1 value across the whole poly2 layer since
        # isolating gate poly2 needs a boolean layer expression.
    ),
    DrcRule(
        id="poly2.space.1",
        description="minimum poly2 spacing (3.3V)",
        layer=(30, 0),  # Poly2
        check="space",
        threshold_dbu=240,  # 0.24 um
        # DRM 7.7 Poly2, rule "PL.3a": "Space on COMP / Space on Field" ->
        # both sub-cases are 0.24 (3.3V column). Approximation: the DRM
        # splits this by context (poly2 over COMP vs. over field oxide);
        # unified into one check since both values coincide.
    ),
    DrcRule(
        id="poly2.enclosing.contact.1",
        description="minimum poly2 overlap of contact",
        layer=(30, 0),  # Poly2
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.3": "Poly2 overlap of contact" -> 0.07um
    ),
    DrcRule(
        id="comp.width.1",
        description="minimum COMP (diffusion/active) width (3.3V)",
        layer=(22, 0),  # Comp
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.5 Comp, rule "DF.1a": "Min. COMP Width" -> 0.22 (3.3V column)
    ),
    DrcRule(
        id="comp.space.1",
        description="minimum COMP (diffusion/active) spacing (3.3V)",
        layer=(22, 0),  # Comp
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.5 Comp, rule "DF.3a": "Min. COMP Space. P-substrate tap
        # (PCOMP outside NWELL and DNWELL) can be butted for different
        # voltage devices..." -> 0.28 (3.3V column). Approximation: the DRM
        # text scopes this to a substrate-tap butting context; approximated
        # here as a general COMP-to-COMP spacing check on the whole drawn
        # layer, since isolating that context needs a boolean layer
        # expression (comp minus well/tap markers). Threshold value
        # unmodified.
    ),
    DrcRule(
        id="comp.enclosing.contact.1",
        description="minimum COMP overlap of contact",
        layer=(22, 0),  # Comp
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.4": "COMP overlap of contact" -> 0.07um
    ),
    DrcRule(
        id="contact.width.1",
        description="minimum contact size (approximates official min/max size rule)",
        layer=(33, 0),  # Contact
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.12 Contact, rule "CO.1": "Min/max contact size" -> 0.22um
        # (contacts are a fixed 0.22 x 0.22um square in the real deck).
        # Approximation: our width_check enforces only a minimum-width
        # lower bound; the "max" (fixed-size) half of the rule is not
        # checked. Threshold value unmodified.
    ),
    DrcRule(
        id="contact.space.1",
        description="minimum contact spacing",
        layer=(33, 0),  # Contact
        check="space",
        threshold_dbu=250,  # 0.25 um
        # DRM 7.12 Contact, rule "CO.2a": "Space" -> 0.25um
    ),
    DrcRule(
        id="metal1.width.1",
        description="minimum metal1 width",
        layer=(34, 0),  # Metal1
        check="width",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.23 (n = 1)
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum metal1 spacing",
        layer=(34, 0),  # Metal1
        check="space",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.23 (n = 1)
    ),
    DrcRule(
        id="metal2.width.1",
        description="minimum metal2 width",
        layer=(36, 0),  # Metal2
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum metal2 spacing",
        layer=(36, 0),  # Metal2
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metal3.width.1",
        description="minimum metal3 width",
        layer=(42, 0),  # Metal3
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metal3.space.1",
        description="minimum metal3 spacing",
        layer=(42, 0),  # Metal3
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metal5.width.1",
        description="minimum metal5 width",
        layer=(81, 0),  # Metal5
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metal5.space.1",
        description="minimum metal5 spacing",
        layer=(81, 0),  # Metal5
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
    ),
    DrcRule(
        id="metaltop.width.1",
        description="minimum metaltop width (standard 6K angstrom thickness)",
        layer=(53, 0),  # MetalTop
        check="width",
        threshold_dbu=360,  # 0.36 um
        # DRM 7.15 MetalTop, rule "MT.1": "Width" -> 0.36 (standard/unstarred
        # value; the DRM's "0.36/0.44*" lists a second value for the 9K/11K
        # angstrom MetalTop thickness option, which -- like the 5V/6V variant
        # elsewhere in this deck -- is not modeled here).
    ),
    DrcRule(
        id="metaltop.space.1",
        description="minimum metaltop spacing (standard 6K angstrom thickness)",
        layer=(53, 0),  # MetalTop
        check="space",
        threshold_dbu=380,  # 0.38 um
        # DRM 7.15 MetalTop, rule "MT.2a": "Space" -> 0.38 (standard/
        # unstarred value; see metaltop.width.1's note on the thickness
        # option not modeled here).
    ),
    DrcRule(
        id="mim.space.1",
        description=(
            "minimum MiM bottom-plate (metal4) spacing to bottom-plate "
            "metal (approximated as general metal4-to-metal4 spacing)"
        ),
        layer=(46, 0),  # Metal4
        check="space",
        threshold_dbu=1200,  # 1.2 um
        # DRM 10.4.2 MIM Option B, rule "MIMTM.1": "Minimum MiM bottom plate
        # spacing to the bottom plate metal (whether adjacent MiM or routing
        # metal)" -> 1.2um. Approximation: see the module docstring's
        # `mim.space.1` note above (virtual-bottom-plate context our engine
        # can't isolate; over-flags ordinary Metal4 routing). Threshold value
        # unmodified.
    ),
    DrcRule(
        id="mim.enclosing.fusetop.1",
        description="minimum MiM bottom plate (metal4) overlap of top plate (fusetop)",
        layer=(46, 0),  # Metal4
        other_layer=(75, 0),  # FuseTop
        check="enclosing",
        threshold_dbu=600,  # 0.6 um
        # DRM 10.4.2 MIM Option B, rule "MIMTM.3": "Minimum MiM bottom plate
        # overlap of Top plate" -> 0.6um.
    ),
    DrcRule(
        id="nwell.space.1",
        description="minimum Nwell spacing (equipotential, 3.3V)",
        layer=(21, 0),  # Nwell
        check="space",
        threshold_dbu=600,  # 0.6 um
        # DRM 7.4 Nwell, rule "NW.2a": "Min. Nwell Space (Outside DNWELL)
        # [Equi-potential], Merge if the space is less than" -> 0.6 (3.3V
        # column). Approximation: the DRM splits this by net-potential
        # context ("NW.2a" equipotential 0.6um vs. "NW.2b" different-
        # potential 1.4um); our engine has no connectivity/netlist
        # information, so this uses the less strict "NW.2a" value across the
        # whole Nwell drawn layer. Threshold value unmodified.
    ),
    DrcRule(
        id="nwell.enclosing.comp.1",
        description="minimum Nwell enclosure of an Nwell tap (COMP), 3.3V",
        layer=(21, 0),  # Nwell
        other_layer=(22, 0),  # Comp
        check="enclosing",
        threshold_dbu=120,  # 0.12 um
        # DRM 7.5 Comp, rule "DF.4d": "Min. (Nwell overlap of NCOMP) outside
        # DNWELL" -> 0.12 (3.3V column). Approximation: the DRM scopes this
        # to NCOMP (comp AND Nplus, i.e. specifically an Nwell substrate tap)
        # rather than all of comp; approximated as Nwell enclosing the whole
        # comp drawn layer, since isolating NCOMP requires the same boolean
        # layer expression our engine does not evaluate. Threshold value
        # unmodified.
    ),
    DrcRule(
        id="bjt.separation.comp.1",
        description="minimum space of DRC_BJT (bipolar device mark layer) to COMP",
        layer=(127, 5),  # DRC_BJT
        other_layer=(22, 0),  # Comp
        check="separation",
        threshold_dbu=100,  # 0.1 um
        # DRM 10.7 DRC_BJT Mark Layer, rule "BJT.3": "Minimum space of
        # DRC_BJT layer to unrelated COMP" -> 0.1um. Approximation: the DRM
        # scopes this to COMP "unrelated" to the BJT device (a connectivity
        # notion); our engine has no connectivity information, so this
        # checks against every comp shape, which may over-flag COMP that is
        # legitimately part of the same BJT structure. Threshold value
        # unmodified.
        #
        # Follow-up (issue #223): `DRC_BJT` is now *also* consumed for
        # device-recognition purposes (not just this DRC mark/separation
        # check) -- see `EXTRACTION_DECK.bipolars` below, which uses it as
        # the `marker` layer of a `klt extract` bipolar device-recognition
        # entry.
    ),
]

# (layer, datatype) -> upstream layer name, from gf180mcu.lyp's layer-map
# (google/globalfoundries-pdk-libs-gf180mcu_fd_pv), used only to render
# violations[].layer as e.g. "Poly2" instead of the bare "30/0" fallback.
# Unlike sky130.lyt, gf180mcu's layer map does not use "name.purpose" pairs,
# so names here are the bare upstream layer names (no ".drawing" suffix).
LAYER_NAMES: dict[tuple[int, int], str] = {
    (21, 0): "Nwell",
    (22, 0): "Comp",
    (30, 0): "Poly2",
    (31, 0): "Pplus",
    (32, 0): "Nplus",
    (33, 0): "Contact",
    (34, 0): "Metal1",
    (36, 0): "Metal2",
    (42, 0): "Metal3",
    (46, 0): "Metal4",
    (81, 0): "Metal5",
    (53, 0): "MetalTop",
    (75, 0): "FuseTop",
    (55, 0): "Dualgate",
    (127, 5): "DRC_BJT",
    (117, 5): "CAP_MK",
    (117, 10): "MIM_L_MK",
    (80, 5): "efuse_mk",
    (125, 5): "plfuse",
}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #
#
# Metal1's pin/label purpose (34/10) is not in LAYER_NAMES above (that table
# only names *drawn* layers); verified against this repo's own gf180mcu
# corpus fixtures (`tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__*.gds`),
# which label every signal and power pin directly on 34/10 (e.g. "I", "ZN",
# "VDD", "VSS").
#
# `poly_label` (30/10) applies that same pin/label purpose (datatype 10) to
# the Poly2 layer, mirroring `well_label`'s pattern on sky130: a text on poly
# names the poly net directly, so a device gate `klt gen` draws as bare poly
# (no contact/metal landing pad -- see `gen.py`'s `_mos_unit_layout`) can be
# promoted to a named `.SUBCKT` pin by `klt gen-compose`'s `pins[]` (#210).
# Like sky130's 66/5, this has no corpus precedent (corpus cells route gates
# in metal) -- a curated choice consistent with this deck's datatype-10 label
# convention.
#
# Unlike sky130, gf180mcu's curated layer set has no distinct substrate/well
# tie layer -- a well tap is drawn on the *same* `Comp` layer as transistor
# active (`tap=None` below). This deck therefore does not attempt to derive
# a well-tie net name: the PMOS body terminal picks up whatever net (if any)
# a Comp/contact/metal shape *inside* the Nwell polygon happens to be tied
# to via ordinary connectivity (see `ExtractionDeck`'s docstring on why
# `nwell` is never connected to `contact` directly), and is otherwise a
# floating, anonymous net in the extracted netlist -- a documented
# approximation for this PDK's curated deck (no `well_label`/`tap` fields
# are set), not a real substrate-tap extraction.
#
# NMOS body: as with sky130, no separate substrate layer is drawn in this
# curated deck, so the NMOS body terminal is tied to the deck's
# `substrate_net` global (`ExtractionDeck.substrate_net`, default `"vsubs"`).
#
# Bipolar (BJT) device recognition (issue #223): base = Nwell (the physical
# n-type body a vertical bipolar's emitter diffusion sits in -- the same
# layer this deck's `nwell` MOS-recognition role already uses), emitter =
# Comp (the same diffusion mask an ordinary PMOS source/drain uses; this
# curated deck does not model Nplus/Pplus implants, so Comp alone cannot
# distinguish device polarity -- see `class_name` below), marker = DRC_BJT
# (127/5, `bjt.separation.comp.1` above) -- `extract.py` intersects
# base/emitter with the marker so only genuine BJT device-cell instances are
# recognised, not every Nwell drawn for an ordinary PMOS. No drawn collector
# layer -- the DRM's vertical bipolar's collector is the substrate, tied to
# `substrate_net` like the NMOS body above (see `BipolarDevice`'s
# docstring). `class_name="bjt"` (not `"npn"`/`"pnp"`): the DRM's own 10.7
# section title ("vertical NPN/PNP bipolar rule category") names both
# polarities under one mark layer, and unlike sky130's `pnp_05v5` this repo
# has no positively-identified single device-cell name to attribute a
# specific polarity to -- a generic class name is used rather than guessing.
#
# MiM capacitor device recognition (issue #225), transcribed from the
# official KLayout LVS deck's own derivation and extraction calls (see the
# module docstring's provenance note above for the exact source files):
#
#     mimtm_virtual = fusetop.sized(1.06.um)
#                            .and(topmin1_metal.interacting(fusetop))
#     fuse_cap = fusetop.interacting(cap_mk).interacting(mim_l_mk)
#                       .not(mimcap_exclude)   # mimcap_exclude = efuse_mk.join(plfuse)
#     extract_devices(capacitor('cap_mim_2f0_m4m5_noshield', 2.0e-15, MIMCap),
#                     { 'P1' => mimtm_virtual, 'P2' => fuse_cap })
#
# for `METAL_LEVEL = '5LM'` (this curated deck's own Metal1-Metal5 stack --
# see the DRC deck's own "10.4 MIM Capacitor" note above on why only Option B
# applies here) and the LVS deck's own default `MIM_CAP = '2'` (2.0fF/um^2 --
# see the provenance note above on why this deck models only the default of
# the PDK's three selectable densities, not all three: which one a given
# fab run actually ships is a foundry-side option selected at LVS-runset
# invocation time, not something a drawn layout's own geometry can
# distinguish -- there is no per-instance layer marking "this cap uses the
# 1.5fF option" the way `requires`/`excludes` narrows other device classes
# below). `topmin1_metal` for `5LM` is `Metal4` (46/0, `metal4.not(metal4_res)`
# in the official deck; this curated deck does not model `metal4_res`, the
# separately-marked-resistor exclusion -- an unmodelled, narrow gap, mirrors
# the "curated starter subset" scope guard elsewhere in this file).
#
# `top_plate`/`bottom_plate` are `FuseTop`/`Metal4`. Even though `Metal4` is
# now part of this deck's `metals` connectivity stack (#220), the capacitor's
# plate regions are registered as their own separate, self-connected layers in
# `extract.py` (a distinct `_region(bottom_plate)` clip, not `metals[3]`
# itself) -- see `CapacitorDevice`'s "Known limitation": the extracted
# capacitor's plate nets are not wired into the routing connectivity stack.
# `bottom_plate_oversize_um=1.06` reproduces the official "virtual bottom
# plate" derivation exactly (`extract.py`'s capacitor-resolution block
# performs the same `interacting`-then-`sized`-`and` two-step). `CAP_MK`
# (117/5) and `MIM_L_MK` (117/10) are both required on the top plate,
# matching `fuse_cap`'s double `.interacting(...)` above; `efuse_mk` (80/5)
# and `plfuse` (125/5) are excluded, matching `mimcap_exclude`.
EXTRACTION_DECK = ExtractionDeck(
    active=(22, 0),  # Comp
    poly=(30, 0),  # Poly2
    nwell=(21, 0),  # Nwell
    contact=(33, 0),  # Contact
    poly_label=(30, 10),  # Poly2 pin/label purpose -- names a bare-poly gate (#210)
    # Full Metal1-Metal5 routing stack (#220). Before this, `metals` stopped
    # at Metal1, so anything drawn above it was invisible to the connectivity
    # graph and a normally-routed block extracted as a pile of disconnected
    # nets (LVS then reported a mismatch on essentially every net). The engine
    # loop in `extract.py`'s `_extract_netlist` is already generic over an
    # arbitrary-depth stack (`vias[i]` connects `metals[i]` to `metals[i+1]`);
    # this is deck data, not new machinery.
    metals=(
        (34, 0),  # Metal1
        (36, 0),  # Metal2
        (42, 0),  # Metal3
        (46, 0),  # Metal4
        (81, 0),  # Metal5
    ),
    # `vias[i]` connects `metals[i]` to `metals[i + 1]`, so this tuple has
    # `len(metals) - 1` entries. Layer numbers from `main.drc`'s via-layer
    # derivations (see the module docstring's layer table): Via1 35/0,
    # Via2 38/0, Via3 40/0, Via4 41/0.
    vias=(
        (35, 0),  # Via1 (Metal1 <-> Metal2)
        (38, 0),  # Via2 (Metal2 <-> Metal3)
        (40, 0),  # Via3 (Metal3 <-> Metal4)
        (41, 0),  # Via4 (Metal4 <-> Metal5)
    ),
    # Each metal's pin/label purpose is datatype 10 (gf180mcu's convention,
    # same as Metal1's already-shipped 34/10). Index-aligned with `metals`.
    metal_labels=(
        (34, 10),  # Metal1 pin/label purpose
        (36, 10),  # Metal2 pin/label purpose
        (42, 10),  # Metal3 pin/label purpose
        (46, 10),  # Metal4 pin/label purpose
        (81, 10),  # Metal5 pin/label purpose
    ),
    bipolars=(
        BipolarDevice(
            base=(21, 0),  # Nwell
            emitter=(22, 0),  # Comp
            marker=(127, 5),  # DRC_BJT
            class_name="bjt",
        ),
    ),
    capacitors=(
        CapacitorDevice(
            name="cap_mim_2f0_m4m5_noshield",  # official LVS deck's own device name
            top_plate=(75, 0),  # FuseTop
            top_plate_requires=(
                (117, 5),  # CAP_MK
                (117, 10),  # MIM_L_MK
            ),
            top_plate_excludes=(
                (80, 5),  # efuse_mk
                (125, 5),  # plfuse
            ),
            bottom_plate=(46, 0),  # Metal4 (topmin1_metal for the 5LM stack)
            # DRM 10.4.2 footnote 1 / MIMTM.1's "virtual bottom plate":
            bottom_plate_oversize_um=1.06,
            area_cap_f_um2=2.0e-15,  # 2.0 fF/um^2, see provenance note above
        ),
    ),
)

# --------------------------------------------------------------------------- #
# `klt extract --parasitics` first-order lumped-RC coefficients
# --------------------------------------------------------------------------- #
#
# Sourced, citable values transcribed from the *public* gf180mcu magic
# technology file `gf180mcu/magic/gf180mcu.tech` in fossi-foundation/open-pdks
# (GPLv3 -- the same upstream the DRC decks are transcribed from), nominal
# corner (the `variants ()` block). Sheet resistances come from that file's
# `resist <layer> <milliohms per square>` entries; area/fringe capacitances
# from its `defaultareacap` (aF/um^2) and `defaultperimeter` (aF/um) entries.
# The .tech header credits GlobalFoundries' PDS_035_03; nothing NDA'd. Values
# remain order-of-magnitude and uncalibrated-to-silicon: calibrating
# parasitic-extraction accuracy against silicon is an explicit non-goal of
# this first cut (issue #216 "Non-goals"). `metals` is index-aligned with
# EXTRACTION_DECK's `metals` stack: index 0 is Metal1. Values expressed as
# (sheet ohms/square, area cap fF/um^2, fringe cap fF/um).
PARASITICS = ParasiticsDeck(
    # No diffusion role (issue #226): the M cards' AS/AD/PS/PD already feed the
    # device model's junction capacitance, so an area/perimeter cap term on the
    # Comp source/drain diffusion here would double-count it. gf180mcu.tech
    # comments its own active-layer cap out for the same reason.
    diffusion=None,
    # Poly2 gate/interconnect. sheet from `resist (allpolynonres)/active 7300`
    # (7300 milliohm/sq = 7.3 ohm/sq); area from `defaultareacap` poly2
    # (110.67 aF/um^2).
    poly=LayerRC(sheet_res_ohm_sq=7.3, cap_area_ff_um2=0.11067, cap_perim_ff_um=0.05),
    metals=(
        # Metal1. perim from `defaultperimeter` Metal1 (39.431 aF/um).
        LayerRC(sheet_res_ohm_sq=0.09, cap_area_ff_um2=0.03, cap_perim_ff_um=0.039431),
    ),
)
