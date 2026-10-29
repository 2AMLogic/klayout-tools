"""ASAP7 (7 nm predictive FinFET, ASU/ARM, BSD-3-Clause) FinFET extraction
deck (issue #2761).

**Scope: extraction only.** This module registers ASAP7 with ``klt
extract`` -- FinFET device recognition with geometry-counted fins, plus the
first-order ``PARASITICS`` interconnect table (#4558) -- and nothing else.
ASAP7 DRC is a separate artefact (the DRC-DSL deck ``asap7.drc`` beside this
module, #2760, run through ``klt drc --engine klayout --deck-file``); there
is no ``--pdk`` subcircuit binding, and ``klt lvs`` refuses a FinFET deck
until its reference reader preserves ``nfin`` (#2813; see
``docs/cli/extract.md``, "ASAP7 FinFET extraction").

**Source of truth.** Every layer number below was read from the pinned
lambdapdk archive ``scripts/fetch-pdks.sh`` fetches (``LAMBDAPDK_VERSION``
0.2.17, checksum-pinned there), the same files #2758 validated:

- ``lambdapdk/asap7/base/setup/klayout/asap7.lyp`` (sha256
  ``3aaada6c4c2d2f2bee9cc65cf43c30ed2ef9ccd5588a723b99a90d58daba417e``) --
  layer names, e.g. ``fin drawing - 2/0``;
- ``lambdapdk/asap7/base/setup/klayout/asap7.lyt`` (sha256
  ``8c461b1fd406fdbf02875937c11384c3a8d2146e70ab73216cfee68aa5038734``) --
  technology ``ASAP7``, ``dbu`` 0.00025 um;
- ``lambdapdk/asap7/base/apr/asap7.layermap`` -- the BEOL via/metal and
  ``/251`` pin-text purposes;
- ``lambdapdk/asap7/libs/asap7sc7p5t_rvt/gds/asap7sc7p5t_28_R.gds.gz``
  (sha256 ``75a7f40e...f262fc``) and its CDL
  ``.../netlist/asap7sc7p5t_28_R.cdl`` (sha256 ``3e3e2548...26777a6``) --
  the geometry and reference device syntax the semantics below were checked
  against.

**Device semantics (verified against the pinned RVT library).** The
reference CDL writes every transistor as
``M<n> d g s b {n,p}mos_{rvt,lvt,slvt,sram} w=<nfin*27n> l=20n nfin=<N>``.
In the GDS (true scale at dbu 0.00025 um: gate 80 dbu = 20 nm, fin pitch
108 dbu = 27 nm, fin width 28 dbu = 7 nm) fins run horizontally across the
row, gates vertically, and gate lines are cut by ``GCut``. One gate finger
crossing an active island is one channel; its fin count is the number of
fin strips crossing it. **The CDL's ``nfin`` is the total over parallel
fingers** -- ``INVx2`` draws two 3-fin fingers and the CDL writes one
``nfin=6`` device -- so the extractor sums ``NFIN`` across fingers that
share all four nets and ``L`` (``extract_finfet`` reports the finger count
separately). With that rule, 197 of the library's 209 cells extract the
exact (class, nfin, L) multiset of their CDL; the other 12 draw parallel
*series stacks* the CDL folds into one wider stack (e.g. ``AOI21x1``), a
topology question for the LVS comparison, not a fin-count one.

**Polarity, body and VT.** Polarity comes from ``Nselect``/``Pselect``
(12/0, 13/0); the well (1/0) is the PMOS body. The cells draw no substrate
or well tie -- the CDL simply ties NMOS bulk to ``VSS`` and PMOS bulk to
``VDD`` -- so the NMOS body is this deck's synthesized ``vsubs`` global and
the PMOS body is the well net, named by its ``well pin`` text (1/251).
Neither is joined to the metal supply of the same name: that is a
connectivity *assumption* a comparison may choose to make, not a drawn
fact. Threshold voltage is geometry: the LVT (98/0), SLVT (97/0) and SRAMVT
(110/0) markers select ``*_lvt``/``*_slvt``/``*_sram``; no marker is
regular VT (``*_rvt``) -- the RVT library draws none of the three, the LVT
and SLVT libraries draw 98/0 and 97/0 respectively.
The predictive THICKOX marker (111/0, #4465) selects the thick-oxide I/O
classes ``nmos_io``/``pmos_io`` (#4466); those model-card names are not
confirmed against 2AMLogic/pdks, and a finger under both a VT marker and
THICKOX is malformed geometry (the I/O classes have no VT axis).

**Connectivity.** ``LISD`` (17/0) lands on source/drain, ``LIG`` (16/0) on
the gate, ``V0`` (18/0) joins either to ``M1`` (19/0); ``V1``..``V8``
join ``M1``..``M9``. ``LIG`` and ``LISD`` **conduct where they overlap**
(``lig_lisd_connected``). The pinned files do not state this, so it was
decided empirically: 31 cells of the pinned RVT library (flip-flops and
clock gates, e.g. ``DFFHQNx2``, ``ICGx2``) overlap the two with no ``V0``
present, and those cells match their CDL at net level (with ``nfin``
compared exactly) only when the overlap conducts. ``SDT`` (88/0) is a mask
drawn inside ``LISD`` and is the **source/drain contact**
(``sd_contact``): diffusion reaches ``LISD`` only through it. ``LISD``
itself may run over a gate and abut the next diffusion without contacting
it -- ``AND2x2``'s ``LISD`` box (168,108;364,432) spans the A-input gate and
edge-touches the stack's internal node; treating ``LISD`` as a diffusion
contact there shorts that node, while routing through ``SDT`` matches the
CDL.
"""

from __future__ import annotations

from .extraction import ResistorDevice
from .finfet import FinFETExtractionDeck, FinFETFlavour, FinFETPassiveCapacitor
from .parasitics import CutRC, LayerRC, ParasiticsDeck
from .rules import RuleProvenance

_LAMBDAPDK_REPO = "siliconcompiler/lambdapdk"
#: lambdapdk release tag pinned by ``scripts/fetch-pdks.sh``; the archive is
#: checksum-verified there. Recorded as the provenance ``commit`` because the
#: release tag, not a commit SHA, is what this repo pins.
_LAMBDAPDK_TAG = "v0.2.17"
_ASAP7_LYP = "lambdapdk/asap7/base/setup/klayout/asap7.lyp"


def _lyp(rule_id: str) -> RuleProvenance:
    return RuleProvenance(
        source_repo=_LAMBDAPDK_REPO,
        source_path=_ASAP7_LYP,
        rule_id=rule_id,
        commit=_LAMBDAPDK_TAG,
    )


#: ``(layer, datatype) -> layer name`` for the roles this deck reads,
#: transcribed from the pinned ``asap7.lyp``.
LAYER_NAMES: dict[tuple[int, int], str] = {
    (1, 0): "well.drawing",
    (1, 251): "well.pin",
    (2, 0): "fin.drawing",
    (7, 0): "Gate.drawing",
    (7, 251): "Gate.pin",
    (10, 0): "GCut.drawing",
    (11, 0): "Active.drawing",
    (12, 0): "Nselect.drawing",
    (13, 0): "Pselect.drawing",
    (16, 0): "LIG.drawing",
    (17, 0): "LISD.drawing",
    (18, 0): "V0.drawing",
    (19, 0): "M1.drawing",
    (19, 251): "M1.pin",
    (20, 0): "M2.drawing",
    (20, 251): "M2.pin",
    (21, 0): "V1.drawing",
    (25, 0): "V2.drawing",
    (30, 0): "M3.drawing",
    (30, 251): "M3.pin",
    (35, 0): "V3.drawing",
    (40, 0): "M4.drawing",
    (40, 251): "M4.pin",
    (45, 0): "V4.drawing",
    (50, 0): "M5.drawing",
    (50, 251): "M5.pin",
    (55, 0): "V5.drawing",
    (60, 0): "M6.drawing",
    (60, 251): "M6.pin",
    (65, 0): "V6.drawing",
    (70, 0): "M7.drawing",
    (70, 251): "M7.pin",
    (75, 0): "V7.drawing",
    (80, 0): "M8.drawing",
    (80, 251): "M8.pin",
    (85, 0): "V8.drawing",
    (90, 0): "M9.drawing",
    (90, 251): "M9.pin",
    (88, 0): "SDT.drawing",
    (97, 0): "SLVT.drawing",
    (98, 0): "LVT.drawing",
    (110, 0): "SRAMVT.drawing",
    # Not in the pinned asap7.lyp/layermap or the DRM: an assumed, unused
    # stream number for the thick-oxide marker (#4465).
    (111, 0): "THICKOX.drawing",
    # klt-private passive-recognition markers (issue #4469): not PDK layers.
    # Absent from the pinned asap7.lyp, so no PDK layout uses them; a layout
    # drawn for another flow that happens to use 112/0 or 113/0 would be
    # reinterpreted, hence the explicit klt-private naming.
    (112, 0): "KLT_RES_MK.assumed",
    (113, 0): "KLT_CAP_MK.assumed",
}

_METALS = (
    (19, 0),
    (20, 0),
    (30, 0),
    (40, 0),
    (50, 0),
    (60, 0),
    (70, 0),
    (80, 0),
    (90, 0),
)
_VIAS = ((21, 0), (25, 0), (35, 0), (45, 0), (55, 0), (65, 0), (75, 0), (85, 0))

#: ASSUMED (predictive) M1 sheet resistance, ohm/sq. Neither value below is
#: read from the pinned lambdapdk files (they publish no sheet resistance);
#: both are klt placeholders for a predictive PDK, labelled as such.
ASSUMED_M1_SHEET_RHO_OHM_SQ = 131.0
#: ASSUMED MOM capacitance per um^2 of marker footprint, farads.
ASSUMED_MOM_AREA_CAP_F_UM2 = 2.0e-16

EXTRACTION_DECK = FinFETExtractionDeck(
    active=(11, 0),
    # The FinFET gate layer plays the planar `poly` role (gate conductor).
    poly=(7, 0),
    nwell=(1, 0),
    # V0: the via from both local interconnects (LISD/LIG) up to M1.
    contact=(18, 0),
    metals=_METALS,
    vias=_VIAS,
    metal_labels=tuple((layer, 251) for layer, _ in _METALS),
    well_label=(1, 251),
    poly_label=(7, 251),
    nfet_class="nmos_rvt",
    pfet_class="pmos_rvt",
    substrate_net="vsubs",
    fin=(2, 0),
    gate_cut=(10, 0),
    nselect=(12, 0),
    pselect=(13, 0),
    lisd=(17, 0),
    lig=(16, 0),
    vt_flavours=(
        FinFETFlavour(
            name="lvt",
            marker=(98, 0),
            nfet_class="nmos_lvt",
            pfet_class="pmos_lvt",
            provenance=_lyp("LVT drawing - 98/0"),
        ),
        FinFETFlavour(
            name="slvt",
            marker=(97, 0),
            nfet_class="nmos_slvt",
            pfet_class="pmos_slvt",
            provenance=_lyp("SLVT drawing - 97/0"),
        ),
        FinFETFlavour(
            name="sram",
            marker=(110, 0),
            nfet_class="nmos_sram",
            pfet_class="pmos_sram",
            provenance=_lyp("SRAMVT drawing - 110/0"),
        ),
        # Thick-oxide I/O device (#4466). The THICKOX marker (111/0) is the
        # assumed, predictive stream number from #4465 -- not in the pinned
        # lyp/DRM -- so this flavour carries the lyp citation of the VT
        # markers' sibling layer only as a pointer, and the nmos_io/pmos_io
        # card names are unconfirmed against 2AMLogic/pdks (declared gap).
        # A finger under a VT marker *and* THICKOX stays malformed geometry.
        FinFETFlavour(
            name="io",
            marker=(111, 0),
            nfet_class="nmos_io",
            pfet_class="pmos_io",
            provenance=_lyp("THICKOX drawing - 111/0 (predictive, #4465)"),
        ),
    ),
    nominal_dbu_um=0.00025,
    # Every one of the 2558 transistors in the pinned RVT CDL is l=20n.
    gate_lengths_um=(0.020,),
    nfet_provenance=_lyp("Active/Gate/fin/Nselect drawing - 11/0, 7/0, 2/0, 12/0"),
    pfet_provenance=_lyp("Active/Gate/fin/Pselect drawing - 11/0, 7/0, 2/0, 13/0"),
    fin_provenance=_lyp("fin drawing - 2/0"),
    lig_lisd_connected=True,
    sd_contact=(88, 0),
    # Passives (issue #4469): ASAP7 defines no resistor/MIM layers, so these
    # are klt conventions with ASSUMED predictive coefficients (see
    # docs/cli/extract.md, "ASAP7 passives").
    resistors=(
        ResistorDevice(
            name="RES",
            body=(19, 0),
            marker=(112, 0),
            sheet_rho_ohm_sq=ASSUMED_M1_SHEET_RHO_OHM_SQ,
        ),
    ),
    passive_capacitors=(
        FinFETPassiveCapacitor(
            name="CAP",
            marker=(113, 0),
            metal=(19, 0),
            area_cap_f_um2=ASSUMED_MOM_AREA_CAP_F_UM2,
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Parasitics (issue #4558)
# --------------------------------------------------------------------------- #
#
# First-order, uncalibrated, ASSUMED coefficients. Source: the OpenRCX
# extraction rules the pinned lambdapdk archive ships at
# ``lambdapdk/asap7/base/pex/openroad/typical.rules`` (sha256
# ``32a117e5e866d3de1406e8f18d65b6f1857781f156638941a4769e25ef5252cc``),
# with the minimum drawn widths from ``lambdapdk/asap7/base/apr/asap7_tech.lef``
# (sha256 ``830a032810b054d0dc24601c3f57c86073469e6c9cfb0314b2c82eb335b021d2``):
# M1-M3 0.018 um, M4-M5 0.024 um, M6-M7 0.032 um, M8-M9 0.040 um. The
# archive's ``asap7_drm_201207a.pdf`` carries no machine-readable sheet
# resistance, so every number below is ASSUMED in the sense that the rules
# file does not state its units; the reading used is:
#
# - ``sheet_res_ohm_sq``: the first row of ``Metal <n> RESOVER 0`` (last
#   column), read as kohm/um, times the minimum drawn width (um) -> ohm/sq.
# - ``cap_area_ff_um2``: the last column of ``Metal <n> OVER 0`` (constant
#   down the table), read as fF per um of wire length at minimum width,
#   divided by that width -> fF/um^2.
# - ``cap_perim_ff_um``: the fringe column of the first row of
#   ``Metal <n> OVER 0`` (nearest-neighbour row), read as fF/um.
# - ``metal_overlaps[i]``: the coupling column of the first row of
#   ``Metal <i+1> UNDER <i+2>``, divided by the lower level's minimum width
#   -> fF/um^2.
#
# Cut resistance has no public source in the pinned archive: every cut is the
# single ASSUMED_CUT_OHM placeholder. None of this is calibrated against
# silicon or a signoff extractor; ``diffusion``/``poly`` are left uncurated.
ASSUMED_CUT_OHM = 10.0

PARASITICS = ParasiticsDeck(
    metals=(
        # M1 (width 0.018)
        LayerRC(
            sheet_res_ohm_sq=0.5632,
            cap_area_ff_um2=0.86908,
            cap_perim_ff_um=1.51331e-05,
        ),
        # M2 (0.018)
        LayerRC(
            sheet_res_ohm_sq=0.5348,
            cap_area_ff_um2=0.82535,
            cap_perim_ff_um=8.84607e-06,
        ),
        # M3 (0.018)
        LayerRC(
            sheet_res_ohm_sq=0.5632,
            cap_area_ff_um2=0.86908,
            cap_perim_ff_um=1.11794e-05,
        ),
        # M4 (0.024)
        LayerRC(
            sheet_res_ohm_sq=0.4329,
            cap_area_ff_um2=0.37576,
            cap_perim_ff_um=9.87151e-06,
        ),
        # M5 (0.024)
        LayerRC(
            sheet_res_ohm_sq=0.4558,
            cap_area_ff_um2=0.39570,
            cap_perim_ff_um=1.14609e-05,
        ),
        # M6 (0.032)
        LayerRC(
            sheet_res_ohm_sq=0.3801,
            cap_area_ff_um2=0.18562,
            cap_perim_ff_um=1.04115e-05,
        ),
        # M7 (0.032)
        LayerRC(
            sheet_res_ohm_sq=0.4003,
            cap_area_ff_um2=0.19546,
            cap_perim_ff_um=9.96683e-06,
        ),
        # M8 (0.040)
        LayerRC(
            sheet_res_ohm_sq=0.3379,
            cap_area_ff_um2=0.10558,
            cap_perim_ff_um=7.30467e-06,
        ),
        # M9 (0.040)
        LayerRC(
            sheet_res_ohm_sq=0.3558,
            cap_area_ff_um2=0.11118,
            cap_perim_ff_um=8.09639e-06,
        ),
    ),
    # Adjacent-pair vertical coupling, M1-M2 .. M8-M9 (see the reading above).
    metal_overlaps=(
        0.004884,
        0.006023,
        0.005119,
        0.004253,
        0.004494,
        0.004163,
        0.004292,
        0.002511,
    ),
    cuts=tuple(
        CutRC(
            name=f"V{level + 1}", lower_metal_index=level, nominal_ohm=ASSUMED_CUT_OHM
        )
        for level in range(8)
    ),
)
