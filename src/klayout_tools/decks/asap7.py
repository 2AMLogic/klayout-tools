"""ASAP7 (7 nm predictive FinFET, ASU/ARM, BSD-3-Clause) FinFET extraction
deck (issue #2761).

**Scope: extraction only.** This module registers ASAP7 with ``klt
extract`` -- FinFET device recognition with geometry-counted fins -- and
nothing else. ASAP7 DRC is a separate artefact (the DRC-DSL deck
``asap7.drc`` beside this module, #2760, run through ``klt drc --engine
klayout --deck-file``); there are no parasitics coefficients, no ``--pdk``
subcircuit binding, and ``klt lvs`` refuses a FinFET deck until its
reference reader preserves ``nfin`` (#2813; see ``docs/cli/extract.md``,
"ASAP7 FinFET extraction").

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

from .finfet import FinFETExtractionDeck, FinFETFlavour
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
    ),
    nominal_dbu_um=0.00025,
    # Every one of the 2558 transistors in the pinned RVT CDL is l=20n.
    gate_lengths_um=(0.020,),
    nfet_provenance=_lyp("Active/Gate/fin/Nselect drawing - 11/0, 7/0, 2/0, 12/0"),
    pfet_provenance=_lyp("Active/Gate/fin/Pselect drawing - 11/0, 7/0, 2/0, 13/0"),
    fin_provenance=_lyp("fin drawing - 2/0"),
    lig_lisd_connected=True,
    sd_contact=(88, 0),
)
