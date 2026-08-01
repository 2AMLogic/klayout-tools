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

from . import DrcRule, ExtractionDeck, ParasiticLayer, ParasiticTech

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
)

# --------------------------------------------------------------------------- #
# `klt extract --parasitics` coefficient table (issue #217)
# --------------------------------------------------------------------------- #
#
# Sheet resistance and area/perimeter capacitance-to-substrate coefficients,
# transcribed from sky130's own **public** process data as published in the
# magic technology file `sky130/magic/sky130.tech` in
# https://github.com/fossi-foundation/open-pdks -- the same canonical
# open_pdks repository (GPLv3) the DRC deck at the top of this module is
# transcribed from, and the file open_pdks itself installs as the sky130
# extraction rule set. Nothing here comes from an NDA'd source; the file's own
# header credits SkyWater's public `PEX/xRC/cap_models` document for the
# capacitance numbers and `trtc.cor` (typical corner) for the resistances.
#
# Both blocks below are taken from the file's **nominal** corner section
# (`variants (),(orig),(si)` -- the one open_pdks uses by default); the
# `(hrhc),(hrlc)` and low-corner sections are deliberately not modelled, per
# the "fixed, not tunable" decision in
# `docs/design/lvs-extraction-spike.md` -> "Addendum (#216)".
#
# Sheet resistance -- sky130.tech "# Resistances are in milliohms per square",
# so each value below is the file's number / 1000:
#
#     resist (allpolynonres)/active   48200   ->  48.2   ohm/sq  (poly)
#     resist (allli)/locali           12800   ->  12.8   ohm/sq  (li1)
#     resist (allm1)/metal1             125   ->   0.125 ohm/sq  (met1)
#
# Capacitance -- sky130.tech "# Units are aF/um^2 for area caps and aF/um for
# perimeter and sidewall caps", `defaultareacap` = parallel-plate cap to the
# plane below, `defaultperimeter` = fringe cap to the plane below:
#
#     defaultareacap   *poly   active 106.13   defaultperimeter *poly  active 55.27
#     defaultareacap   allli   locali  36.99   defaultperimeter allli  locali 40.70
#     defaultareacap   allm1   metal1  25.78   defaultperimeter allm1  metal1 40.57
#
# The file's `defaultsidewall` / `defaultoverlap` / `defaultsideoverlap`
# entries (same-layer neighbour coupling and layer-to-layer overlap) are
# deliberately **not** transcribed: they are net-to-net coupling terms, which
# the first-cut lumped capacitance-to-ground model has nowhere to put and
# which #217 explicitly defers.
#
# Diffusion (`active`/`tap`) has no entry, following the source file's own
# instruction -- sky130.tech comments out its `allnactivenonfet` /
# `allpactivenonfet` cap entries with "Rely on device models to capture
# *ndiff area cap", and `klt extract`'s `M` cards already emit AS/AD/PS/PD.
PARASITICS = ParasiticTech(
    poly=ParasiticLayer(  # poly.drawing (66/20)
        sheet_ohm_per_sq=48.2,
        area_cap_af_per_um2=106.13,
        perimeter_cap_af_per_um=55.27,
    ),
    metals=(
        ParasiticLayer(  # li1.drawing (67/20) -- EXTRACTION_DECK.metals[0]
            sheet_ohm_per_sq=12.8,
            area_cap_af_per_um2=36.99,
            perimeter_cap_af_per_um=40.70,
        ),
        ParasiticLayer(  # met1.drawing (68/20) -- EXTRACTION_DECK.metals[1]
            sheet_ohm_per_sq=0.125,
            area_cap_af_per_um2=25.78,
            perimeter_cap_af_per_um=40.57,
        ),
    ),
)
