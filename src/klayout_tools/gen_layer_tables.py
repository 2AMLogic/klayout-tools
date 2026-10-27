"""PDK per-family constant tables for :mod:`gen_layer_params`.

Data-only sibling of :mod:`klayout_tools.gen_layer_params` (issue #3722), in
the oversized-module split series (#1875, #3689): the geometry floors, the
``_PDK_ROLE_LAYERS`` layer map and the cap-geometry floor tables. The
resolver functions that read them stay in ``gen_layer_params``, which
re-exports every name here so existing imports are unchanged. This module
imports nothing from ``gen``.
"""

from __future__ import annotations

#: A contact-to-contact edge gap (um) below this is *legal* under sky130's
#: curated deck (which checks no ``licon1`` spacing rule at all) but close
#: enough to gf180mcu's real ``contact.space.1`` limit (>= 0.25um) that it
#: can measurably violate that rule -- issue #685 confirmed a 0.2118um gap
#: fails it while a 0.255um gap (still under this 0.3um margin) is clean.
#: ``_guard_ring_validate`` is PDK-agnostic (called before the resolved PDK
#: family is known to it), so this can't be enforced as a hard rejection
#: without also rejecting sky130 configurations that are genuinely DRC-clean
#: on that family (sky130 has no spacing rule to violate at any gap down to
#: 0). Instead ``_guard_ring_describe`` (which does know the family) flags a
#: gap this tight via the response's ``drc_hints.notes`` -- callers targeting
#: gf180mcu should treat a value close to this margin as worth double-
#: checking with `klt drc`. Other generators that compose
#: :func:`_ring_layout` with an internally-sized, non-caller-controlled
#: contact count (``diff_pair``, ``bjt_array``, ``esd_device``) don't run
#: this check at all -- their own defaults stay comfortably clear of it.
CONTACT_GAP_SAFE_UM = 0.3

#: Smallest unit-device/unit-resistor width (um) that leaves room for a
#: `CONTACT_SIZE_UM` contact enclosed by `ENCLOSURE_MARGIN_UM` on every side
#: -- below this the contact does not fit at all (a structural error, not a
#: DRC-adjacent one), so every phase-2 generator rejects it outright in its
#: ``validate()``. A literal (not ``CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM``)
#: so every generator's own default (also a literal ``0.42``) compares equal
#: rather than tripping on float addition drift (``0.22 + 2 * 0.1 ==
#: 0.42000000000000004``).
UNIT_MIN_W_UM = 0.42

#: Smallest gate length (um) `mos_array`/`diff_pair` default their ``l_um``
#: param to -- comfortably clear of both curated decks' rules with margin, so
#: a caller who never touches ``l_um`` gets a device with no DRC risk at all.
#: A request *below* this is no longer a DRC risk either (see
#: :data:`SD_PAD_GATE_GAP_MIN_UM` -- issue #1187): :func:`_mos_finger_positions`
#: pads the S/D local-metal pads clear of the gate whenever the requested
#: ``l_um`` would otherwise pull them closer together than a curated deck's
#: same-layer metal-spacing rule allows, so this constant now only picks the
#: *default*, not a floor a caller must stay above. A gate length below the
#: target PDK's own poly minimum-width rule (e.g. sky130's ``poly.width.1``:
#: 0.15um) is a separate, still-real risk this constant does not cover --
#: padding the S/D pads does not change the poly width, which stays
#: ``l_um`` -- see :func:`_mos_array_describe`'s ``drc_hints.notes``.
GATE_LENGTH_SAFE_MIN_UM = 0.28

#: Smallest S/D-local-metal-pad-to-pad gap (um) :func:`_mos_finger_positions`
#: guarantees across every poly gate stripe, regardless of the requested
#: ``l_um`` (issue #1187). Exceeds both curated decks' same-layer
#: metal-spacing rule with margin (gf180mcu ``metal1.space.1``: 0.23um is the
#: binding one; sky130's ``li1.space.1`` is only 0.17um) -- the same pair
#: :data:`GATE_LENGTH_SAFE_MIN_UM`'s own margin was sized against, but kept as
#: its own named constant, strictly smaller than that default, so a caller's
#: existing ``l_um >= GATE_LENGTH_SAFE_MIN_UM`` output never shifts by even a
#: single dbu: at ``l_um == GATE_LENGTH_SAFE_MIN_UM`` the computed offset
#: (below) is already zero. Below it, each side of every gate stripe is
#: pushed out by ``max(0, (SD_PAD_GATE_GAP_MIN_UM - l_um) / 2)`` -- padding
#: the unit device's own pitch, never the requested gate length -- so the
#: S/D pads stay legally spaced even at a PDK's absolute minimum gate length
#: (e.g. sky130's 0.15um).
SD_PAD_GATE_GAP_MIN_UM = 0.25

#: Extra margin (um) `cap_array`'s bottom-plate conductor (sky130's `met3`)
#: is drawn beyond its unit cell's top-plate mark (`capm`) footprint on every
#: side -- an implementation choice, not a transcribed DRC/LVS rule (neither
#: curated deck constrains this repo's MiM-cap layer pair, and sky130 needs
#: no "virtual bottom plate" derivation the way gf180mcu's stack does -- see
#: `CapacitorDevice.bottom_plate_oversize_um`'s docstring). Sized generously
#: so the bottom conductor's own edge is never the limiting edge of the
#: top/bottom overlap area `CapacitorDevice.area_cap_f_um2` derives `C` from
#: (`extract.py`'s `_capacitor_plate_regions`), and so the top-plate via's
#: own `ENCLOSURE_MARGIN_UM` clearance from the bottom-plate edge is never in
#: question either.
CAP_BOTTOM_PLATE_MARGIN_UM = 0.5

#: Clearance (um) `cap_array`'s top-plate escape pad (issue #1494) keeps
#: beyond the bottom plate's own top edge before landing. Like
#: `CAP_BOTTOM_PLATE_MARGIN_UM` itself, this is an implementation choice, not
#: a transcribed DRC rule (no curated deck constrains an escape pad's
#: clearance from an unrelated layer) -- reusing that same margin value keeps
#: the escape pad's own bounding box well clear of the bottom plate's sheet,
#: the exact silent-short hazard issue #1494 diagnosed (a via stepping down
#: at the *reported* port position must never be able to land on the bottom
#: plate).
CAP_TOP_ESCAPE_CLEARANCE_UM = CAP_BOTTOM_PLATE_MARGIN_UM

#: Guard ring sizing `diff_pair` uses for its own, automatically-generated
#: ring. `GUARD_RING_DEFAULT_PADDING_UM` is the default for `diff_pair`'s own
#: `ring_padding_um` param (issue #484) -- the ring's thickness itself
#: (`GUARD_RING_DEFAULT_WIDTH_UM`) stays fixed; use the standalone
#: `guard_ring` generator directly for a fully-parametrized ring.
GUARD_RING_DEFAULT_WIDTH_UM = 0.42
GUARD_RING_DEFAULT_CONTACTS_PER_SIDE = 2
GUARD_RING_DEFAULT_PADDING_UM = 0.5

#: Minimum top-metal overlap of a bond pad's passivation opening (um) --
#: `bond_pad`'s (issue #568) `enclosure_um` default and hard floor. Sourced
#: from gf180mcu's *only* hard, DRC-coded bond-pad rule: DRM 9.1 "PAD.4" ("Top
#: layer metal overlap of pad opening" -> 2.0um), transcribed at
#: `decks/gf180mcu.py`'s `pad.enclosing.metal5.1` rule from
#: https://github.com/google/gf180mcu-pdk (Apache-2.0) commit `de3240d`,
#: `docs/physical_verification/design_manual/tables_clear/29_BondPad1_70.csv`
#: -- scoped to the 5LM variant that deck (and `bond_pad`'s own `top_metal`
#: role, below) already exclusively models; see `_PDK_ROLE_LAYERS`'s
#: `"top_metal"` entries. sky130's curated deck has no equivalent hard rule
#: (only `pad.2`'s unrelated 1.27um pad-to-pad *spacing* check,
#: `sky130.lydrc` line 695) -- like `WELL_ENCLOSURE_MARGIN_UM`, this constant
#: is applied as a conservative floor to *both* families, binding only on
#: gf180mcu today.
PAD_TOP_METAL_ENCLOSURE_MIN_UM = 2.0

#: Guideline (not DRC-hard) minimum pad-opening side length (um) per
#: `bond_pad`'s `bond_type` param, from gf180mcu's DRM 9.2 "PAD.1" ("Pad
#: opening") *guideline* table (same source/commit as
#: `PAD_TOP_METAL_ENCLOSURE_MIN_UM` above,
#: `tables_clear/29_BondPad2_70.csv`): wedge-type wire bond (without CUP) 40,
#: ball-type wire bond (with CUP) 40, gold bump 4. Unlike PAD.4, this is
#: explicitly a *guideline* (DRM section 9.2's own heading, and
#: `decks/gf180mcu.py`'s PAD.4 docstring: "9.2's PAD.1/PAD.2/PAD.5-PAD.20 are
#: a guideline table, out of scope") -- an `opening_um` below the value here
#: is only flagged via `drc_hints.notes`, never rejected (see
#: `_bond_pad_describe`). sky130's curated deck has no equivalent guideline
#: table; the same values are applied uniformly, per this module's existing
#: single-constant-across-both-families convention (see
#: `PAD_TOP_METAL_ENCLOSURE_MIN_UM` above).
PAD_OPENING_GUIDELINE_MIN_UM: dict[str, float] = {
    "wedge": 40.0,
    "ball_cup": 40.0,
    "bump": 4.0,
}

#: `esd_device` (issue #569) validation bounds for its `finger_width_um`/
#: `fingers` params -- a grounded-gate MOS ESD clamp's finger-width and
#: fingers-per-tap-ring limits.
#:
#: Provenance caveat (read this before trusting these as a literal DRM rule
#: id, the way `PAD.4`/`BJT.3`/etc. are cited elsewhere in this codebase):
#: neither curated deck in this repo transcribes the source PDK's own
#: dedicated ESD-protection-device chapter (gf180mcu DRM's "ESD" rule
#: section; sky130's own I/O-library ESD-cell sizing notes) -- the same gap
#: the `bond_pad` sibling issue (#568) documents for `PAD.1`/`PAD.2`/
#: `PAD.5`-`PAD.20`'s *guideline* (not DRC-coded) table. So, mirroring
#: `UNIT_MIN_W_UM`'s own precedent above, these three constants are
#: conservative, engineering-derived structural floors/ceilings -- not
#: verbatim-transcribed rule ids -- documented here so a future PR that does
#: obtain a verified per-family DRM citation has exactly one place to correct.
#:
#: `ESD_FINGER_WIDTH_MIN_UM` reuses `UNIT_MIN_W_UM` directly (kept as its own
#: named constant, not just an inline reference, so a later per-family
#: correction doesn't have to touch every other generator's own floor).
ESD_FINGER_WIDTH_MIN_UM = UNIT_MIN_W_UM

#: Individual ESD-clamp fingers are conventionally kept narrow -- rarely more
#: than a few tens of um in bulk 130-180nm CMOS -- specifically so current
#: shares evenly across every finger during an ESD pulse instead of
#: localising into (and destroying) one finger first. Chosen comfortably
#: inside that commonly-published range.
ESD_FINGER_WIDTH_MAX_UM = 20.0

#: Fingers this generator encloses in a single automatically-sized tap ring.
#: Beyond this, the ring's own resistance from the far fingers back to a real
#: ground/tap contact grows enough that per-finger triggering starts to skew
#: -- the same uniform-turn-on concern `ESD_FINGER_WIDTH_MAX_UM` encodes for
#: one finger's own width, applied across the array instead.
ESD_MAX_FINGERS_PER_RING = 32

#: Generic layer *roles* the phase-2 analog primitive generators draw on,
#: resolved to each supported PDK family's curated-DRC-deck layer/datatype
#: pair -- the *same* numbers `klayout_tools.decks.sky130`/`gf180mcu`
#: document and check, never a second, private layer map. `None` means that
#: family's curated deck (see `klayout_tools.decks`) has no rule for the
#: role at all (e.g. sky130's curated *DRC* deck never checks a well layer),
#: so a generator simply omits drawing it for that family. That is distinct
#: from "the layer doesn't exist" -- sky130's `well` entry below is a real
#: GDS layer (`klayout_tools.decks.sky130.EXTRACTION_DECK.nwell`), just one
#: no curated *DRC* rule happens to check.
_PDK_ROLE_LAYERS: dict[str, dict[str, tuple[int, int] | None]] = {
    "sky130": {
        "active": (65, 20),  # diff.drawing
        "tap": (65, 44),  # tap.drawing -- width- and licon-enclosure-checked
        # by sky130.py's `tap.width.1`/`tap.enclosing.licon.1` since issue
        # #2321; before that rule pair, a tap ring was DRC-free there.
        "poly": (66, 20),  # poly.drawing
        "contact": (66, 44),  # licon1.drawing
        "metal": (67, 20),  # li1.drawing
        "nplus": (93, 44),  # nsdm.drawing -- n+ S/D implant (nfet flavor),
        # issue #2934; margin in :data:`_PDK_SD_IMPLANT_MARGIN_UM`.
        "pplus": (94, 20),  # psdm.drawing -- p+ S/D implant (pfet flavor)
        "npc": (95, 20),  # npc.drawing -- over poly-contact licons (issue #2934);
        # margin/spacing in :data:`_PDK_NPC_MARGIN_UM`/:data:`_PDK_NPC_MIN_SPACE_UM`.
        "well": (64, 20),  # nwell.drawing -- matches
        # `klayout_tools.decks.sky130.EXTRACTION_DECK.nwell`; no curated DRC
        # rule checks this layer, but it is real and extraction already
        # trusts it to split nfet/pfet active regions (see `extract.py`).
        "diode_mark": (81, 23),  # areaid.diode -- decks/sky130.py _DIODE_MARK; the
        # recognition mark `EXTRACTION_DECK.diodes` keys off (issue #4300).
        "hvi": (75, 20),  # hvi.drawing -- selects the *_11v0 diode flavours.
        "bjt_mark": (82, 44),  # pnp.drawing -- no curated *DRC* rule checks
        # this layer (see decks/sky130.py's "Negative finding" docstring
        # note), but `klayout_tools.decks.sky130.EXTRACTION_DECK.bipolars`
        # keys off it for device recognition (issue #223, follow-up #432):
        # without it, `bjt_array`'s output extracts as `device_count: 0`.
        # Drawn per unit device (see `_bjt_unit_layout`), not array-wide,
        # matching `res_mark`'s "drawn for extraction even where the DRC
        # deck has no rule" precedent below.
        "res_mark": (66, 13),  # poly.res -- the resistor-ID marker
        # `klayout_tools.decks.sky130.EXTRACTION_DECK.resistors`'s
        # `res_generic_po` keys off (issue #369): without it, a drawn poly
        # body extracts as plain interconnect (a short) instead of a
        # resistor. No curated DRC rule checks this layer either, matching
        # `bjt_mark` above.
        # The resistor *implant*/*salicide-block* layers are no longer a
        # single per-family constant here -- sky130 recognises three
        # poly-resistor flavours (`res_generic_po`/`res_high_po`/
        # `res_xhigh_po`) that differ only in those masks, so they live in the
        # per-flavour :data:`_PDK_RES_FLAVOR_LAYERS` table below, selected by
        # `res_array`'s `flavor` request param (issue #463).
        # MiM-capacitor plate roles (issue #1117), for `cap_array`: the
        # *same* layer/datatype pair `klayout_tools.decks.sky130`'s
        # `EXTRACTION_DECK.capacitors[0]` (`sky130_fd_pr__model__cap_mim`)
        # declares -- not a second, private map, so a `cap_array` cell's
        # output round-trips through `klt extract` to that exact device
        # class rather than drifting from it. `cap_top_plate` is `capm`
        # (89/44), the purpose-drawn MiM top-plate mark; `cap_bottom_plate`
        # is `met3` (70/20), the conductor the bottom plate is drawn on --
        # sky130 needs no "virtual bottom plate" oversize derivation the way
        # gf180mcu's `FuseTop`/`Metal4` stack does (see
        # `CapacitorDevice.bottom_plate_oversize_um`'s docstring), so the
        # generator can draw the bottom plate as an ordinary, generously
        # sized conductor around the top-plate mark. `cap_top_via`/
        # `cap_top_via_metal` are `via3`/`met4` (70/44, 71/20) -- the real
        # via that lands directly on the top plate and the metal it connects
        # up to (`CapacitorDevice.top_plate_via`/`top_plate_via_metal`,
        # issue #775), giving the drawn top plate a routable local-metal
        # landing pad instead of an isolated node. Only sky130 is wired up
        # today -- gf180mcu's own MiM stack is out of this generator's
        # initial scope (see `_cap_family_layers`); a family missing these
        # keys is a supported "not implemented yet" state, not a bug.
        "cap_top_plate": (89, 44),  # capm.drawing
        "cap_bottom_plate": (70, 20),  # met3.drawing
        "cap_top_via": (70, 44),  # via3.drawing (met3<->met4)
        "cap_top_via_metal": (71, 20),  # met4.drawing
        # Second routing-metal role + its connecting via (issue #454, follow-up
        # to #433): sky130's curated *extraction* deck already declares a
        # second metal level and the via that lands on it
        # (`klayout_tools.decks.sky130.EXTRACTION_DECK.metals[1]`/`.vias[0]`),
        # but `gen_compose`'s router had no role name to select either one --
        # `routing.layer_role` could only ever resolve to `"metal"` (li1), so
        # a same-block bus that needed to cross one of its own block's other
        # li1 pads had no routable path (#433 made that a visible rejection,
        # not a fix). `"metal2"` lets a route's backbone run on met1 instead;
        # `gen_compose.route_two_pin`'s via-drop logic drops back to each
        # target pin's own `"metal"`-role pad only at the connecting via
        # (`"via1"`), so the backbone itself never touches another pad's li1
        # layer. Not drawn by any `klt gen` generator itself -- both roles
        # exist purely for `gen_compose`'s router to resolve.
        "metal2": (68, 20),  # met1.drawing -- EXTRACTION_DECK.metals[1]
        "via1": (67, 44),  # mcon.drawing -- EXTRACTION_DECK.vias[0] (li1<->met1)
        # Third routing-metal role + its connecting via (issue #508, follow-up
        # to #454/#468): sky130's curated *extraction* deck now declares a
        # third connectivity level and the via that lands on it
        # (`klayout_tools.decks.sky130.EXTRACTION_DECK.metals[2]`/`.vias[1]`)
        # -- a genuinely independent second routing plane above `"metal2"`
        # (met1) for a caller whose own intra-block bussing already saturates
        # met1, mirroring the exact shape `"metal2"`/`"via1"` established
        # above. Same caveat as `"metal2"`/`"via1"`: not drawn by any `klt
        # gen` generator itself -- both roles exist purely for
        # `gen_compose`'s router to resolve. `gen_compose`'s via-drop walks
        # the full `metals`/`vias` ladder between the two layers (issue
        # #1567) -- so `"metal3"` reaches a pin on `"metal2"` (met1, one via
        # hop away) via `"via2"` directly, and a pin still on the base
        # `"metal"` role (li1, two hops away) via a two-hop `"via1"` +
        # `"via2"` ladder, with a landing pad on met1 in between.
        "metal3": (69, 20),  # met2.drawing -- EXTRACTION_DECK.metals[2]
        "via2": (68, 44),  # via.drawing -- EXTRACTION_DECK.vias[1] (met1<->met2)
        # Upper routing planes (issue #2738, mirroring gf180mcu's #1670): the
        # role numbering counts li1 as `"metal"`, so `"metalN"` is physical
        # met(N-2) from `"metal3"` up -- `"metal4"` is met3, `"metal5"` is
        # met4, `"metal6"` is met5 -- and `"viaN"` joins `"metalN"` to the
        # plane below it (`"via3"` is via2, met2<->met3). These match
        # EXTRACTION_DECK.metals[3:6]/.vias[2:5] (issue #619). `"top_metal"`
        # below is an unchanged alias of `"metal6"` (met5).
        "metal4": (70, 20),  # met3.drawing -- EXTRACTION_DECK.metals[3]
        "via3": (69, 44),  # via2.drawing -- EXTRACTION_DECK.vias[2] (met2<->met3)
        "metal5": (71, 20),  # met4.drawing -- EXTRACTION_DECK.metals[4]
        "via4": (70, 44),  # via3.drawing -- EXTRACTION_DECK.vias[3] (met3<->met4)
        "metal6": (72, 20),  # met5.drawing -- EXTRACTION_DECK.metals[5]
        "via5": (71, 44),  # via4.drawing -- EXTRACTION_DECK.vias[4] (met4<->met5)
        # Label/pin purpose of the *base* routing metal role above (`metal`,
        # li1) -- the same pair `klayout_tools.decks.sky130`'s
        # `EXTRACTION_DECK.metal_labels[0]` declares, never a second private
        # one. Drawn by `well_island` (issue #1421) as a `kdb.Text` sitting on
        # the island's own tap-ring metal, so the ring's *conductor* carries
        # the caller's requested net name into `klt extract`.
        #
        # Deliberately **not** `well_label` (64/5, `EXTRACTION_DECK.well_label`):
        # a text there names the `nwell` polygon *directly*, so the extracted
        # body net would read back as the intended name even when the
        # tap/contact/metal tie that is supposed to bias the well is broken or
        # absent -- the "well-label tautology" issue #1421 exists to avoid.
        # Naming the metal instead means the name only reaches the well
        # through the physical tie (li1 -> licon1 -> tap -> nwell), so
        # `klt extract`'s `devices[].nets["b"]` is real evidence the tie
        # works. No `klt gen` generator draws on `well_label` at all.
        "metal_label": (67, 5),  # li1.pin -- EXTRACTION_DECK.metal_labels[0]
        # No `well_tap_implant` entry: sky130 has a real, dedicated tap layer
        # (`tap.drawing`, the `"tap"` role above), and this deck's own
        # `tap`-inside-`nwell` split (see `decks/sky130.py`) already
        # recognises a shape drawn there as a well tie with no additional
        # implant mask. gf180mcu, which has no dedicated tap layer, does need
        # one -- see its own entry below.
        "dummy": (83, 20),  # curated marker, matches
        # `klayout_tools.decks.sky130.EXTRACTION_DECK.dummy` -- see that
        # deck's own comment for why sky130 has no native dummy-device GDS
        # layer and why (83, 20) was chosen (issue #491). Drawn per dummy
        # unit device by `mos_array`/`res_array`/`bjt_array` (never over a
        # real, non-dummy unit) so `klt extract`'s existing dummy-suppression
        # guards (issue #295/#462) actually fire on sky130.
        # Bond-pad roles (issue #568): `bond_pad` is the first generator to
        # draw at the chip boundary rather than in core analog -- its top
        # metal is *not* the shared `metal`/`metal2`/`metal3` roles above
        # (li1/met1/met2), it is the resolved family's own topmost routing
        # metal. Both numbers below are transcribed from `sky130.lyt` (the
        # layer-properties file cited at this module's top, same
        # fossi-foundation/open-pdks source as `LAYER_NAMES`):
        # `pad.drawing : 76/20`, `met5.drawing : 72/20`.
        "pad": (76, 20),  # pad.drawing -- passivation opening (bond pad)
        "top_metal": (72, 20),  # met5.drawing -- same layer as `metal6` above
        # (kept as the bond-pad-facing name). `bond_pad`'s own `down_to`
        # param still only supports `"top_metal"` (see `_bond_pad_validate`).
        #
        # No `"esd_mark"`/`"salicide_block"` entry (issue #569, `esd_device`):
        # this repo's curated sky130 deck cites no numbered layer for either
        # role -- sky130.py's own resistor-flavour provenance notes name
        # `hvtr`/`hvtp` (high-voltage-transistor exclusion layers, the closest
        # ESD/IO-voltage-domain-adjacent marks in that transcription) only as
        # unnumbered exclusions in the official `sky130.lvs`'s exclusion set,
        # never with a transcribed layer/datatype pair this module could cite
        # without guessing. `esd_device` simply omits both roles on this
        # family (`_role_layer_info` already returns `None` for a missing
        # key), reported via `drc_hints.notes` like every other role-absent
        # case in this table.
    },
    "gf180mcu": {
        "active": (22, 0),  # Comp
        "tap": (22, 0),  # Comp -- no separate tap layer in the curated deck
        "poly": (30, 0),  # Poly2
        "contact": (33, 0),  # Contact
        "metal": (34, 0),  # Metal1
        "well": (21, 0),  # Nwell
        "bjt_mark": (127, 5),  # DRC_BJT -- the vertical-bipolar device mark
        # layer the curated deck's `bjt.separation.comp.1` (`BJT.3`) rule keys
        # off (see klayout_tools.decks.gf180mcu).
        "res_mark": (110, 5),  # RES_MK -- the resistor-ID marker
        # `klayout_tools.decks.gf180mcu.EXTRACTION_DECK.resistors`'s
        # `ppolyf_u` keys off (issue #369). Unlike sky130, gf180mcu's
        # `ppolyf_u` also *requires* an implant (Pplus) and salicide-block
        # (SAB) layer to cover the same segment -- the marker alone recognises
        # nothing there. gf180mcu's per-flavour implant/block mask sets (the
        # default `"generic"` -> `ppolyf_u`, plus the high-sheet-rho
        # `"1k"`/`"2k"`/`"3k"` -> `ppolyf_u_1k`/`_2k`/`_3k`, issue #1550) live
        # in :data:`_PDK_RES_FLAVOR_LAYERS` below (issue #463).
        # Second routing-metal role + its connecting via (issue #454, same
        # rationale as sky130's pair above). gf180mcu's curated extraction
        # deck's `metals` stack already runs Metal1-Metal5 (#220); this only
        # exposes the next level up (Metal2) and the via connecting it to
        # `"metal"` (Metal1) -- `gen_compose`'s via-drop walks the full
        # `metals`/`vias` ladder between two roles (issue #1567), not just
        # one hop.
        # A third plane (`"metal3"`/`"via2"`, Metal3) was added below by
        # issue #1058; a fourth (`"metal4"`/`"via3"`, Metal4) was added below
        # that by issue #1670. Metal5/`vias[3]` (the Via4 hop up to Metal5)
        # still stay unexposed here as general routing roles (out of that
        # issue's scope, not a structural limit -- see
        # `EXTRACTION_DECK.metals`/`.vias` in `klayout_tools.decks.gf180mcu`;
        # Metal5 is already independently reachable via this table's own
        # single-purpose `"top_metal"` bond-pad role below).
        "metal2": (36, 0),  # Metal2 -- EXTRACTION_DECK.metals[1]
        "via1": (35, 0),  # Via1 -- EXTRACTION_DECK.vias[0] (Metal1<->Metal2)
        # Third routing-metal role + its connecting via (issue #1058, same
        # shape as sky130's #508 addition above): gf180mcu's curated
        # *extraction* deck's `metals` stack already runs Metal1-Metal5
        # (#220), but this table stopped at `"metal2"`/`"via1"` (Metal2), the
        # only routing plane above the base `"metal"` pads -- a block whose
        # own nets needed to cross had no second independent plane to route
        # on. This exposes the next level up (Metal3) and the via connecting
        # it to `"metal2"` (Metal2), mirroring `"metal2"`/`"via1"`'s own
        # rationale exactly. `gen_compose`'s via-drop walks the full
        # `metals`/`vias` ladder between the two layers (issue #1567) -- so
        # `"metal3"` reaches a pin on `"metal2"` (Metal2, one via hop away)
        # via `"via2"` directly, and a pin still on the base `"metal"` role
        # (Metal1, two hops away) via a two-hop `"via1"` + `"via2"` ladder,
        # with a landing pad on Metal2 in between.
        "metal3": (42, 0),  # Metal3 -- EXTRACTION_DECK.metals[2]
        "via2": (38, 0),  # Via2 -- EXTRACTION_DECK.vias[1] (Metal2<->Metal3)
        # Fourth routing-metal role + its connecting via (issue #1670, same
        # shape as #1058's `"metal3"`/`"via2"` addition above): a
        # `klt place-and-route`-produced gf180mcu macro routinely lands its
        # own top-level pins on Metal4 -- OpenROAD's global router picks the
        # pin escape layer, and Metal4 is an unremarkable choice for a
        # design of any size -- but `gen_compose`'s router had no role name
        # that resolved to it, so a `connectivity[]`/`routing` request
        # targeting one of those pins was structurally unresolvable, not
        # merely rejected with a diagnostic. This exposes the next level up
        # (Metal4) and the via connecting it to `"metal3"` (Metal3), mirroring
        # `"metal3"`/`"via2"`'s own rationale exactly. `gen_compose`'s
        # via-drop walks the full `metals`/`vias` ladder between the two
        # layers (issue #1567) -- so `"metal4"` reaches a pin on `"metal3"`
        # (Metal3, one via hop away) via `"via3"` directly, and a pin still on
        # a lower role (Metal2/Metal1) via the corresponding multi-hop
        # `via1`/`via2`/`via3` ladder, with a landing pad on each intervening
        # metal in between. Metal5/`vias[3]` (Via4) stays unexposed here as a
        # general routing role -- see the comment above `"metal2"` for why.
        "metal4": (46, 0),  # Metal4 -- EXTRACTION_DECK.metals[3]
        "via3": (40, 0),  # Via3 -- EXTRACTION_DECK.vias[2] (Metal3<->Metal4)
        # Label/pin purpose of the base `metal` role (Metal1) -- the same pair
        # `klayout_tools.decks.gf180mcu`'s `EXTRACTION_DECK.metal_labels[0]`
        # declares. Same rationale (and the same deliberate avoidance of a
        # well-label text) as sky130's entry above.
        "metal_label": (34, 10),  # Metal1 pin -- EXTRACTION_DECK.metal_labels[0]
        # Well-tie implant (issue #1421). Unlike sky130, gf180mcu has no
        # dedicated tap layer -- a well tie is drawn on the *same* `Comp`
        # layer as transistor active (see this table's own `"tap"` entry),
        # so the only thing distinguishing an n+ well tie from a PMOS's own
        # p+ source/drain inside the well is the implant covering it. This is
        # the *same* `Nplus` (32/0) layer `klayout_tools.decks.gf180mcu`'s
        # `EXTRACTION_DECK.tap_nplus` already declares for exactly that
        # derivation ("an n+ (`Nplus`)-covered Comp shape *inside* `Nwell` is
        # a well tie", issue #1084) -- never a second, private citation.
        # `well_island` draws it as a ring exactly coincident with its own tap
        # ring (never a blanket over the enclosed area, which would re-dope
        # the enclosed devices' own source/drain diffusion and make it extract
        # as a well tie instead). No curated *DRC* rule in this deck checks
        # 32/0, so drawing it never affects `klt drc --deck gf180mcu` status.
        "well_tap_implant": (32, 0),  # Nplus -- EXTRACTION_DECK.tap_nplus
        # Source/drain implant roles (issue #1577, `mos_array`'s/
        # `diff_pair`'s/`esd_device`'s own unit-device body -- distinct from
        # `well_tap_implant` above, which is the same `Nplus` layer but only
        # ever drawn on the separate well-tie tap pad). Unlike sky130/sg13g2/
        # sg13cmos5l -- which all recognise a MOS device from `active`/`well`
        # alone -- gf180mcu's real signoff deck's `DF.12` ("COMP not covered
        # by Nplus or Pplus is forbidden") requires *every* drawn `Comp` shape
        # to carry one implant or the other; this repo's curated deck never
        # transcribed that rule, so a unit device's bare `Comp` body went
        # unimplanted (and therefore DF.12-violating) until #1577.
        # `"nplus"`/`"pplus"` select the doping :func:`_device_layer_params`
        # resolves off `params.flavor` (`"nfet"` -> `nplus`, the same n+
        # implant an NMOS's own source/drain always carries; `"pfet"` ->
        # `pplus`), the *same* `Nplus`/`Pplus` (32/0, 31/0) layers this
        # table's own `well_tap_implant`/`_PDK_RES_FLAVOR_LAYERS` entries
        # already cite -- never new, private numbers.
        "nplus": (32, 0),  # Nplus -- n+ source/drain implant (nfet flavor)
        "pplus": (31, 0),  # Pplus -- p+ source/drain implant (pfet flavor)
        "dummy": (100, 50),  # curated marker, matches
        # `klayout_tools.decks.gf180mcu.EXTRACTION_DECK.dummy` -- see that
        # deck's own comment for why gf180mcu has no native dummy-device GDS
        # layer and why (100, 50) was chosen (issue #2599, the gf180mcu
        # counterpart of sky130's own #491). Drawn per dummy unit device by
        # `mos_array`/`res_array`/`bjt_array` (never over a real, non-dummy
        # unit) so `klt extract`'s existing dummy-suppression guards (issue
        # #295/#462) actually fire on this family too -- without it, `klt gen
        # mos_array`'s own `dummy` param produced geometry `klt lvs` on the
        # same family could only report as `device.unmatched`. No curated
        # *DRC* rule in this deck checks layer 100, so drawing it never
        # affects `klt drc --deck gf180mcu` status.
        # Bond-pad roles (issue #568, same rationale as sky130's pair above).
        # `pad` matches `decks/gf180mcu.py`'s `pad.enclosing.metal5.1` (PAD.4)
        # `other_layer`; `top_metal` matches that same rule's `layer` --
        # scoped to the 5LM variant this deck already exclusively models
        # (see that rule's own docstring). A 6LM variant's true top metal
        # (`MetalTop`, 53/0) is *not* distinguishable from the resolved
        # `pdk.variant` string today (`gf180mcuA`-`D` name voltage/process
        # options, not the metal-stack height, per `klayout_tools.pdk`) --
        # `bond_pad`'s gf180mcu output always assumes 5LM. A known,
        # documented limitation (mirroring PAD.4's own scoping note), not a
        # silent misapplication; see `docs/cli/gen.md`'s `bond_pad` section.
        "pad": (37, 0),  # Pad -- passivation opening
        "top_metal": (81, 0),  # Metal5
        # `esd_device`'s device-class marker (issue #569). This deck cites no
        # single dedicated "ESD" GDS layer with a verified layer/datatype pair
        # (`res_exclude`'s exclusion-list comment above names `esd`/`esd_mk`
        # only, with no transcribed numbers this module could cite without
        # guessing) -- the closest *already-verified, already-cited* marker
        # this deck ties to ESD-clamp device recognition is `Dualgate` (55/0,
        # see the diode-derivation note above): "the two 6V (`Dualgate`-
        # marked, medium-voltage) flavours are the ones the PDK's own I/O
        # library uses for its ESD clamps". Reusing it here gives a
        # grounded-gate ESD MOSFET the same voltage-domain marking its
        # diode-clamp counterparts already carry, distinguishing it from an
        # ordinary core `mos_array` unit -- no curated *DRC* rule currently
        # checks `Dualgate` in this deck (`dualgate.drc` is cited but not
        # transcribed, see the module docstring), so drawing it never affects
        # `klt drc --deck gf180mcu` status.
        "esd_mark": (55, 0),  # Dualgate
        # Salicide-block role, shared with `_PDK_RES_FLAVOR_LAYERS`'s own
        # gf180mcu `"generic"` mask set -- the *same* SAB (49/0) layer, not a
        # second private one: SAB is documented above as a general
        # "salicide block" mask, not a resistor-specific one, so reusing it
        # for `esd_device`'s own `salicide_block` option (a ballast-style
        # unsalicided region over the finger array, the same "floorplan
        # fidelity, not process-exact cross-section" approximation
        # `_bjt_unit_layout` already documents) is not a new citation.
        "salicide_block": (49, 0),  # SAB
        # MiM-capacitor plate roles (issue #1555, the follow-on #1117
        # explicitly deferred when it scoped `cap_array` to sky130 only), for
        # `cap_array`: the *same* layer/datatype pairs
        # `klayout_tools.decks.gf180mcu.EXTRACTION_DECK.capacitors[0]`
        # (`"cap_mim_2f0_m4m5_noshield"`, the official LVS deck's own device
        # name) declares -- not a second, private map, transcribed from that
        # entry's own `top_plate`/`bottom_plate`/`top_plate_via`/
        # `top_plate_via_metal` fields (and the module docstring's "10.4 MIM
        # Capacitor" note), so a `cap_array` cell's output round-trips
        # through `klt extract --deck gf180mcu` to that exact device class.
        # `cap_top_plate` is `FuseTop` (75/0), the purpose-drawn MiM
        # top-plate conductor; `cap_bottom_plate` is `Metal4` (46/0), the
        # 5LM stack's `topmin1_metal`. `cap_top_via`/`cap_top_via_metal` are
        # `Via4`/`Metal5` (41/0, 81/0) -- `main.drc`'s own `top_via = via4` /
        # `top_metal = metal5`, the via that lands directly on `FuseTop` and
        # the metal it connects up to (the same `Metal5` this table's
        # `"top_metal"` bond-pad role already names; `_cap_family_layers`
        # reads its own dedicated key rather than reusing that role, so the
        # two stay independently citable).
        #
        # Unlike sky130/sg13g2, this stack needs three things beyond the four
        # plate/via roles, all of them per-family data rather than new
        # geometry code (see the tables immediately below
        # :data:`_PDK_ROLE_LAYERS`):
        #
        # - the top plate is only *recognised* where `CAP_MK` (117/5) and
        #   `MIM_L_MK` (117/10) also cover it (that entry's
        #   `top_plate_requires`) -- :data:`_PDK_CAP_TOP_PLATE_REQUIRES`;
        # - the bottom plate is the DRM's oversized "virtual bottom plate",
        #   so `Metal4` must extend at least `mim.enclosing.fusetop.1`
        #   (MIMTM.3, 0.6um) past `FuseTop` -- further than the generic
        #   `CAP_BOTTOM_PLATE_MARGIN_UM` (0.5um) draws it;
        # - `Via4`'s own minimum size (`via4.width.1`, 0.26um) is coarser
        #   than the generic `CONTACT_SIZE_UM` (0.22um), and adjacent virtual
        #   bottom plates must stay `mim.space.1` (MIMTM.1, 1.2um) apart --
        #   both floors live in :data:`_PDK_CAP_GEOMETRY_MIN_UM`.
        "cap_top_plate": (75, 0),  # FuseTop
        "cap_bottom_plate": (46, 0),  # Metal4 (the 5LM stack's topmin1_metal)
        "cap_top_via": (41, 0),  # Via4.drawing (FuseTop<->Metal5)
        "cap_top_via_metal": (81, 0),  # Metal5.drawing
    },
    # sg13g2 (IHP-Open-PDK), issue #1448 -- the third family this table
    # supports, following #1266's own "Adding a third PDK family" guide
    # below. Every number here is transcribed from
    # `klayout_tools.decks.sg13g2` (never a second, private layer map),
    # cross-checked against a real fetched IHP-Open-PDK v0.3.0 install (the
    # same commit that deck's own module docstring pins).
    #
    # `res_array`/`guard_ring` (#1448), `mos_array`/`diff_pair` (#1450), and
    # `cap_array` (#1455, once issue #1454 populated
    # `EXTRACTION_DECK.capacitors` for `cap_cmim`/`rfcmim`) are wired up to
    # *run* on this family; `bjt_array`/`esd_device`/`bond_pad`/`well_island`
    # are still explicitly rejected (`_GENERATOR_FAMILY_DEFERRED`, and
    # `_bond_pad_layer_params`'s missing-role guard) rather than silently
    # producing untested output:
    #
    # - `mos_array`/`diff_pair` were rejected by #1448 too, for a reason
    #   unlike every other deferral here: their shared unit device's
    #   gate-poly landing pad (issue #461) *actually failed* this family's
    #   real `gatpoly.separation.activ.1` (`Gat.d`) DRC rule -- verified with
    #   a real generated shape. #1450 fixed the geometry rather than the
    #   table: `_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` stands that pad off the
    #   diffusion edge for this family only (see `_mos_unit_layout` for the
    #   full geometric explanation), leaving sky130/gf180mcu byte-for-byte
    #   unchanged. `esd_device` draws the same unit device and would very
    #   likely follow for free, but it also composes a ring/marker stack this
    #   issue did not verify on this family, so it stays deferred rather than
    #   shipped untested.
    # - `bjt_array` still has no recognised device class in this deck's
    #   `EXTRACTION_DECK` at all (`.bipolars` is empty), and `bond_pad`/
    #   `well_island` were not attempted by #1448 (no citable curated
    #   passivation-opening layer for the former; no verification effort
    #   spent on the latter's well-tie-implant story for the latter).
    #   `cap_array` was the one member of this group with a real recognised
    #   device class blocked only on the deck's own metals/vias stack
    #   reaching Metal5/TopMetal1 -- #1243 extended that stack and #1454
    #   populated `EXTRACTION_DECK.capacitors`, so #1455 wires the
    #   generator's own plate/via layer roles up to match (see the
    #   `cap_top_plate`/`cap_bottom_plate`/`cap_top_via`/`cap_top_via_metal`
    #   entries below).
    #
    # Every generator wired up here was verified DRC-clean against
    # `klt drc --deck sg13g2` on its documented default `params`, and
    # `res_array`'s default output round-trips through `klt extract --deck
    # sg13g2` to the `"rsil"` device class (see `_PDK_RES_FLAVOR_LAYERS`
    # below) -- `cap_array`'s own default output likewise round-trips to the
    # `"cap_cmim"` device class (`klayout_tools.decks.sg13g2.EXTRACTION_DECK
    # .capacitors[0]`) -- the same bar `docs/cli/gen.md`'s "Adding a third PDK family"
    # section states.
    "sg13g2": {
        "active": (1, 0),  # Activ.drawing -- EXTRACTION_DECK.active
        "poly": (5, 0),  # GatPoly.drawing -- EXTRACTION_DECK.poly
        "contact": (6, 0),  # Cont.drawing -- EXTRACTION_DECK.contact
        "metal": (8, 0),  # Metal1.drawing -- EXTRACTION_DECK.metals[0]
        "well": (31, 0),  # NWell.drawing -- EXTRACTION_DECK.nwell; this
        # deck's own curated `DECK` list transcribes no rule against
        # NWell.drawing at all (verified: no `nwell.*` entry in
        # `klayout_tools.decks.sg13g2.DECK`), the identical "the layer is
        # real, just uncheckable" situation sky130's own `"well"` entry
        # above documents -- so `guard_ring`'s `add_well` never affects `klt
        # drc --deck sg13g2` status.
        "tap": (1, 0),  # Activ.drawing -- sg13g2 declares no distinct tap
        # mask (`klayout_tools.decks.sg13g2.EXTRACTION_DECK.tap` is `None`;
        # that module's own docstring: "sg13g2 draws no distinct tap mask
        # ... derives well ties (`ntap`/`ptap` in `general_derivations.lvs`)
        # from the *same* Activ layer as transistor source/drain"), the
        # identical "no distinct tap layer, shared with transistor active"
        # situation `gf180mcu`'s own `"tap": (22, 0)` (Comp) entry above
        # documents (there, reusing gf180mcu's own `"active"` layer number
        # for exactly the same reason). `guard_ring` draws its tap ring here
        # -- DRC-clean against this deck's `activ.width.1`/`activ.space.1`/
        # `activ.enclosing.cont.1` rules with the same generous margin every
        # other family gets (`UNIT_MIN_W_UM`/`MIN_SAME_LAYER_SPACING_UM`/
        # `ENCLOSURE_MARGIN_UM` above all exceed every sg13g2 FEOL/BEOL
        # threshold this deck transcribes, the smallest of which -- `V1.c`'s
        # 0.01um Metal1-encloses-Via1 -- is not even drawn by `guard_ring`).
        # Not recognised by `klt extract` as a distinct well/substrate *tie*
        # the way `well_island`'s own `well_tap_implant` role makes
        # gf180mcu's tap ring recognisable
        # (issue #1084) -- `guard_ring`/`diff_pair` draw no such implant on
        # any currently-supported family, gf180mcu included, so this is not
        # a new gap sg13g2 introduces.
        "res_mark": (128, 0),  # PolyRes.drawing --
        # `EXTRACTION_DECK.resistors[0]` (`"rsil"`, sg13g2's lowest-sheet-rho
        # recognised poly resistor, 7 ohm/sq)'s `marker` field (issue #369's
        # precedent: without it, a drawn poly body extracts as plain
        # interconnect instead of a resistor device).
        #
        # MiM-capacitor plate roles (issue #1455), for `cap_array`: the
        # *same* layer/datatype pairs
        # `klayout_tools.decks.sg13g2.EXTRACTION_DECK.capacitors[0]`
        # (`"cap_cmim"`) declares -- not a second, private map, transcribed
        # from that entry's own `top_plate`/`bottom_plate`/`top_plate_via`/
        # `top_plate_via_metal` fields (see that module's "MiM capacitors"
        # docstring section), not from this issue's own filed description
        # (whose suggested `cap_top_via` value, `TopVia1` 125/0, does not
        # match the deck's actual `Vmim` 129/0 via -- verified against the
        # curated deck, the source of truth, rather than the issue text).
        # `cap_top_plate` is `MIM` (36/0), the purpose-drawn MiM top-plate
        # mark; `cap_bottom_plate` is `Metal5` (67/0), the conductor the
        # bottom plate is drawn on -- like sky130, sg13g2 needs no "virtual
        # bottom plate" oversize derivation, so the generator draws the
        # bottom plate as an ordinary, generously sized conductor around the
        # top-plate mark. `cap_top_via`/`cap_top_via_metal` are `Vmim`/
        # `TopMetal1` (129/0, 126/0) -- the real via that lands directly on
        # the top plate and the metal it connects up to, giving the drawn
        # top plate a routable local-metal landing pad instead of an
        # isolated node (mirrors sky130's `via3`/`met4` precedent). Neither
        # `cap_cmim`'s own `top_plate_excludes`/`bottom_plate_excludes`
        # (`PWell.block`/`Ind.*`, `Metal5.res`) nor `rfcmim`'s distinguishing
        # `PWell.block` `..._requires` term are drawn by this generator, so
        # its output always classifies as the base `"cap_cmim"` flavour, not
        # `"rfcmim"` -- matching `res_array`'s own "generic" default
        # precedent for a family with more than one recognised flavour of a
        # device class (`cap_array` has no `flavor` param yet -- see
        # `_cap_family_layers`'s docstring). This deck's own curated *DRC*
        # deck transcribes no rule against `MIM`/`Vmim` (36/0, 129/0) at all
        # (verified: no `layer=(36, ...)`/`layer=(129, ...)` entry in
        # `klayout_tools.decks.sg13g2.DECK`), only against `Metal5`/
        # `TopMetal1` (`metal5.width.1`/`metal5.space.1`,
        # `topmetal1.width.1`/`topmetal1.space.1`) -- so `klt drc --deck
        # sg13g2` never checks the plate/via layers themselves, only the
        # bottom-plate conductor and the via's landing pad, the identical
        # "the layer is real, just uncheckable for its own purpose" situation
        # this table's own `well`/`tap` entries above document.
        # `topmetal1.width.1`'s 1.64um minimum (far coarser than sky130's
        # `met4.width.1` 0.3um) is wider than this generator's generic
        # `CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM` landing-pad default
        # (0.42um) -- see this family's own `cap_top_via_metal_min_w_um`
        # entry in `_PDK_CAP_GEOMETRY_MIN_UM` below, which widens the drawn
        # pad (and its reported `C<i>_TOP` port width) for this family only,
        # leaving every other family byte-for-byte unchanged.
        "cap_top_plate": (36, 0),  # MIM.drawing
        "cap_bottom_plate": (67, 0),  # Metal5.drawing
        "cap_top_via": (129, 0),  # Vmim.drawing (MIM <-> TopMetal1)
        "cap_top_via_metal": (126, 0),  # TopMetal1.drawing
        # Second/third routing-metal roles + their connecting vias (issue
        # #1474): sg13g2's curated *extraction* deck already declares a
        # seven-level Metal1..TopMetal2 stack
        # (`klayout_tools.decks.sg13g2.EXTRACTION_DECK.metals`/`.vias`), but
        # until now this table exposed only the base `"metal"` role (Metal1),
        # so `routing.cross_block_layer_role` had no second plane to name on
        # this family at all -- every request setting it was rejected before
        # any routing was attempted. `"metal2"`/`"via1"` and `"metal3"`/
        # `"via2"` mirror the exact `sky130`/`gf180mcu` indexing convention
        # established above (`"metal2"`/`"via1"` = `metals[1]`/`vias[0]`,
        # `"metal3"`/`"via2"` = `metals[2]`/`vias[1]`) -- not a new
        # convention invented for this family.
        "metal2": (10, 0),  # Metal2.drawing -- EXTRACTION_DECK.metals[1]
        "via1": (19, 0),  # Via1.drawing -- EXTRACTION_DECK.vias[0] (Metal1<->Metal2)
        "metal3": (30, 0),  # Metal3.drawing -- EXTRACTION_DECK.metals[2]
        "via2": (29, 0),  # Via2.drawing -- EXTRACTION_DECK.vias[1] (Metal2<->Metal3)
        "dummy": (99, 50),  # curated marker, matches
        # `klayout_tools.decks.sg13g2.EXTRACTION_DECK.dummy` -- see that
        # deck's own comment for why SG13G2 has no native dummy-device GDS
        # layer and why (99, 50) was chosen (issue #2590, the sg13g2
        # counterpart of sky130's own #491). Drawn per dummy unit device by
        # `mos_array`/`res_array` (never over a real, non-dummy unit) so
        # `klt extract`'s existing dummy-suppression guards (issue #295/#462)
        # actually fire on this family too -- without it, `klt gen
        # mos_array`'s own `dummy` param produced geometry `klt lvs` on the
        # same family could only report as `device.unmatched`.
    },
    # sg13cmos5l (IHP-Open-PDK's SG13G2_CMOS5L sibling), issue #1462 -- the
    # fourth family this table supports. Every number here is transcribed
    # from `klayout_tools.decks.sg13cmos5l.EXTRACTION_DECK` (never a second,
    # private layer map) -- that deck's own module docstring documents each
    # value's provenance against a real, independently-fetched
    # `ihp-sg13cmos5l` install.
    #
    # Only `mos_array`/`res_array` are wired up to *run* on this family
    # (issue #1462's own scope); `guard_ring`/`diff_pair`/`well_island`/
    # `bjt_array`/`esd_device`/`bond_pad`/`cap_array` are all deferred --
    # see `_GENERATOR_FAMILY_DEFERRED` and `_cap_family_layers`'s/
    # `_bond_pad_layer_params`'s own missing-role checks:
    #
    # - `bjt_array`: this deck's `EXTRACTION_DECK` declares no bipolar
    #   device class at all (MOS/resistors only, per its own module
    #   docstring's scope note) -- the identical gap `sg13g2`'s own entry
    #   above documents.
    # - `esd_device`/`guard_ring`/`diff_pair`/`well_island`: this deck
    #   declares no distinct `tap` mask (`EXTRACTION_DECK.tap` is `None`,
    #   the same "no distinct tap layer" shape `sg13g2`'s own `"tap"` entry
    #   documents) -- but unlike `sg13g2` (whose curated deck derives a well/
    #   substrate tie from plain `Activ ∩ nwell`, so drawing an undoped
    #   `"tap"` ring alone is at least DRC-legal there), this deck's own tie
    #   derivation (`EXTRACTION_DECK.tap_nplus`/`.tap_pplus`, see that
    #   module's own docstring) requires an `nSD`/`pSD` implant *on top* of
    #   the tap shape -- so reusing `sg13g2`'s bare-`Activ` tap-ring pattern
    #   here unmodified would draw geometry that is DRC-legal but silently
    #   fails to extract as a recognised tie (an `unbiased_pmos_body_nets`/
    #   `device.body_unverified` surprise, not caught by DRC at all).
    #   Issue #1462 does not attempt that geometry change (`mos_array`/
    #   `res_array` need no `"tap"` role to satisfy their own AC), so this
    #   table deliberately omits a `"tap"` entry rather than copy `sg13g2`'s
    #   pattern unverified -- a follow-on issue that wires up `guard_ring`/
    #   `diff_pair`/`well_island`/`esd_device` for this family must add both
    #   a real `"tap"` role *and* the implant-covered tie geometry, then
    #   verify the result classifies as a tie under `klt extract --deck
    #   sg13cmos5l` (never assume parity with `sg13g2`'s own, simpler tap
    #   derivation).
    #
    #   Update (issue #1473): `mos_array`'s own `flavor="pfet"` well shape
    #   was exactly this "DRC-legal but silently unbiased" trap -- it drew
    #   the well with nothing tying it to a net. Fixed *without* adding a
    #   `"tap"` role: `mos_array` draws its own well-tie pad directly on the
    #   shared `"active"` role (implant-covered by the new
    #   `"well_tap_implant"` role below), never through `_ring_layer_params`
    #   -- so `guard_ring`/`diff_pair`/`well_island` (which *do* go through
    #   `_ring_layer_params`'s `"tap"` role) are still correctly deferred
    #   above; this fix does not unblock them.
    # - `cap_array`: this deck's module docstring states cmos5l has no MIM
    #   capacitor at all (a forbidden-layer requirement on `MIM`/`Vmim`/etc.,
    #   confirmed against cmos5l's own forbidden-layer lists) -- there is no
    #   `cap_top_plate`/`cap_bottom_plate` role to populate, so
    #   `_cap_family_layers`'s own existing missing-role check already
    #   raises a clear error naming the families that *are* configured
    #   (unchanged by this table, since neither key is added here).
    # - `bond_pad`: this deck cites no passivation-opening/pad-boundary
    #   layer (no `"pad"`/`"top_metal"` role below), so
    #   `_bond_pad_layer_params`'s own existing missing-role check already
    #   raises a clear error, the identical `sg13g2` precedent.
    #
    # `mos_array`'s documented default `params` were verified DRC-clean
    # against `klt drc --deck sg13cmos5l` and re-extract to the `nfet`/
    # `pfet` device class; `res_array`'s default output round-trips through
    # `klt extract --deck sg13cmos5l` to the `"rsil"` device class (see
    # `_PDK_RES_FLAVOR_LAYERS` below) -- the same bar
    # `docs/cli/gen.md`'s "Adding a third PDK family" section states. Unlike
    # `sg13g2` (issue #1450), this deck transcribes no
    # `gatpoly.separation.activ.1`-shaped rule at all (its curated `DECK`
    # checks only `activ`/`gatpoly`/`metal1..topmetal1`/`via1..topvia1`
    # width/space/enclosure -- no poly-to-unrelated-active spacing rule), so
    # `mos_array`'s unit device needs no per-family gate-pad clearance here
    # (`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` has no `"sg13cmos5l"` entry,
    # resolving to the pre-#1450 `0.0` default) -- verified by running `klt
    # drc --deck sg13cmos5l` against the generator's own output, not assumed
    # by analogy to `sg13g2`.
    "sg13cmos5l": {
        "active": (1, 0),  # Activ.drawing -- EXTRACTION_DECK.active
        "poly": (5, 0),  # GatPoly.drawing -- EXTRACTION_DECK.poly
        "contact": (6, 0),  # Cont.drawing -- EXTRACTION_DECK.contact
        "metal": (8, 0),  # Metal1.drawing -- EXTRACTION_DECK.metals[0]
        "well": (31, 0),  # NWell.drawing -- EXTRACTION_DECK.nwell; lets a
        # `flavor="pfet"` mos_array request enclose the unit device's active
        # region in a well, the same `well`/`well_present` mechanism every
        # other family uses.
        "res_mark": (128, 0),  # PolyRes.drawing --
        # EXTRACTION_DECK.resistors[*].marker (every flavour shares it,
        # `rsil`/`rppd`/`rhigh` are distinguished by the implant/block masks
        # over it -- see `_PDK_RES_FLAVOR_LAYERS` below), the same "marker
        # alone selects the base device class" precedent `sky130`'s/
        # `gf180mcu`'s/`sg13g2`'s own `res_mark` entries document (issue
        # #369).
        "metal_label": (8, 2),  # Metal1.pin -- EXTRACTION_DECK.metal_labels[0].
        # This deck's own convention picks `.pin` (datatype 2), *not*
        # `sg13g2`'s `.text` (datatype 25) -- confirmed against this deck's
        # own module docstring ("a deliberate consistency choice, not a
        # cmos5l-versus-sg13g2 layer-map difference"), not a transcription
        # slip. No `klt gen` generator draws on this role for this family
        # today (only `well_island` does, and it is deferred above).
        "well_label": (31, 2),  # NWell.pin -- EXTRACTION_DECK.well_label; no
        # `sg13g2` counterpart exists at all (that deck declares no
        # `well_label`). Recorded here for completeness/future use, but
        # deliberately unread by every current generator -- no `klt gen`
        # generator anywhere resolves a `"well_label"` role (see the
        # sky130 `"metal_label"` entry's own "well-label tautology" note,
        # issue #1421): naming a well polygon directly would let an
        # extracted body net read back as intended even when the physical
        # tie to it is broken or absent.
        # Second/third routing-metal roles + their connecting vias (issue
        # #1474): cmos5l's own curated *extraction* deck declares the same
        # Metal1/Metal2/Metal3 prefix as `sg13g2`'s stack above (this deck's
        # own module docstring: "cmos5l's real BEOL stack, Metal1 through
        # TopMetal1" -- a documented prefix of sg13g2's Metal1..TopMetal2
        # stack), so `"metal2"`/`"via1"`/`"metal3"`/`"via2"` resolve to the
        # *same* layer/datatype pairs as `sg13g2`'s own entries above --
        # transcribed independently from
        # `klayout_tools.decks.sg13cmos5l.EXTRACTION_DECK.metals`/`.vias`,
        # not copied by analogy. Same `sky130`/`gf180mcu` indexing
        # convention as every other family in this table.
        "metal2": (10, 0),  # Metal2.drawing -- EXTRACTION_DECK.metals[1]
        "via1": (19, 0),  # Via1.drawing -- EXTRACTION_DECK.vias[0] (Metal1<->Metal2)
        "metal3": (30, 0),  # Metal3.drawing -- EXTRACTION_DECK.metals[2]
        "via2": (29, 0),  # Via2.drawing -- EXTRACTION_DECK.vias[1] (Metal2<->Metal3)
        #
        # Well-tie implant (issue #1473): `mos_array`'s own `flavor="pfet"`
        # request draws the `well` shape above (enclosing the unit devices'
        # active regions in `NWell`) but, without this role, nothing tied
        # that well to a real net -- every generated PMOS body extracted
        # unbiased (`klt extract --deck sg13cmos5l`'s own
        # `unbiased_pmos_body_nets[]`, issue #555). This deck declares no
        # distinct tap mask (`EXTRACTION_DECK.tap` is `None`, see the
        # `_GENERATOR_FAMILY_DEFERRED` comment above), but does derive a well
        # tie from `nSD`-covered `Activ` diffusion *inside* `NWell`
        # (`EXTRACTION_DECK.tap_nplus`, issue #1414) -- the same "no
        # dedicated tap layer, implant distinguishes the tie from an ordinary
        # source/drain" shape `gf180mcu`'s own `"well_tap_implant"` entry
        # above documents (there, `Nplus`/32,0 over the shared `Comp`/
        # `"tap"`==`"active"` layer; here, `nSD`/7,0 over the shared `Activ`/
        # `"active"` layer -- no separate `"tap"` role needed since
        # `mos_array` draws its own tap pad directly on `"active"`, it never
        # goes through `_ring_layer_params`'s `"tap"` role at all).
        # `mos_array` draws one small `nSD`-covered `Activ` pad (+ contact +
        # local metal) per array, clear of every unit device's own diffusion
        # by `MIN_SAME_LAYER_SPACING_UM` (well inside this deck's own
        # `activ.space.1` floor, 0.21um) -- see `_mos_array_well_tap_layout`.
        "well_tap_implant": (7, 0),  # nSD.drawing -- EXTRACTION_DECK.tap_nplus
        "dummy": (100, 50),  # curated marker, matches
        # `klayout_tools.decks.sg13cmos5l.EXTRACTION_DECK.dummy` -- see that
        # deck's own comment (issue #2602) for why sg13cmos5l has no native
        # dummy-device GDS layer and why layer 100 (wholly unassigned in
        # this family's own transcribed `.lyp`/DRC/LVS layer tables, unlike
        # gf180mcu's reused `LVS_*` layer number) was picked. `res_array` is
        # the only generator this role currently drives for this family
        # (`_GENERATOR_FAMILY_DEFERRED` above rejects `mos_array`/
        # `diff_pair`/`guard_ring`/`bjt_array`/`esd_device`/`well_island` on
        # `sg13cmos5l`).
    },
}

#: Per-PDK-family geometry **floors** (um) for `cap_array`'s drawn MiM stack,
#: keyed the same way :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` keys its own
#: per-family geometry override. Every value is applied as a ``max(generic,
#: floor)``, never as a replacement, so a family absent from this table (or
#: absent a given key) draws exactly the generic geometry
#: :func:`_cap_unit_layout` computes from `CAP_BOTTOM_PLATE_MARGIN_UM`/
#: `CONTACT_SIZE_UM`/`ENCLOSURE_MARGIN_UM` -- sky130's output is
#: byte-for-byte unchanged by anything here.
#:
#: Keys (all optional per family, ``0.0`` when absent -- see
#: :func:`_cap_geometry_min_um`):
#:
#: - ``cap_bottom_plate_margin_min_um`` -- how far the bottom-plate conductor
#:   must extend past the top plate on every side.
#: - ``cap_top_via_min_w_um`` -- minimum drawn side of the top-plate via.
#: - ``cap_top_via_metal_min_w_um`` -- minimum drawn width of the top-plate
#:   via's landing pad (and of the escape stub/pad drawn on the same layer).
#: - ``cap_min_spacing_um`` -- minimum spacing between adjacent unit cells,
#:   floored under `params.spacing_um`.
#:
#: Per-family provenance:
#:
#: - ``sg13g2`` (issues #1455/#2576): two floors --
#:
#:   - ``cap_top_via_metal_min_w_um`` 1.64um (issue #1455) --
#:     `topmetal1.width.1` (`klayout_tools.decks.sg13g2`'s
#:     `5_22_topmetal1.drc` rule "TM1.a", "Min. TopMetal1 width"), far
#:     coarser than sky130's `met4.width.1` (0.3um) or gf180mcu's own
#:     metal-width rules the generic pad size was originally sized against.
#:     Without this floor, `cap_array`'s default-sized landing pad would
#:     violate `klt drc --deck sg13g2` on every request.
#:   - ``cap_bottom_plate_margin_min_um`` 0.6um (issue #2576) -- "MIM.c"
#:     ("Min. Metal5 enclosure of MIM is 0.60 um", IHP-Open-PDK's own
#:     `libs.tech/klayout/tech/drc/rule_decks/beol/6_11_mim.drc`, value
#:     `drc_rules['Mim_c']` = 0.6 in that deck's `sg13g2_tech_default.json`).
#:     This family's plate pair is `MIM` over `Metal5` (see
#:     :data:`_PDK_ROLE_LAYERS`'s own sg13g2 `cap_top_plate`/
#:     `cap_bottom_plate` entries), so MIM.c governs exactly this margin, and
#:     the generic `CAP_BOTTOM_PLATE_MARGIN_UM` (0.5um) misses it by 0.1um --
#:     every pre-#2576 sg13g2 `cap_array` stream carried that shortfall at
#:     any `plate_w_um`/`plate_h_um`. Unlike every other floor in this table,
#:     it is transcribed from the **PDK's own signoff deck** rather than from
#:     this repo's curated `klayout_tools.decks.sg13g2` `DECK`, because that
#:     curated deck carries no `MIM` rule group at all: `klt drc --deck
#:     sg13g2` could not see the violation (it reported `clean`), and cannot
#:     see the fix either -- the sg13g2 `MIM` layer pair simply stops being
#:     an unruled layer nobody margined against. Transcribing that rule group
#:     into the curated deck is tracked separately (issue #2581); it is not
#:     what makes the drawn geometry legal, this floor is. The floor *was*
#:     verified end to end against the PDK's own runset
#:     (`libs.tech/klayout/tech/drc/ihp-sg13g2.drc`, `tables=main`): 4
#:     `MIM.c` violations before, 0 violations of any rule after. Unlike
#:     gf180mcu's 1.06um below,
#:     no "virtual bottom plate" oversize applies here (sg13g2's bottom plate
#:     is an ordinary conductor), so the floor is the rule's own minimum.
#: - ``gf180mcu`` (issue #1555): three floors its MiM stack's own DRM rules
#:   impose, all transcribed from `klayout_tools.decks.gf180mcu`'s curated
#:   `DECK` (never re-derived here):
#:
#:   - ``cap_bottom_plate_margin_min_um`` 1.06um -- `mim.enclosing.fusetop.1`
#:     (DRM 10.4.2 "MIMTM.3", "Minimum MiM bottom plate overlap of Top
#:     plate") demands at least 0.6um, which the generic
#:     `CAP_BOTTOM_PLATE_MARGIN_UM` (0.5um) does not satisfy. The value used
#:     is the *larger* 1.06um -- `CapacitorDevice.bottom_plate_oversize_um`,
#:     the DRM's own "virtual bottom plate" oversize (10.4.2 footnote 1) --
#:     so the drawn `Metal4` is exactly the virtual bottom plate both
#:     `klt extract` (`FuseTop.sized(1.06) & Metal4.interacting(FuseTop)`)
#:     and this deck's `mim.space.1`/`mim.enclosing.via4.1` `DerivedLayer`
#:     construct, rather than an arbitrary rectangle that merely clears
#:     MIMTM.3. Nothing drawn falls outside the derived plate, and nothing
#:     inside it is left undrawn.
#:   - ``cap_top_via_min_w_um`` 0.26um -- `via4.width.1` (DRM 7.14 "Vn.1",
#:     min Via4 size). The generic `CONTACT_SIZE_UM` (0.22um) was sized
#:     against gf180mcu's `contact.width.1`, which is a *contact* rule; the
#:     top-plate via here is a `Via4`, whose own minimum is coarser.
#:   - ``cap_min_spacing_um`` 1.2um -- `mim.space.1` (DRM 10.4.2 "MIMTM.1",
#:     minimum MiM bottom-plate spacing to other bottom-plate-or-routing
#:     Metal4), measured between the *virtual* bottom plates of adjacent
#:     units. `cap_array`'s own `spacing_um` default (0.5um) and the generic
#:     `MIN_SAME_LAYER_SPACING_UM` advisory (0.4um) are both well under it,
#:     so without this floor every multi-unit gf180mcu request would violate
#:     `klt drc --deck gf180mcu`.
_PDK_CAP_GEOMETRY_MIN_UM: dict[str, dict[str, float]] = {
    "sg13g2": {
        # MIM.c ("Min. Metal5 enclosure of MIM is 0.60 um") -- the drawn
        # `Metal5` bottom plate's own enclosure of the `MIM` top plate, which
        # the generic `CAP_BOTTOM_PLATE_MARGIN_UM` (0.5um) misses by 0.1um:
        "cap_bottom_plate_margin_min_um": 0.6,
        "cap_top_via_metal_min_w_um": 1.64,  # topmetal1.width.1 (TM1.a)
    },
    "gf180mcu": {
        # mim.enclosing.fusetop.1 (MIMTM.3, 0.6um), drawn at the DRM's own
        # virtual-bottom-plate oversize (CapacitorDevice
        # .bottom_plate_oversize_um) instead:
        "cap_bottom_plate_margin_min_um": 1.06,
        "cap_top_via_min_w_um": 0.26,  # via4.width.1 (Vn.1)
        "cap_min_spacing_um": 1.2,  # mim.space.1 (MIMTM.1)
    },
}

#: Every geometry-floor key :data:`_PDK_CAP_GEOMETRY_MIN_UM` may carry --
#: also the exact set of hidden PCell params
#: :func:`_cap_array_layer_params` resolves and `_CapArrayPCell` declares, so
#: the two can never drift apart.
_CAP_GEOMETRY_MIN_KEYS = (
    "cap_bottom_plate_margin_min_um",
    "cap_top_via_min_w_um",
    "cap_top_via_metal_min_w_um",
    "cap_min_spacing_um",
)
