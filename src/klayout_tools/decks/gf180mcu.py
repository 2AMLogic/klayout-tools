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
  - ``drm_07_15.rst`` / ``tables_clear/23_Vian_59.csv`` — 7.14 Vian (``Vn.*``,
    ``n = 1 to 5`` in the DRM; only ``n = 1 to 4`` (``Via1``-``Via4``) are
    modeled here, matching this deck's existing ``Via1``-``Via4`` layer
    coverage (issue #220) -- this deck's curated layer set has no drawn
    ``Via5``/``n = 5`` layer (issue #546)
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
  - ``drm_09_01.rst`` / ``tables_clear/29_BondPad1_70.csv`` — 9.1 Bond Pad
    (``PAD.*``), section 9.0's *hard, coded* rule table (as opposed to 9.2's
    ``tables_clear/29_BondPad2_70.csv``, a *guideline* table -- not
    transcribed here, see the "PAD.4" rule's own docstring below).

Each rule's docstring below cites the exact source rule id (e.g. ``"DF.1a"``)
and its DRM description, so values can be re-verified against a fresh
checkout of that repo at any time. Unless noted otherwise, the **3.3V
column** value is used (this deck does not model the 5V/6V high-voltage
variants).

``CO.*``/``Vn.*`` re-derivation note (issue #551): the nine
conductor-over-cut enclosure rules below (``metal1.enclosing.contact.1``
plus the eight ``metalN.enclosing.viaM.1`` rules) are the one family here
whose values were re-derived from *executable* rule-deck code rather than
from the DRM tables alone. A current PDK build ships them -- unlike the
``999a6ff`` DRC-only snapshot the provenance note below describes -- so each
cites both its DRM rule id and the statement it was re-derived from in
``libs.tech/klayout/drc/rule_decks/{contact,via1,via2,via3,via4}.drc`` of a
real fetched install (``volare enable gf180mcu
c6d73a35f524070e85faff4a6a9eef49553ebc2b``, the same open_pdks build the
``EXTRACTION_DECK`` MiM notes below already cite, and the same one the
issue's own reproducer was cross-checked against). Every one of those
statements has the form ``cut.enclosed(conductor, d, euclidian) OR
cut.not(conductor)`` -- exactly the pair of conditions our ``"enclosing"``
check reports (marginal facing-edge enclosure plus ``_run_check``'s
``outside_region`` zero-overlap escape term, see ``drc.py``) -- so none of
the nine is an approximation.

Their **end-of-line** companions (``CO.6a``/``CO.6b``, ``Vn.3c``/``Vn.3d``,
``Vn.4b``/``Vn.4c``) are deliberately **not** transcribed: each conditions a
0.06um margin on a narrow-metal (< 0.34um) end-of-line predicate, expressed
in the PDK deck via ``first_edges``/``second_edges``/``extended_in``
edge-set operations that :class:`~klayout_tools.decks.DrcRule`'s
(layer, other_layer, check, threshold) vocabulary cannot express at all --
the same class of gap as sky130's un-transcribed ``m2.6`` area rule, and
left for a follow-on that extends the vocabulary rather than approximated
with a wrong threshold.

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
#188, to metal2/metal3/metal5/metaltop and the MiM capacitor stack, per #546
to via1-via4 size/spacing, and per #551 to the conductor-over-cut enclosures
that tie the two together), plus a
first increment of well/substrate-tap coverage (Nwell), one bipolar
(BJT)-specific device rule, and one bond-pad rule (``PAD.4``, issue #545),
wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) for a second PDK. Coverage is
expected to grow incrementally in follow-on issues (e.g. Pplus/Nplus
implant-specific rules, LVPWELL/DNWELL, the remaining BJT rules that key off
DNWELL/LVPWELL, 5V/6V variants, and DFM guidelines). ``MIMTM.2``'s
sized/derived-layer need (see its own note below) is closed as of #345 via
:class:`~klayout_tools.decks.DerivedLayer`.

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

``area_cap_f_um2``/``perim_cap_f_um`` (issue #512) are refined from the LVS
deck's own ``2.0e-15`` F/um² above to the *simulation* model's own
two-term law, ``c_c0 = c_cox * c_AREA + c_capsw * c_PERI``, transcribed from
the same ``volare enable gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b``
fetch's ``libs.tech/ngspice/sm141064.ngspice``, ``.subckt cap_mim_2f0fF``
(the "2.0fF/um² MiM" variant matching the LVS deck's own default cited
above): ``c_cox = 1.99e-3`` F/m² = ``1.99e-15`` F/um² (the area term --
0.5% below the LVS deck's rounded ``2.0e-15`` nominal-density label) and
``c_capsw = 2.383e-10`` F/m = ``2.383e-16`` F/um (the previously-unmodelled
perimeter/fringe term). Both device-recognition *geometry* (this deck's own
job) and the simulation model's *coefficients* trace to the same PDK
snapshot, so using the model card's own numbers here -- rather than the
LVS-extraction script's separately-rounded restatement of them -- keeps
``klt extract``'s reported ``c_f`` consistent with what the same drawn
geometry simulates as.

Sixteen rules below approximate the official DRM rule in some way (each is
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
- ``via1.width.1``/``via2.width.1``/``via3.width.1``/``via4.width.1``: the
  official ``Vn.1`` specifies each via as a fixed 0.26 x 0.26um square (a min
  **and** max bound) -- the same shape of rule as ``contact.width.1``'s
  ``CO.1`` above; our ``width_check`` primitive only supports a
  minimum-width lower bound, so only the min half of the rule is enforced
  here.
- ``via1.space.1``/``via2.space.1``/``via3.space.1``/``via4.space.1``: the
  official ``Vn.2a``/``Vn.2b`` split via-to-via spacing by array density
  (``Vn.2a`` 0.26um for an ordinary two-via space, ``Vn.2b`` a tighter 0.36um
  inside a >=4x4 via array); our engine's ``space_check`` primitive has no
  array-density context, so this is approximated using only the less strict
  ``Vn.2a`` value across the whole via drawn layer -- this means vias inside
  a genuine >=4x4 array, spaced between 0.26um and 0.36um apart, will **not**
  be flagged even though the real DRM's ``Vn.2b`` would flag them (the same
  class of context-collapsing approximation as ``nwell.space.1`` below).
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
  spacing check across the whole drawn layer rather than the virtual bottom
  plate. Unlike ``MIMTM.2`` below, this rule has *not* been rewritten to use
  :class:`~klayout_tools.decks.DerivedLayer` (added for ``MIMTM.2`` in #345)
  -- that primitive derives a checked *region*, and ``space_check`` is a
  single-region check, so scoping it would still over-flag any ordinary
  ``Metal4`` shape spaced near a genuine virtual bottom plate, just from a
  smaller set of shapes; left as today's more conservative over-approximation
  rather than half-fixed. This means ordinary ``Metal4`` routing traces
  spaced between the deck's generic metal-spacing range and 1.2um apart --
  nothing to do with a MiM capacitor -- will be flagged even though they
  violate no real DRM rule. Threshold value unmodified.

``MIMTM.2`` ("min. MiM bottom-plate overlap of ``Via4``", 0.4um) is
transcribed below as ``mim.enclosing.via4.1`` (issue #345), scoped to the
same "virtual bottom plate" derived layer ``MIMTM.1``/``mim.space.1``
already cite (``FuseTop`` sized by 1.06um, restricted to wherever ``Metal4``
already comes near it) via :class:`~klayout_tools.decks.DerivedLayer`
(``klayout_tools/decks/__init__.py``). An *unscoped* approximation of this
rule (checking raw ``Metal4`` enclosure of every ``Via4``) would have been
actively wrong, not merely conservative like ``mim.space.1``'s: ordinary
``Metal4``-to-``Metal5`` routing vias use a much smaller enclosure than a
MiM cap's virtual bottom plate requires, so a blanket "``Metal4`` must
enclose every ``Via4`` by 0.4um" check would flag legitimate routing vias
throughout *any* layout using ``Metal4``/``Via4``/``Metal5`` at all, not
just genuine MiM structures -- that is exactly the false-positive risk
``DerivedLayer`` exists to avoid: the checked region is only the part of
``Metal4`` that already overlaps a ``FuseTop`` shape somewhere, oversized by
the DRM's own 1.06um margin, so ordinary routing with no MiM cap anywhere
nearby has an empty derived region and never trips this rule (see
``tests/test_drc.py``'s
``test_run_drc_gf180mcu_mim_enclosing_via4_ordinary_routing_clean`` negative
control).

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
    DerivedLayer,
    DiodeDevice,
    DrcRule,
    ExtractionDeck,
    LayerRC,
    ParasiticsDeck,
    ResistorDevice,
    ResistorFlavour,
    RuleProvenance,
)

# `RuleProvenance.source_repo`/`.commit` shared by every gf180mcu width/space
# rule's provenance below (issue #747, piloted on this deck's 26 width/space
# rules only) -- the same `google/gf180mcu-pdk` DRM repo/commit the module
# docstring's own top-of-file provenance note already cites for every rule
# transcribed from the published DRM tables (as opposed to the nine
# `CO.*`/`Vn.*` conductor-over-cut enclosure rules re-derived from a
# *different* repo/commit, `globalfoundries-pdk-libs-gf180mcu_fd_pv`
# `999a6ff` -- none of those are width/space checks, so none are piloted
# here).
_GF180MCU_PDK_REPO = "google/gf180mcu-pdk"
_GF180MCU_PDK_COMMIT = "de3240d"

# DRM section (`DrcRule.scope`, issue #566) -> the numeric CSV table under
# `docs/physical_verification/design_manual/tables_clear/` this deck's
# width/space rule values were transcribed from, per the module docstring's
# own section/table citation list above.
_DRM_TABLES_DIR = "docs/physical_verification/design_manual/tables_clear"
_GF180MCU_DRM_CSV: dict[str, str] = {
    "7.4 Nwell": f"{_DRM_TABLES_DIR}/13_Nwell31.csv",
    "7.5 Comp": f"{_DRM_TABLES_DIR}/14_COMP33_1.csv",
    "7.7 Poly2": f"{_DRM_TABLES_DIR}/16_Poly2_42.csv",
    "7.12 Contact": f"{_DRM_TABLES_DIR}/21_Contact_56.csv",
    "7.13 Metaln": f"{_DRM_TABLES_DIR}/22_Metaln_58.csv",
    "7.14 Vian": f"{_DRM_TABLES_DIR}/23_Vian_59.csv",
    "7.15 MetalTop": f"{_DRM_TABLES_DIR}/24_MetalTop_61.csv",
    "10.4.2 MIM Option B": f"{_DRM_TABLES_DIR}/35_MIM2_88.csv",
}


def _gf180mcu_provenance(scope: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a DRM-table-sourced gf180mcu
    width/space rule (issue #747) -- `source_path` from `scope`'s own DRM
    section via `_GF180MCU_DRM_CSV`, everything else shared across the whole
    deck (see the module-level constants above)."""
    return RuleProvenance(
        source_repo=_GF180MCU_PDK_REPO,
        source_path=_GF180MCU_DRM_CSV[scope],
        rule_id=rule_id,
        commit=_GF180MCU_PDK_COMMIT,
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
        scope="7.7 Poly2",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.7 Poly2", "PL.1"),
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
        scope="7.7 Poly2",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.7 Poly2", "PL.3a"),
    ),
    DrcRule(
        id="poly2.enclosing.contact.1",
        description="minimum poly2 overlap of contact",
        layer=(30, 0),  # Poly2
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.3": "Poly2 overlap of contact" -> 0.07um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
    ),
    DrcRule(
        id="comp.width.1",
        description="minimum COMP (diffusion/active) width (3.3V)",
        layer=(22, 0),  # Comp
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.5 Comp, rule "DF.1a": "Min. COMP Width" -> 0.22 (3.3V column)
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.5 Comp", "DF.1a"),
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
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.5 Comp", "DF.3a"),
    ),
    DrcRule(
        id="comp.enclosing.contact.1",
        description="minimum COMP overlap of contact",
        layer=(22, 0),  # Comp
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.4": "COMP overlap of contact" -> 0.07um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
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
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.1"),
    ),
    DrcRule(
        id="contact.space.1",
        description="minimum contact spacing",
        layer=(33, 0),  # Contact
        check="space",
        threshold_dbu=250,  # 0.25 um
        # DRM 7.12 Contact, rule "CO.2a": "Space" -> 0.25um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.2a"),
    ),
    DrcRule(
        id="metal1.enclosing.contact.1",
        description="minimum metal1 overlap of contact",
        layer=(34, 0),  # Metal1 -- the conductor *above* the cut
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # DRM 7.12 Contact, rule "CO.6": "Metal1 overlap of contact" ->
        # 0.005um. Re-derived from the PDK's own executable rule deck (see
        # the "CO.*/Vn.* re-derivation" note in the module docstring):
        # `rule_decks/contact.drc`'s CO.6 is
        # `contact.enclosed(metal1, 0.005.um, euclidian) OR
        # contact.not(metal1)` -- exactly the pair of conditions our
        # "enclosing" check reports (marginal facing-edge enclosure plus the
        # zero-overlap escape term, see `_run_check`'s `outside_region` in
        # `drc.py`), so this transcription is exact, not an approximation.
        # The end-of-line companions "CO.6a"/"CO.6b" are *not* modeled -- see
        # the module docstring's end-of-line coverage note (issue #551).
    ),
    DrcRule(
        id="via1.width.1",
        description="minimum via1 size (approximates official min/max size rule)",
        layer=(35, 0),  # Via1
        check="width",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um (each via is a fixed 0.26 x 0.26um square in the real
        # deck). Approximation: our width_check enforces only a
        # minimum-width lower bound; the "max" (fixed-size) half of the
        # rule is not checked -- same class of approximation as
        # contact.width.1's own CO.1 note above. Threshold value unmodified.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via1.space.1",
        description="minimum via1 spacing (approximates official 4x4-array rule)",
        layer=(35, 0),  # Via1
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: the DRM tightens this to 0.36um ("Vn.2b") inside a
        # >=4x4 via array; our space_check primitive has no array-density
        # context, so this uses the ordinary two-via "Vn.2a" threshold only
        # -- same class of context-collapsing approximation
        # nwell.space.1/comp.space.1 already document. Threshold value
        # unmodified.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal1.enclosing.via1.1",
        description="minimum metal1 overlap of via1 (the conductor below the cut)",
        layer=(34, 0),  # Metal1
        other_layer=(35, 0),  # Via1
        check="enclosing",
        threshold_dbu=0,  # 0.0 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V1.3a": "metal1 overlap of via1"
        # -> 0.0um, i.e. the cut must land on metal1 but needs no margin.
        # Re-derived from `rule_decks/via1.drc`, whose V1.3a is the bare
        # `via1.not(metal1)` -- exactly this deck's zero-threshold
        # "enclosing" form (`enclosing_check(..., 0)` reports nothing, and
        # the rule's whole content is carried by `_run_check`'s
        # `outside_region` escape term, see `drc.py`). Via1 is the one via
        # level whose *lower* conductor carries a 0.0um requirement; Via2-
        # Via4 use 0.01um ("Vn.3b"), below.
    ),
    DrcRule(
        id="metal2.enclosing.via1.1",
        description="minimum metal2 overlap of via1 (the conductor above the cut)",
        layer=(36, 0),  # Metal2
        other_layer=(35, 0),  # Via1
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V1.4a": "metal2 overlap of via1"
        # -> 0.01um. Re-derived from `rule_decks/via1.drc`, whose V1.4a is
        # `via1.enclosed(metal2, 0.01.um, euclidian) OR via1.not(metal2)` --
        # exactly the pair of conditions our "enclosing" check reports.
        # The end-of-line companions "V1.4b"/"V1.4c" are not modeled (see
        # the module docstring's end-of-line coverage note).
    ),
    DrcRule(
        id="via2.width.1",
        description="minimum via2 size (approximates official min/max size rule)",
        layer=(38, 0),  # Via2
        check="width",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Approximation: see via1.width.1's note above (min-only
        # width_check; the fixed-size "max" half is not checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via2.space.1",
        description="minimum via2 spacing (approximates official 4x4-array rule)",
        layer=(38, 0),  # Via2
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal2.enclosing.via2.1",
        description="minimum metal2 overlap of via2 (the conductor below the cut)",
        layer=(36, 0),  # Metal2
        other_layer=(38, 0),  # Via2
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V2.3b": "metal2 overlap of via2"
        # -> 0.01um. Re-derived from `rule_decks/via2.drc`, whose V2.3b is
        # `via2.not(metal2) OR via2.enclosed(metal2, 0.01.um, euclidian)` --
        # exactly the pair of conditions our "enclosing" check reports.
    ),
    DrcRule(
        id="metal3.enclosing.via2.1",
        description="minimum metal3 overlap of via2 (the conductor above the cut)",
        layer=(42, 0),  # Metal3
        other_layer=(38, 0),  # Via2
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V2.4a": "metal3 overlap of via2"
        # -> 0.01um. Re-derived from `rule_decks/via2.drc` (same
        # `enclosed(...) OR not(...)` form as V2.3b above).
    ),
    DrcRule(
        id="via3.width.1",
        description="minimum via3 size (approximates official min/max size rule)",
        layer=(40, 0),  # Via3
        check="width",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Approximation: see via1.width.1's note above (min-only
        # width_check; the fixed-size "max" half is not checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via3.space.1",
        description="minimum via3 spacing (approximates official 4x4-array rule)",
        layer=(40, 0),  # Via3
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal3.enclosing.via3.1",
        description="minimum metal3 overlap of via3 (the conductor below the cut)",
        layer=(42, 0),  # Metal3
        other_layer=(40, 0),  # Via3
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V3.3b": "metal3 overlap of via3"
        # -> 0.01um. Re-derived from `rule_decks/via3.drc` (same
        # `not(...) OR enclosed(...)` form as V2.3b above).
    ),
    DrcRule(
        id="metal4.enclosing.via3.1",
        description="minimum metal4 overlap of via3 (the conductor above the cut)",
        layer=(46, 0),  # Metal4
        other_layer=(40, 0),  # Via3
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V3.4a": "metal4 overlap of via3"
        # -> 0.01um. Re-derived from `rule_decks/via3.drc`.
    ),
    DrcRule(
        id="via4.width.1",
        description="minimum via4 size (approximates official min/max size rule)",
        layer=(41, 0),  # Via4
        check="width",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Approximation: see via1.width.1's note above (min-only
        # width_check; the fixed-size "max" half is not checked). Via4
        # (41/0) was previously referenced only as mim.enclosing.via4.1's
        # other_layer (MiM top-plate overlap, DRM "MIMTM.2"); this rule adds
        # coverage of Via4's own size, which that reference alone did not
        # provide (issue #546).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via4.space.1",
        description="minimum via4 spacing (approximates official 4x4-array rule)",
        layer=(41, 0),  # Via4
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked). Via4 (41/0) was previously referenced only as
        # mim.enclosing.via4.1's other_layer; this rule adds coverage of
        # Via4's own spacing (issue #546).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal4.enclosing.via4.1",
        description="minimum metal4 overlap of via4 (the conductor below the cut)",
        layer=(46, 0),  # Metal4
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V4.3b": "metal4 overlap of via4"
        # -> 0.01um. Re-derived from `rule_decks/via4.drc`. Distinct from
        # `mim.enclosing.via4.1` (MIMTM.2, 0.4um) below: that one is the MiM
        # capacitor's *virtual bottom plate* overlap of Via4, scoped via
        # `DerivedLayer` to Metal4 that already overlaps FuseTop; this one is
        # the ordinary BEOL routing rule for raw Metal4, which every Via4 --
        # MiM or routing -- must also satisfy (a genuine MiM bottom plate
        # clears 0.01um by two orders of magnitude, so the two never
        # conflict).
    ),
    DrcRule(
        id="metal5.enclosing.via4.1",
        description="minimum metal5 overlap of via4 (the conductor above the cut)",
        layer=(81, 0),  # Metal5
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V4.4a": "metal5 overlap of via4"
        # -> 0.01um. Re-derived from `rule_decks/via4.drc`. Scoped, like
        # `pad.enclosing.metal5.1`, to the 5LM variant this deck models
        # (Metal5 as the conductor above Via4).
    ),
    DrcRule(
        id="metal1.width.1",
        description="minimum metal1 width",
        layer=(34, 0),  # Metal1
        check="width",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.23 (n = 1)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum metal1 spacing",
        layer=(34, 0),  # Metal1
        check="space",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.23 (n = 1)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal2.width.1",
        description="minimum metal2 width",
        layer=(36, 0),  # Metal2
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum metal2 spacing",
        layer=(36, 0),  # Metal2
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal3.width.1",
        description="minimum metal3 width",
        layer=(42, 0),  # Metal3
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal3.space.1",
        description="minimum metal3 spacing",
        layer=(42, 0),  # Metal3
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal5.width.1",
        description="minimum metal5 width",
        layer=(81, 0),  # Metal5
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal5.space.1",
        description="minimum metal5 spacing",
        layer=(81, 0),  # Metal5
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
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
        scope="7.15 MetalTop",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.15 MetalTop", "MT.1"),
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
        scope="7.15 MetalTop",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.15 MetalTop", "MT.2a"),
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
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("10.4.2 MIM Option B", "MIMTM.1"),
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
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
    ),
    DrcRule(
        id="mim.enclosing.via4.1",
        description=(
            "minimum MiM virtual bottom plate overlap of via4 (fusetop-sized "
            "metal4, scoped to genuine MiM structures)"
        ),
        layer=(46, 0),  # Metal4 -- reporting identity, matches the two rules above
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=400,  # 0.4 um
        derived_layer=DerivedLayer(
            base=(75, 0),  # FuseTop, sized (oversized) below
            sized_by_um=1.06,
            intersect_with=(46, 0),  # Metal4
        ),
        # DRM 10.4.2 MIM Option B, rule "MIMTM.2": "Minimum MiM bottom plate
        # overlap of Via4" -> 0.4um, per the DRM's own note scoped to the
        # "virtual bottom plate" (FuseTop sized by 1.06um AND Metal4
        # restricted to wherever it already overlaps FuseTop) rather than raw
        # Metal4 -- see DerivedLayer's docstring and the module docstring's
        # own MIMTM.2 note (issue #345) for why an unscoped check here would
        # be actively wrong (it would flag ordinary Metal4/Via4/Metal5
        # routing throughout any layout, ordinary interconnect this deck's
        # `metals`/`vias` stack draws everywhere), not merely conservative.
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
    ),
    DrcRule(
        id="pad.enclosing.metal5.1",
        description="minimum top-metal (Metal5) overlap of pad opening",
        layer=(81, 0),  # Metal5 -- the enclosing layer, 5LM variant only
        other_layer=(37, 0),  # Pad (passivation opening)
        check="enclosing",
        threshold_dbu=2000,  # 2.0 um
        # DRM 9.1 Bond Pad, rule "PAD.4": "Top layer metal overlap of pad
        # opening" -> 2.0um. The only hard, coded bond-pad rule (9.2's
        # PAD.1/PAD.2/PAD.5-PAD.20 are a guideline table, out of scope here).
        # Scoped to the 5LM variant this deck already exclusively models
        # (Metal5/81,0 as top metal) -- 6LM's MetalTop/53,0 case is not
        # covered; this deck has no variant-selection mechanism today.
        scope="9.1 Bond Pad",  # DRM section this rule is transcribed from (#566)
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
        scope="7.4 Nwell",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.4 Nwell", "NW.2a"),
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
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
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
        scope="10.7 DRC_BJT Mark Layer",  # DRM section transcribed from (#566)
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
    (35, 0): "Via1",
    (36, 0): "Metal2",
    (38, 0): "Via2",
    (42, 0): "Metal3",
    (40, 0): "Via3",
    (46, 0): "Metal4",
    (41, 0): "Via4",
    (81, 0): "Metal5",
    (53, 0): "MetalTop",
    (37, 0): "Pad",
    (75, 0): "FuseTop",
    (55, 0): "Dualgate",
    (127, 5): "DRC_BJT",
    (117, 5): "CAP_MK",
    (117, 10): "MIM_L_MK",
    (80, 5): "efuse_mk",
    (125, 5): "plfuse",
}

# Voltage-domain marker layers this deck draws but does not model the DRC/
# extraction *scoping* of (issue #552): `Dualgate` (55/0) selects gf180mcu's
# 5V/6V thick-oxide domain. This deck's DRC rules above (`DF.1a`, `DF.3a`,
# `DF.6`, `PL.5a`/`PL.5b`, ...) transcribe only the 3.3V/`_LV` column -- their
# own descriptions say so explicitly ("minimum COMP (diffusion/active) width
# (3.3V)") -- and never read `Dualgate`; `EXTRACTION_DECK` below likewise
# derives `nfet`/`pfet` from `Nwell` alone (no `Dualgate` read at all), so a
# transistor drawn entirely inside `Dualgate` is checked against the wrong
# (3.3V) thresholds and extracted as the wrong (3.3V) model. Registered here
# purely as a "fail loudly" diagnostic (`decks.get_unmodeled_voltage_markers`,
# consumed by `drc.py`'s `coverage.voltage_domain_warnings` and `extract.py`'s
# `voltage_domain_warnings`), not a corrected threshold or model binding --
# see this issue's curator comment for why the full `_LV`/`_MV` rule-pair
# split (option 1, blocked on a subtraction-capable `DerivedLayer`) and a
# per-flavour MOS marker + `_06v0` model table entry (option 2) are each a
# separate, larger follow-on rather than bundled here.
#
# `Dualgate` is *also* read correctly today (not part of this gap) by the
# `DiodeDevice` entries further below, each via its own
# `requires=(..., Dualgate)` field (issue #542) -- this registry only
# concerns the *unscoped* paths: the DRC width/space/enclosure rules above
# and the plain `nfet`/`pfet` MOS recognition in `EXTRACTION_DECK`.
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {
    (55, 0): (
        "Dualgate (55/0) marks gf180mcu's 5V/6V thick-oxide voltage domain. "
        "This curated deck's DRC rules apply the 3.3V/_LV thresholds to "
        "geometry regardless of Dualgate's presence (e.g. DF.1a min COMP "
        "width 0.22 vs. the real 5V/6V DF.1a_MV 0.30, DF.3a min COMP space "
        "0.28 vs. 0.36, DF.6 COMP extend beyond gate 0.24 vs. 0.40, "
        "PL.5a/PL.5b field poly to COMP 0.10 vs. 0.30 -- all in um), and MOS "
        "extraction always binds the 3.3V models (nfet_03v3/pfet_03v3) even "
        "to a transistor drawn entirely inside Dualgate."
    ),
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
# `area_cap_f_um2`/`perim_cap_f_um` below use the more precise two-term
# `sm141064.ngspice` simulation-model coefficients rather than the LVS
# script's own rounded `2.0e-15` single-term call shown above -- see the
# module docstring's provenance note (issue #512) for the exact source and
# why both terms come from that file instead.
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
# `top_plate`/`bottom_plate` are `FuseTop`/`Metal4`. `Metal4` is part of this
# deck's `metals` connectivity stack (#220) -- `extract.py`'s bottom-plate
# connectivity wiring (issue #314) ties the recognised (virtual-plate-clipped)
# bottom region into `metals[3]` because `bottom_plate` matches that entry,
# so ordinary routing to `Metal4` reaches this terminal. `top_plate_via`/
# `top_plate_via_metal` (`Via4` 41/0 / `Metal5` 81/0, #314) wire the top plate
# the same way: the official deck's `main.drc` connectivity confirms
# `top_via = via4` lands on `top_metal = metal5` for this stack (see the
# module docstring's "10.4 MIM Capacitor" note), the same `Via4`/`Metal5`
# entries already in this deck's own `vias`/`metals` tuples above.
# `bottom_plate_oversize_um=1.06` reproduces the official "virtual bottom
# plate" derivation exactly (`extract.py`'s capacitor-resolution block
# performs the same `interacting`-then-`sized`-`and` two-step). `CAP_MK`
# (117/5) and `MIM_L_MK` (117/10) are both required on the top plate,
# matching `fuse_cap`'s double `.interacting(...)` above; `efuse_mk` (80/5)
# and `plfuse` (125/5) are excluded, matching `mimcap_exclude`.
#
# Drawn resistors (#222). Additional layer numbers, from the same
# `google/globalfoundries-pdk-libs-gf180mcu_fd_pv` source cited at the top of
# this module -- specifically its KLayout **LVS** deck's own layer table,
# `klayout/lvs/rule_decks/layers_definitions.lvs` (as installed by volare
# under `libs.tech/klayout/lvs/`):
#
#     DNWELL      12/0    (`dnwell   = get_polygons(12, 0)`)
#     Pplus       31/0    (`pplus    = get_polygons(31, 0)`)
#     Nplus       32/0    (`nplus    = get_polygons(32, 0)`)
#     SAB         49/0    (`sab      = get_polygons(49, 0)`  -- salicide block)
#     RES_MK     110/5    (`res_mk   = get_polygons(110, 5)` -- resistor mark)
#     Resistor    62/0    (`resistor = get_polygons(62, 0)`  -- the high-sheet-rho
#                          poly mark; this is the layer the DRC deck's own
#                          `hres.drc` rule file keys off)
#
# The one resistor device modelled below is `ppolyf_u` -- the DRM's "P+ poly,
# unsalicided" precision resistor (DRM chapter "10.0 Analog Device Related
# Rules", and `magic/gf180mcuD.tech`'s device list comment
# "#  ppolyf_u  resistor (P+ poly, unsalicided)"). The official KLayout LVS
# deck derives and extracts it (`lvs/rule_decks/res_derivations.lvs` +
# `res_extraction.lvs`) as:
#
#     ppoly_exclude   = comp.join(res_exclude).join(nplus)
#     ppolyf_u_layer  = pplus.and(poly2).and(sab).and(res_mk)
#                            .not_interacting(resistor).not(dnwell)
#                            .not(ppoly_exclude)
#     poly2_con       = poly2.not(res_mk).not(plfuse)
#     extract_devices(resistor_with_bulk('ppolyf_u', 350, BResistor),
#                     { 'R' => ppolyf_u_layer, 'C' => poly2_con, 'W' => sub })
#
# i.e. sheet resistance **350 ohm/square**, with a bulk terminal tied to the
# substrate (hence `bulk_to_substrate=True` below, which carries this deck's
# same documented substrate-global approximation). Cross-checked against a
# second, independent source in the same PDK install: open_pdks' magic
# technology file `libs.tech/magic/gf180mcuD.tech`, whose extract section
# reads `resist (rpp)/active 350000` (milliohms per square = 350 ohm/sq) for
# the same `rpp` device type (`device rsubcircuit ppolyf_u rpp *poly ...`).
#
# Approximations relative to that official derivation, all conservative:
#
# - `SAB` (salicide block) is a *required* marker, not an optional one. It is
#   exactly what separates this 350 ohm/sq unsalicided device from the
#   salicided `ppolyf_s` (7.3 ohm/sq, `resist (allpolynonres)/active 7300` in
#   the same magic tech file) -- a ~48x difference. A salicided p+ poly
#   resistor therefore stays ordinary connected poly here (today's behaviour
#   -- a short) rather than being reported with a 48x-too-large resistance.
# - `Resistor` (62/0) is an exclusion for `ppolyf_u`: it marks the
#   high-sheet-rho `ppolyf_u_1k`/`_2k`/`_3k` flavours (1000/2000/3000 ohm/sq;
#   see the `ppolyf_u_1k` entry immediately below, which now claims that
#   marked geometry instead of leaving it an unmodelled short). Upstream
#   drops the whole shape (`not_interacting`); this deck subtracts it
#   (`not`), which differs only for a marked segment the `Resistor` layer
#   partially covers.
# - `res_exclude` (esd, fusewindow_d, polyfuse, diode_mk, drc_bjt, nat,
#   mos_cap_mk, esd_mk, lvs_source, efuse_mk, plfuse, mvsd, mvpsd,
#   ldmos_xtor, schottky_diode) is **not** modelled -- none of those layers
#   are in this curated starter subset. A resistor-marked poly segment also
#   carrying one of them extracts as `ppolyf_u` rather than being excluded.
# - The terminal ("C") region is `Poly2` minus the recognised body rather
#   than upstream's `poly2.not(res_mk).not(plfuse)`; the two agree except on
#   marked poly this deck does not recognise as a resistor, which stays
#   ordinary connected poly here rather than being cut out of connectivity.
#
# `ppolyf_u_1k` (issue #299): the high-sheet-rho flavour `ppolyf_u`'s own
# `excludes` above rules out. The official LVS deck's own `res_derivations.lvs`
# derives it as one single region regardless of which of the three named
# flavours a given fab run selects:
#
#     ppoly_exclude = poly_exclude.join(nplus)   # poly_exclude = comp.join(res_exclude)
#     ppolyf_u_h    = poly2.and(sab).and(res_mk).and(resistor)
#                          .not(dnwell).not(v5_xtor).not(dualgate).not(ppoly_exclude)
#
# and `res_extraction.lvs` then extracts *that same* `ppolyf_u_h` region three
# separate ways, selected by a build-time `POLY_RES` deck option this curated
# deck models no equivalent of -- **not** by any additional drawn geometry:
#
#     case POLY_RES
#     when '1k'  extract_devices(resistor_with_bulk('ppolyf_u_1k', 1000, BResistor), ..)
#     when '2k'  extract_devices(resistor_with_bulk('ppolyf_u_2k', 2000, BResistor), ..)
#     when '3k'  extract_devices(resistor_with_bulk('ppolyf_u_3k', 3000, BResistor), ..)
#     end
#
# i.e. a drawn `Resistor`-marked poly segment is geometrically identical
# regardless of which of the three sheet-rho values it is meant to model --
# the same "no per-instance layer marking distinguishes the option" situation
# `EXTRACTION_DECK.capacitors`' MiM density note above already documents for
# `POLY_RES`'s sibling `MIM_CAP` option. Wiring all three as separate
# `ResistorDevice` entries would therefore have every one of them recognise
# the *same* drawn shape and each cut it out of `poly` in turn -- three
# duplicate `R` devices across one physical resistor, not three selectable
# flavours a layout can actually distinguish between. So exactly **one**
# `ResistorDevice` entry is wired below, mirroring the upstream deck's own
# structure (one region, one `case POLY_RES` selection at build time, never
# three simultaneous devices) -- but, unlike before issue #595, that one
# entry now carries all three named flavours via `flavour_option="poly_res"`/
# `flavours=(...)`, so a caller who knows their design was built against the
# `2k`/`3k` interpretation can select it explicitly
# (`klt extract --deck-option poly_res=2k`) instead of being stuck with
# whichever flavour the deck happens to default to. The **default** stays the
# PDK's own default absent an override: `gf180mcu.lvs` sets
# `POLY_RES = $poly_res || '1k'` when the invoking flow does not override it,
# and (like the MiM density default cited above) this is independently
# confirmed as the default *this specific PDK build* ships by open_pdks'
# `gf180mcu.json` variant-description string, `"...2fF MiM + 1k high sheet
# rho poly"`. Selecting `2k`/`3k` only ever changes this entry's *reported*
# name/sheet-rho -- it is still exactly one recognised device per drawn
# segment, never three, and a caller who never passes `--deck-option
# poly_res=...` sees the identical `ppolyf_u_1k`-at-1000-ohm/sq extraction
# this deck reported before #595, byte-for-byte.
#
# Unlike the base `ppolyf_u` entry, `ppolyf_u_h` above does **not** `.and()`
# `Pplus`: the official H-POLY-RES derivation recognises this flavour off
# `SAB`/`RES_MK`/`Resistor` alone (narrowed only by `not(comp)`/`not(nplus)`/
# `not(dnwell)`), so `ppolyf_u_1k` below mirrors that by requiring `SAB` +
# `Resistor` (its own high-sheet-rho marker) without also requiring `Pplus`,
# deliberately not adding a stricter condition the real deck does not apply.
#
# Junction-diode device recognition (issue #542). Additional layer numbers
# from the same `klayout/lvs/rule_decks/layers_definitions.lvs` table cited
# for the resistors above:
#
#     Dualgate    55/0    (`dualgate = get_polygons(55, 0)`)
#     diode_mk    115/5   (`diode_mk = get_polygons(115, 5)`)
#
# Transcribed from the official KLayout LVS deck's own
# `diode_derivations.lvs` / `diode_extraction.lvs` (same installed
# `libs.tech/klayout/lvs/` source as the resistor/MiM provenance above). The
# two 6V (`Dualgate`-marked, medium-voltage) flavours are the ones the PDK's
# own I/O library uses for its ESD clamps -- e.g.
# `gf180mcu_fd_io__asig_5p0`'s CDL is a plain dual-diode clamp built from
# `diode_nd2ps_06v0` and `diode_pd2nw_06v0` -- so they are the two wired
# here:
#
#     ncomp   = comp.and(nplus)          # pcomp = comp.and(pplus)
#     ncomp_mv = ncomp.and(dualgate)     # pcomp_mv = pcomp.and(dualgate)
#     nwell_mv = nwell.overlapping(dualgate)
#     diode_nd2ps_exclude = nwell.join(diode_exclude)
#     diode_pd2nw_exclude = lvpwell.join(diode_exclude)
#
#     diode_nd2ps_06v0_terminal_n = ncomp_mv.not(dnwell).and(diode_mk)
#                                           .not(diode_nd2ps_exclude)
#     diode_pd2nw_06v0_terminal_p = pcomp_mv.not(dnwell).and(diode_mk)
#                                           .not(diode_pd2nw_exclude)
#     diode_pd2nw_06v0_terminal_n = nwell_mv.not(dnwell).interacting(ncomp)
#                                           .not(diode_pd2nw_exclude)
#
#     extract_devices(diode('diode_nd2ps_06v0'),
#                     { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell_con })
#     extract_devices(diode('diode_pd2nw_06v0'),
#                     { 'N' => diode_pd2nw_06v0_terminal_n,
#                       'P' => diode_pd2nw_06v0_terminal_p })
#
# Mapping onto this deck's `DiodeDevice` fields:
#
# - `diode_nd2ps_06v0` is an n+ diffusion in the p-substrate. Its cathode is
#   `Comp` scoped to `diode_mk` and narrowed by `requires=(Nplus, Dualgate)`
#   (`ncomp_mv`) / `excludes=(Nwell, DNWELL)` -- `Nwell` is upstream's own
#   `diode_nd2ps_exclude` head term (an n+ diffusion *inside* Nwell is a well
#   tap, not this diode) and `DNWELL` routes the deep-nwell `_dn` variant
#   away rather than mislabelling it as this one. Its **anode is the
#   p-substrate**, for which gf180mcu draws no mask (upstream substitutes the
#   drawn `lvpwell_con`, which only exists inside DNWELL): declared `None`,
#   so `extract.py` uses the device's own `diode_mk` footprint -- minus
#   `Nwell`/`DNWELL`, keeping it genuinely outside every well -- and ties it
#   to this deck's `substrate_net` global, exactly as the collector-less
#   `bipolars` entry above and the NMOS body already do.
# - `diode_pd2nw_06v0` is a p+ diffusion in Nwell. Anode = `Comp` scoped to
#   `diode_mk`, narrowed by `requires=(Pplus, Dualgate)` (`pcomp_mv`) /
#   `excludes=(DNWELL)`; cathode = this deck's own `Nwell` layer, scoped to
#   the same `diode_mk` and excluding `DNWELL`. Scoping the well side to the
#   marker replaces upstream's `nwell_mv...interacting(ncomp)` well-selection
#   idiom: the mark is drawn over the p+ anode, which lies inside the well,
#   so `Nwell & diode_mk` selects exactly the well the marked anode sits in
#   and (because it is a subset of `nwell`) shares that well's net identity
#   through `extract.py`'s well wiring.
#
# Deliberately **not** modelled, each an explicit gap rather than a silent
# one -- geometry these entries do not claim stays unrecognised (no device
# emitted, a loud LVS mismatch) rather than being extracted as the wrong
# device:
#
# - The 3.3V (`_03v3`) flavours. Upstream tells them apart by the *absence*
#   of `Dualgate` plus `.not(v5_xtor)` (112/1, not in this curated layer
#   set); wiring them off "no Dualgate" alone would also claim every
#   `v5_xtor`-marked shape. Both `_dn` (deep-nwell) variants are likewise
#   excluded via `DNWELL` above rather than guessed at.
# - `diode_nw2ps_*`, `diode_pw2dw_*`, `diode_dw2ps_*` and `sc_diode`: all key
#   off `well_diode_mk` (153/51), `lvpwell`, `dnwell` or `schottky_diode`,
#   none of which this curated deck models.
# - `diode_exclude` (poly2, resistor, esd, sab, fusewindow_d, polyfuse,
#   res_mk, drc_bjt, nat, mos_cap_mk, efuse_mk, plfuse, mvsd, mvpsd,
#   ldmos_xtor) is only partially modelled: the three members this deck
#   already carries as *device* markers and that could genuinely collide on
#   the same diffusion -- `Poly2` (a marked MOS gate), `RES_MK` (a marked
#   resistor body) and `DRC_BJT` (a marked bipolar) -- are applied to each
#   diode's diffusion terminal. They are deliberately *not* applied to the
#   `Nwell` cathode: subtracting `Poly2` from a well region would punch
#   holes in the well terminal wherever poly crosses it, for no
#   disambiguation benefit (the marked diffusion side already decides
#   whether the device exists).
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
            # The base (Nwell) is contacted by an n+ tie ring drawn on the
            # same Comp diffusion layer as the p+ emitter, inside the same
            # DRC_BJT mark -- without narrowing, that ring extracts as a
            # second, artefact `bjt` sharing the base net (issue #302).
            # Excluding Nplus drops it: a real p+ emitter carries Pplus, not
            # Nplus, so this leaves the emitter untouched while removing the
            # n+ base tie. Mirrors the resistor deck's own `(32, 0)` exclude.
            emitter_excludes=(
                (32, 0),
            ),  # Nplus -- an n+ base tie is not the p+ emitter
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
            # sm141064.ngspice's cap_mim_2f0fF: c_cox=1.99e-3 F/m^2 (area) /
            # c_capsw=2.383e-10 F/m (perimeter/fringe), see the module
            # docstring's provenance note (issue #512) above.
            area_cap_f_um2=1.99e-15,
            perim_cap_f_um=2.383e-16,
            # Top-plate connectivity (issue #314): `main.drc`'s own
            # `top_via = via4` / `top_metal = metal5` confirms Via4 lands
            # directly on FuseTop and connects up to Metal5 for this 5LM
            # stack -- see the module docstring's "10.4 MIM Capacitor" note.
            top_plate_via=(41, 0),  # Via4
            top_plate_via_metal=(81, 0),  # Metal5
        ),
    ),
    resistors=(
        ResistorDevice(
            name="ppolyf_u",  # gf180mcu_fd_pr__ppolyf_u
            body=(30, 0),  # Poly2
            marker=(110, 5),  # RES_MK
            sheet_rho_ohm_sq=350.0,  # res_extraction.lvs / magic tech, see above
            requires=(
                (31, 0),  # Pplus -- p+ doped poly (vs. Nplus's npolyf_*)
                (49, 0),  # SAB   -- unsalicided (vs. ppolyf_s, 7.3 ohm/sq)
            ),
            excludes=(
                (22, 0),  # Comp   -- a marked gate is a transistor
                (32, 0),  # Nplus  -> npolyf_* devices, not this one
                (62, 0),  # Resistor -> ppolyf_u_{1k,2k,3k} high-sheet-rho poly
                (12, 0),  # DNWELL -> the `_dw` (deep-nwell) device variants
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
        ),
        # High-sheet-rho flavour (issue #299, made caller-selectable by
        # #595), see the module docstring's `ppolyf_u_1k` note above for the
        # full derivation. `sheet_rho_ohm_sq`/`name` below are the PDK's own
        # '1k' default (used whenever a caller passes no `poly_res`
        # `deck_options` override); `flavours` additionally declares the
        # `2k`/`3k` siblings selectable via `flavour_option="poly_res"`
        # (`klt extract --deck-option poly_res=2k`) -- see
        # `ResistorDevice.flavour_option`'s own docstring for why this is a
        # deck-option selection rather than a `requires`/`excludes` split:
        # all three share *identical* drawn geometry, so there is no layer a
        # deck could key off to tell them apart on its own.
        ResistorDevice(
            name="ppolyf_u_1k",  # gf180mcu_fd_pr__ppolyf_u_1k (POLY_RES='1k' default)
            body=(30, 0),  # Poly2
            marker=(110, 5),  # RES_MK
            sheet_rho_ohm_sq=1000.0,  # res_extraction.lvs 'when 1k', see above
            requires=(
                (49, 0),  # SAB      -- unsalicided, same as the base ppolyf_u
                (62, 0),  # Resistor -- the high-sheet-rho marker itself
            ),
            excludes=(
                (22, 0),  # Comp   -- a marked gate is a transistor
                (32, 0),  # Nplus  -> npolyf_* devices, not this one
                (12, 0),  # DNWELL -> the `_dw` (deep-nwell) device variants
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
            flavour_option="poly_res",
            flavours=(
                ResistorFlavour(
                    value="1k", name="ppolyf_u_1k", sheet_rho_ohm_sq=1000.0
                ),
                ResistorFlavour(
                    value="2k", name="ppolyf_u_2k", sheet_rho_ohm_sq=2000.0
                ),
                ResistorFlavour(
                    value="3k", name="ppolyf_u_3k", sheet_rho_ohm_sq=3000.0
                ),
            ),
        ),
    ),
    # Junction diodes (issue #542) -- see the provenance note above for the
    # official `diode_derivations.lvs`/`diode_extraction.lvs` derivations
    # these transcribe and for what is deliberately left unmodelled.
    diodes=(
        DiodeDevice(
            name="diode_nd2ps_06v0",  # gf180mcu_fd_pr__diode_nd2ps_06v0
            marker=(115, 5),  # diode_mk
            # Anode = the p-substrate; gf180mcu draws no mask for it, so the
            # device's own mark footprint (kept outside every well) stands in
            # and is tied to `substrate_net`.
            anode=None,
            anode_excludes=(
                (21, 0),  # Nwell  -- upstream's `diode_nd2ps_exclude` head term
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
            ),
            cathode=(22, 0),  # Comp (n+ diffusion)
            cathode_requires=(
                (32, 0),  # Nplus    -- n+ doped (`ncomp`)
                (55, 0),  # Dualgate -- the 6V/MV flavour (`ncomp_mv`)
            ),
            cathode_excludes=(
                (21, 0),  # Nwell  -- n+ inside Nwell is a well tap, not this
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
                (30, 0),  # Poly2   \
                (110, 5),  # RES_MK  | modelled subset of `diode_exclude`
                (127, 5),  # DRC_BJT /
            ),
        ),
        DiodeDevice(
            name="diode_pd2nw_06v0",  # gf180mcu_fd_pr__diode_pd2nw_06v0
            marker=(115, 5),  # diode_mk
            anode=(22, 0),  # Comp (p+ diffusion)
            anode_requires=(
                (31, 0),  # Pplus    -- p+ doped (`pcomp`)
                (55, 0),  # Dualgate -- the 6V/MV flavour (`pcomp_mv`)
            ),
            anode_excludes=(
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
                (30, 0),  # Poly2   \
                (110, 5),  # RES_MK  | modelled subset of `diode_exclude`
                (127, 5),  # DRC_BJT /
            ),
            cathode=(21, 0),  # Nwell -- shares this deck's own well node
            cathode_excludes=(
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
            ),
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
        # Metal2 (issue #547). sheet from `resist (allm2)/metal2 90`
        # (unconditional -- M2 is never the top/thick metal in any gf180mcu
        # stack option); area/perim from `defaultareacap`/`defaultperimeter`
        # allm2 metal2 (15.016 aF/um^2, 33.298 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09, cap_area_ff_um2=0.015016, cap_perim_ff_um=0.033298
        ),
        # Metal3 (issue #547). sheet from `resist (allm3)/metal3 90`, the
        # `#ifdef METALS4 || METALS5 || METALS6` row -- this deck's 5-metal
        # stack (`EXTRACTION_DECK.metals` has 5 entries) means M3 is not the
        # top metal, so it takes the fixed 90 milliohm/sq row rather than the
        # THICKMET-dependent one reserved for a 3-metal-only stack. area/perim
        # from `defaultareacap`/`defaultperimeter` allm3 metal3 (10.094
        # aF/um^2, 30.021 aF/um) -- unconditional; thickness options only vary
        # M3's sheet resistance, never its cap coefficients.
        LayerRC(
            sheet_res_ohm_sq=0.09, cap_area_ff_um2=0.010094, cap_perim_ff_um=0.030021
        ),
        # Metal4 (issue #547). sheet from `resist (allm4)/metal4 90`, the
        # `#ifdef METALS5 || METALS6` row (same reasoning as Metal3 -- not the
        # top metal in a 5-level stack). area/perim from
        # `defaultareacap`/`defaultperimeter` allm4 metal4 (7.602 aF/um^2,
        # 28.153 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09, cap_area_ff_um2=0.007602, cap_perim_ff_um=0.028153
        ),
        # Metal5 (issue #547, corrected by #572), the top metal of this
        # 5-level stack. sheet from the resolved, pre-built
        # `gf180mcuD.tech` for the pinned PDK commit
        # c6d73a35f524070e85faff4a6a9eef49553ebc2b (nominal `variants ()`
        # corner block, line 3050): `resist (allm5)/metal5    60` (60
        # milliohm/sq = 0.06 ohm/sq), the `else` (non-thick-metal) branch --
        # not the `#ifdef METALS5` / `THICKMET0P9 || THICKMET1P1` branch of
        # the generic templated `gf180mcu.tech` (40 milliohm/sq), which a
        # prior curation (#571) mistakenly used by manually resolving that
        # file's unresolved macros instead of reading the shipped,
        # pre-resolved file for this exact pinned commit. area/perim from
        # `defaultareacap`/`defaultperimeter` allm5 metal5 (5.798 aF/um^2,
        # 30.386 aF/um) -- unconditional across every THICKMET option.
        LayerRC(
            sheet_res_ohm_sq=0.06, cap_area_ff_um2=0.005798, cap_perim_ff_um=0.030386
        ),
    ),
    # `defaultoverlap` vertical plate-coupling coefficients (issue #760,
    # Extract Stage 2a): entry `i` is the coefficient between `metals[i]`
    # (lower plate) and `metals[i+1]` (upper plate), so a fully-populated
    # table has `len(metals) - 1` = 4 entries for this 5-level stack.
    #
    # Transcribed from the resolved, pre-built `gf180mcuD.tech` for the
    # pinned PDK commit c6d73a35f524070e85faff4a6a9eef49553ebc2b, nominal
    # `variants ()` corner block -- the same source and corner Metal5's
    # sheet resistance above is cited from (issue #572), chosen over the
    # generic `gf180mcu.tech` because only the resolved file has the
    # `#ifdef METALS5` Metal4/Metal5 rows already selected for this deck's
    # actual 5-level stack. Only `allmN metalN allmM metalM` rows are used
    # (metal-over-metal); the same blocks' `... nwell well` / `...
    # allpolynonres active` rows describe metal-over-substrate/poly overlap,
    # which the existing `cap_area_ff_um2` ground term already covers.
    #
    # Magic states these in aF/um^2; divided by 1000 to fF/um^2, matching
    # `cap_area_ff_um2`'s convention above.
    metal_overlaps=(
        # Metal1 (idx0) <-> Metal2 (idx1): `defaultoverlap allm2 metal2
        # allm1 metal1 59.027`.
        0.059027,
        # Metal2 (idx1) <-> Metal3 (idx2): `defaultoverlap allm3 metal3
        # allm2 metal2 59.027`.
        0.059027,
        # Metal3 (idx2) <-> Metal4 (idx3): `defaultoverlap allm4 metal4
        # allm3 metal3 59.027`.
        0.059027,
        # Metal4 (idx3) <-> Metal5 (idx4): `defaultoverlap allm5 metal5
        # allm4 metal4 39.351`.
        0.039351,
    ),
)
