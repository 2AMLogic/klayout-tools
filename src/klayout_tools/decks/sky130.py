"""sky130 DRC deck: a curated subset of the official rule set.

Layer/datatype pairs and rule thresholds below are transcribed from the
official, community-maintained sky130 KLayout DRC deck and layer map,
fetched directly from the canonical source repository for this issue:

- ``sky130/klayout/sky130.lydrc`` (the DRC-DSL rule script)
- ``sky130/klayout/sky130.lyt`` (the layer map / layer-properties technology
  file, which defines the ``name.purpose : layer/datatype`` pairs used below,
  e.g. ``poly.drawing : 66/20``)

both from https://github.com/fossi-foundation/open-pdks (the current home of
the repo historically known as ``open_pdks``/``RTimothyEdwards/open_pdks``;
distributed under GPLv3). Each rule's docstring cites the exact source line
(rule id and comment) it was transcribed from, so values can be re-verified
against a fresh checkout of that file at any time.

This is *not* a full transcription of the official deck (hundreds of rules
across dozens of layers) — see the "Scope guard" section of issue #9: a
curated starter subset spanning width/space/enclosure checks across
poly/diff/li1/met1/licon1/mcon, wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) and produce a non-trivial worked
example. Coverage is expected to grow incrementally in follow-on issues.

Negative finding — bipolar (BJT) device-mark rule (issue #183): sky130's
layer map (``sky130.lyt``) defines a ``pnp.drawing`` mark layer (82/44,
alongside an unused ``npn.drawing`` at 82/20) that the vertical-PNP bipolar
device library cells (e.g. ``sky130_fd_pr__pnp_05v5``) draw over themselves
for device recognition — the sky130 counterpart of gf180mcu's ``DRC_BJT``
mark layer (127/5, see ``gf180mcu.py``). But the *only* rule in
``sky130.lydrc`` that references it is ``dnwell.4``::

    dnwell.and(pnp).output("dnwell.4", "dnwell.4 : dnwell must not overlap pnp")

— a compatibility exclusion between two unrelated process layers (``dnwell``,
the deep-nwell triple-well isolation layer at 64/18, is a *different* layer
from ``nwell.drawing`` at 64/20, and is not drawn by this curated deck or by
``bjt_array``'s sky130 output, whose ``well`` role is ``None``). It is not a
separation/spacing rule protecting
the bipolar device from unrelated diffusion/tap the way gf180mcu's
``BJT.3`` (``bjt.separation.comp.1`` below) is, and its "must never overlap
at all" semantics have no representation among the check kinds this
engine's ``Region.*_check()`` dispatch supports (``width``/``space``/
``notch``/``separation``/``enclosing``/``enclosed``/``overlap`` — see
``DrcRule``'s docstring): ``overlap_check`` enforces a *minimum required*
overlap, the opposite of "forbid any overlap," so transcribing ``dnwell.4``
under that check kind would silently pass every layout rather than flag
anything. Conclusion: sky130's official DRC deck has no analogue to
gf180mcu's ``BJT.3`` bipolar device-mark separation rule; sky130's vertical
PNP device relies on ``pnp.drawing`` for LVS/device-recognition purposes
only, not DRC-level mark/separation checking. No rule was added here, and
``_PDK_ROLE_LAYERS["sky130"]["bjt_mark"]`` in ``klayout_tools.gen`` remains
``None`` accordingly.

Follow-up (issue #223): ``pnp.drawing`` (82/44) is now consumed for exactly
that LVS/device-recognition purpose -- ``EXTRACTION_DECK.bipolars`` below
uses it as the ``marker`` layer of a :class:`~klayout_tools.decks.BipolarDevice`
entry, scoping ``klt extract``'s ``DeviceExtractorBJT3Transistor`` wiring to
genuine ``sky130_fd_pr__pnp_05v5``-style device-cell instances rather than
every drawn ``nwell.drawing`` region. ``npn.drawing`` (82/20) is deliberately
*not* wired into a second ``bipolars`` entry: this module's own finding above
(no ``.lydrc`` rule references it beyond the unrelated ``dnwell.4``
compatibility exclusion) is the only evidence available in this repo, and it
does not on its own establish that some sky130 bipolar device cell draws it
for recognition the way ``pnp.drawing`` is drawn by ``..._pnp_05v5`` -- absent
that positive confirmation (this repo vendors no sky130 device-library GDS to
check directly), this deck stays with the one PNP entry it can attribute to a
concretely-named device cell.

``klt extract``'s MiM-capacitor device recognition (``EXTRACTION_DECK.capacitors``
below, issue #225) is transcribed from a *different* source than the DRC
rules above: neither ``sky130.lydrc`` nor ``sky130.lyt`` (both cited above)
carries a MiM-cap device-recognition derivation or capacitance-per-area
coefficient -- that lives in the companion **KLayout LVS** deck,
``sky130.lvs``, which (like ``sky130A.lyp``, the layer-properties file naming
the ``capm``/``capm2`` mark layers below) is fetched by ``open_pdks`` from a
*different* upstream repo than the ``open-pdks`` mono-repo itself:
``efabless/sky130_klayout_pdk`` (its ``klayout-repo`` Makefile target;
confirmed via a real sky130A install, volare,
``libs.tech/klayout/lvs/sky130.lvs`` / ``libs.tech/klayout/tech/sky130A.lyp``).
``sky130.lvs`` derives and extracts sky130's two independent MiM stacks
unconditionally (no foundry-side selectable-density option the way
gf180mcu's does -- see ``gf180mcu.py``'s own provenance note)::

    connect(capm , via3)
    connect(met3_ncap, met3_con)
    extract_devices(capacitor("sky130_fd_pr__model__cap_mim", 2e-15, MIMCap),
                    { "P1" => met3_con, "P2" => capm })

    connect(capm2, via4)
    connect(met4_ncap, met4_con)
    extract_devices(capacitor("sky130_fd_pr__model__cap_mim_m4", 2e-15, MIMCap),
                    { "P1" => met4_con, "P2" => capm2 })

i.e. sheet capacitance **2.0 fF/um^2** for both, a plain two-terminal
``DeviceExtractorCapacitor`` (no bulk terminal), with the bottom plate simply
"whatever conductor the purpose-drawn top-plate mark layer sits over" -- no
"virtual bottom plate" sizing derivation needed the way gf180mcu's MiM stack
requires (sky130's ``capm``/``capm2`` are purpose-built device-recognition
layers, not an ordinary routing-metal layer a real device geometrically
implies). Both curated `capacitors` entries below reuse the deck's own
``met3``/``met4`` (70/20, 71/20) conductor layers as ``bottom_plate`` --
unfiltered, since ``capm``/``capm2`` alone already scope recognition to
genuine MiM-cap instances -- rather than the full official
``met3_con``/``met4_con`` derivations (which additionally exclude
``met3_res``/``met4_res``/``inductor``/``met3_fuse``/``met4_fuse``/``vpp``,
none of which this curated deck otherwise models -- the same "curated
starter subset" scope guard elsewhere in this file).

Two rules below (``poly.width.1`` is not one of them) approximate an
official rule defined on a *compound* layer expression (a union of two mask
layers) as a check against a single drawn layer, because our engine (native
``klayout.db.Region`` check primitives — see ``docs/cli/drc.md``) checks one
layer, or one layer against one other layer, at a time; it does not evaluate
arbitrary boolean layer expressions the way the DRC-DSL script runner does.
Each such approximation is called out explicitly in its rule's docstring.

Layer numbers (verified against ``sky130.lyt``'s ``layer-map`` and the
``sky130.lydrc`` layer variable definitions, e.g. ``poly = polygons(66,
20)``):

    diff.drawing    65/20
    tap.drawing     65/44
    poly.drawing    66/20
    licon1.drawing  66/44
    li1.drawing     67/20
    mcon.drawing    67/44
    met1.drawing    68/20
    via.drawing     68/44
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
# nanometres (dbu_um = 0.001), so a threshold in micrometres times 1000 gives
# threshold_dbu. `run_drc()` rescales threshold_dbu by NOMINAL_DBU_UM /
# layout.dbu at run time, so the deck still gives correct results against a
# layout written at a different dbu (see DrcRule's docstring).
NOMINAL_DBU_UM = 0.001

DECK: list[DrcRule] = [
    DrcRule(
        id="poly.width.1",
        description="minimum poly width",
        layer=(66, 20),  # poly.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # sky130.lydrc rule "poly.1a": poly.width(0.15, euclidian)
        # -> "poly.1a : min. poly width : 0.15um"
    ),
    DrcRule(
        id="diff.width.1",
        description="minimum diff width (approximates the official difftap.1 rule)",
        layer=(65, 20),  # diff.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # sky130.lydrc rule "difftap.1": difftap.width(0.15, euclidian), where
        # difftap = diff.or(tap) -- a compound layer our engine does not
        # evaluate; approximated here by checking diff.drawing alone. The
        # threshold value (0.15um) is the real, unmodified source value.
        # -> "difftap.1 : min. difftap width : 0.15um"
    ),
    DrcRule(
        id="li1.width.1",
        description="minimum li1 (local interconnect) width",
        layer=(67, 20),  # li1.drawing
        check="width",
        threshold_dbu=170,  # 0.17 um
        # sky130.lydrc rule "li.1": not_in_cell5_li.width(0.17, euclidian)
        # -> "li.1 : min. li width : 0.17um"
        # (the "not_in_cell5_li" exclusion covers a handful of named analog
        # macro cells not modelled here; general-case threshold is used.)
    ),
    DrcRule(
        id="li1.space.1",
        description="minimum li1 (local interconnect) spacing",
        layer=(67, 20),  # li1.drawing
        check="space",
        threshold_dbu=170,  # 0.17 um
        # sky130.lydrc rule "li.3": not_in_cell5_li.space(0.17, euclidian)
        # -> "li.3 : min. li spacing : 0.17um"
    ),
    DrcRule(
        id="met1.width.1",
        description="minimum met1 width",
        layer=(68, 20),  # met1.drawing
        check="width",
        threshold_dbu=140,  # 0.14 um
        # sky130.lydrc rule "m1.1": m1.width(0.14, euclidian)
        # -> "m1.1 : min. m1 width : 0.14um"
    ),
    DrcRule(
        id="met1.space.1",
        description="minimum met1 spacing",
        layer=(68, 20),  # met1.drawing
        check="space",
        threshold_dbu=140,  # 0.14 um
        # sky130.lydrc rule "m1.2": non_huge_m1.space(0.14, euclidian)
        # -> "m1.2 : min. m1 spacing : 0.14um"
        # (the wide-metal 0.28um exception, "m1.3ab", is not modelled here.)
    ),
    DrcRule(
        id="met1.enclosing.mcon.1",
        description="minimum met1 enclosure of mcon",
        layer=(68, 20),  # met1.drawing
        other_layer=(67, 44),  # mcon.drawing
        check="enclosing",
        threshold_dbu=30,  # 0.03 um
        # sky130.lydrc rule "m1.4": not_in_cell6_m1.enclosing(mcon, 0.03, euclidian)
        # -> "m1.4 : min. m1 enclosure of mcon : 0.03um"
    ),
    DrcRule(
        id="diff.enclosing.licon.1",
        description="minimum diff enclosure of licon1",
        layer=(65, 20),  # diff.drawing
        other_layer=(66, 44),  # licon1.drawing
        check="enclosing",
        threshold_dbu=40,  # 0.04 um
        # sky130.lydrc rule "licon.5": diff.enclosing(licon, 0.04, euclidian)
        # -> "licon.5 : min. diff enclosure of licon : 0.04um"
    ),
    DrcRule(
        id="poly.enclosing.licon.1",
        description="minimum poly enclosure of licon1",
        layer=(66, 20),  # poly.drawing
        other_layer=(66, 44),  # licon1.drawing
        check="enclosing",
        threshold_dbu=50,  # 0.05 um
        # sky130.lydrc rule "licon.8": poly.enclosing(licon, 0.05, euclidian)
        # -> "licon.8 : min. poly enclosure of licon : 0.05um"
    ),
    DrcRule(
        id="mcon.space.1",
        description="minimum mcon spacing",
        layer=(67, 44),  # mcon.drawing
        check="space",
        threshold_dbu=190,  # 0.19 um
        # sky130.lydrc rule "ct.2": mcon.space(0.19, euclidian)
        # -> "ct.2 : min. mcon spacing : 0.19um"
    ),
]

# (layer, datatype) -> "name.purpose" string, from sky130.lyt's layer-map,
# used only to render violations[].layer as e.g. "poly.drawing" instead of
# the bare "66/20" fallback.
LAYER_NAMES: dict[tuple[int, int], str] = {
    (65, 20): "diff.drawing",
    (65, 44): "tap.drawing",
    (66, 20): "poly.drawing",
    (66, 44): "licon1.drawing",
    (67, 20): "li1.drawing",
    (67, 44): "mcon.drawing",
    (68, 20): "met1.drawing",
    (68, 44): "via.drawing",
    (82, 44): "pnp.drawing",
    (69, 20): "met2.drawing",
    (70, 20): "met3.drawing",
    (71, 20): "met4.drawing",
    (89, 44): "capm.drawing",
    (97, 44): "capm2.drawing",
}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #
#
# Layer numbers not already covered above, from the same source cited at the
# top of this module (sky130.lyt's layer-map / open_pdks):
#
#     nwell.drawing   64/20
#     nwell.pin       64/5   (a text/label layer -- carries the body-tie pin
#                             name, e.g. "VPB", directly on the well shape)
#     poly.pin        66/5   (a text/label layer on the poly shape itself --
#                             the datatype-5 ".pin" convention every other
#                             label layer here follows; used to name a gate
#                             node that has no metal landing pad, see below)
#     li1.pin         67/5
#     met1.pin        68/5
#
# Verified against this repo's own sky130 corpus fixtures
# (`tests/corpus/sky130/sky130_fd_sc_hd__*.gds`): every cell's nwell body pin
# is labelled directly on layer 64/5 (e.g. "VPB"); li1/met1 signal and power
# pins are labelled on 67/5 / 68/5 respectively (e.g. "A", "Y", "VPWR",
# "VGND"). `tap.drawing` (65/44) is a *distinct* drawn layer from
# `diff.drawing` (65/20) -- sky130 never draws a substrate/well tie on the
# transistor active layer -- so it is safe to connect `tap` to the well and
# to `contact` directly (see `ExtractionDeck`'s docstring for why the
# opposite (connecting the *well* to *every* contact inside it) is wrong).
#
# NMOS body: sky130 draws no separate substrate/pwell layer for a curated
# subset like this one (the whole non-well area is one native P-substrate),
# so there is no drawn tap geometry to derive a real net name from; the NMOS
# body terminal is tied to the deck's `substrate_net` global instead
# (`ExtractionDeck.substrate_net`, default `"vsubs"`) -- a documented
# approximation, not a real substrate-tap extraction (this repo's real
# sky130 corpus cells keep VPB (nwell) as a genuine standalone pin, but never
# expose an equivalent pin for the native substrate at the single-cell
# level -- see the well-tap connectivity open question in
# `docs/design/lvs-extraction-spike.md`).
#
# `poly_label` (66/5) mirrors `well_label`: a text on the poly layer names the
# poly net directly, so a device gate `klt gen` draws as bare poly -- with no
# contact/metal landing pad (see `gen.py`'s `_mos_unit_layout`, which gives
# S/D segments a contact+metal pad but leaves gate fingers as bare poly) --
# can still be promoted to a named `.SUBCKT` pin by `klt gen-compose`'s
# `pins[]` (#210). Unlike the metal/well pins above, sky130's own corpus
# cells route gates in li1 rather than labelling poly directly, so 66/5 has no
# corpus precedent here; it is a curated choice consistent with the
# datatype-5 ".pin" convention every other label layer in this deck follows.
EXTRACTION_DECK = ExtractionDeck(
    active=(65, 20),  # diff.drawing
    poly=(66, 20),  # poly.drawing
    nwell=(64, 20),  # nwell.drawing
    tap=(65, 44),  # tap.drawing -- distinct from diff.drawing, see above
    well_label=(64, 5),  # nwell.pin
    poly_label=(66, 5),  # poly.pin -- names a bare-poly gate node (#210)
    contact=(66, 44),  # licon1.drawing
    metals=((67, 20), (68, 20)),  # li1.drawing, met1.drawing
    metal_labels=((67, 5), (68, 5)),  # li1.pin, met1.pin
    vias=((67, 44),),  # mcon.drawing (li1 -> met1)
    # Vertical PNP (issue #223, see the module docstring's "Follow-up" note
    # above): base = nwell.drawing (the physical n-type body the device's
    # p+ emitter sits in), emitter = diff.drawing (the same p+ diffusion
    # mask an ordinary PMOS source/drain uses), marker = pnp.drawing --
    # `extract.py` intersects base/emitter with the marker so only genuine
    # `sky130_fd_pr__pnp_05v5`-style device-cell instances are recognised,
    # not every PMOS's nwell. No drawn collector layer -- the collector is
    # the native P-substrate, tied to `substrate_net` like the NMOS body
    # above (see `BipolarDevice`'s docstring).
    bipolars=(
        BipolarDevice(
            base=(64, 20),  # nwell.drawing
            emitter=(65, 20),  # diff.drawing
            marker=(82, 44),  # pnp.drawing
            class_name="pnp",
        ),
    ),
    # MiM capacitors (issue #225, see the module docstring's provenance note
    # above): two independent stacks, one per metal level sky130's official
    # LVS deck draws a purpose-built top-plate mark layer on. Neither
    # `bottom_plate` (met3/met4) is one of this deck's own `metals` (which
    # stops at met1 above) -- see `CapacitorDevice`'s "Known limitation": the
    # extracted capacitors' plate nets are not wired into this deck's li1/
    # met1-only connectivity stack. No `bottom_plate_oversize_um` -- unlike
    # gf180mcu's MiM stack, sky130's bottom plate is simply the conductor the
    # purpose-drawn top-plate layer sits over, no "virtual bottom plate"
    # derivation needed.
    capacitors=(
        CapacitorDevice(
            name="sky130_fd_pr__model__cap_mim",
            top_plate=(89, 44),  # capm.drawing
            bottom_plate=(70, 20),  # met3.drawing
            area_cap_f_um2=2.0e-15,  # 2.0 fF/um^2, see provenance note above
        ),
        CapacitorDevice(
            name="sky130_fd_pr__model__cap_mim_m4",
            top_plate=(97, 44),  # capm2.drawing
            bottom_plate=(71, 20),  # met4.drawing
            area_cap_f_um2=2.0e-15,  # 2.0 fF/um^2, see provenance note above
        ),
    ),
)

# --------------------------------------------------------------------------- #
# `klt extract --parasitics` first-order lumped-RC coefficients
# --------------------------------------------------------------------------- #
#
# Representative, uncalibrated starter values drawn from the *public* sky130
# process documentation (SkyWater sky130 open PDK, the same open-PDK source
# family cited at the top of this module; sheet resistances from the PDK's
# published per-layer R tables, area/fringe capacitances from its published
# interconnect parasitic-capacitance tables). Order-of-magnitude only --
# parasitic-extraction accuracy tuning/calibration against silicon is an
# explicit non-goal of the first cut (issue #216 "Non-goals"); these exist to
# give `--parasitics` a plausible, self-consistent RC magnitude per net, not a
# sign-off-grade extraction. `metals` is index-aligned with EXTRACTION_DECK's
# `metals` stack: index 0 is li1 (local interconnect -- relatively high sheet
# R), index 1 is met1 (low sheet R). Values expressed as (sheet ohms/square,
# area cap fF/um^2, fringe cap fF/um).
PARASITICS = ParasiticsDeck(
    # n+/p+ source-drain diffusion -- high sheet R; the area coefficient
    # stands in for the (bias-dependent) junction capacitance to bulk, taken
    # here as a fixed zero-bias-order approximation.
    diffusion=LayerRC(
        sheet_res_ohm_sq=120.0, cap_area_ff_um2=0.40, cap_perim_ff_um=0.30
    ),
    # poly gate/interconnect.
    poly=LayerRC(sheet_res_ohm_sq=48.2, cap_area_ff_um2=0.107, cap_perim_ff_um=0.067),
    metals=(
        # li1 local interconnect.
        LayerRC(sheet_res_ohm_sq=12.8, cap_area_ff_um2=0.037, cap_perim_ff_um=0.078),
        # met1.
        LayerRC(sheet_res_ohm_sq=0.125, cap_area_ff_um2=0.036, cap_perim_ff_um=0.082),
    ),
)
