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

from . import BULK_LAYER, DerivedLayer, DeviceSpec, DrcRule, ExtractionDeck

# Database units are nanometres (sky130 streams use dbu_um = 0.001), so a
# threshold in micrometres times 1000 gives threshold_dbu.
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

# --------------------------------------------------------------------------
# Extraction (connectivity + device) deck
# --------------------------------------------------------------------------
#
# Drives `klt extract` (klayout.db.LayoutToNetlist) — see
# docs/design/lvs-extraction-spike.md for the engine choice and
# docs/cli/extract.md for the contract.
#
# Layer/datatype pairs below come from the same `sky130.lyt` layer map the
# DRC deck above was transcribed from (fossi-foundation/open-pdks,
# `sky130/klayout/sky130.lyt`), extended past the DRC deck's poly/li1/met1
# subset to cover the full drawn interconnect stack extraction needs:
#
#     nwell.drawing   64/20     nwell.label   64/5
#     pwell.label     64/59
#     diff.drawing    65/20
#     tap.drawing     65/44
#     poly.drawing    66/20
#     licon1.drawing  66/44
#     li1.drawing     67/20     li1.label     67/5
#     mcon.drawing    67/44
#     met1.drawing    68/20     met1.label    68/5
#     via.drawing     68/44
#     met2.drawing    69/20     met2.label    69/5
#     via2.drawing    69/44
#     met3.drawing    70/20     met3.label    70/5
#     via3.drawing    70/44
#     met4.drawing    71/20     met4.label    71/5
#     via4.drawing    71/44
#     met5.drawing    72/20     met5.label    72/5
#
# Scope guard (mirrors the DRC deck's): this is a *curated* recipe, not a
# transcription of a full PDK LVS setup. It extracts MOS devices only —
# resistors, capacitors, diodes, and bipolars are not recognised, and the two
# extracted MOS classes are the core 1.8 V flavours; thick-oxide (5 V) and
# hvt/lvt variants are not distinguished from them, because doing so needs
# the implant/marker layer booleans a follow-on increment adds. Coverage is
# expected to grow incrementally, exactly as the DRC deck's has.
#
# Substrate handling follows KLayout's own LVS idiom: sky130 draws no p-well
# layer, so the nfet body terminal binds to the synthetic empty `bulk` region
# (decks.BULK_LAYER), which is then tied to the p-substrate taps and given
# the global net name "VSUBS".

EXTRACTION = ExtractionDeck(
    name="sky130",
    layers={
        "nwell": (64, 20),
        "diff": (65, 20),
        "tap": (65, 44),
        "poly": (66, 20),
        "licon1": (66, 44),
        "li1": (67, 20),
        "mcon": (67, 44),
        "met1": (68, 20),
        "via": (68, 44),
        "met2": (69, 20),
        "via2": (69, 44),
        "met3": (70, 20),
        "via3": (70, 44),
        "met4": (71, 20),
        "via4": (71, 44),
        "met5": (72, 20),
    },
    texts={
        "nwell_label": (64, 5),
        "pwell_label": (64, 59),
        "li1_label": (67, 5),
        "met1_label": (68, 5),
        "met2_label": (69, 5),
        "met3_label": (70, 5),
        "met4_label": (71, 5),
        "met5_label": (72, 5),
    },
    derived=(
        # Diffusion inside an n-well is p-type (pfet source/drain); the rest
        # is n-type. Using the well rather than the nsdm/psdm implants keeps
        # the recipe working on streams that ship well geometry but omit the
        # implant layers.
        DerivedLayer("pactive", "and", "diff", "nwell"),
        DerivedLayer("nactive", "not", "diff", "nwell"),
        # Channel = active under poly; source/drain = the remainder.
        DerivedLayer("pgate", "and", "pactive", "poly"),
        DerivedLayer("psd", "not", "pactive", "poly"),
        DerivedLayer("ngate", "and", "nactive", "poly"),
        DerivedLayer("nsd", "not", "nactive", "poly"),
        # Taps: inside the n-well they bias the well, outside they bias the
        # p-substrate.
        DerivedLayer("ntap", "and", "tap", "nwell"),
        DerivedLayer("ptap", "not", "tap", "nwell"),
    ),
    devices=(
        DeviceSpec(
            name="nfet_01v8",
            kind="mos4",
            gate="ngate",
            source_drain="nsd",
            gate_conductor="poly",
            well=BULK_LAYER,
        ),
        DeviceSpec(
            name="pfet_01v8",
            kind="mos4",
            gate="pgate",
            source_drain="psd",
            gate_conductor="poly",
            well="nwell",
        ),
    ),
    intra_connect=(
        "psd",
        "nsd",
        "ptap",
        "ntap",
        "nwell",
        "poly",
        "licon1",
        "li1",
        "mcon",
        "met1",
        "via",
        "met2",
        "via2",
        "met3",
        "via3",
        "met4",
        "via4",
        "met5",
    ),
    inter_connect=(
        ("licon1", "psd"),
        ("licon1", "nsd"),
        ("licon1", "ptap"),
        ("licon1", "ntap"),
        ("licon1", "poly"),
        ("licon1", "li1"),
        ("mcon", "li1"),
        ("mcon", "met1"),
        ("via", "met1"),
        ("via", "met2"),
        ("via2", "met2"),
        ("via2", "met3"),
        ("via3", "met3"),
        ("via3", "met4"),
        ("via4", "met4"),
        ("via4", "met5"),
        ("ntap", "nwell"),
        (BULK_LAYER, "ptap"),
    ),
    global_connect=((BULK_LAYER, "VSUBS"),),
    labels=(
        ("nwell", "nwell_label"),
        ("ptap", "pwell_label"),
        ("li1", "li1_label"),
        ("met1", "met1_label"),
        ("met2", "met2_label"),
        ("met3", "met3_label"),
        ("met4", "met4_label"),
        ("met5", "met5_label"),
    ),
)
