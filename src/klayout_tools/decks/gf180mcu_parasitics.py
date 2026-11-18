"""`klt extract --parasitics` first-order lumped-RC coefficients for the
gf180mcu deck.

Sourced, citable values transcribed from the *public* gf180mcu magic
technology file `gf180mcu/magic/gf180mcu.tech` in fossi-foundation/open-pdks
(GPLv3 -- the same upstream the DRC decks are transcribed from), nominal
corner (the `variants ()` block). Sheet resistances come from that file's
`resist <layer> <milliohms per square>` entries; area/fringe capacitances
from its `defaultareacap` (aF/um^2) and `defaultperimeter` (aF/um) entries.
The .tech header credits GlobalFoundries' PDS_035_03; nothing NDA'd. Values
remain order-of-magnitude and uncalibrated-to-silicon: calibrating
parasitic-extraction accuracy against silicon is an explicit non-goal of
this first cut (issue #216 "Non-goals"). `metals` is index-aligned with
``gf180mcu.EXTRACTION_DECK``'s `metals` stack: index 0 is Metal1. Values
expressed as (sheet ohms/square, area cap fF/um^2, fringe cap fF/um).

The `metal_sidewalls` lateral-coupling table and its lookback distances
(issue #5742) carry their full provenance -- corner-block selection, the
pinned PDK commits, and the 2x-minimum-spacing lookback rationale -- in
`docs/design/extract-fidelity-roadmap.md` ("gf180mcu transcription").

Split out of :mod:`.gf180mcu` (issue #5742) when this table's growth
pushed that module past its file-size ratchet ceiling; re-exported there
so the `decks` registry and every `gf180mcu.PARASITICS` reader is
unaffected.
"""

from __future__ import annotations

from .parasitics import CutRC, LayerRC, ParasiticsDeck

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
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.03,
            cap_perim_ff_um=0.039431,
        ),
        # Metal2 (issue #547). sheet from `resist (allm2)/metal2 90`
        # (unconditional -- M2 is never the top/thick metal in any gf180mcu
        # stack option); area/perim from `defaultareacap`/`defaultperimeter`
        # allm2 metal2 (15.016 aF/um^2, 33.298 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.015016,
            cap_perim_ff_um=0.033298,
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
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.010094,
            cap_perim_ff_um=0.030021,
        ),
        # Metal4 (issue #547). sheet from `resist (allm4)/metal4 90`, the
        # `#ifdef METALS5 || METALS6` row (same reasoning as Metal3 -- not the
        # top metal in a 5-level stack). area/perim from
        # `defaultareacap`/`defaultperimeter` allm4 metal4 (7.602 aF/um^2,
        # 28.153 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.007602,
            cap_perim_ff_um=0.028153,
        ),
        # Metal5 (issue #547, revised by #572 and again by #3540), the top
        # metal of this 5-level stack. sheet from the resolved, pre-built
        # `gf180mcuD.tech` for the pinned PDK commit
        # f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 (nominal `variants ()`
        # corner block, line 3347): `resist (allm5)/metal5    40` (40
        # milliohm/sq = 0.04 ohm/sq). At the earlier pin
        # c6d73a35f524070e85faff4a6a9eef49553ebc2b that same resolved row read
        # 60 (the other two corner blocks 70 -> 49 and 50 -> 31), which #572
        # adopted over #571's 40 taken from the unresolved generic
        # `gf180mcu.tech`; f6eeac7's resolved file now carries 40, matching
        # the value #572 rejected, so 0.04 here is a re-pin delta rather than
        # a return to #571's derivation. No other `resist`/`defaultareacap`/
        # `defaultperimeter`/`defaultoverlap allm[2-5]` row changed between
        # the two pins. area/perim from `defaultareacap`/`defaultperimeter`
        # allm5 metal5 (5.798 aF/um^2, 30.386 aF/um) -- unconditional across
        # every THICKMET option.
        LayerRC(
            sheet_res_ohm_sq=0.04,
            sheet_res_high_ohm_sq=0.049,
            sheet_res_low_ohm_sq=0.031,
            cap_area_ff_um2=0.005798,
            cap_perim_ff_um=0.030386,
        ),
    ),
    # `defaultoverlap` vertical plate-coupling coefficients (issue #760,
    # Extract Stage 2a): entry `i` is the coefficient between `metals[i]`
    # (lower plate) and `metals[i+1]` (upper plate), so a fully-populated
    # table has `len(metals) - 1` = 4 entries for this 5-level stack.
    #
    # Transcribed from the resolved, pre-built `gf180mcuD.tech` for the
    # pinned PDK commit f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 (values
    # re-checked unchanged from c6d73a3, issue #3540), nominal
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
    # `defaultsidewall` lateral (same-layer) coupling coefficients (issue
    # #5742): entry `i` describes `metals[i]`'s own same-layer coupling
    # (like sky130's table; unlike `metal_overlaps`' adjacent-*pair*
    # indexing above, so 5 entries for this Metal1..Metal5 stack). First
    # parameter only (aF/um / 1000 -> fF/um, matching `cap_perim_ff_um`'s
    # convention) -- the second parameter stays unread (roadmap open
    # question). Corner-block selection, pinned-commit provenance and the
    # `#ifdef METALS5` Metal5 row: docs/design/extract-fidelity-roadmap.md
    # ("gf180mcu transcription", #5742).
    metal_sidewalls=(
        # Metal1 (idx0): `defaultsidewall allm1 metal1 40.512 -0.053`.
        0.040512,
        # Metal2 (idx1): `defaultsidewall allm2 metal2 46.736 0.289`.
        0.046736,
        # Metal3 (idx2): `defaultsidewall allm3 metal3 70.675 0.534`.
        0.070675,
        # Metal4 (idx3): `defaultsidewall allm4 metal4 77.388 0.611`.
        0.077388,
        # Metal5 (idx4): `defaultsidewall allm5 metal5 114.86 0.025` (the
        # `#ifdef METALS5` row).
        0.11486,
    ),
    # Lateral-coupling lookback distance per metal level (issue #5742),
    # sky130's convention: **2x** this deck's own same-layer minimum
    # spacing for that level (`metal[1-5].space.1`), not magic's
    # `defaultsidewall` second parameter. The 2x rationale:
    # docs/design/extract-fidelity-roadmap.md ("gf180mcu transcription").
    metal_sidewall_lookback_um=(
        0.46,  # Metal1 (idx0): 2x `metal1.space.1` (0.23).
        0.56,  # Metal2 (idx1): 2x `metal2.space.1` (0.28).
        0.56,  # Metal3 (idx2): 2x `metal3.space.1` (0.28).
        0.56,  # Metal4 (idx3): 2x `metal4.space.1` (0.28).
        0.56,  # Metal5 (idx4): 2x `metal5.space.1` (0.28).
    ),
    # Per-cut via resistance and the interconnect-resistance corners (issue
    # #3053), from the same resolved `gf180mcuD.tech` / pinned PDK commit
    # f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 as Metal5 above. Magic states
    # `contact` rows in milliohm per cut; divided by 1000. Nominal: `variants
    # ()` block (L3355-3358); high-R: `variants (hrhc),(hrlc)` (L3404-3407,
    # 15000 = 15 ohm, 3.3x nominal); low-R: `variants (lrhc),(lrlc)`
    # (L3453-3456, 0 -- an ideal short, stored as published, not clamped; see
    # `CutRC`). The high/low *metal* sheet resistances are on `metals` above
    # (L3390-3396 / L3439-3445). `lower_metal_index` is the Metal level the
    # cut lands on: m2c = Metal1->Metal2, m3c = Metal2->Metal3, via3 =
    # Metal3->Metal4, via4 = Metal4->Metal5. `mimcc` (MiM cap contact, also
    # 4.5 ohm) and the diffusion/poly contacts (`ndc` ... `pc`) are not
    # curated: they are not between two metals of this stack.
    cuts=(
        CutRC("m2c", 0, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("m3c", 1, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("via3", 2, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("via4", 3, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
    ),
)
