"""Generate parametrized layout cells headlessly via KLayout PCells.

Pure library: :func:`generate` and :func:`list_generators` return plain
Python data (a ``dict`` of JSON-serialisable primitives) and never print,
mirroring ``render.py``/``sim.py``. Serialisation and human-readable
formatting live in the CLI command module (``cli/gen_cmd.py``).

This is phase 1 of Epic #152 (``klt gen``), the build carried by the accepted
spike, ``docs/design/layout-generator-spike.md`` -- read that document first;
its section 2 ("Proposed generator contract") settles the request/response
JSON shape this module implements, and section 1's "KLayout PCells (native)"
entry settles the implementation substrate: a generator is a
``pya.PCellDeclarationHelper`` subclass with a declared parameter schema and
a ``produce_impl()`` method, wrapped by a thin, generator-agnostic harness
(:func:`_produce`) that adapts the request/response envelope to it.

Scope (phase 1): **one** reference generator (``resistor_strip`` -- a row of
parametrized rectangles, no well/tap logic, not DRC-clean) that proves the
headless request -> geometry -> response loop end-to-end.

Scope (phase 2, this module's current state): the four analog primitive
families the spike scopes -- ``mos_array`` (matched transistor array),
``res_array`` (resistor/capacitor array), ``guard_ring`` (substrate/well tap
ring), and ``diff_pair`` (differential pair / current mirror cell, composing
``mos_array``'s unit-device drawing and ``guard_ring``'s ring drawing). Unlike
``resistor_strip``, these four generators are PDK-*aware*: they draw on each
resolved PDK family's own curated-DRC-deck layers (see
:data:`_PDK_ROLE_LAYERS`, sourced from ``klayout_tools.decks.sky130``/
``gf180mcu``, never a private layer map) and are sized to pass ``klt drc
--deck sky130``/``--deck gf180mcu`` clean on their documented default
``params``. Only the ``sky130``/``gf180mcu`` PDK families are supported by
these four generators (see :func:`_pdk_family`); ``resistor_strip`` remains
PDK-agnostic as it always was.

PDK resolution goes through the one resolver every other verb uses
(:func:`klayout_tools.pdk.find_pdk`) -- this module never implements its own
PDK lookup.

Deviation from the spike: the spike's example request shape carries a
``"pdk": {"name": ..., "variant": ...}`` pair where ``name`` is a PDK family
(e.g. ``"sky130"``) distinct from a specific install ``variant`` (e.g.
``"sky130A"``). ``klayout_tools.pdk.find_pdk`` has no family concept -- it
resolves a single ``variant`` string (the same one ``klt pdk find --pdk``
accepts) against an install root. This module keeps that one-resolver
contract rather than inventing a family/variant split the resolver doesn't
have: the request's ``pdk.variant`` field is passed straight through to
``find_pdk(variant=...)``, and the response's ``pdk.name``/``pdk.variant``
both echo the *resolved* variant (see :func:`generate`). A future phase may
revisit this once a real family-aware need surfaces.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._layout import write_layout
from .pdk import PdkNotFoundError, find_pdk

if TYPE_CHECKING:
    import klayout.db as kdb

#: Contract identifier for the request envelope (spike section 2).
REQUEST_SCHEMA = "klt.gen.request/1"

#: Bumped only on a non-additive (breaking) change to this command's own
#: response JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: Name this module registers its reference PCell library under. Registered
#: lazily, once per process (see :func:`_pcell_library`).
_PCELL_LIBRARY_NAME = "klt_gen_reference"

#: PCell parameters are never sourced from the request -- they carry no
#: caller-meaningful value (the drawing layer is a generator implementation
#: detail, not something a request should have to know KLayout's
#: ``LayerInfo`` shape to set). ``layer`` is ``resistor_strip``'s (phase 1)
#: single drawing-layer param; the rest are the phase-2 generators' per-role
#: layer params (see :func:`_device_layer_params` and friends) plus
#: ``well_present``, the harness-computed flag that tells a generator
#: whether its ``well_layer`` param is a real, DRC-checked layer for the
#: resolved PDK family.
_HIDDEN_PARAMS = {
    "layer",
    "active_layer",
    "poly_layer",
    "contact_layer",
    "metal_layer",
    "tap_layer",
    "well_layer",
    "well_present",
    "bjt_mark_layer",
    "bjt_mark_present",
    "res_mark_layer",
    "res_mark_present",
    "res_implant_layer",
    "res_implant_present",
    "res_block_layer",
    "res_block_present",
    "dummy_layer",
    "dummy_present",
    "pad_layer",
    "top_metal_layer",
    "esd_mark_layer",
    "esd_mark_present",
    "salicide_block_layer",
    "salicide_block_present",
}

#: Minimum contact/via drawn size (um) used by every phase-2 generator --
#: exceeds both curated decks' contact-size rule (sky130's ``licon1`` has no
#: size rule at all; gf180mcu's ``contact.width.1`` is 0.22um).
CONTACT_SIZE_UM = 0.22

#: The dbu (database unit, um) every ``_GeneratorSpec.dbu`` entry uses --
#: see the literal ``dbu=0.001`` on each ``_GENERATOR_SPECS`` entry below.
#: Used by :func:`_snap_square_box_um` to pre-round a contact's centre to
#: the manufacturing grid *before* building its box, so the later,
#: independent per-edge ``int(round(x / dbu))`` conversion in
#: :func:`_insert_boxes` can never round one edge up and the other down and
#: silently draw a contact 1 dbu narrower/shorter than ``CONTACT_SIZE_UM``
#: -- exactly the ``contact.width.1`` failure mode diagnosed in issue #685
#: (``CONTACT_SIZE_UM`` has zero DRC margin above gf180mcu's ``CO.1``
#: threshold, so a single dbu of rounding slop is enough to trip it). If a
#: generator ever ships with a different dbu, this needs to become a real
#: parameter instead of a shared constant.
_GRID_DBU_UM = 0.001

#: Margin (um) an active/poly/tap region must keep around a contact it
#: encloses -- exceeds both curated decks' enclosure rules (sky130
#: ``diff.enclosing.licon.1``/``poly.enclosing.licon.1``: 0.04um/0.05um;
#: gf180mcu ``comp.enclosing.contact.1``/``poly2.enclosing.contact.1``:
#: 0.07um each).
ENCLOSURE_MARGIN_UM = 0.1

#: Minimum spacing (um) kept between same-layer shapes belonging to
#: different unit instances (adjacent array cells, ring segments) --
#: exceeds every curated same-layer spacing rule on both decks (sky130
#: ``li1.space.1``: 0.17um; gf180mcu ``comp.space.1``/``poly2.space.1``/
#: ``metal1.space.1``: 0.28um/0.24um/0.23um).
MIN_SAME_LAYER_SPACING_UM = 0.4

#: Margin (um) a well ring/tie keeps around the tap/comp region it encloses
#: -- exceeds gf180mcu's ``nwell.enclosing.comp.1`` (0.12um). sky130's
#: curated deck has no well-layer rule, so this only ever matters on
#: gf180mcu (see :data:`_PDK_ROLE_LAYERS`'s ``"well"`` entries).
WELL_ENCLOSURE_MARGIN_UM = 0.15

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

#: Smallest gate length (um) that keeps a unit MOS device's S/D local-metal
#: pads (see :func:`_mos_unit_layout` -- they abut the poly gate directly on
#: both sides, so the S-D metal gap is exactly ``l_um``) clear of both
#: curated decks' same-layer metal-spacing rule (gf180mcu ``metal1.space.1``:
#: 0.23um is the binding one; sky130's ``li1.space.1`` is only 0.17um), with
#: margin. `mos_array`/`diff_pair` default their ``l_um`` param to this.
GATE_LENGTH_SAFE_MIN_UM = 0.28

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
        "tap": (65, 44),  # tap.drawing -- present in sky130.py's LAYER_NAMES
        # but no curated rule checks it, so a tap ring is DRC-free there.
        "poly": (66, 20),  # poly.drawing
        "contact": (66, 44),  # licon1.drawing
        "metal": (67, 20),  # li1.drawing
        "well": (64, 20),  # nwell.drawing -- matches
        # `klayout_tools.decks.sky130.EXTRACTION_DECK.nwell`; no curated DRC
        # rule checks this layer, but it is real and extraction already
        # trusts it to split nfet/pfet active regions (see `extract.py`).
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
        # `gen_compose`'s router to resolve. `gen_compose`'s via-drop is
        # single-hop only (see `_resolve_via_drop_layer`), so `"metal3"`
        # only reaches a pin already on `"metal2"` (met1, one via hop away)
        # -- a pin still on the base `"metal"` role (li1, two hops away) is
        # not reachable from a `"metal3"` backbone, the same
        # "more-than-one-hop" rejection `docs/cli/gen-compose.md`'s Via-drop
        # section already documents.
        "metal3": (69, 20),  # met2.drawing -- EXTRACTION_DECK.metals[2]
        "via2": (68, 44),  # via.drawing -- EXTRACTION_DECK.vias[1] (met1<->met2)
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
        "top_metal": (72, 20),  # met5.drawing -- this curated deck models no
        # via role between this and `metal3` (met2) above -- sky130.lyt also
        # defines met3/met4/via3/via4, none of which this deck curates -- so
        # `bond_pad`'s own `down_to` param only ever supports `"top_metal"`
        # today (see `_bond_pad_validate`).
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
        # nothing there. gf180mcu exposes only that single flavour, so its
        # implant/block pair lives in :data:`_PDK_RES_FLAVOR_LAYERS` below
        # under the default `"generic"` flavour (issue #463).
        # Second routing-metal role + its connecting via (issue #454, same
        # rationale as sky130's pair above). gf180mcu's curated extraction
        # deck's `metals` stack already runs Metal1-Metal5 (#220); this only
        # exposes the next level up (Metal2) and the via connecting it to
        # `"metal"` (Metal1) -- `gen_compose`'s via-drop is single-hop only,
        # so Metal3-5/`vias[1:]` stay unexposed here (out of this issue's
        # scope, not a structural limit -- see `EXTRACTION_DECK.metals`/
        # `.vias` in `klayout_tools.decks.gf180mcu`).
        "metal2": (36, 0),  # Metal2 -- EXTRACTION_DECK.metals[1]
        "via1": (35, 0),  # Via1 -- EXTRACTION_DECK.vias[0] (Metal1<->Metal2)
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
        # `"generic"` `res_block` entry -- the *same* SAB (49/0) layer, not a
        # second private one: SAB is documented above as a general
        # "salicide block" mask, not a resistor-specific one, so reusing it
        # for `esd_device`'s own `salicide_block` option (a ballast-style
        # unsalicided region over the finger array, the same "floorplan
        # fidelity, not process-exact cross-section" approximation
        # `_bjt_unit_layout` already documents) is not a new citation.
        "salicide_block": (49, 0),  # SAB
    },
}

#: Per-PDK-family poly-resistor *flavour* -> implant/salicide-block layer pair,
#: selected by ``res_array``'s ``flavor`` request param (issue #463). Unlike
#: the always-single roles in :data:`_PDK_ROLE_LAYERS`, a poly resistor's
#: recognised device *class* is chosen by which implant/precision-resistor
#: mask covers its body, and a single PDK family exposes several such classes.
#: The layer/purpose numbers below are the *same* ``requires`` layers the
#: curated *extraction* decks key each flavour off -- never a second, private
#: map: sky130's ``res_high_po``/``res_xhigh_po`` from
#: ``klayout_tools.decks.sky130.EXTRACTION_DECK.resistors`` (issue #222/#299),
#: and gf180mcu's single ``ppolyf_u`` (issue #369). ``None`` means the flavour
#: needs no layer of that kind (sky130's base ``res_generic_po`` requires
#: neither, so a generator simply omits both there). The first-listed flavour
#: per family is that family's default (``_DEFAULT_RES_FLAVOR``), chosen so a
#: request that never mentions ``flavor`` reproduces the pre-#463 geometry
#: exactly (sky130 -> ``res_generic_po``, gf180mcu -> ``ppolyf_u``).
_PDK_RES_FLAVOR_LAYERS: dict[str, dict[str, dict[str, tuple[int, int] | None]]] = {
    "sky130": {
        # sky130_fd_pr__res_generic_po (48.2 ohm/sq): the poly.res marker
        # alone, no implant/block -- the base, lowest-sheet-rho flavour.
        "generic": {"res_implant": None, "res_block": None},
        # sky130_fd_pr__res_high_po_* (319.8 ohm/sq): psdm P+ implant + rpm
        # precision-resistor mask (EXTRACTION_DECK.resistors["res_high_po"].
        # requires).
        "high": {"res_implant": (94, 20), "res_block": (86, 20)},
        # sky130_fd_pr__res_xhigh_po_* (2 kohm/sq): psdm P+ implant + urpm
        # (EXTRACTION_DECK.resistors["res_xhigh_po"].requires). rpm/urpm are
        # mutually exclusive, so drawing urpm (not rpm) selects xhigh.
        "xhigh": {"res_implant": (94, 20), "res_block": (79, 20)},
    },
    "gf180mcu": {
        # ppolyf_u: gf180mcu exposes a single recognised drawn-poly-resistor
        # flavour, which requires both Pplus (implant) and SAB (salicide
        # block) over the RES_MK marker (issue #369).
        "generic": {"res_implant": (31, 0), "res_block": (49, 0)},
    },
}

#: The default ``res_array`` flavour: the first flavour listed for each family
#: in :data:`_PDK_RES_FLAVOR_LAYERS`. Every family's first entry is keyed
#: ``"generic"``, so one default name works across families while resolving to
#: each family's own base device class.
_DEFAULT_RES_FLAVOR = "generic"


def _res_flavor_layers(family: str, flavor: str) -> dict[str, tuple[int, int] | None]:
    """Return the ``res_implant``/``res_block`` layer pair for ``flavor`` in
    ``family`` (see :data:`_PDK_RES_FLAVOR_LAYERS`).

    Raises :class:`GenError` for a flavour the family does not expose, listing
    the valid names -- mirroring :func:`_pdk_family`'s unsupported-family
    error so a bad ``res_array`` ``flavor`` request fails clearly and early
    (during layer resolution, before any geometry is drawn)."""
    flavors = _PDK_RES_FLAVOR_LAYERS[family]
    try:
        return flavors[flavor]
    except KeyError:
        raise GenError(
            f"generator 'res_array': params.flavor '{flavor}' is not a "
            f"recognised poly-resistor flavour for PDK family '{family}' -- "
            f"supported flavours: {', '.join(sorted(flavors))}"
        ) from None


def _pdk_family(variant: str) -> str:
    """Map a resolved PDK ``variant`` (e.g. ``"sky130A"``) to the layer-role
    family (``"sky130"``/``"gf180mcu"``) key in :data:`_PDK_ROLE_LAYERS`.

    Raises :class:`GenError` for any other family -- the four phase-2
    generators draw PDK-specific layers (unlike ``resistor_strip``, which is
    PDK-agnostic and never calls this).
    """
    for family in _PDK_ROLE_LAYERS:
        if variant.startswith(family):
            return family
    raise GenError(
        f"PDK variant '{variant}' is not supported by this generator -- "
        f"supported families: {', '.join(sorted(_PDK_ROLE_LAYERS))}"
    )


def _role_layer_info(family: str, role: str) -> Any:
    """Return the ``kdb.LayerInfo`` for ``role`` in ``family``, or ``None``
    when that family's curated deck has no layer for the role (see
    :data:`_PDK_ROLE_LAYERS`)."""
    import klayout.db as kdb

    pair = _PDK_ROLE_LAYERS[family].get(role)
    return kdb.LayerInfo(*pair) if pair is not None else None


def _resistor_strip_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """``resistor_strip``'s hidden drawing-layer param -- fixed, PDK-agnostic
    (see the module docstring's phase-1/phase-2 scope note)."""
    import klayout.db as kdb

    return {"layer": kdb.LayerInfo(67, 20)}


def _device_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for a generator drawing unit MOS-like devices
    (``mos_array``, and the device half of ``diff_pair``).

    Resolves ``well_layer``/``well_present`` the same way
    :func:`_ring_layer_params` already does, so a ``flavor="pfet"`` request
    can enclose the unit device's active region in a well on PDK families
    whose curated deck checks one.

    Also resolves ``dummy_layer``/``dummy_present`` (issue #491) -- the
    optional PDK dummy-device marker (see :data:`_PDK_ROLE_LAYERS`'s
    ``"dummy"`` role and ``klayout_tools.decks.sky130``'s
    ``EXTRACTION_DECK.dummy``) that ``mos_array`` draws over its
    ``dummy_cells``' gate footprint so ``klt extract``'s existing
    dummy-suppression guards (#295/#462) actually fire. ``None`` (e.g.
    gf180mcu, or ``diff_pair``, which never populates ``dummy_cells``) is a
    silent no-op, matching every other ``*_present``-gated role here."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    well = _role_layer_info(family, "well")
    dummy = _role_layer_info(family, "dummy")
    return {
        "active_layer": _role_layer_info(family, "active"),
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "dummy_layer": dummy if dummy is not None else kdb.LayerInfo(0, 0),
        "dummy_present": dummy is not None,
    }


def _resistor_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``res_array`` (a poly-body unit resistor/cap
    array -- no separate active-layer role).

    Also resolves the PDK's resistor-ID ("marker") layer covering each unit's
    resistive body segment -- without it, `klt extract` cannot recognise the
    drawn poly body as a resistor device: it is absorbed into ordinary poly
    interconnect and the two terminals come out shorted together (issue
    #369). The marker alone selects the *base* device class; the
    higher-sheet-rho flavours a family recognises are selected by additionally
    drawing an implant and/or precision-resistor mask over the same segment
    (see :data:`_PDK_RES_FLAVOR_LAYERS`). The ``flavor`` request param picks
    which flavour -- sky130's `res_generic_po` (default) needs neither mask,
    while `res_high_po`/`res_xhigh_po` each need the psdm implant plus their
    own rpm/urpm mask; gf180mcu's single `ppolyf_u` flavour needs both Pplus
    and SAB. A ``None`` in the resolved pair follows the
    `well_present`/`bjt_mark_present` precedent (the generator omits it).

    Also resolves ``dummy_layer``/``dummy_present`` (issue #491), the same
    optional PDK dummy-device marker :func:`_device_layer_params` resolves --
    see that function's docstring."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    flavor = params.get("flavor", _DEFAULT_RES_FLAVOR)
    flavor_layers = _res_flavor_layers(family, flavor)
    mark = _role_layer_info(family, "res_mark")
    implant_pair = flavor_layers["res_implant"]
    block_pair = flavor_layers["res_block"]
    implant = kdb.LayerInfo(*implant_pair) if implant_pair is not None else None
    block = kdb.LayerInfo(*block_pair) if block_pair is not None else None
    dummy = _role_layer_info(family, "dummy")
    return {
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "res_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "res_mark_present": mark is not None,
        "res_implant_layer": implant if implant is not None else kdb.LayerInfo(0, 0),
        "res_implant_present": implant is not None,
        "res_block_layer": block if block is not None else kdb.LayerInfo(0, 0),
        "res_block_present": block is not None,
        "dummy_layer": dummy if dummy is not None else kdb.LayerInfo(0, 0),
        "dummy_present": dummy is not None,
    }


def _ring_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for a generator drawing a guard ring
    (``guard_ring``, and the optional ring half of ``diff_pair``)."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    well = _role_layer_info(family, "well")
    return {
        "tap_layer": _role_layer_info(family, "tap"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
    }


def _diff_pair_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """``diff_pair`` composes a unit-device array and an optional guard ring
    -- union of both their hidden layer params."""
    layer_params = _device_layer_params(pdk_info, params)
    layer_params.update(_ring_layer_params(pdk_info, params))
    return layer_params


def _bjt_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``bjt_array`` (a matched vertical-bipolar/PNP
    array drawn from base layers -- see the module docstring and
    ``docs/design/gen-bjt-array-spike.md`` for why draw-from-scratch was
    chosen over vendor-library-cell instantiation).

    A unit device draws its emitter/base/collector diffusion on the ``active``
    (COMP/diff) role, contacts on ``contact``, local metal on ``metal``, a
    ``tap`` shape over the base-tie contact (mirrors ``mos_array``'s/
    ``guard_ring``'s ``tap_layer`` -- always a real layer for every currently
    supported family, unlike the ``*_present``-gated roles below; see
    :func:`_bjt_unit_layout`'s docstring for why this is needed so the base
    terminal resolves to a real net rather than a floating node, issue #432),
    an optional shared base ``well`` (Nwell on gf180mcu; sky130's curated deck
    checks none), and a per-unit ``bjt_mark`` device-marking layer drawn on
    every PDK family whose *extraction* deck declares a bipolar marker --
    gf180mcu's ``DRC_BJT`` (also DRC-checked, via its ``bjt.separation.comp.1``
    rule) and sky130's ``pnp.drawing`` (extraction-only, issue #432; see
    :data:`_PDK_ROLE_LAYERS`). ``*_present`` flags follow ``guard_ring``'s
    ``well_present`` precedent so the generator omits a role no currently
    supported family resolves to ``None`` for.

    Also resolves ``dummy_layer``/``dummy_present`` (issue #491), the same
    optional PDK dummy-device marker :func:`_device_layer_params` resolves --
    see that function's docstring.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    well = _role_layer_info(family, "well")
    mark = _role_layer_info(family, "bjt_mark")
    dummy = _role_layer_info(family, "dummy")
    return {
        "active_layer": _role_layer_info(family, "active"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "tap_layer": _role_layer_info(family, "tap"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "bjt_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "bjt_mark_present": mark is not None,
        "dummy_layer": dummy if dummy is not None else kdb.LayerInfo(0, 0),
        "dummy_present": dummy is not None,
    }


def _bond_pad_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``bond_pad`` (issue #568): the passivation
    opening (``pad`` role) and the family's own topmost routing metal
    (``top_metal`` role) that must overlap it by ``params.enclosure_um`` on
    every side. Both roles are real, always-resolved layers for every
    currently supported family -- unlike ``well``/``bjt_mark``/``dummy``
    above, neither is ever ``None``, so this needs no ``*_present`` flag."""
    family = _pdk_family(pdk_info["variant"])
    return {
        "pad_layer": _role_layer_info(family, "pad"),
        "top_metal_layer": _role_layer_info(family, "top_metal"),
    }


def _esd_device_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``esd_device`` (issue #569) -- composes a unit
    MOS device's own layer roles (:func:`_device_layer_params`'s ``active``/
    ``poly``/``contact``/``metal``) and ``guard_ring``'s ring ``tap`` role,
    plus two new roles this generator introduces: ``esd_mark`` (a
    device-class marker, only resolved for gf180mcu -- see
    :data:`_PDK_ROLE_LAYERS`'s own comment for the citation and the sky130
    gap) and ``salicide_block`` (only resolved for gf180mcu, reusing the same
    SAB layer :data:`_PDK_RES_FLAVOR_LAYERS` already cites). Both follow the
    established ``*_present``-gated precedent (``bjt_mark_present``,
    ``dummy_present``) so the generator omits a role no currently supported
    family resolves to a real layer for.

    Deliberately **not** resolving ``guard_ring``'s own ``well`` role here
    (unlike :func:`_ring_layer_params`, which every other ring-composing
    generator uses): ``esd_device`` has no ``flavor`` option and is always an
    NMOS-style device (see the ``_EsdDevicePCell`` class docstring), so
    drawing the ring's automatic well tie the way ``guard_ring``'s own
    ``add_well`` default does would enclose the whole device in an Nwell --
    exactly the well ``diff_pair`` suppresses for its own default
    ``flavor="nfet"`` case (``self.well_present and self.flavor == "pfet"``
    in its ``produce_impl``), because it silently misclassifies the
    enclosed active region as ``pfet`` under ``klt extract``'s ``active &
    nwell`` test (``extract.py``'s ``pfet_active`` derivation) instead of
    ``nfet``.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    esd_mark = _role_layer_info(family, "esd_mark")
    salicide_block = _role_layer_info(family, "salicide_block")
    return {
        "active_layer": _role_layer_info(family, "active"),
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "tap_layer": _role_layer_info(family, "tap"),
        "esd_mark_layer": esd_mark if esd_mark is not None else kdb.LayerInfo(0, 0),
        "esd_mark_present": esd_mark is not None,
        "salicide_block_layer": (
            salicide_block if salicide_block is not None else kdb.LayerInfo(0, 0)
        ),
        "salicide_block_present": salicide_block is not None,
    }


# --------------------------------------------------------------------------- #
# Shared, pure-Python (no `kdb`) geometry helpers -- each phase-2 generator's
# `produce_impl()` (draws, in dbu) and `_GeneratorSpec.describe()` (reports
# ports/device_count in um, for the JSON response) call the *same* one of
# these instead of independently re-deriving the same layout math, so the
# drawn GDS and the reported `ports[]` never drift apart.
# --------------------------------------------------------------------------- #


def _grid_snapped(dbu: float, *values_um: float) -> bool:
    """Whether any of ``values_um`` is not an exact multiple of ``dbu``."""

    def _snapped(value_um: float) -> bool:
        count = value_um / dbu
        return abs(count - round(count)) > 1e-9

    return any(_snapped(v) for v in values_um)


def _mos_finger_positions(
    l_um: float, fingers: int
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """``(seg_positions, poly_positions, total_len_um)`` for a ``fingers``-
    finger MOS unit device: ``fingers + 1`` contact-sized source/drain
    segments alternating with ``fingers`` ``l_um``-wide gate stripes, laid
    out left to right from ``x = 0``.

    Shared by both finger topologies (see :func:`_mos_unit_layout`) so the
    along-the-diffusion arithmetic -- and therefore ``total_len_um``, the
    array column pitch -- is identical whether or not the fingers are
    strapped in parallel.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    seg_positions: list[tuple[float, float]] = []
    poly_positions: list[tuple[float, float]] = []
    x = 0.0
    for i in range(fingers + 1):
        seg_positions.append((x, x + contact_region_um))
        x += contact_region_um
        if i < fingers:
            poly_positions.append((x, x + l_um))
            x += l_um
    return seg_positions, poly_positions, x


def _mos_unit_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    gate_contact: bool = False,
    finger_topology: str = "series",
) -> dict[str, Any]:
    """One MOS-like unit device: a diffusion strip crossed by ``fingers``
    poly gates, with a contact + local-metal pad in each source/drain
    segment (``fingers + 1`` of them) between/around the gates.

    ``finger_topology`` selects what ``fingers > 1`` *means* electrically
    (issue #777). ``"parallel"`` -- what "an N-finger device" conventionally
    denotes -- straps the alternating S/D segments and ties every gate, so
    the unit is one device of width ``fingers * w_um``; that geometry is
    built by :func:`_mos_unit_strapped_layout`. ``"series"`` (this
    function's own body, and the default so
    :func:`_diff_pair_layout`/:func:`_esd_device_layout` keep their existing
    output) draws the bare fingers with no straps at all, which extracts as
    ``fingers`` transistors chained source-to-drain on ``fingers``
    independent gate nets.

    In the ``"series"`` shape only the two end segments (the
    leftmost/rightmost, i.e. this unit's overall source/drain) are exposed
    as ports -- interior segments (for ``fingers > 1``) are drawn but not
    individually reported, mirroring ``resistor_strip``'s P1/P2-only
    precedent, and the interior gates are drawn without a landing pad and so
    cannot be contacted at all. ``mos_array`` warns about exactly that (see
    :func:`_mos_array_describe`).

    The first gate finger additionally carries a **poly landing pad** that
    extends past the diffusion's top edge, so a contact can be placed on the
    gate outside the channel (issue #461). Without it the gate poly shared
    both the top and bottom edge of the diffusion, leaving nowhere legal for
    a gate contact to land: one at the diff edge straddles it
    (``poly.enclosing.licon.1``/``diff.enclosing.licon.1`` violations) and
    one moved inward sits over the channel (a gate-oxide short). The pad is a
    ``contact_region_um`` square (``CONTACT_SIZE_UM`` enclosed by
    ``ENCLOSURE_MARGIN_UM`` on every side -- the same enclosure budget the
    S/D contacts use), centred on the gate ``ENCLOSURE_MARGIN_UM`` clear of
    the diffusion, and the reported gate port sits at its centre. Only the
    reported (first) finger is padded, so the pad never neighbours another
    pad and no inter-pad spacing rule can bind regardless of gate length.

    ``gate_contact`` (issue #492) finishes that stack: it draws a contact
    **and** a local-metal pad on the landing pad, so the gate terminal is a
    metal-role pad exactly like S/D and `klt gen-compose`'s router can reach
    it with no hand-drawn licon/li1 patchwork. It also *moves* the landing
    pad's contact region up by :data:`MIN_SAME_LAYER_SPACING_UM`: the #461
    pad abuts the diffusion's top edge, so a ``contact_region_um`` metal
    square centred on it would share an edge with -- and therefore merge
    into, i.e. **short** to -- the S/D local-metal pads that fill the
    diffusion's own height. The poly grows into a stem of the same
    ``contact_region_um`` width across that clearance so the poly stays one
    connected region, and the reported gate port moves to the raised
    contact's centre. Left at ``False`` (the default) the drawn geometry and
    the reported gate port are byte-for-byte the pre-#492 bare-poly gate.
    """
    if finger_topology == "parallel" and fingers > 1:
        return _mos_unit_strapped_layout(w_um, l_um, fingers, gate_contact)

    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(l_um, fingers)

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, 0.0, total_len_um, w_um)],
        "poly": [(px0, 0.0, px1, w_um) for (px0, px1) in poly_positions],
        "contact": [],
        "metal": [],
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, w_um))
        cx = (sx0 + sx1) / 2.0
        cy = w_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )

    s_xy = ((seg_positions[0][0] + seg_positions[0][1]) / 2.0, w_um / 2.0)
    d_xy = ((seg_positions[-1][0] + seg_positions[-1][1]) / 2.0, w_um / 2.0)

    if poly_positions:
        # Gate landing pad above the first finger. Its half-width
        # (contact_region_um / 2) can exceed l_um / 2, so the pad overhangs
        # the narrow gate on both sides -- that is the point: the pad, not
        # the sub-contact-width gate, is what encloses the contact. It abuts
        # the gate at y == w_um (keeping the poly one connected region) and
        # extends contact_region_um past it.
        #
        # With `gate_contact` the pad's contact region is first pushed
        # MIN_SAME_LAYER_SPACING_UM further out (the poly simply grows into a
        # stem of the same width across that clearance, so it stays one
        # connected region): the metal pad drawn on it is a full
        # contact_region_um square, and centred on the #461 pad it would
        # share the y == w_um edge with the S/D local-metal pads either side
        # -- merging into one polygon and shorting the gate to source/drain.
        g_cx = (poly_positions[0][0] + poly_positions[0][1]) / 2.0
        pad_half = contact_region_um / 2.0
        stem_um = MIN_SAME_LAYER_SPACING_UM if gate_contact else 0.0
        gate_ext_um = stem_um + contact_region_um
        boxes["poly"].append(
            (g_cx - pad_half, w_um, g_cx + pad_half, w_um + gate_ext_um)
        )
        g_cy = w_um + stem_um + contact_region_um / 2.0
        if gate_contact:
            boxes["contact"].append(
                (
                    g_cx - contact_half,
                    g_cy - contact_half,
                    g_cx + contact_half,
                    g_cy + contact_half,
                )
            )
            boxes["metal"].append(
                (g_cx - pad_half, g_cy - pad_half, g_cx + pad_half, g_cy + pad_half)
            )
        g_xy = (g_cx, g_cy)
        g_width_um = contact_region_um
    else:
        g_xy = (total_len_um / 2.0, w_um)
        g_width_um = l_um
        gate_ext_um = 0.0

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        # Full drawn height including the gate landing pad -- what row-to-row
        # placement (see `_mos_array_layout`/`_diff_pair_layout`) and the
        # enclosing well box must span. `height_um` stays the bare diffusion
        # height because it is the S/D ports' perpendicular width.
        "bbox_height_um": w_um + gate_ext_um,
        "gate_ext_um": gate_ext_um,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": g_xy,
        "g_width_um": g_width_um,
        # Perpendicular width of the reported S/D ports. Equal to the bare
        # diffusion height here (the ports sit on the S/D pads, which span
        # it); the strapped topology reports its strap-rail height instead
        # (see `_mos_unit_strapped_layout`).
        "sd_width_um": w_um,
        "finger_topology": "series",
    }


def _mos_unit_strapped_layout(
    w_um: float, l_um: float, fingers: int, gate_contact: bool = False
) -> dict[str, Any]:
    """One **parallel** multi-finger MOS unit device (issue #777): the same
    ``fingers``-stripe diffusion :func:`_mos_unit_layout` draws, plus the
    straps that actually make it one device of width ``fingers * w_um``.

    Conventionally "an N-finger device" means N gate stripes over a shared
    diffusion with the alternating S/D segments strapped together and all N
    gates tied -- the whole reason to fold a wide device rather than place N
    separate unit devices. Drawing only the stripes (what
    :func:`_mos_unit_layout`'s ``"series"`` shape does) instead yields N
    transistors chained source-to-drain on N floating gates, which is not a
    device any caller asked for and cannot be repaired from outside: the
    interior segments and gates have no reported ports and no landing pads.

    The strapping is two full-width ``metal`` rails plus a ``poly`` comb,
    drawn bottom to top on the single role pair the phase-2 generators have.
    There is no second routing metal to cross on, so the two S/D rails go on
    *opposite* sides of the diffusion and the gate stripes cross **under**
    the drain rail on ``poly`` to reach their comb -- legal on both curated
    decks, and no contact there means no connection::

        y = 0                    source rail  (metal, full width)
        + MIN_SAME_LAYER_SPACING_UM           source stubs (even segments)
        + w_um                   diffusion + per-segment S/D pads
        + MIN_SAME_LAYER_SPACING_UM           drain stubs (odd segments)
        + CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
                                 drain rail   (metal, full width)
        + MIN_SAME_LAYER_SPACING_UM           gate stripes crossing under it
        + CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
                                 gate comb    (poly, ties every finger)

    Even-indexed S/D segments (0, 2, ...) drop a stub to the source rail and
    odd-indexed ones (1, 3, ...) raise a stub to the drain rail, so every
    finger sits between the two rails -- i.e. ``fingers`` transistors in
    parallel on one gate net, which ``klt lvs``'s
    ``options.combine_devices`` folds into the single ``W = fingers * w_um``
    device the schematic states. Each gate stripe is simply extended past
    the drain rail into the comb (so the whole gate net stays one connected
    poly region, no per-finger landing pad needed), and the reported gate
    terminal (with
    ``gate_contact``, a contact + local-metal pad; without it, bare poly, as
    ``mos_array``'s pre-#492 default) sits at the comb's centre, a full
    :data:`MIN_SAME_LAYER_SPACING_UM` clear of the drain rail's metal.

    Every clearance is :data:`MIN_SAME_LAYER_SPACING_UM`, which already
    exceeds both curated decks' same-layer spacing rules, and every rail and
    stub is :data:`CONTACT_SIZE_UM` + 2 * :data:`ENCLOSURE_MARGIN_UM` wide
    -- the same width budget the S/D pads use -- so no minimum-width rule
    binds either. ``total_len_um`` is identical to the series shape's, so
    the array column pitch does not move; only ``bbox_height_um`` grows.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    clearance_um = MIN_SAME_LAYER_SPACING_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(l_um, fingers)

    source_rail_y1 = contact_region_um
    diff_y0 = source_rail_y1 + clearance_um
    diff_y1 = diff_y0 + w_um
    drain_rail_y0 = diff_y1 + clearance_um
    drain_rail_y1 = drain_rail_y0 + contact_region_um
    comb_y0 = drain_rail_y1 + clearance_um
    comb_y1 = comb_y0 + contact_region_um

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, diff_y0, total_len_um, diff_y1)],
        # Each gate stripe runs the diffusion height and then up past the
        # drain rail into the comb, keeping the whole gate net one connected
        # poly region.
        "poly": [(px0, diff_y0, px1, comb_y0) for (px0, px1) in poly_positions],
        "contact": [],
        "metal": [
            (0.0, 0.0, total_len_um, source_rail_y1),
            (0.0, drain_rail_y0, total_len_um, drain_rail_y1),
        ],
    }
    boxes["poly"].append(
        (poly_positions[0][0], comb_y0, poly_positions[-1][1], comb_y1)
    )

    contact_half = CONTACT_SIZE_UM / 2.0
    for i, (sx0, sx1) in enumerate(seg_positions):
        boxes["metal"].append((sx0, diff_y0, sx1, diff_y1))
        cx = (sx0 + sx1) / 2.0
        cy = (diff_y0 + diff_y1) / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )
        if i % 2 == 0:
            boxes["metal"].append((sx0, source_rail_y1, sx1, diff_y0))
        else:
            boxes["metal"].append((sx0, diff_y1, sx1, drain_rail_y0))

    # S/D terminals are reported on their rails, at the end the series shape
    # reported them at (west for source, east for drain) so a consumer's
    # `direction_deg`-driven stub still exits the cell the same way.
    s_xy = (contact_region_um / 2.0, source_rail_y1 / 2.0)
    d_xy = (
        total_len_um - contact_region_um / 2.0,
        (drain_rail_y0 + drain_rail_y1) / 2.0,
    )

    g_cx = (poly_positions[0][0] + poly_positions[-1][1]) / 2.0
    g_cy = (comb_y0 + comb_y1) / 2.0
    if gate_contact:
        pad_half = contact_region_um / 2.0
        boxes["contact"].append(
            (
                g_cx - contact_half,
                g_cy - contact_half,
                g_cx + contact_half,
                g_cy + contact_half,
            )
        )
        boxes["metal"].append(
            (g_cx - pad_half, g_cy - pad_half, g_cx + pad_half, g_cy + pad_half)
        )

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        "bbox_height_um": comb_y1,
        "gate_ext_um": comb_y1 - diff_y1,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": (g_cx, g_cy),
        "g_width_um": contact_region_um,
        # The S/D ports sit on the strap rails, not on the diffusion pads,
        # so their perpendicular width is the rail height.
        "sd_width_um": contact_region_um,
        "finger_topology": "parallel",
    }


def _centroid_order(rows: int, cols: int) -> list[tuple[int, int]]:
    """A centroid-symmetric visiting order over a ``rows`` x ``cols`` grid:
    positions are visited nearest-to-center first, each immediately followed
    by its point-reflection through the grid center -- the numbering
    convention ``mos_array``'s ``topology="common_centroid"`` uses so a
    downstream matching/LVS consumer can pair instance ``2k`` with
    ``2k + 1`` as centroid-symmetric partners (see ``docs/cli/gen.md``).
    """
    positions = [(r, c) for r in range(rows) for c in range(cols)]
    cy = (rows - 1) / 2.0
    cx = (cols - 1) / 2.0

    def _dist(pos: tuple[int, int]) -> float:
        r, c = pos
        return (r - cy) ** 2 + (c - cx) ** 2

    positions.sort(key=lambda pos: (_dist(pos), pos[0], pos[1]))

    ordered: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for pos in positions:
        if pos in seen:
            continue
        ordered.append(pos)
        seen.add(pos)
        mirror = (rows - 1 - pos[0], cols - 1 - pos[1])
        if mirror != pos and mirror not in seen:
            ordered.append(mirror)
            seen.add(mirror)
    return ordered


def _mos_array_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    rows: int,
    cols: int,
    dummy: int,
    topology: str,
    gate_contact: bool = False,
    finger_topology: str = "parallel",
) -> dict[str, Any]:
    """A ``rows`` x ``cols`` grid of :func:`_mos_unit_layout` unit devices,
    with ``dummy`` extra unit-device columns flanking each side.

    ``finger_topology`` is forwarded to :func:`_mos_unit_layout` and decides
    whether a ``fingers > 1`` unit is one parallel folded device (the
    default) or an unstrapped series chain (issue #777).

    Also computes ``well_box_um`` -- a single well shape (per
    :data:`WELL_ENCLOSURE_MARGIN_UM`) enclosing every cell's (real and dummy)
    active region, the same "one shared tub for the whole matched group"
    shape :func:`_bjt_array_layout` draws for its own shared base well --
    used only when the caller requests ``flavor="pfet"``."""
    unit = _mos_unit_layout(w_um, l_um, fingers, gate_contact, finger_topology)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["bbox_height_um"] + MIN_SAME_LAYER_SPACING_UM

    order = (
        _centroid_order(rows, cols)
        if topology == "common_centroid"
        else [(r, c) for r in range(rows) for c in range(cols)]
    )
    cells = [
        {"idx": idx, "row": r, "col": c, "x0_um": c * col_pitch, "y0_um": r * row_pitch}
        for idx, (r, c) in enumerate(order)
    ]

    dummy_cells = []
    for r in range(rows):
        for dc in range(1, dummy + 1):
            dummy_cells.append(
                {"row": r, "col": -dc, "x0_um": -dc * col_pitch, "y0_um": r * row_pitch}
            )
            dummy_cells.append(
                {
                    "row": r,
                    "col": cols - 1 + dc,
                    "x0_um": (cols - 1 + dc) * col_pitch,
                    "y0_um": r * row_pitch,
                }
            )

    all_cells = cells + dummy_cells
    min_x0 = min(c["x0_um"] for c in all_cells)
    max_x1 = max(c["x0_um"] + unit["total_len_um"] for c in all_cells)
    min_y0 = min(c["y0_um"] for c in all_cells)
    max_y1 = max(c["y0_um"] + unit["bbox_height_um"] for c in all_cells)
    margin = WELL_ENCLOSURE_MARGIN_UM
    well_box = (min_x0 - margin, min_y0 - margin, max_x1 + margin, max_y1 + margin)

    return {
        "unit": unit,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "well_box_um": well_box,
    }


def _res_unit_layout(length_um: float, width_um: float) -> dict[str, Any]:
    """One unit resistor (or unit MoM/MiM cap cell footprint): a poly body
    of ``length_um`` between two contact+local-metal end pads.

    ``boxes_um["marker"]`` is the recognised resistive *body segment* -- the
    middle ``length_um``-wide span between the two ``contact_region_um`` end
    pads, deliberately excluding them. This is where the PDK's resistor-ID
    layer (and, on gf180mcu, its implant/salicide-block requires-layers) get
    drawn (issue #369): covering the whole unit (heads included) would
    consume the contacted end pads into the recognised body too, leaving no
    poly behind for the contacts to land on -- see
    ``klayout_tools.extract._resolve_resistors``, which subtracts the
    recognised body from the conductor region to derive the terminal poly.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    total_len_um = 2 * contact_region_um + length_um
    seg_positions = [
        (0.0, contact_region_um),
        (total_len_um - contact_region_um, total_len_um),
    ]

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "poly": [(0.0, 0.0, total_len_um, width_um)],
        "contact": [],
        "metal": [],
        "marker": [(contact_region_um, 0.0, contact_region_um + length_um, width_um)],
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, width_um))
        cx = (sx0 + sx1) / 2.0
        cy = width_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )

    a_xy = ((seg_positions[0][0] + seg_positions[0][1]) / 2.0, width_um / 2.0)
    b_xy = ((seg_positions[-1][0] + seg_positions[-1][1]) / 2.0, width_um / 2.0)
    return {
        "total_len_um": total_len_um,
        "height_um": width_um,
        "boxes_um": boxes,
        "a_xy": a_xy,
        "b_xy": b_xy,
    }


def _res_array_layout(
    length_um: float,
    width_um: float,
    spacing_um: float,
    num: int,
    dummy: int,
    rows: int = 1,
) -> dict[str, Any]:
    """``num`` matched unit resistors (see :func:`_res_unit_layout`), folded
    into ``rows`` parallel rows in boustrophedon ("snake") order once
    ``rows > 1`` -- row 0 runs left to right, row 1 right to left, and so on
    -- so consecutive unit indices ``i``/``i + 1`` (the series-chain order
    implied by the ``R<i>_A``/``R<i>_B`` port-naming convention) stay
    physically adjacent across a row transition, the same way a real
    interdigitated resistor ladder folds a long string into a compact,
    roughly-square footprint instead of one long strip (issue #415).
    ``rows=1`` (the default) reproduces the original single-row layout
    exactly, byte-for-byte in cell placement.

    Row-to-row pitch mirrors :func:`_mos_array_layout`'s own row pitch (unit
    height plus the fixed :data:`MIN_SAME_LAYER_SPACING_UM` margin) rather
    than the caller-supplied ``spacing_um``, which stays scoped to
    within-row (horizontal) unit spacing only, per its documented meaning.

    Dummy elements (``dummy`` per end) extend the row that the real chain's
    first/last unit sits in, continuing in that row's own fold direction --
    for ``rows=1`` this is exactly the original "before index 0 / after
    index num - 1" placement.

    Each real cell carries a ``direction`` (+1/-1): a unit's own poly body
    is drawn identically regardless of row direction (never mirrored), so
    on a right-to-left row the physically-adjacent pad pair for a short
    row-transition jumper is the *reverse* of the left-to-right case --
    ``direction`` lets :func:`_res_array_describe`'s port-naming swap which
    physical pad (``a_xy``/``b_xy``) reports as ``_A``/``_B`` so ``R<i>_B``
    stays physically next to ``R<i + 1>_A`` in every row, not just even
    ones.
    """
    unit = _res_unit_layout(length_um, width_um)
    pitch = unit["total_len_um"] + spacing_um
    row_pitch = unit["height_um"] + MIN_SAME_LAYER_SPACING_UM
    cols_per_row = -(-num // rows) if rows > 0 else num  # ceil(num / rows)

    def _row_and_column(i: int) -> tuple[int, int, int]:
        """(row, column, direction) for unit index ``i`` under the
        boustrophedon fold -- ``direction`` is +1 for a left-to-right row,
        -1 for a right-to-left one."""
        row = i // cols_per_row
        local_j = i % cols_per_row
        direction = 1 if row % 2 == 0 else -1
        column = local_j if direction == 1 else cols_per_row - 1 - local_j
        return row, column, direction

    cells = []
    for i in range(num):
        row, column, direction = _row_and_column(i)
        cells.append(
            {
                "idx": i,
                "x0_um": column * pitch,
                "y0_um": row * row_pitch,
                "direction": direction,
            }
        )

    dummy_cells = []
    first_row, first_column, first_direction = _row_and_column(0)
    last_row, last_column, last_direction = _row_and_column(num - 1)
    for dc in range(1, dummy + 1):
        dummy_cells.append(
            {
                "x0_um": (first_column - first_direction * dc) * pitch,
                "y0_um": first_row * row_pitch,
            }
        )
        dummy_cells.append(
            {
                "x0_um": (last_column + last_direction * dc) * pitch,
                "y0_um": last_row * row_pitch,
            }
        )
    return {
        "unit": unit,
        "pitch_um": pitch,
        "row_pitch_um": row_pitch,
        "cols_per_row": cols_per_row,
        "cells": cells,
        "dummy_cells": dummy_cells,
    }


#: The four sides a ring gap (``ring_gap_side``) can be cut on, and the
#: outward normal (``direction_deg``) each one's reported port faces.
RING_SIDE_DIRECTIONS: dict[str, int] = {"N": 90, "S": 270, "E": 0, "W": 180}


def _ring_gap_layout(
    outer_w_um: float,
    outer_h_um: float,
    ring_w_um: float,
    side: str,
    gap_um: float,
    gap_offset_um: float,
) -> dict[str, Any]:
    """One opening ("gap") cut through a ring's band on ``side`` (#434).

    The opening spans the ring's full thickness on that side and ``gap_um``
    along it, centred on the side's midpoint plus ``gap_offset_um`` (positive
    is toward +x on ``"N"``/``"S"``, toward +y on ``"E"``/``"W"``). Exactly
    *one* side may be cut (enforced by the per-generator validators), so the
    ring stays a single connected C-shaped conductor -- two openings would
    split it into two electrically separate arcs while its tap ports still
    claimed one net.

    Returns the cut ``box_um`` (subtracted from the ring by
    :func:`_insert_ring`), the opening's centre (``x_um``/``y_um``, on the
    ring's own centre line for that side, i.e. where a ``GAP_<side>`` port is
    reported), its ``opening_um`` length, and the ``span_um`` interval it
    covers along the side's axis.
    """
    half = gap_um / 2.0
    if side in ("N", "S"):
        cx = outer_w_um / 2.0 + gap_offset_um
        cy = outer_h_um - ring_w_um / 2.0 if side == "N" else ring_w_um / 2.0
        y0 = outer_h_um - ring_w_um if side == "N" else 0.0
        y1 = outer_h_um if side == "N" else ring_w_um
        box = (cx - half, y0, cx + half, y1)
        span = (cx - half, cx + half)
    else:
        cy = outer_h_um / 2.0 + gap_offset_um
        cx = outer_w_um - ring_w_um / 2.0 if side == "E" else ring_w_um / 2.0
        x0 = outer_w_um - ring_w_um if side == "E" else 0.0
        x1 = outer_w_um if side == "E" else ring_w_um
        box = (x0, cy - half, x1, cy + half)
        span = (cy - half, cy + half)

    return {
        "side": side,
        "box_um": box,
        "x_um": cx,
        "y_um": cy,
        "opening_um": gap_um,
        "span_um": span,
    }


def _auto_ring_inner_size_um(info: dict[str, Any]) -> tuple[float, float]:
    """Inner (protected-area) size of the automatically-sized ring in a
    ``diff_pair``/``bjt_array`` layout dict -- the straight run available on
    each ring side, which is what a ring opening has to fit inside
    (:func:`_validate_ring_gap`). ``(0.0, 0.0)`` when the layout draws no
    ring."""
    ring = info.get("ring")
    if ring is None:
        return (0.0, 0.0)
    x0, y0, x1, y1 = ring["inner_box_um"]
    return (x1 - x0, y1 - y0)


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    """Whether two axis-aligned boxes overlap with non-zero area (a shared
    edge alone is not an overlap)."""
    eps = 1e-9
    return (
        min(a[2], b[2]) - max(a[0], b[0]) > eps
        and min(a[3], b[3]) - max(a[1], b[1]) > eps
    )


def _snap_square_box_um(
    cx_um: float, cy_um: float, half_um: float, dbu_um: float
) -> tuple[float, float, float, float]:
    """A ``2 * half_um``-wide square centred on ``(cx_um, cy_um)``, snapped
    to the ``dbu_um`` manufacturing grid *before* the box edges are derived,
    so the returned box's width and height are exactly ``2 * round(half_um /
    dbu_um) * dbu_um`` regardless of where the un-snapped centre falls
    relative to the grid.

    This matters because :func:`_insert_boxes`/``_insert_ring``'s ``_to_box``
    round each of a box's *four edges* to the dbu grid independently
    (``int(round(x0 / dbu))`` ... ``int(round(x1 / dbu))``). Building the box
    from an un-snapped float centre first (``cx - half``, ``cx + half``) lets
    ordinary floating-point noise put one edge just below a half-dbu grid
    boundary and the other just above it -- Python's banker's-rounding
    ``round()`` can then round those two edges in *different* directions,
    silently drawing a box 1 dbu narrower/shorter than intended. That is
    exactly the ``contact.width.1`` failure mode diagnosed in issue #685:
    ``CONTACT_SIZE_UM`` carries zero DRC margin above gf180mcu's ``CO.1``
    threshold, so a single dbu of asymmetric rounding is enough to trip it.
    Snapping the centre (and the half-width, which is already grid-exact for
    every current caller) to integer dbu counts *first* makes both edges
    derive from the same rounded integer, so they can never drift apart.
    """
    cx_dbu = round(cx_um / dbu_um)
    cy_dbu = round(cy_um / dbu_um)
    half_dbu = round(half_um / dbu_um)
    return (
        (cx_dbu - half_dbu) * dbu_um,
        (cy_dbu - half_dbu) * dbu_um,
        (cx_dbu + half_dbu) * dbu_um,
        (cy_dbu + half_dbu) * dbu_um,
    )


def _ring_layout(
    inner_w_um: float,
    inner_h_um: float,
    ring_w_um: float,
    contacts_per_side: int | tuple[int, int],
    gap_side: str = "",
    gap_um: float = 0.0,
    gap_offset_um: float = 0.0,
) -> dict[str, Any]:
    """A tap/metal ring (drawn as an outer-box-minus-inner-box boolean, see
    :func:`_insert_ring`, so it is always one unbroken polygon -- no
    same-layer spacing violation between ring segments) around an
    ``inner_w_um`` x ``inner_h_um`` protected area, with contacts evenly
    spaced along each of the four sides.

    ``contacts_per_side`` is either a single ``int`` -- the historical
    behaviour, applied uniformly to all four sides -- or an ``(ns, ew)``
    tuple: ``ns`` contacts on each of the N/S (top/bottom) sides, spaced
    along ``inner_w_um``, and ``ew`` contacts on each of the E/W (left/
    right) sides, spaced along ``inner_h_um``. A caller sizing a ring around
    a strongly non-square inner region can then target an independent pitch
    on each axis instead of having the achievable count capped by whichever
    side is shorter (issue #685).

    ``gap_side`` (``""`` = closed ring, the default) cuts one routing opening
    through the ring on that side (#434) -- see :func:`_ring_gap_layout`. A
    contact that would be clipped by the opening is dropped, and the side's
    own tap port is dropped from ``ports`` when its midpoint falls inside the
    opening (there is no metal left under it there); the ring stays one
    connected conductor either way, so the remaining tap ports still describe
    a single tap net.
    """
    outer_w = inner_w_um + 2 * ring_w_um
    outer_h = inner_h_um + 2 * ring_w_um
    contact_half = CONTACT_SIZE_UM / 2.0
    contacts_ns, contacts_ew = (
        contacts_per_side
        if isinstance(contacts_per_side, tuple)
        else (contacts_per_side, contacts_per_side)
    )

    gap = (
        _ring_gap_layout(outer_w, outer_h, ring_w_um, gap_side, gap_um, gap_offset_um)
        if gap_side
        else None
    )

    centers: list[tuple[float, float]] = []
    for i in range(contacts_ns):
        t = (i + 1) / (contacts_ns + 1)
        cx = ring_w_um + t * inner_w_um
        centers.append((cx, outer_h - ring_w_um / 2.0))  # top
        centers.append((cx, ring_w_um / 2.0))  # bottom
    for i in range(contacts_ew):
        t = (i + 1) / (contacts_ew + 1)
        cy = ring_w_um + t * inner_h_um
        centers.append((ring_w_um / 2.0, cy))  # left
        centers.append((outer_w - ring_w_um / 2.0, cy))  # right

    contact_boxes = [
        _snap_square_box_um(cx, cy, contact_half, _GRID_DBU_UM) for (cx, cy) in centers
    ]
    if gap is not None:
        contact_boxes = [
            box for box in contact_boxes if not _boxes_overlap(box, gap["box_um"])
        ]

    ports = {
        "N": (outer_w / 2.0, outer_h - ring_w_um / 2.0),
        "S": (outer_w / 2.0, ring_w_um / 2.0),
        "E": (outer_w - ring_w_um / 2.0, outer_h / 2.0),
        "W": (ring_w_um / 2.0, outer_h / 2.0),
    }
    if gap is not None:
        px, py = ports[gap["side"]]
        along = px if gap["side"] in ("N", "S") else py
        lo, hi = gap["span_um"]
        if lo - 1e-9 <= along <= hi + 1e-9:
            del ports[gap["side"]]

    return {
        "outer_w_um": outer_w,
        "outer_h_um": outer_h,
        "outer_box_um": (0.0, 0.0, outer_w, outer_h),
        "inner_box_um": (
            ring_w_um,
            ring_w_um,
            ring_w_um + inner_w_um,
            ring_w_um + inner_h_um,
        ),
        "contact_boxes_um": contact_boxes,
        "gap": gap,
        "ports": ports,
    }


def _diff_pair_layout(
    w_um: float,
    l_um: float,
    splits: int,
    add_guard_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    row_spacing_um: float = MIN_SAME_LAYER_SPACING_UM,
    gate_contact: bool = False,
) -> dict[str, Any]:
    """Two matched devices (``"A"``/``"B"``), each split into ``splits``
    unit sub-instances, interleaved in a true common-centroid cross-quad
    checkerboard over a 2-row x ``splits``-col grid: ``label(row, col) = "A"
    if (row + col) is even else "B"`` -- for ``splits=2`` this is exactly the
    classic differential-pair "A B / B A" layout; it generalises the same
    way for any ``splits``, each column always holding one A and one B.

    ``ring_padding_um``/``row_spacing_um`` (issue #484) are the ring-to-core
    padding and the device-row-to-device-row gap, both fixed at their
    present-day values by default so omitting them reproduces prior geometry
    byte-for-byte; a caller that needs more room to route both matched
    devices' gate nets out of the block can grow either and pay the area.
    ``col_pitch`` (the within-row gap between interleaved splits) stays
    fixed to ``MIN_SAME_LAYER_SPACING_UM`` -- out of scope for #484.

    ``gate_contact`` (issue #492) is forwarded to :func:`_mos_unit_layout`;
    it grows each unit's ``bbox_height_um``, so both the row pitch and the
    automatically-sized guard ring follow it without any further arithmetic
    here.
    """
    unit = _mos_unit_layout(w_um, l_um, 1, gate_contact)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["bbox_height_um"] + row_spacing_um

    counts = {"A": 0, "B": 0}
    cells = []
    for row in range(2):
        for col in range(splits):
            label = "A" if (row + col) % 2 == 0 else "B"
            counts[label] += 1
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "label": label,
                    "n": counts[label],
                    "x0_um": col * col_pitch,
                    "y0_um": row * row_pitch,
                }
            )

    core_w = splits * col_pitch - MIN_SAME_LAYER_SPACING_UM
    core_h = 2 * row_pitch - row_spacing_um

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            core_w + 2 * padding,
            core_h + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = (-(ring_w + padding), -(ring_w + padding))

    return {
        "unit": unit,
        "cells": cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "core_w_um": core_w,
        "core_h_um": core_h,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


#: Gap (um) kept between the shared base well and the collector guard ring
#: that surrounds it -- exceeds gf180mcu's ``comp.space.1`` (0.28um), so the
#: collector COMP ring never crowds the emitter/base COMP inside the well.
BJT_COLLECTOR_GAP_UM = 0.4

#: Margin (um) the per-unit bipolar device-mark box (``boxes_um["marker"]``,
#: see :func:`_bjt_unit_layout`) is grown beyond the emitter pad it encloses,
#: on every side. Two independent reasons this must be strictly positive
#: (issue #432):
#:
#: 1. A device-mark box exactly coincident with the emitter pad makes
#:    ``base == emitter`` in `extract.py` (``base = active & marker``,
#:    ``emitter = active & base``) -- `klt extract`'s
#:    ``DeviceExtractorBJT3Transistor`` then finds no base-minus-emitter
#:    extension to derive a collector terminal from and aborts with an
#:    unhandled ``RuntimeError`` (the "coincident-marker" bug this issue
#:    fixes) instead of the documented clean error envelope.
#: 2. Reuses `MIN_SAME_LAYER_SPACING_UM`'s existing 0.4um unit-to-unit gap:
#:    growing the marker by `ENCLOSURE_MARGIN_UM` (0.1um) on every side still
#:    leaves 0.3um of clearance to the base-tie pad (same unit) and to every
#:    neighbouring unit/row -- comfortably above gf180mcu's
#:    ``bjt.separation.comp.1`` 0.1um threshold (see `decks/gf180mcu.py`), so
#:    the per-unit marker never trips that DRC rule.
BJT_MARK_GROWTH_UM = ENCLOSURE_MARGIN_UM


def _bjt_unit_layout(emitter_um: float) -> dict[str, Any]:
    """One vertical-PNP unit device, drawn from base layers (not a vendor
    library cell -- see ``docs/design/gen-bjt-array-spike.md``): a square
    emitter diffusion pad beside a base-tie diffusion pad, each with a
    contact and a covering local-metal pad, both sitting inside the array's
    shared base well.

    The two pads model the device's emitter (P+ in the Nwell base) and its
    base tie (N+ Nwell contact) as adjacent COMP/diff regions -- a
    DRC-clean, matching-faithful *floorplan* of the device, deliberately not
    a process-exact vertical cross-section (the curated decks check no
    implant layer, so emitter/base/collector all draw on the one ``active``
    role; that fidelity limit is recorded in the design note). The array's
    collector guard ring is drawn once at array level, not per unit (see
    :func:`_bjt_array_layout`).

    ``boxes_um["marker"]`` is the per-unit bipolar device-mark box (issue
    #432) -- the emitter pad grown by :data:`BJT_MARK_GROWTH_UM` on every
    side, deliberately *not* the whole unit (base-tie pad included): sky130's
    ``EXTRACTION_DECK.bipolars`` entry derives ``base = active & marker`` and
    ``emitter = active & base``, so a marker coincident with the whole unit
    (or the whole array, as this generator drew previously) would make every
    diffusion pad inside it -- the base-tie pad included -- look like a
    second emitter of the same device.

    ``boxes_um["tap"]`` is a ``tap``-role shape over the base-tie pad
    (coincident with ``base_box``) -- sky130's extraction deck reaches the
    shared base well's node through ``nwell -> tap -> contact -> metals``
    (see `extract.py`'s connectivity section); without a drawn ``tap`` shape
    there, the base-tie pad's own contact/metal stack has no path to the
    device's recognised base region and the base terminal resolves to a
    floating anonymous node instead of a real net.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    gap = MIN_SAME_LAYER_SPACING_UM
    base_x0 = emitter_um + gap
    base_x1 = base_x0 + contact_region_um
    total_len_um = base_x1
    height_um = emitter_um

    emitter_box = (0.0, 0.0, emitter_um, height_um)
    base_box = (base_x0, 0.0, base_x1, height_um)
    marker_box = (
        -BJT_MARK_GROWTH_UM,
        -BJT_MARK_GROWTH_UM,
        emitter_um + BJT_MARK_GROWTH_UM,
        height_um + BJT_MARK_GROWTH_UM,
    )

    contact_half = CONTACT_SIZE_UM / 2.0
    ecx, ecy = emitter_um / 2.0, height_um / 2.0
    bcx, bcy = (base_x0 + base_x1) / 2.0, height_um / 2.0

    def _contact_box(cx: float, cy: float) -> tuple[float, float, float, float]:
        return (
            cx - contact_half,
            cy - contact_half,
            cx + contact_half,
            cy + contact_half,
        )

    contacts = [_contact_box(ecx, ecy), _contact_box(bcx, bcy)]

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [emitter_box, base_box],
        "contact": contacts,
        "metal": [emitter_box, base_box],
        "marker": [marker_box],
        "tap": [base_box],
    }
    return {
        "total_len_um": total_len_um,
        "height_um": height_um,
        "boxes_um": boxes,
        "e_xy": (ecx, ecy),
        "b_xy": (bcx, bcy),
    }


def _bjt_array_layout(
    emitter_um: float,
    rows: int,
    cols: int,
    dummy: int,
    topology: str,
    add_collector_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
) -> dict[str, Any]:
    """A ``rows`` x ``cols`` common-centroid (or plain-array) grid of
    :func:`_bjt_unit_layout` unit devices, with ``dummy`` flanking columns
    each side, a single shared base well enclosing every unit's diffusion,
    and an optional collector guard ring (drawn via :func:`_ring_layout` so
    it is one unbroken polygon).

    Each unit's own ``boxes_um["marker"]``/``boxes_um["tap"]`` (see
    :func:`_bjt_unit_layout`) are placed per cell by the caller (there is no
    array-level device-mark box here as of issue #432 -- a marker coincident
    with the whole shared well would make every diffusion pad inside it,
    base-tie pads included, look like a second emitter). The collector ring
    is still held :data:`BJT_COLLECTOR_GAP_UM` outside the well, well beyond
    gf180mcu's ``bjt.separation.comp.1`` 0.1um limit.
    """
    unit = _bjt_unit_layout(emitter_um)
    col_pitch = unit["total_len_um"] + MIN_SAME_LAYER_SPACING_UM
    row_pitch = unit["height_um"] + MIN_SAME_LAYER_SPACING_UM

    order = (
        _centroid_order(rows, cols)
        if topology == "common_centroid"
        else [(r, c) for r in range(rows) for c in range(cols)]
    )
    cells = [
        {"idx": idx, "row": r, "col": c, "x0_um": c * col_pitch, "y0_um": r * row_pitch}
        for idx, (r, c) in enumerate(order)
    ]

    dummy_cells = []
    for r in range(rows):
        for dc in range(1, dummy + 1):
            dummy_cells.append(
                {"row": r, "col": -dc, "x0_um": -dc * col_pitch, "y0_um": r * row_pitch}
            )
            dummy_cells.append(
                {
                    "row": r,
                    "col": cols - 1 + dc,
                    "x0_um": (cols - 1 + dc) * col_pitch,
                    "y0_um": r * row_pitch,
                }
            )

    all_cells = cells + dummy_cells
    min_x0 = min(c["x0_um"] for c in all_cells)
    max_x1 = max(c["x0_um"] + unit["total_len_um"] for c in all_cells)
    min_y0 = min(c["y0_um"] for c in all_cells)
    max_y1 = max(c["y0_um"] + unit["height_um"] for c in all_cells)

    margin = WELL_ENCLOSURE_MARGIN_UM
    well_box = (min_x0 - margin, min_y0 - margin, max_x1 + margin, max_y1 + margin)

    ring = None
    ring_offset = (0.0, 0.0)
    if add_collector_ring:
        gap = BJT_COLLECTOR_GAP_UM
        ring_w = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
        inner_x0 = well_box[0] - gap
        inner_y0 = well_box[1] - gap
        inner_w = (well_box[2] - well_box[0]) + 2 * gap
        inner_h = (well_box[3] - well_box[1]) + 2 * gap
        contacts_per_side = max(1, min(rows + cols, 4))
        ring = _ring_layout(
            inner_w,
            inner_h,
            ring_w,
            contacts_per_side,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = (inner_x0 - ring_w, inner_y0 - ring_w)

    return {
        "unit": unit,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "well_box_um": well_box,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _esd_device_layout(
    finger_width_um: float,
    l_um: float,
    fingers: int,
    add_guard_ring: bool,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    gate_contact: bool = False,
) -> dict[str, Any]:
    """Grounded-gate multi-finger ESD MOS clamp (issue #569): a single
    :func:`_mos_unit_layout` unit device with ``fingers`` gate stripes across
    one shared diffusion strip -- the standard ggNMOS ESD-clamp layout idiom
    -- optionally enclosed by an automatically-sized tap ring.

    Composes exactly the way :func:`_diff_pair_layout` composes families 1
    and 3: it calls :func:`_mos_unit_layout` (the same unit-device helper
    ``mos_array``/``diff_pair`` already use, here with its own ``fingers``
    parameter doing double duty as the ESD device's own multi-finger count)
    and :func:`_ring_layout` (the same ring helper ``guard_ring``/
    ``diff_pair``/``bjt_array`` already use) directly -- no second, private
    layout mechanism, and no sub-cell instantiation of ``_GuardRingPCell``
    itself (``diff_pair`` doesn't either; see that class's own docstring)."""
    unit = _mos_unit_layout(finger_width_um, l_um, fingers, gate_contact)

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        ring = _ring_layout(
            unit["total_len_um"] + 2 * padding,
            unit["bbox_height_um"] + 2 * padding,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = (-(ring_w + padding), -(ring_w + padding))

    return {
        "unit": unit,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _shift_box(
    box_um: tuple[float, float, float, float], ox_um: float, oy_um: float
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box_um
    return (x0 + ox_um, y0 + oy_um, x1 + ox_um, y1 + oy_um)


def _insert_boxes(
    cell: Any,
    layer_index: int,
    dbu: float,
    boxes_um: list[tuple[float, float, float, float]],
    ox_um: float = 0.0,
    oy_um: float = 0.0,
) -> None:
    import klayout.db as kdb

    shapes = cell.shapes(layer_index)
    for x0, y0, x1, y1 in boxes_um:
        shapes.insert(
            kdb.Box(
                int(round((x0 + ox_um) / dbu)),
                int(round((y0 + oy_um) / dbu)),
                int(round((x1 + ox_um) / dbu)),
                int(round((y1 + oy_um) / dbu)),
            )
        )


def _insert_ring(
    cell: Any,
    layer_index: int,
    dbu: float,
    outer_box_um: tuple[float, float, float, float],
    inner_box_um: tuple[float, float, float, float],
    gap_box_um: tuple[float, float, float, float] | None = None,
) -> None:
    """Insert an outer-box-minus-inner-box ring as one boolean ``Region`` --
    guarantees a single unbroken polygon (no same-layer internal-space
    violation between "segments"), per :func:`_ring_layout`'s docstring.

    ``gap_box_um`` (#434), when given, is subtracted as well: it cuts one
    routing opening through the ring's band on a single side, leaving a
    C-shaped -- still connected, still one polygon -- conductor.
    """
    import klayout.db as kdb

    def _to_box(box_um: tuple[float, float, float, float]) -> Any:
        x0, y0, x1, y1 = box_um
        return kdb.Box(
            int(round(x0 / dbu)),
            int(round(y0 / dbu)),
            int(round(x1 / dbu)),
            int(round(y1 / dbu)),
        )

    ring = kdb.Region(_to_box(outer_box_um)) - kdb.Region(_to_box(inner_box_um))
    if gap_box_um is not None:
        ring = ring - kdb.Region(_to_box(gap_box_um))
    cell.shapes(layer_index).insert(ring)


class GenError(Exception):
    """Raised when a generation request cannot be fulfilled.

    Covers an unknown generator name, an unresolvable PDK, and invalid/
    out-of-range ``params`` -- the CLI turns this into a clean stderr
    message + exit code 1, never a traceback (see docs/cli/gen.md's exit
    code table).
    """


def list_generators() -> dict[str, Any]:
    """Enumerate the available generators (``klt gen --list``).

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generators": [
                {
                    "name": str,
                    "summary": str,
                    "params": [
                        {
                            "name": str,
                            "type": "double" | "int" | "string" | "bool",
                            "default": <JSON value>,
                            "description": str,
                        },
                        ...
                    ],
                },
                ...
            ],
        }

    ``params`` documents every request-adjustable ``params`` field the
    generator's PCell declares (hidden, implementation-only parameters such
    as the drawing layer are omitted -- see ``_HIDDEN_PARAMS``).
    """
    pcell_classes = _build_pcell_classes()

    generators: list[dict[str, Any]] = []
    for name in sorted(_GENERATOR_SPECS):
        spec = _GENERATOR_SPECS[name]
        decl = pcell_classes[name]()
        params = [
            {
                "name": p.name,
                "type": _PARAM_TYPE_NAMES.get(p.type, "unknown"),
                "default": p.default,
                "description": p.description,
            }
            for p in decl.get_parameters()
            if p.name not in _HIDDEN_PARAMS
        ]
        generators.append(
            {"name": spec.name, "summary": spec.summary, "params": params}
        )

    return {"schema_version": SCHEMA_VERSION, "generators": generators}


def load_params_arg(value: str | None) -> dict[str, Any]:
    """Resolve a CLI ``--params`` value into a ``params`` dict.

    ``value`` is either a path to a JSON file (existing files win first) or
    an inline JSON object string, per docs/cli/gen.md. ``None`` (flag
    omitted) resolves to ``{}`` -- every generator's parameters have
    defaults, so an empty ``params`` object is a valid request.

    Raises :class:`GenError` if the value is neither a readable JSON file
    nor valid inline JSON, or decodes to something other than a JSON object.
    """
    import json

    if value is None:
        return {}

    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise GenError(f"could not read params file '{value}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GenError(f"params file '{value}' is not valid JSON: {exc}") from exc
    else:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GenError(
                "--params must be a path to a JSON file or an inline JSON "
                f"object: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise GenError("--params must decode to a JSON object")
    return data


def generate(request: dict[str, Any]) -> dict[str, Any]:
    """Run one generator request end-to-end and return the response envelope.

    ``request`` follows the ``klt.gen.request/1`` shape (spike section 2)::

        {
            "schema": "klt.gen.request/1",
            "generator": "resistor_strip",
            "pdk": {"variant": "sky130A", "root": None},
            "params": {"length_um": 2.0, "num": 4},
            "options": {"cell_name": "res_strip_0", "output": "res_strip_0.gds"},
        }

    ``pdk``/``params``/``options`` are all optional; missing ``pdk`` falls
    through to ``find_pdk()``'s own ``$PDK``/``$PDK_ROOT`` search (same
    behaviour as ``klt pdk find`` with no flags). Returns a dict matching
    the documented response schema (see ``docs/cli/gen.md``)::

        {
            "schema_version": 1,
            "generator": str,
            "cell_name": str,
            "gds_path": str,
            "pdk": {"name": str, "variant": str, "version": str | None},
            "bbox_um": {"x0": float, "y0": float, "x1": float, "y1": float},
            "device_count": int,
            "ports": [...],
            "drc_hints": {...},
            "warnings": [...],
        }

    Raises :class:`GenError` for an unknown generator, an unresolvable PDK,
    invalid/out-of-range ``params``, or a write failure (e.g. the
    ``options.output`` directory does not exist).
    """
    if not isinstance(request, dict):
        raise GenError("request must be a JSON object")

    generator_name = request.get("generator")
    if not isinstance(generator_name, str) or not generator_name:
        raise GenError("request.generator is required")

    spec = _GENERATOR_SPECS.get(generator_name)
    if spec is None:
        raise GenError(
            f"unknown generator '{generator_name}' -- available: "
            f"{', '.join(sorted(_GENERATOR_SPECS))} (see `klt gen --list`)"
        )

    pdk_request = request.get("pdk") or {}
    if not isinstance(pdk_request, dict):
        raise GenError("request.pdk must be a JSON object")
    try:
        pdk_info = find_pdk(
            variant=pdk_request.get("variant"), root=pdk_request.get("root")
        )
    except PdkNotFoundError as exc:
        raise GenError(str(exc)) from exc

    raw_params = request.get("params") or {}
    if not isinstance(raw_params, dict):
        raise GenError("request.params must be a JSON object")

    options = request.get("options") or {}
    if not isinstance(options, dict):
        raise GenError("request.options must be a JSON object")
    cell_name = options.get("cell_name") or f"{generator_name}_0"
    output_path = options.get("output") or f"{cell_name}.gds"

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir and not os.path.isdir(output_dir):
        raise GenError(f"output directory does not exist: {output_dir}")

    resolved_params = _resolve_params(spec, raw_params)
    spec.validate(resolved_params)

    layout, top_cell = _produce(spec, cell_name, resolved_params, pdk_info)

    write_layout(layout, output_path, GenError)

    dbu = layout.dbu
    bbox = top_cell.bbox()
    bbox_um = {
        "x0": bbox.left * dbu,
        "y0": bbox.bottom * dbu,
        "x1": bbox.right * dbu,
        "y1": bbox.top * dbu,
    }

    described = spec.describe(resolved_params, dbu, pdk_info)

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": generator_name,
        "cell_name": cell_name,
        "gds_path": output_path,
        "pdk": {
            "name": pdk_info["variant"],
            "variant": pdk_info["variant"],
            "version": pdk_info["version"],
        },
        "bbox_um": bbox_um,
        "device_count": described["device_count"],
        "ports": described["ports"],
        "drc_hints": described["drc_hints"],
        "warnings": described["warnings"],
    }


# --------------------------------------------------------------------------- #
# PCell harness -- adapts a request's `params` to a PCellDeclarationHelper
# subclass's declared parameter schema + produce_impl(), per spike section 1's
# "KLayout PCells (native)" entry. Generic across every registered generator;
# generator-specific knowledge (validation, port/bbox reporting) lives in each
# _GeneratorSpec, not here.
# --------------------------------------------------------------------------- #


def _resolve_params(spec: _GeneratorSpec, raw_params: dict[str, Any]) -> dict[str, Any]:
    """Merge ``raw_params`` onto the generator's declared PCell defaults.

    Every declared, non-hidden parameter ends up in the result: the request's
    value when given (type-checked against the PCell's declared type), the
    PCell's own default otherwise. Raises :class:`GenError` for an unknown
    parameter name or a value that doesn't match the declared type.
    """
    pcell_classes = _build_pcell_classes()
    decl = pcell_classes[spec.name]()
    declared = {
        p.name: p for p in decl.get_parameters() if p.name not in _HIDDEN_PARAMS
    }

    unknown = sorted(set(raw_params) - set(declared))
    if unknown:
        raise GenError(f"generator '{spec.name}': unknown params: {', '.join(unknown)}")

    resolved: dict[str, Any] = {}
    for name, pdecl in declared.items():
        if name in raw_params:
            resolved[name] = _coerce_param(
                spec.name, name, raw_params[name], pdecl.type
            )
        else:
            resolved[name] = pdecl.default
    return resolved


def _coerce_param(generator: str, name: str, value: Any, ptype: int) -> Any:
    import klayout.db as kdb

    Decl = kdb.PCellParameterDeclaration
    if ptype == Decl.TypeDouble:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GenError(f"generator '{generator}': params.{name} must be a number")
        return float(value)
    if ptype == Decl.TypeInt:
        if isinstance(value, bool) or not isinstance(value, int):
            raise GenError(f"generator '{generator}': params.{name} must be an integer")
        return int(value)
    if ptype == Decl.TypeString:
        if not isinstance(value, str):
            raise GenError(f"generator '{generator}': params.{name} must be a string")
        return value
    if ptype == Decl.TypeBoolean:
        if not isinstance(value, bool):
            raise GenError(f"generator '{generator}': params.{name} must be a boolean")
        return value
    raise GenError(
        f"generator '{generator}': params.{name} has an unsupported PCell "
        "parameter type"
    )


def _produce(
    spec: _GeneratorSpec,
    cell_name: str,
    resolved_params: dict[str, Any],
    pdk_info: dict[str, Any],
) -> tuple[kdb.Layout, kdb.Cell]:
    """Instantiate ``spec``'s PCell into a fresh layout as cell ``cell_name``.

    This is the harness's one KLayout-specific step: register (once per
    process) a ``pya.Library`` wrapping every reference generator's PCell
    declaration, then use ``Layout.add_pcell_variant`` -- the same mechanism
    KLayout's own GUI PCell panel uses -- to build geometry headlessly.

    ``pdk_info`` (the resolved request's ``pdk`` payload, from
    :func:`~klayout_tools.pdk.find_pdk`) is threaded through to
    ``spec.layer_params`` so a PDK-aware generator (every phase-2 generator
    except ``resistor_strip``) can resolve its hidden layer params against
    the *resolved* PDK family rather than a fixed default -- see
    :func:`_device_layer_params` and friends.
    """
    import klayout.db as kdb

    lib = _pcell_library()
    decl = lib.layout().pcell_declaration(spec.name)

    pcell_values = dict(resolved_params)
    pcell_values.update(spec.layer_params(pdk_info, resolved_params))

    layout = kdb.Layout()
    layout.dbu = spec.dbu
    pcell_var = layout.add_pcell_variant(lib, decl.id(), pcell_values)
    top = layout.create_cell(cell_name)
    top.insert(kdb.CellInstArray(pcell_var, kdb.Trans()))
    return layout, top


def _pcell_library() -> kdb.Library:
    """Return the reference PCell library, registering it on first use.

    ``pya.Library.register()`` is process-global -- registering twice under
    the same name would either error or shadow the first registration
    depending on KLayout version, so this guards with ``library_by_name()``
    and registers exactly once per process, mirroring how a real PDK's
    KLayout tech file registers its own PCell libraries on load.
    """
    import klayout.db as kdb

    existing = kdb.Library.library_by_name(_PCELL_LIBRARY_NAME)
    if existing is not None:
        return existing

    pcell_classes = _build_pcell_classes()

    class _ReferenceLibrary(kdb.Library):
        def __init__(self) -> None:
            super().__init__()
            self.description = "klt gen reference PCell library (phase 1 skeleton)"
            for name, pcell_cls in pcell_classes.items():
                self.layout().register_pcell(name, pcell_cls())
            self.register(_PCELL_LIBRARY_NAME)

    return _ReferenceLibrary()


def _build_pcell_classes() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Define the reference generators' ``PCellDeclarationHelper`` subclasses.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason.
    """
    import klayout.db as kdb

    class _ResistorStripPCell(kdb.PCellDeclarationHelper):
        """Row of parametrized rectangles standing in for a unit-resistor
        string (spike section 4.2's family). Phase-1 skeleton only: no
        well/tap/contact logic, and not claimed to be DRC-clean on any PDK
        -- it exists to prove the request -> PCell -> response loop end to
        end. Phase 2 replaces this with a real resistor-array generator.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "length_um", self.TypeDouble, "Unit resistor length (um)", default=2.0
            )
            self.param(
                "width_um", self.TypeDouble, "Unit resistor width (um)", default=0.42
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit resistors (um)",
                default=0.42,
            )
            self.param("num", self.TypeInt, "Number of unit resistors", default=4)
            self.param(
                "layer",
                self.TypeLayer,
                "Drawing layer for the unit resistors",
                default=kdb.LayerInfo(67, 20),
            )

        def display_text_impl(self) -> str:
            return f"resistor_strip(l={self.length_um},w={self.width_um},n={self.num})"

        def produce_impl(self) -> None:
            li = self.layout.layer(self.layer)
            dbu = self.layout.dbu
            length = max(1, int(round(self.length_um / dbu)))
            width = max(1, int(round(self.width_um / dbu)))
            spacing = max(0, int(round(self.spacing_um / dbu)))
            x = 0
            for _ in range(self.num):
                self.cell.shapes(li).insert(kdb.Box(x, 0, x + length, width))
                x += length + spacing

    class _MosArrayPCell(kdb.PCellDeclarationHelper):
        """Matched MOS transistor array (spike section 4's family 1): a
        ``rows`` x ``cols`` grid of identical unit devices (see
        :func:`_mos_unit_layout`), with ``dummy`` extra unit-device columns
        flanking each side and a ``topology``-selected port-numbering order
        (see :func:`_centroid_order`). ``flavor="pfet"`` additionally
        encloses every unit device (real and dummy) in a shared well on PDK
        families whose curated deck checks one (see ``well_present``);
        ``flavor="nfet"`` (the default) draws no well shape at all (#208).

        ``finger_topology`` (#777) selects what a ``fingers > 1`` unit
        device *is*: ``"parallel"`` (the default) straps the alternating
        S/D segments and ties every gate, so the unit is one folded device
        of width ``fingers * w_um``; ``"series"`` draws the bare, unstrapped
        stripes -- a chain of transistors whose interior terminals are
        neither reported nor contactable (warned about by
        :func:`_mos_array_describe`)."""

        def __init__(self) -> None:
            super().__init__()
            self.param("w_um", self.TypeDouble, "Unit device width (um)", default=0.42)
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param(
                "fingers", self.TypeInt, "Gate fingers per unit device", default=1
            )
            self.param(
                "finger_topology",
                self.TypeString,
                "How a multi-finger unit device is wired: 'parallel' "
                "(default -- alternating S/D segments strapped and every "
                "gate tied, i.e. one folded device of width fingers*w_um) "
                "or 'series' (bare stripes, no straps: fingers transistors "
                "chained source-to-drain on independent, uncontactable "
                "gates). No effect when fingers is 1",
                default="parallel",
            )
            self.param("rows", self.TypeInt, "Array rows", default=2)
            self.param("cols", self.TypeInt, "Array columns", default=2)
            self.param(
                "topology",
                self.TypeString,
                "Port-numbering topology: 'array' (row-major) or "
                "'common_centroid' (centroid-symmetric pairing)",
                default="common_centroid",
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit-device columns added on each side of the array",
                default=1,
            )
            self.param(
                "flavor",
                self.TypeString,
                "Device flavor: 'nfet' (default, no well drawn) or 'pfet' "
                "(unit devices enclosed in a well on PDK families that check one)",
                default="nfet",
            )
            self.param(
                "gate_contact",
                self.TypeBoolean,
                "Finish the gate stack: draw a contact and a local-metal pad "
                "on each unit device's gate landing pad and report U<i>_G on "
                "the metal role (symmetric with U<i>_S/U<i>_D) instead of "
                "bare poly. Raises the landing pad clear of the S/D metal, "
                "so the unit device grows taller",
                default=False,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when flavor is 'pfet' and well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit device's gate footprint (only used when dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"mos_array({self.rows}x{self.cols},w={self.w_um},l={self.l_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _mos_array_layout(
                self.w_um,
                self.l_um,
                self.fingers,
                self.rows,
                self.cols,
                self.dummy,
                self.topology,
                self.gate_contact,
                self.finger_topology,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"] + info["dummy_cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            if self.flavor == "pfet" and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [info["well_box_um"]])

            # Dummy-device marker (issue #491): drawn only over
            # dummy_cells' gate footprint (unit_boxes["poly"], the exact
            # geometry `klt extract`'s nfet_gate/pfet_gate regions are built
            # from) -- never over a real cell -- so the deck's existing
            # dummy-suppression guards (#295/#462) drop these gates from the
            # extracted netlist instead of reporting them as unmatched
            # devices under `klt lvs`.
            if self.dummy_present and info["dummy_cells"]:
                li_dummy = self.layout.layer(self.dummy_layer)
                for c in info["dummy_cells"]:
                    _insert_boxes(
                        self.cell,
                        li_dummy,
                        dbu,
                        unit_boxes["poly"],
                        c["x0_um"],
                        c["y0_um"],
                    )

    class _ResArrayPCell(kdb.PCellDeclarationHelper):
        """Unit resistor/capacitor array (spike section 4's family 2): a row
        of ``num`` matched unit elements (see :func:`_res_unit_layout`) with
        ``dummy`` dummy elements at each end, per the
        ``kb/entries/sky130-bandgap-reference.json`` resistor-array idiom."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "length_um",
                self.TypeDouble,
                "Unit resistor body length (um)",
                default=2.0,
            )
            self.param(
                "width_um", self.TypeDouble, "Unit resistor width (um)", default=0.42
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit resistors (um)",
                default=0.5,
            )
            self.param(
                "num", self.TypeInt, "Number of matched unit resistors", default=4
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit resistors added at each end of the row",
                default=1,
            )
            self.param(
                "rows",
                self.TypeInt,
                "Fold the num unit resistors into this many parallel rows "
                "(boustrophedon order) instead of one long row -- keeps a "
                "long resistor string's bounding box roughly square. Must "
                "be >= 1; default 1 reproduces the original single-row "
                "layout.",
                default=1,
            )
            self.param(
                "flavor",
                self.TypeString,
                "Poly-resistor flavour selecting the recognised device class "
                "by which implant/precision-resistor masks cover each body: "
                "'generic' (default, the base sheet-rho flavour -- "
                "res_generic_po on sky130, ppolyf_u on gf180mcu), or, on "
                "sky130, 'high' (res_high_po) / 'xhigh' (res_xhigh_po) for the "
                "higher-sheet-rho flavours (issue #463)",
                default=_DEFAULT_RES_FLAVOR,
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Resistor body drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "res_mark_layer",
                self.TypeLayer,
                "Resistor-ID (device-mark) drawing layer covering each unit's "
                "body segment (only used when res_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "res_mark_present",
                self.TypeBoolean,
                "Whether res_mark_layer is a real, DRC-checked layer for the "
                "resolved PDK",
                default=False,
            )
            self.param(
                "res_implant_layer",
                self.TypeLayer,
                "Resistor implant drawing layer (e.g. gf180mcu's Pplus) "
                "required over the body segment for device recognition "
                "(only used when res_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "res_implant_present",
                self.TypeBoolean,
                "Whether res_implant_layer is a real, DRC-checked layer for "
                "the resolved PDK",
                default=False,
            )
            self.param(
                "res_block_layer",
                self.TypeLayer,
                "Resistor salicide-block drawing layer (e.g. gf180mcu's SAB) "
                "required over the body segment for device recognition "
                "(only used when res_block_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "res_block_present",
                self.TypeBoolean,
                "Whether res_block_layer is a real, DRC-checked layer for "
                "the resolved PDK",
                default=False,
            )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit resistor's recognised body segment (only used when "
                "dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )

        def display_text_impl(self) -> str:
            return (
                f"res_array(l={self.length_um},w={self.width_um},"
                f"n={self.num},rows={self.rows},flavor={self.flavor})"
            )

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _res_array_layout(
                self.length_um,
                self.width_um,
                self.spacing_um,
                self.num,
                self.dummy,
                self.rows,
            )
            unit_boxes = info["unit"]["boxes_um"]
            all_cells = info["cells"] + info["dummy_cells"]
            for c in all_cells:
                for role, li in (
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            # Draw the PDK resistor-ID marker (and, on gf180mcu, the implant/
            # salicide-block requires-layers) over every unit's body segment
            # -- dummies included, since they are structurally real unit
            # resistors too (mirrors `mos_array`'s dummy gates, which are
            # drawn identically to real ones) -- see :func:`_res_unit_layout`
            # for why the marker box excludes the contacted end pads.
            marker_boxes = unit_boxes["marker"]
            for present, layer_param in (
                (self.res_mark_present, self.res_mark_layer),
                (self.res_implant_present, self.res_implant_layer),
                (self.res_block_present, self.res_block_layer),
            ):
                if not present:
                    continue
                li_mark = self.layout.layer(layer_param)
                for c in all_cells:
                    _insert_boxes(
                        self.cell, li_mark, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

            # Dummy-device marker (issue #491): drawn only over
            # dummy_cells' recognised body footprint (the same marker_boxes
            # span the resistor-ID marker above uses) -- never over a real
            # cell -- so `klt extract`'s existing dummy-suppression guards
            # (#295/#462) drop these dummy resistor bodies from the
            # extracted netlist instead of reporting them as unmatched
            # devices under `klt lvs`.
            if self.dummy_present and info["dummy_cells"]:
                li_dummy = self.layout.layer(self.dummy_layer)
                for c in info["dummy_cells"]:
                    _insert_boxes(
                        self.cell, li_dummy, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

    class _GuardRingPCell(kdb.PCellDeclarationHelper):
        """Substrate/well tap guard ring (spike section 4's family 3): a
        tap ring + local-metal ring with evenly-spaced contacts (see
        :func:`_ring_layout`), optionally enclosed by a well tie on PDK
        families whose curated deck checks one (see ``well_present``)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "inner_width_um",
                self.TypeDouble,
                "Width of the protected inner area (um)",
                default=3.0,
            )
            self.param(
                "inner_height_um",
                self.TypeDouble,
                "Height of the protected inner area (um)",
                default=3.0,
            )
            self.param(
                "ring_width_um",
                self.TypeDouble,
                "Tap ring thickness (um)",
                default=0.42,
            )
            self.param(
                "contacts_per_side",
                self.TypeInt,
                "Tap contacts evenly spaced along each ring side -- applied "
                "uniformly to all four sides unless overridden per-axis by "
                "contacts_per_side_ns/contacts_per_side_ew",
                default=4,
            )
            self.param(
                "contacts_per_side_ns",
                self.TypeInt,
                "Tap contacts on the N/S (top/bottom) sides, spaced along "
                "inner_width_um -- 0 (default) inherits contacts_per_side, "
                "so existing single-scalar callers are unaffected",
                default=0,
            )
            self.param(
                "contacts_per_side_ew",
                self.TypeInt,
                "Tap contacts on the E/W (left/right) sides, spaced along "
                "inner_height_um -- 0 (default) inherits contacts_per_side",
                default=0,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the ring on this side: "
                "'' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "add_well",
                self.TypeBoolean,
                "Enclose the ring in a well tie when the resolved PDK checks one",
                default=True,
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Substrate/well tap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"guard_ring({self.inner_width_um}x{self.inner_height_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_tap = self.layout.layer(self.tap_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            contacts_ns = self.contacts_per_side_ns or self.contacts_per_side
            contacts_ew = self.contacts_per_side_ew or self.contacts_per_side
            info = _ring_layout(
                self.inner_width_um,
                self.inner_height_um,
                self.ring_width_um,
                (contacts_ns, contacts_ew),
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
            )
            gap_box = info["gap"]["box_um"] if info["gap"] is not None else None
            _insert_ring(
                self.cell,
                li_tap,
                dbu,
                info["outer_box_um"],
                info["inner_box_um"],
                gap_box,
            )
            _insert_ring(
                self.cell,
                li_metal,
                dbu,
                info["outer_box_um"],
                info["inner_box_um"],
                gap_box,
            )
            _insert_boxes(self.cell, li_contact, dbu, info["contact_boxes_um"])
            if self.add_well and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                margin = WELL_ENCLOSURE_MARGIN_UM
                well_box = (
                    -margin,
                    -margin,
                    info["outer_w_um"] + margin,
                    info["outer_h_um"] + margin,
                )
                _insert_boxes(self.cell, li_well, dbu, [well_box])

    class _DiffPairPCell(kdb.PCellDeclarationHelper):
        """Differential pair / current mirror cell (spike section 4's
        family 4): two matched devices, each split into ``splits``
        sub-instances, interleaved in a true common-centroid cross-quad
        pattern (see :func:`_diff_pair_layout`) -- composes ``mos_array``'s
        unit-device drawing (family 1) and ``guard_ring``'s ring drawing
        (family 3). ``flavor="pfet"`` encloses the device pair's own active
        footprint in a well (independent of ``add_guard_ring``'s own,
        separate well tie -- see ``guard_ring``); ``flavor="nfet"`` (the
        default) draws no additional well shape (#208)."""

        def __init__(self) -> None:
            super().__init__()
            self.param("w_um", self.TypeDouble, "Unit device width (um)", default=0.42)
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param(
                "splits",
                self.TypeInt,
                "Interleaved sub-instances per device (cross-quad splits)",
                default=2,
            )
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the pair in an automatically-sized guard ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the guard ring on this side: "
                "'' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the guard-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the guard-ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "ring_padding_um",
                self.TypeDouble,
                "Padding between the device core and the guard ring's inner "
                "edge (um), when add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "row_spacing_um",
                self.TypeDouble,
                "Spacing between the two interleaved device rows (um)",
                default=MIN_SAME_LAYER_SPACING_UM,
            )
            self.param(
                "mirror",
                self.TypeBoolean,
                "Label devices M1/M2 (current mirror) instead of Q1/Q2 "
                "(differential pair)",
                default=False,
            )
            self.param(
                "flavor",
                self.TypeString,
                "Device flavor: 'nfet' (default, no well drawn) or 'pfet' "
                "(unit devices enclosed in a well on PDK families that check one)",
                default="nfet",
            )
            self.param(
                "gate_contact",
                self.TypeBoolean,
                "Finish the gate stack: draw a contact and a local-metal pad "
                "on each unit device's gate landing pad and report *_G on the "
                "metal role (symmetric with *_S/*_D) instead of bare poly. "
                "Raises the landing pad clear of the S/D metal, so the unit "
                "device (and any automatically-sized guard ring) grows taller",
                default=False,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Guard ring tap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Guard ring well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"diff_pair(w={self.w_um},l={self.l_um},splits={self.splits})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _diff_pair_layout(
                self.w_um,
                self.l_um,
                self.splits,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.row_spacing_um,
                self.gate_contact,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for c in info["cells"]:
                for role, li in (
                    ("active", li_active),
                    ("poly", li_poly),
                    ("contact", li_contact),
                    ("metal", li_metal),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            if self.flavor == "pfet" and self.well_present:
                # Enclose the device pair's own active footprint in a well,
                # independent of the (optional) automatically-sized guard
                # ring's own well tie drawn below -- both land on the same
                # well_layer, so they simply merge into one region on any
                # boolean/DRC pass over it.
                li_well = self.layout.layer(self.well_layer)
                margin = WELL_ENCLOSURE_MARGIN_UM
                device_well_box = (
                    -margin,
                    -margin,
                    info["core_w_um"] + margin,
                    info["core_h_um"] + margin,
                )
                _insert_boxes(self.cell, li_well, dbu, [device_well_box])

            if info["ring"] is not None and self.add_guard_ring:
                li_tap = self.layout.layer(self.tap_layer)
                ox, oy = info["ring_offset_um"]
                ring = info["ring"]
                gap_box = (
                    _shift_box(ring["gap"]["box_um"], ox, oy)
                    if ring["gap"] is not None
                    else None
                )
                _insert_ring(
                    self.cell,
                    li_tap,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_ring(
                    self.cell,
                    li_metal,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_boxes(
                    self.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy
                )
                if self.well_present and self.flavor == "pfet":
                    li_well = self.layout.layer(self.well_layer)
                    margin = WELL_ENCLOSURE_MARGIN_UM
                    well_box = (
                        -margin,
                        -margin,
                        ring["outer_w_um"] + margin,
                        ring["outer_h_um"] + margin,
                    )
                    _insert_boxes(
                        self.cell, li_well, dbu, [_shift_box(well_box, ox, oy)]
                    )

    class _BjtArrayPCell(kdb.PCellDeclarationHelper):
        """Matched vertical-bipolar (PNP/BJT) array (Epic #152 phase 4): a
        ``rows`` x ``cols`` common-centroid grid of identical unit devices
        (see :func:`_bjt_unit_layout`), sharing one base well, surrounded by
        a collector guard ring, with each unit individually covered by a
        device-mark layer (and a base-tie ``tap`` shape) on every PDK family
        whose extraction deck declares a bipolar marker -- gf180mcu's
        ``DRC_BJT`` (also DRC-checked) and sky130's ``pnp.drawing``
        (extraction-only, issue #432).

        Draws from base layers rather than instantiating a vendor library
        cell (e.g. gf180mcu ``pnp_05p00x05p00``) -- the design note
        ``docs/design/gen-bjt-array-spike.md`` records why: this repo's
        ``find_pdk`` has no library-GDS-instantiation path, CI never has a
        real PDK installed, and CLAUDE.md forbids vendoring PDK cell data.
        The trade-off (a DRC-clean matching floorplan, not a SPICE-model-
        exact device) is recorded there too.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "emitter_um",
                self.TypeDouble,
                "Emitter diffusion side length (um)",
                default=0.6,
            )
            self.param("rows", self.TypeInt, "Array rows", default=3)
            self.param("cols", self.TypeInt, "Array columns", default=3)
            self.param(
                "topology",
                self.TypeString,
                "Placement topology: 'array' (row-major) or "
                "'common_centroid' (centroid-symmetric pairing)",
                default="common_centroid",
            )
            self.param(
                "dummy",
                self.TypeInt,
                "Dummy unit-device columns added on each side of the array",
                default=1,
            )
            self.param(
                "ratio",
                self.TypeInt,
                "Intended emitter matching ratio (e.g. 8 for a bandgap's "
                "8:1 group) -- documented in the matched-group id and hints",
                default=8,
            )
            self.param(
                "add_collector_ring",
                self.TypeBoolean,
                "Surround the array with a collector/substrate guard ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the collector ring on this "
                "side: '' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the collector-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the collector-ring opening from its side's midpoint "
                "(um): +x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Emitter/base/collector diffusion (COMP) drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Base-tie tap drawing layer, covering each unit's base-tie "
                "contact so the base terminal resolves to a real net",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_layer",
                self.TypeLayer,
                "Shared base well drawing layer (only used when well_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_present",
                self.TypeBoolean,
                "Whether well_layer is a real, DRC-checked layer for the resolved PDK",
                default=False,
            )
            self.param(
                "bjt_mark_layer",
                self.TypeLayer,
                "Per-unit bipolar device-mark drawing layer, enclosing only "
                "the emitter pad (only used when bjt_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "bjt_mark_present",
                self.TypeBoolean,
                "Whether bjt_mark_layer is a real layer this PDK's extraction "
                "deck (and, on some families, its DRC deck too) keys off",
                default=False,
            )
            self.param(
                "dummy_layer",
                self.TypeLayer,
                "PDK dummy-device marker drawing layer, covering each dummy "
                "unit device's recognised base-mark footprint (only used "
                "when dummy_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "dummy_present",
                self.TypeBoolean,
                "Whether dummy_layer is a real layer this PDK's extraction "
                "deck declares (see ExtractionDeck.dummy)",
                default=False,
            )

        def display_text_impl(self) -> str:
            return f"bjt_array({self.rows}x{self.cols},e={self.emitter_um})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            li_tap = self.layout.layer(self.tap_layer)
            info = _bjt_array_layout(
                self.emitter_um,
                self.rows,
                self.cols,
                self.dummy,
                self.topology,
                self.add_collector_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            all_cells = info["cells"] + info["dummy_cells"]
            for c in all_cells:
                for role, li in (
                    ("active", li_active),
                    ("contact", li_contact),
                    ("metal", li_metal),
                    ("tap", li_tap),
                ):
                    _insert_boxes(
                        self.cell, li, dbu, unit_boxes[role], c["x0_um"], c["y0_um"]
                    )

            if self.well_present:
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [info["well_box_um"]])

            if info["ring"] is not None and self.add_collector_ring:
                ring = info["ring"]
                ox, oy = info["ring_offset_um"]
                gap_box = (
                    _shift_box(ring["gap"]["box_um"], ox, oy)
                    if ring["gap"] is not None
                    else None
                )
                _insert_ring(
                    self.cell,
                    li_active,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_ring(
                    self.cell,
                    li_metal,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_boxes(
                    self.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy
                )

            # Per-unit bipolar device-mark, drawn on every unit (dummies
            # included -- they are structurally real unit devices too,
            # mirroring `res_array`'s marker precedent). Deliberately *not*
            # an array-wide box (issue #432): see `_bjt_unit_layout`'s
            # `boxes_um["marker"]` docstring for why that would misrecognise
            # every base-tie pad as a second emitter.
            if self.bjt_mark_present:
                li_mark = self.layout.layer(self.bjt_mark_layer)
                marker_boxes = unit_boxes["marker"]
                for c in all_cells:
                    _insert_boxes(
                        self.cell, li_mark, dbu, marker_boxes, c["x0_um"], c["y0_um"]
                    )

                # Dummy-device marker (issue #491): drawn only over
                # dummy_cells' bipolar device-mark footprint (the same
                # marker_boxes span the bjt_mark drawing above uses) --
                # never over a real cell -- so `klt extract`'s existing
                # dummy-suppression guards (#295/#462) drop these dummy
                # bipolar units from the extracted netlist instead of
                # reporting them as unmatched devices under `klt lvs`.
                if self.dummy_present and info["dummy_cells"]:
                    li_dummy = self.layout.layer(self.dummy_layer)
                    for c in info["dummy_cells"]:
                        _insert_boxes(
                            self.cell,
                            li_dummy,
                            dbu,
                            marker_boxes,
                            c["x0_um"],
                            c["y0_um"],
                        )

    class _BondPadPCell(kdb.PCellDeclarationHelper):
        """Chip-boundary bond pad (issue #568): a passivation opening (``pad``
        role) enclosed by the resolved PDK family's own topmost routing metal
        (``top_metal`` role) overlapping it by ``enclosure_um`` on every side
        -- gf180mcu's only hard, DRC-coded bond-pad rule (DRM 9.1 "PAD.4",
        ``decks/gf180mcu.py``'s ``pad.enclosing.metal5.1``). Unlike every
        other generator in this family, a bond pad's strap is *not* the
        shared ``metal`` role (li1/Metal1) `mos_array`/`res_array`/
        `guard_ring`/`diff_pair`/`bjt_array` all draw on -- see
        :data:`_PDK_ROLE_LAYERS`'s ``"top_metal"`` entries.

        ``down_to`` currently supports only its default, ``"top_metal"`` --
        this generator does not yet draw a via stack connecting the pad down
        to a lower metal level (a real via4/via3/.../via1 chain neither
        curated deck models this far up the stack; see the ``"top_metal"``
        role's own docstring). ``via_style`` is validated but has no
        geometric effect until ``down_to`` supports a lower level -- both are
        reported back via ``drc_hints.notes`` (see
        :func:`_bond_pad_describe`), never silently ignored.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "opening_um",
                self.TypeDouble,
                "Pad opening (passivation window) side length (um)",
                default=PAD_OPENING_GUIDELINE_MIN_UM["wedge"],
            )
            self.param(
                "bond_type",
                self.TypeString,
                "Bond process: 'wedge' (default, wire bond without CUP), "
                "'ball_cup' (wire bond with CUP) or 'bump' (gold bump) -- "
                "selects the PAD.1 guideline minimum opening size",
                default="wedge",
            )
            self.param(
                "enclosure_um",
                self.TypeDouble,
                "Top-metal overlap of the pad opening on every side (um)",
                default=PAD_TOP_METAL_ENCLOSURE_MIN_UM,
            )
            self.param(
                "down_to",
                self.TypeString,
                "Lowest metal level the pad straps to -- only 'top_metal' "
                "(default) is supported today",
                default="top_metal",
            )
            self.param(
                "via_style",
                self.TypeString,
                "Via arrangement once down_to supports a level below "
                "top_metal: 'ring' (default, peripheral) or 'array' -- "
                "currently has no geometric effect (see drc_hints.notes)",
                default="ring",
            )
            self.param(
                "pad_layer",
                self.TypeLayer,
                "Pad opening (passivation) drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "top_metal_layer",
                self.TypeLayer,
                "Top-metal strap drawing layer",
                default=kdb.LayerInfo(0, 0),
            )

        def display_text_impl(self) -> str:
            return f"bond_pad({self.opening_um}um,{self.bond_type})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_pad = self.layout.layer(self.pad_layer)
            li_top_metal = self.layout.layer(self.top_metal_layer)
            half_open = self.opening_um / 2.0
            half_strap = half_open + self.enclosure_um
            _insert_boxes(
                self.cell,
                li_pad,
                dbu,
                [(-half_open, -half_open, half_open, half_open)],
            )
            _insert_boxes(
                self.cell,
                li_top_metal,
                dbu,
                [(-half_strap, -half_strap, half_strap, half_strap)],
            )

    class _EsdDevicePCell(kdb.PCellDeclarationHelper):
        """Grounded-gate multi-finger ESD protection MOS (issue #569): one
        multi-finger unit device (see :func:`_mos_unit_layout` -- ``fingers``
        gate stripes across a shared diffusion, the standard ggNMOS ESD-clamp
        layout idiom), optionally enclosed in an automatically-sized tap ring
        composing ``guard_ring``'s own ring-drawing helper (family 3), the
        same composition mechanism ``diff_pair`` already uses for families 1
        and 3 (see :func:`_esd_device_layout`).

        Always an NMOS-style device (no ``flavor``/well option, unlike
        ``mos_array``/``diff_pair``, and -- unlike every other ring-composing
        generator -- the tap ring itself draws **no** well tie either): a
        grounded-gate ESD clamp is conventionally NMOS, and enclosing it in
        an Nwell the way ``guard_ring``'s own ``add_well`` default would
        silently misclassifies the device as ``pfet`` under ``klt
        extract``'s ``active & nwell`` test (see
        :func:`_esd_device_layer_params`'s docstring for the full
        explanation -- the same well ``diff_pair`` suppresses for its own
        default ``flavor="nfet"`` case).

        Draws two optional marker layers, neither DRC-checked by either
        curated deck (see :data:`_PDK_ROLE_LAYERS`'s ``esd_mark``/
        ``salicide_block`` role comments for citations and the sky130 gap):
        ``esd_mark`` (always drawn when the resolved family has one -- an
        unconditional device-class marker, mirroring ``bjt_mark``'s own
        always-on-when-present precedent) and ``salicide_block`` (drawn only
        when ``params.salicide_block`` opts in -- a ballast-style unsalicided
        region over the whole finger footprint, the same "floorplan
        fidelity, not a process-exact cross-section" approximation
        `_bjt_unit_layout` already documents for its own device).
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "finger_width_um",
                self.TypeDouble,
                "Width of each gate finger (um)",
                default=2.0,
            )
            self.param(
                "l_um",
                self.TypeDouble,
                "Gate length (um)",
                default=GATE_LENGTH_SAFE_MIN_UM,
            )
            self.param("fingers", self.TypeInt, "Gate fingers", default=4)
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the device in an automatically-sized tap ring",
                default=True,
            )
            self.param(
                "ring_gap_side",
                self.TypeString,
                "Cut one routing opening through the tap ring on this side: "
                "'' (default, closed ring), 'N', 'S', 'E' or 'W'",
                default="",
            )
            self.param(
                "ring_gap_um",
                self.TypeDouble,
                "Length of the tap-ring opening along its side (um), when "
                "ring_gap_side is set",
                default=0.0,
            )
            self.param(
                "ring_gap_offset_um",
                self.TypeDouble,
                "Shift of the tap-ring opening from its side's midpoint (um): "
                "+x on 'N'/'S', +y on 'E'/'W'",
                default=0.0,
            )
            self.param(
                "ring_padding_um",
                self.TypeDouble,
                "Padding between the finger array and the tap ring's inner "
                "edge (um), when add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "gate_contact",
                self.TypeBoolean,
                "Finish the gate stack: draw a contact and a local-metal pad "
                "on the gate landing pad and report M1_G on the metal role "
                "instead of bare poly -- see mos_array's equivalent note",
                default=False,
            )
            self.param(
                "salicide_block",
                self.TypeBoolean,
                "Draw the PDK's salicide-block layer over the finger array "
                "on families that curate one (see the esd_mark/salicide_block "
                "role comments in _PDK_ROLE_LAYERS)",
                default=False,
            )
            self.param(
                "active_layer",
                self.TypeLayer,
                "Active/diffusion drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "poly_layer",
                self.TypeLayer,
                "Poly gate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "contact_layer",
                self.TypeLayer,
                "Contact drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_layer",
                self.TypeLayer,
                "Local routing metal drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Tap ring drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "esd_mark_layer",
                self.TypeLayer,
                "ESD device-class marker drawing layer (only used when "
                "esd_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "esd_mark_present",
                self.TypeBoolean,
                "Whether esd_mark_layer is a real layer this PDK family cites",
                default=False,
            )
            self.param(
                "salicide_block_layer",
                self.TypeLayer,
                "Salicide-block drawing layer (only used when "
                "salicide_block_present and params.salicide_block)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "salicide_block_present",
                self.TypeBoolean,
                "Whether salicide_block_layer is a real layer this PDK family cites",
                default=False,
            )

        def display_text_impl(self) -> str:
            return (
                f"esd_device(fingers={self.fingers},w={self.finger_width_um},"
                f"l={self.l_um})"
            )

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_active = self.layout.layer(self.active_layer)
            li_poly = self.layout.layer(self.poly_layer)
            li_contact = self.layout.layer(self.contact_layer)
            li_metal = self.layout.layer(self.metal_layer)
            info = _esd_device_layout(
                self.finger_width_um,
                self.l_um,
                self.fingers,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.gate_contact,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for role, li in (
                ("active", li_active),
                ("poly", li_poly),
                ("contact", li_contact),
                ("metal", li_metal),
            ):
                _insert_boxes(self.cell, li, dbu, unit_boxes[role])

            # ESD device-class marker (issue #569): unconditional whenever the
            # resolved family cites one, mirroring `bjt_mark`'s own
            # always-on-when-present precedent (`res_mark`/`bjt_mark` are
            # never behind their own boolean opt-in -- only `salicide_block`
            # below is, matching its own "optional process option" nature).
            if self.esd_mark_present:
                li_mark = self.layout.layer(self.esd_mark_layer)
                _insert_boxes(self.cell, li_mark, dbu, unit_boxes["active"])

            if self.salicide_block and self.salicide_block_present:
                li_block = self.layout.layer(self.salicide_block_layer)
                _insert_boxes(self.cell, li_block, dbu, unit_boxes["active"])

            if info["ring"] is not None and self.add_guard_ring:
                li_tap = self.layout.layer(self.tap_layer)
                ox, oy = info["ring_offset_um"]
                ring = info["ring"]
                gap_box = (
                    _shift_box(ring["gap"]["box_um"], ox, oy)
                    if ring["gap"] is not None
                    else None
                )
                _insert_ring(
                    self.cell,
                    li_tap,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_ring(
                    self.cell,
                    li_metal,
                    dbu,
                    _shift_box(ring["outer_box_um"], ox, oy),
                    _shift_box(ring["inner_box_um"], ox, oy),
                    gap_box,
                )
                _insert_boxes(
                    self.cell, li_contact, dbu, ring["contact_boxes_um"], ox, oy
                )
                # Deliberately no well tie drawn around the ring here (unlike
                # `_GuardRingPCell`'s own `add_well` default, and unlike
                # `diff_pair`'s `flavor="pfet"` case) -- see this class's own
                # docstring and `_esd_device_layer_params`'s for why: it
                # would enclose this always-NMOS device in an Nwell and
                # misclassify it as `pfet` under `klt extract`.

    return {
        "resistor_strip": _ResistorStripPCell,
        "mos_array": _MosArrayPCell,
        "res_array": _ResArrayPCell,
        "guard_ring": _GuardRingPCell,
        "diff_pair": _DiffPairPCell,
        "bjt_array": _BjtArrayPCell,
        "bond_pad": _BondPadPCell,
        "esd_device": _EsdDevicePCell,
    }


# --------------------------------------------------------------------------- #
# Reference generator registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _GeneratorSpec:
    """Generator-specific knowledge the generic harness (:func:`_produce`)
    doesn't have: sanity-check bounds, how to describe the produced
    geometry as the response envelope's ``device_count``/``ports``/
    ``drc_hints`` fields, and how to resolve its hidden layer params against
    a resolved PDK (see :func:`_device_layer_params` and friends).
    """

    name: str
    summary: str
    dbu: float
    validate: Callable[[dict[str, Any]], None]
    describe: Callable[[dict[str, Any], float, dict[str, Any]], dict[str, Any]]
    layer_params: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _resistor_strip_validate(params: dict[str, Any]) -> None:
    if params["num"] < 1:
        raise GenError("generator 'resistor_strip': params.num must be >= 1")
    if params["length_um"] <= 0:
        raise GenError("generator 'resistor_strip': params.length_um must be > 0")
    if params["width_um"] <= 0:
        raise GenError("generator 'resistor_strip': params.width_um must be > 0")
    if params["spacing_um"] < 0:
        raise GenError("generator 'resistor_strip': params.spacing_um must be >= 0")


def _resistor_strip_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    length_um = params["length_um"]
    width_um = params["width_um"]
    spacing_um = params["spacing_um"]
    num = params["num"]
    pitch_um = length_um + spacing_um

    def _snapped(value_um: float) -> bool:
        count = value_um / dbu
        return abs(count - round(count)) > 1e-9

    snapped_to_grid = any(_snapped(v) for v in (length_um, width_um, spacing_um))
    # Per spike section 2's field table, a snapped dimension is reported as a
    # top-level `warnings` entry (its own worked example), not a `drc_hints`
    # note -- `drc_hints.notes` is reserved for generator-specific DRC-adjacent
    # notes (e.g. a spacing bump), which this skeleton generator has none of.
    warnings = (
        ["one or more dimensions were rounded to the technology grid"]
        if snapped_to_grid
        else []
    )

    # Layer name is not resolved against a PDK layer map at phase 1 -- the
    # drawing layer is a generator implementation detail (see the module
    # docstring's `_HIDDEN_PARAMS` note); a real per-PDK layer name lookup is
    # phase 2 scope, alongside the primitive families that actually need one.
    layer = {"layer": 67, "datatype": 20, "name": None}

    ports = [
        {
            "name": "P1",
            "net": None,
            "layer": layer,
            "x_um": 0.0,
            "y_um": width_um / 2.0,
            "width_um": width_um,
            "direction_deg": 180,
        },
        {
            "name": "P2",
            "net": None,
            "layer": layer,
            "x_um": (num - 1) * pitch_um + length_um,
            "y_um": width_um / 2.0,
            "width_um": width_um,
            "direction_deg": 0,
        },
    ]

    return {
        "device_count": num,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": spacing_um,
            "matched_group_id": None,
            "snapped_to_grid": snapped_to_grid,
            "notes": [],
        },
        "warnings": warnings,
    }


def _mos_array_validate(params: dict[str, Any]) -> None:
    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'mos_array': params.w_um must be >= {UNIT_MIN_W_UM} "
            "(the smallest width that fits an enclosed contact with margin -- "
            "a generator-side structural floor, not the target PDK's own "
            "diffusion-width rule)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'mos_array': params.l_um must be > 0")
    if params["fingers"] < 1:
        raise GenError("generator 'mos_array': params.fingers must be >= 1")
    if params["rows"] < 1:
        raise GenError("generator 'mos_array': params.rows must be >= 1")
    if params["cols"] < 1:
        raise GenError("generator 'mos_array': params.cols must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'mos_array': params.dummy must be >= 0")
    if params["topology"] not in ("array", "common_centroid"):
        raise GenError(
            "generator 'mos_array': params.topology must be 'array' or "
            "'common_centroid'"
        )
    if params["flavor"] not in ("nfet", "pfet"):
        raise GenError("generator 'mos_array': params.flavor must be 'nfet' or 'pfet'")
    if params["finger_topology"] not in ("parallel", "series"):
        raise GenError(
            "generator 'mos_array': params.finger_topology must be 'parallel' "
            "or 'series'"
        )


def _mos_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _mos_array_layout(
        params["w_um"],
        params["l_um"],
        params["fingers"],
        params["rows"],
        params["cols"],
        params["dummy"],
        params["topology"],
        params["gate_contact"],
        params["finger_topology"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492: with `gate_contact` the gate terminal *is* a metal pad, so it is
    # reported on the metal role exactly like S/D -- the whole point being
    # that `klt gen-compose`'s router treats it identically. Without it the
    # gate stays the bare-poly node #210 established.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer

    ports = []
    sx, sy = unit["s_xy"]
    dx, dy = unit["d_xy"]
    gx, gy = unit["g_xy"]
    for c in info["cells"]:
        idx = c["idx"]
        ports.append(
            {
                "name": f"U{idx}_S",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + sx,
                "y_um": c["y0_um"] + sy,
                "width_um": unit["sd_width_um"],
                "direction_deg": 180,
            }
        )
        ports.append(
            {
                "name": f"U{idx}_D",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + dx,
                "y_um": c["y0_um"] + dy,
                "width_um": unit["sd_width_um"],
                "direction_deg": 0,
            }
        )
        ports.append(
            {
                "name": f"U{idx}_G",
                "net": None,
                "layer": gate_layer,
                "x_um": c["x0_um"] + gx,
                "y_um": c["y0_um"] + gy,
                "width_um": unit["g_width_um"],
                "direction_deg": 90,
            }
        )

    notes = []
    if params["l_um"] < GATE_LENGTH_SAFE_MIN_UM:
        notes.append(
            f"gate length below {GATE_LENGTH_SAFE_MIN_UM}um may violate the target "
            "PDK's poly minimum-width or S/D metal minimum-spacing rule"
        )
    if params["flavor"] == "pfet" and _PDK_ROLE_LAYERS[family]["well"] is None:
        notes.append(
            f"params.flavor is 'pfet' but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    series_fingers = params["finger_topology"] == "series" and params["fingers"] > 1
    if series_fingers:
        notes.append(
            "params.finger_topology is 'series', so each unit device's "
            f"{params['fingers']} fingers are drawn unstrapped -- they extract as "
            f"{params['fingers']} transistors chained source-to-drain, not one "
            f"parallel device of width {params['fingers']} x {params['w_um']}um"
        )

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    grid = f"{params['rows']}x{params['cols']}"
    # `flavor` is not folded into `matched_group_id` -- a matched group is
    # always one generator call by construction, so every instance in it
    # already shares one `flavor`; the id only needs to disambiguate groups
    # from each other (see the issue's Implementation Guidance item 5).
    matched_group_id = f"mos_array:{grid}:{params['topology']}"

    return {
        "device_count": params["rows"] * params["cols"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        )
        # #777: the unstrapped series shape's interior S/D segments and
        # interior gates have no reported ports and no landing pads, so a
        # caller cannot reach them -- that has to surface at generate time,
        # not as a surprise in an extracted netlist or an LVS diff.
        + (
            [
                f"params.finger_topology 'series' with fingers={params['fingers']}: "
                "the interior S/D segments and all but the first gate are drawn "
                "but not reported as ports and cannot be contacted -- the unit "
                "extracts as a series chain of transistors with floating gates "
                "(use the default 'parallel' for one folded device)"
            ]
            if series_fingers
            else []
        ),
    }


def _res_array_validate(params: dict[str, Any]) -> None:
    if params["length_um"] <= 0:
        raise GenError("generator 'res_array': params.length_um must be > 0")
    if params["width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'res_array': params.width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["spacing_um"] < 0:
        raise GenError("generator 'res_array': params.spacing_um must be >= 0")
    if params["num"] < 1:
        raise GenError("generator 'res_array': params.num must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'res_array': params.dummy must be >= 0")
    if params["rows"] < 1:
        raise GenError("generator 'res_array': params.rows must be >= 1")


def _res_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _res_array_layout(
        params["length_um"],
        params["width_um"],
        params["spacing_um"],
        params["num"],
        params["dummy"],
        params["rows"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}

    ports = []
    left_xy = unit["a_xy"]  # physically local-left pad -- always points 180deg
    right_xy = unit["b_xy"]  # physically local-right pad -- always points 0deg
    for c in info["cells"]:
        idx = c["idx"]
        # A unit's poly body is drawn identically regardless of row
        # direction (never mirrored) -- on a right-to-left (odd) row the
        # physically-adjacent pad for a short row-transition jumper is the
        # *entry* pad on that unit's physical right, not its left. Swapping
        # which physical pad reports as `_A` (entry, from the previous unit
        # in the chain) vs `_B` (exit, toward the next) keeps `R<i>_B`
        # physically next to `R<i + 1>_A` in every row, per
        # :func:`_res_array_layout`'s docstring.
        if c.get("direction", 1) >= 0:
            entry_xy, entry_deg = left_xy, 180
            exit_xy, exit_deg = right_xy, 0
        else:
            entry_xy, entry_deg = right_xy, 0
            exit_xy, exit_deg = left_xy, 180
        ports.append(
            {
                "name": f"R{idx}_A",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + entry_xy[0],
                "y_um": c["y0_um"] + entry_xy[1],
                "width_um": unit["height_um"],
                "direction_deg": entry_deg,
            }
        )
        ports.append(
            {
                "name": f"R{idx}_B",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + exit_xy[0],
                "y_um": c["y0_um"] + exit_xy[1],
                "width_um": unit["height_um"],
                "direction_deg": exit_deg,
            }
        )

    notes = []
    if 0 <= params["spacing_um"] < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    snapped = _grid_snapped(
        dbu, params["length_um"], params["width_um"], params["spacing_um"]
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": params["spacing_um"],
            "matched_group_id": f"res_array:{params['num']}",
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _validate_ring_gap(
    generator: str,
    side: str,
    gap_um: float,
    offset_um: float,
    inner_w_um: float,
    inner_h_um: float,
) -> None:
    """Validate a ring-opening request (``ring_gap_side``/``ring_gap_um``/
    ``ring_gap_offset_um``, #434) against the ring it would be cut into.

    Rejects an unknown side, a gap requested without a side (or a side
    without a usable opening), an opening narrower than the minimum
    same-layer spacing (the two cut ends would violate it), and an opening
    that does not fit inside the side's *straight run* -- cutting into a
    corner would break the ring into two disconnected arcs while its tap
    ports still claimed a single tap net.

    Only one side can be named, so the ring always stays a single connected
    conductor.
    """
    if side not in ("", *RING_SIDE_DIRECTIONS):
        raise GenError(
            f"generator '{generator}': params.ring_gap_side must be '' (a "
            "closed ring), 'N', 'S', 'E' or 'W'"
        )
    if not side:
        if gap_um != 0.0 or offset_um != 0.0:
            raise GenError(
                f"generator '{generator}': params.ring_gap_um/"
                "params.ring_gap_offset_um require params.ring_gap_side to "
                "name the side the opening is cut on"
            )
        return

    if gap_um < MIN_SAME_LAYER_SPACING_UM:
        raise GenError(
            f"generator '{generator}': params.ring_gap_um must be >= "
            f"{MIN_SAME_LAYER_SPACING_UM} when params.ring_gap_side is set -- "
            "a narrower opening leaves the ring's two cut ends closer than the "
            "minimum same-layer spacing"
        )

    straight_um = inner_w_um if side in ("N", "S") else inner_h_um
    if abs(offset_um) + gap_um / 2.0 > straight_um / 2.0 + 1e-9:
        raise GenError(
            f"generator '{generator}': the ring opening (ring_gap_um={gap_um}, "
            f"ring_gap_offset_um={offset_um}) does not fit inside the "
            f"{straight_um}um straight run of ring side '{side}' -- an opening "
            "that reaches a corner would split the ring into two disconnected "
            "arcs"
        )


def _ring_ports(
    ring: dict[str, Any],
    offset_um: tuple[float, float],
    tap_prefix: str,
    tap_layer: dict[str, Any],
    tap_width_um: float,
    gap_layer: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reported ``ports[]`` entries for a drawn ring.

    One ``<tap_prefix><side>`` tap port at the midpoint of every side that is
    still covered by ring metal there, plus -- when the ring was cut with
    ``ring_gap_side`` (#434) -- one ``GAP_<side>`` entry marking the opening
    itself. A ``GAP_*`` entry is a *marker, not a conductor*: it reports where
    a route may cross the ring (``x_um``/``y_um`` = the opening's centre on
    the ring's own centre line, ``width_um`` = the opening's length along that
    side, ``direction_deg`` = the side's outward normal) on the layer a route
    would cross it on, and `klt gen-compose` rejects any attempt to wire or
    label it.
    """
    ox, oy = offset_um
    ports: list[dict[str, Any]] = []
    for side, direction in RING_SIDE_DIRECTIONS.items():
        xy = ring["ports"].get(side)
        if xy is None:
            continue  # this side's midpoint sits inside the ring opening
        ports.append(
            {
                "name": f"{tap_prefix}{side}",
                "net": None,
                "layer": tap_layer,
                "x_um": xy[0] + ox,
                "y_um": xy[1] + oy,
                "width_um": tap_width_um,
                "direction_deg": direction,
            }
        )

    gap = ring["gap"]
    if gap is not None:
        ports.append(
            {
                "name": f"GAP_{gap['side']}",
                "net": None,
                "layer": gap_layer,
                "x_um": gap["x_um"] + ox,
                "y_um": gap["y_um"] + oy,
                "width_um": gap["opening_um"],
                "direction_deg": RING_SIDE_DIRECTIONS[gap["side"]],
            }
        )
    return ports


def _ring_gap_notes(ring: dict[str, Any] | None, ring_gap_side: str) -> list[str]:
    """``drc_hints.notes`` entries about a requested ring opening (#434)."""
    if not ring_gap_side:
        return []
    if ring is None or ring["gap"] is None:
        return [
            f"params.ring_gap_side is '{ring_gap_side}' but no ring is drawn -- "
            "no opening was cut"
        ]
    gap = ring["gap"]
    return [
        f"the ring carries a {gap['opening_um']}um routing opening on its "
        f"'{gap['side']}' side (params.ring_gap_side) -- it is a connected "
        "C-shaped conductor rather than a closed loop, so substrate isolation "
        "is interrupted there in exchange for letting a route reach the "
        f"enclosed devices through the reported GAP_{gap['side']} position"
    ]


def _guard_ring_axis_contacts(params: dict[str, Any]) -> tuple[int, int]:
    """Resolve ``params``'s per-axis contact counts: ``contacts_per_side_ns``/
    ``contacts_per_side_ew`` when set (non-zero), else ``contacts_per_side``
    inherited on that axis -- the same "0 means inherit" rule the PCell's
    ``produce_impl`` applies, kept in sync here so ``_guard_ring_validate``/
    ``_guard_ring_describe`` never disagree with what actually gets drawn."""
    contacts_ns = params["contacts_per_side_ns"] or params["contacts_per_side"]
    contacts_ew = params["contacts_per_side_ew"] or params["contacts_per_side"]
    return contacts_ns, contacts_ew


def _guard_ring_validate(params: dict[str, Any]) -> None:
    if params["inner_width_um"] <= 0:
        raise GenError("generator 'guard_ring': params.inner_width_um must be > 0")
    if params["inner_height_um"] <= 0:
        raise GenError("generator 'guard_ring': params.inner_height_um must be > 0")
    if params["ring_width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'guard_ring': params.ring_width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["contacts_per_side"] < 1:
        raise GenError("generator 'guard_ring': params.contacts_per_side must be >= 1")
    if params["contacts_per_side_ns"] < 0:
        raise GenError(
            "generator 'guard_ring': params.contacts_per_side_ns must be >= 0 "
            "(0 inherits params.contacts_per_side)"
        )
    if params["contacts_per_side_ew"] < 0:
        raise GenError(
            "generator 'guard_ring': params.contacts_per_side_ew must be >= 0 "
            "(0 inherits params.contacts_per_side)"
        )

    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)

    for label, straight, contacts, param_name in (
        (
            "inner_width_um",
            params["inner_width_um"],
            contacts_ns,
            "contacts_per_side_ns",
        ),
        (
            "inner_height_um",
            params["inner_height_um"],
            contacts_ew,
            "contacts_per_side_ew",
        ),
    ):
        pitch = straight / (contacts + 1)
        if pitch <= CONTACT_SIZE_UM:
            raise GenError(
                f"generator 'guard_ring': {param_name} (effectively {contacts}, "
                "from params.contacts_per_side unless overridden) does not fit "
                f"along {label}={straight}um without overlapping contacts"
            )

    _validate_ring_gap(
        "guard_ring",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["inner_width_um"],
        params["inner_height_um"],
    )


def _guard_ring_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    info = _ring_layout(
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        (contacts_ns, contacts_ew),
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    well_supported = _PDK_ROLE_LAYERS[family]["well"] is not None

    ports = _ring_ports(
        info,
        (0.0, 0.0),
        "TAP_",
        metal_layer,
        params["ring_width_um"],
        metal_layer,
    )

    notes = _ring_gap_notes(info, params["ring_gap_side"])
    # `_guard_ring_validate` only rejects a per-axis contact count that would
    # literally overlap (see its own `pitch <= CONTACT_SIZE_UM` check) -- it
    # can't reject on `CONTACT_GAP_SAFE_UM` alone since that margin's real
    # violation only applies to gf180mcu (sky130's curated deck checks no
    # contact-spacing rule at all), and `_guard_ring_validate` doesn't know
    # the resolved PDK family. `_guard_ring_describe` does, so it flags a
    # tight-but-still-accepted gap here instead, per-axis, so a caller
    # targeting gf180mcu knows to double-check with `klt drc` (issue #685).
    for label, straight, contacts in (
        ("inner_width_um", params["inner_width_um"], contacts_ns),
        ("inner_height_um", params["inner_height_um"], contacts_ew),
    ):
        pitch = straight / (contacts + 1)
        if pitch - CONTACT_SIZE_UM < CONTACT_GAP_SAFE_UM:
            notes.append(
                f"the resolved contact count ({contacts}) leaves less than "
                f"{CONTACT_GAP_SAFE_UM}um between adjacent contacts along {label} -- "
                "may violate the target PDK's minimum contact spacing rule"
            )
    if params["add_well"] and not well_supported:
        notes.append(
            f"params.add_well is true but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    snapped = _grid_snapped(
        dbu,
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
    )

    return {
        # The contacts actually drawn -- `2 * (contacts_ns + contacts_ew)`
        # unless a ring opening (params.ring_gap_side) clipped one or more
        # of them away.
        "device_count": len(info["contact_boxes_um"]),
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _diff_pair_validate(params: dict[str, Any]) -> None:
    if params["w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'diff_pair': params.w_um must be >= {UNIT_MIN_W_UM} "
            "(the smallest width that fits an enclosed contact with margin -- "
            "a generator-side structural floor, not the target PDK's own "
            "diffusion-width rule)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'diff_pair': params.l_um must be > 0")
    if params["splits"] < 1:
        raise GenError("generator 'diff_pair': params.splits must be >= 1")
    if params["flavor"] not in ("nfet", "pfet"):
        raise GenError("generator 'diff_pair': params.flavor must be 'nfet' or 'pfet'")
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'diff_pair': params.ring_padding_um must be >= 0")
    if params["row_spacing_um"] < 0:
        raise GenError("generator 'diff_pair': params.row_spacing_um must be >= 0")

    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _diff_pair_layout(
            params["w_um"],
            params["l_um"],
            params["splits"],
            True,
            ring_padding_um=params["ring_padding_um"],
            row_spacing_um=params["row_spacing_um"],
            gate_contact=params["gate_contact"],
        )
    )
    _validate_ring_gap(
        "diff_pair",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _diff_pair_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _diff_pair_layout(
        params["w_um"],
        params["l_um"],
        params["splits"],
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        params["row_spacing_um"],
        params["gate_contact"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492 -- see the equivalent comment in `_mos_array_describe`.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer
    prefix = "M" if params["mirror"] else "Q"

    ports = []
    sx, sy = unit["s_xy"]
    dx, dy = unit["d_xy"]
    gx, gy = unit["g_xy"]
    for c in info["cells"]:
        device_num = 1 if c["label"] == "A" else 2
        base = f"{prefix}{device_num}_{c['n']}"
        ports.append(
            {
                "name": f"{base}_S",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + sx,
                "y_um": c["y0_um"] + sy,
                "width_um": unit["height_um"],
                "direction_deg": 180,
            }
        )
        ports.append(
            {
                "name": f"{base}_D",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + dx,
                "y_um": c["y0_um"] + dy,
                "width_um": unit["height_um"],
                "direction_deg": 0,
            }
        )
        ports.append(
            {
                "name": f"{base}_G",
                "net": None,
                "layer": gate_layer,
                "x_um": c["x0_um"] + gx,
                "y_um": c["y0_um"] + gy,
                "width_um": unit["g_width_um"],
                "direction_deg": 90,
            }
        )

    ring_drawn = info["ring"] is not None and params["add_guard_ring"]
    if ring_drawn:
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "TAP_",
                metal_layer,
                GUARD_RING_DEFAULT_WIDTH_UM,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not params["add_guard_ring"]:
        notes.append(
            "no guard ring drawn (params.add_guard_ring is false) -- isolation from "
            "adjacent structures is the caller's responsibility"
        )
    if params["flavor"] == "pfet" and _PDK_ROLE_LAYERS[family]["well"] is None:
        notes.append(
            f"params.flavor is 'pfet' but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    kind = "mirror" if params["mirror"] else "pair"
    # `flavor` is not folded into `matched_group_id` -- see the equivalent
    # comment in `_mos_array_describe`.
    matched_group_id = f"diff_pair:{kind}:{params['splits']}"

    return {
        "device_count": 2 * params["splits"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _bjt_array_validate(params: dict[str, Any]) -> None:
    if params["emitter_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'bjt_array': params.emitter_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["rows"] < 1:
        raise GenError("generator 'bjt_array': params.rows must be >= 1")
    if params["cols"] < 1:
        raise GenError("generator 'bjt_array': params.cols must be >= 1")
    if params["dummy"] < 0:
        raise GenError("generator 'bjt_array': params.dummy must be >= 0")
    if params["ratio"] < 1:
        raise GenError("generator 'bjt_array': params.ratio must be >= 1")
    if params["topology"] not in ("array", "common_centroid"):
        raise GenError(
            "generator 'bjt_array': params.topology must be 'array' or "
            "'common_centroid'"
        )

    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _bjt_array_layout(
            params["emitter_um"],
            params["rows"],
            params["cols"],
            params["dummy"],
            params["topology"],
            True,
        )
    )
    _validate_ring_gap(
        "bjt_array",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _bjt_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _bjt_array_layout(
        params["emitter_um"],
        params["rows"],
        params["cols"],
        params["dummy"],
        params["topology"],
        params["add_collector_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )
    unit = info["unit"]
    active_pair = _PDK_ROLE_LAYERS[family]["active"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    active_layer = {"layer": active_pair[0], "datatype": active_pair[1], "name": None}
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    well_supported = _PDK_ROLE_LAYERS[family]["well"] is not None
    mark_supported = _PDK_ROLE_LAYERS[family]["bjt_mark"] is not None

    ports = []
    ex, ey = unit["e_xy"]
    bx, by = unit["b_xy"]
    for c in info["cells"]:
        idx = c["idx"]
        ports.append(
            {
                "name": f"Q{idx}_E",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + ex,
                "y_um": c["y0_um"] + ey,
                "width_um": CONTACT_SIZE_UM,
                "direction_deg": 90,
            }
        )
        ports.append(
            {
                "name": f"Q{idx}_B",
                "net": None,
                "layer": metal_layer,
                "x_um": c["x0_um"] + bx,
                "y_um": c["y0_um"] + by,
                "width_um": CONTACT_SIZE_UM,
                "direction_deg": 90,
            }
        )

    ring_drawn = info["ring"] is not None and params["add_collector_ring"]
    if ring_drawn:
        ring_w = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
        # The collector ring is drawn on both the diffusion and the local-metal
        # role, so its tap ports report the diffusion layer (what makes them a
        # collector tie) while a ring opening reports the metal layer -- the one
        # a `klt gen-compose` route actually crosses it on.
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "COLL_",
                active_layer,
                ring_w,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not mark_supported:
        notes.append(
            f"the resolved PDK family ('{family}') has no bipolar device-mark "
            "layer checked by its curated DRC deck -- no device-mark shape was "
            "drawn; the vertical-bipolar geometry is a DRC-clean matching "
            "floorplan, not a SPICE-model-exact device (see "
            "docs/design/gen-bjt-array-spike.md)"
        )
    if not well_supported:
        notes.append(
            f"the resolved PDK family ('{family}') has no well layer checked by "
            "its curated DRC deck -- no shared base-well shape was drawn"
        )
    if not params["add_collector_ring"]:
        notes.append(
            "no collector guard ring drawn (params.add_collector_ring is false)"
        )
    if params["rows"] * params["cols"] < params["ratio"] + 1:
        notes.append(
            f"array holds {params['rows'] * params['cols']} devices -- too few to "
            f"realise the requested {params['ratio']}:1 matching ratio around a "
            "single reference device"
        )

    snapped = _grid_snapped(dbu, params["emitter_um"])
    grid = f"{params['rows']}x{params['cols']}"
    matched_group_id = f"bjt_array:{grid}:{params['topology']}:ratio{params['ratio']}"

    return {
        "device_count": params["rows"] * params["cols"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _bond_pad_validate(params: dict[str, Any]) -> None:
    if params["opening_um"] <= 0:
        raise GenError("generator 'bond_pad': params.opening_um must be > 0")
    if params["bond_type"] not in PAD_OPENING_GUIDELINE_MIN_UM:
        raise GenError(
            "generator 'bond_pad': params.bond_type must be one of "
            f"{', '.join(sorted(PAD_OPENING_GUIDELINE_MIN_UM))}"
        )
    if params["enclosure_um"] < PAD_TOP_METAL_ENCLOSURE_MIN_UM:
        raise GenError(
            "generator 'bond_pad': params.enclosure_um must be >= "
            f"{PAD_TOP_METAL_ENCLOSURE_MIN_UM} (gf180mcu's PAD.4 hard rule -- "
            "minimum top-metal overlap of the pad opening)"
        )
    if params["down_to"] != "top_metal":
        raise GenError(
            "generator 'bond_pad': params.down_to must be 'top_metal' -- "
            "this generator does not yet draw a via stack connecting the "
            "pad's top-metal strap down to any lower metal level (a real "
            "via4/via3/.../via1 chain neither curated deck models this far "
            "up the stack); 'top_metal' is the only currently supported "
            "value"
        )
    if params["via_style"] not in ("ring", "array"):
        raise GenError(
            "generator 'bond_pad': params.via_style must be 'ring' or 'array'"
        )


def _bond_pad_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    opening_um = params["opening_um"]
    enclosure_um = params["enclosure_um"]

    # The `pad` role (the passivation opening) is drawn by `produce_impl` via
    # `layer_params` but not itself reported in `ports[]` below -- the pad
    # net is the top-metal strap electrically, per PAD.4's own framing ("top
    # layer metal overlap of pad opening"); the opening is a process step,
    # not a separate net a `klt gen-compose` route would ever target.
    top_metal_pair = _PDK_ROLE_LAYERS[family]["top_metal"]
    top_metal_layer = {
        "layer": top_metal_pair[0],
        "datatype": top_metal_pair[1],
        "name": None,
    }

    ports = [
        {
            "name": "PAD",
            "net": None,
            "layer": top_metal_layer,
            "x_um": 0.0,
            "y_um": 0.0,
            "width_um": opening_um,
            "direction_deg": 90,
        }
    ]

    notes: list[str] = []
    guideline_min = PAD_OPENING_GUIDELINE_MIN_UM[params["bond_type"]]
    if opening_um < guideline_min:
        notes.append(
            f"params.opening_um ({opening_um}um) is below the "
            f"{guideline_min}um guideline minimum pad opening for "
            f"bond_type '{params['bond_type']}' (gf180mcu DRM 9.2 'PAD.1' -- "
            "a guideline, not a DRC-hard rule; confirm with your assembly "
            "house before tapeout)"
        )
    notes.append(
        "params.via_style has no effect while params.down_to is fixed to "
        "'top_metal' -- this generator does not yet draw a via stack "
        "connecting the pad down to a lower metal level"
    )
    if family == "gf180mcu":
        notes.append(
            "gf180mcu output assumes the 5LM metal stack (top_metal = "
            "Metal5) -- a 6LM variant's true top metal (MetalTop) is not "
            "distinguishable from the resolved PDK variant string today; "
            "see docs/cli/gen.md's bond_pad section"
        )

    snapped = _grid_snapped(dbu, opening_um, enclosure_um)

    return {
        "device_count": 1,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": enclosure_um,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


def _esd_device_validate(params: dict[str, Any]) -> None:
    if params["finger_width_um"] < ESD_FINGER_WIDTH_MIN_UM:
        raise GenError(
            f"generator 'esd_device': params.finger_width_um must be >= "
            f"{ESD_FINGER_WIDTH_MIN_UM} (the smallest width that fits an "
            "enclosed contact with margin -- a generator-side structural "
            "floor, not the target PDK's own diffusion-width rule)"
        )
    if params["finger_width_um"] > ESD_FINGER_WIDTH_MAX_UM:
        raise GenError(
            f"generator 'esd_device': params.finger_width_um must be <= "
            f"{ESD_FINGER_WIDTH_MAX_UM} -- individual ESD-clamp fingers wider "
            "than this risk non-uniform current sharing across fingers "
            "during an ESD pulse (see ESD_FINGER_WIDTH_MAX_UM's own "
            "docstring)"
        )
    if params["l_um"] <= 0:
        raise GenError("generator 'esd_device': params.l_um must be > 0")
    if params["fingers"] < 1:
        raise GenError("generator 'esd_device': params.fingers must be >= 1")
    if params["fingers"] > ESD_MAX_FINGERS_PER_RING:
        raise GenError(
            f"generator 'esd_device': params.fingers must be <= "
            f"{ESD_MAX_FINGERS_PER_RING} fingers per automatically-sized tap "
            "ring (see ESD_MAX_FINGERS_PER_RING's own docstring)"
        )
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'esd_device': params.ring_padding_um must be >= 0")

    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _esd_device_layout(
            params["finger_width_um"],
            params["l_um"],
            params["fingers"],
            True,
            ring_padding_um=params["ring_padding_um"],
            gate_contact=params["gate_contact"],
        )
    )
    _validate_ring_gap(
        "esd_device",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _esd_device_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    info = _esd_device_layout(
        params["finger_width_um"],
        params["l_um"],
        params["fingers"],
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        params["gate_contact"],
    )
    unit = info["unit"]
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    poly_pair = _PDK_ROLE_LAYERS[family]["poly"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    poly_layer = {"layer": poly_pair[0], "datatype": poly_pair[1], "name": None}
    # #492 -- see the equivalent comment in `_mos_array_describe`.
    gate_layer = metal_layer if params["gate_contact"] else poly_layer

    sx, sy = unit["s_xy"]
    dx, dy = unit["d_xy"]
    gx, gy = unit["g_xy"]
    ports = [
        {
            "name": "M1_S",
            "net": None,
            "layer": metal_layer,
            "x_um": sx,
            "y_um": sy,
            "width_um": unit["height_um"],
            "direction_deg": 180,
        },
        {
            "name": "M1_D",
            "net": None,
            "layer": metal_layer,
            "x_um": dx,
            "y_um": dy,
            "width_um": unit["height_um"],
            "direction_deg": 0,
        },
        {
            "name": "M1_G",
            "net": None,
            "layer": gate_layer,
            "x_um": gx,
            "y_um": gy,
            "width_um": unit["g_width_um"],
            "direction_deg": 90,
        },
    ]

    ring_drawn = info["ring"] is not None and params["add_guard_ring"]
    if ring_drawn:
        ports.extend(
            _ring_ports(
                info["ring"],
                info["ring_offset_um"],
                "TAP_",
                metal_layer,
                GUARD_RING_DEFAULT_WIDTH_UM,
                metal_layer,
            )
        )

    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if not params["add_guard_ring"]:
        notes.append(
            "no tap ring drawn (params.add_guard_ring is false) -- substrate "
            "isolation from adjacent structures is the caller's responsibility"
        )
    if _PDK_ROLE_LAYERS[family].get("esd_mark") is None:
        notes.append(
            f"the resolved PDK family ('{family}') has no citable ESD "
            "device-class marker layer in this repo's curated deck -- no "
            "esd_mark shape was drawn (see the esd_mark role's own comment "
            "in _PDK_ROLE_LAYERS)"
        )
    if (
        params["salicide_block"]
        and _PDK_ROLE_LAYERS[family].get("salicide_block") is None
    ):
        notes.append(
            f"params.salicide_block is true but the resolved PDK family "
            f"('{family}') has no citable salicide-block layer in this "
            "repo's curated deck -- no shape was drawn"
        )

    snapped = _grid_snapped(dbu, params["finger_width_um"], params["l_um"])

    return {
        "device_count": 1,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
            else []
        ),
    }


#: Maps ``PCellParameterDeclaration`` type constants to the JSON type names
#: reported by ``klt gen --list``.
_PARAM_TYPE_NAMES = {
    0: "int",
    1: "double",
    2: "string",
    3: "bool",
}

_GENERATOR_SPECS: dict[str, _GeneratorSpec] = {
    "resistor_strip": _GeneratorSpec(
        name="resistor_strip",
        summary=(
            "Row of parametrized rectangles standing in for a unit-resistor "
            "string -- the phase-1 reference generator proving the request/"
            "response contract end-to-end. Not DRC-clean; phase 2 replaces "
            "this with a real resistor-array generator "
            "(docs/design/layout-generator-spike.md section 4.2)."
        ),
        dbu=0.001,
        validate=_resistor_strip_validate,
        describe=_resistor_strip_describe,
        layer_params=_resistor_strip_layer_params,
    ),
    "mos_array": _GeneratorSpec(
        name="mos_array",
        summary=(
            "Matched MOS transistor array: identical unit devices (active + "
            "poly gate; contact + local-metal landing pads on the S/D "
            "terminals, gate exposed as bare poly unless params.gate_contact "
            "finishes its stack too) placed on a "
            "uniform grid, with optional dummy columns at each end and a "
            "centroid-symmetric port-numbering order for common-centroid "
            "matching -- family 1 of the analog primitive generators "
            "(docs/design/layout-generator-spike.md section 4)."
        ),
        dbu=0.001,
        validate=_mos_array_validate,
        describe=_mos_array_describe,
        layer_params=_device_layer_params,
    ),
    "res_array": _GeneratorSpec(
        name="res_array",
        summary=(
            "Unit resistor/capacitor array: a row of matched unit elements "
            "(poly body + contact + local-metal pads at both ends) with "
            "dummy elements at each end, per the sky130-bandgap-reference KB "
            "entry's resistor-array layout idiom -- family 2."
        ),
        dbu=0.001,
        validate=_res_array_validate,
        describe=_res_array_describe,
        layer_params=_resistor_layer_params,
    ),
    "guard_ring": _GeneratorSpec(
        name="guard_ring",
        summary=(
            "Substrate/well tap guard ring: a tap ring with evenly-spaced "
            "contacts and a local-metal ring, optionally enclosed by a well "
            "tie on PDK families whose curated deck checks one -- family 3."
        ),
        dbu=0.001,
        validate=_guard_ring_validate,
        describe=_guard_ring_describe,
        layer_params=_ring_layer_params,
    ),
    "diff_pair": _GeneratorSpec(
        name="diff_pair",
        summary=(
            "Differential pair / current mirror cell: two matched devices "
            "(Q1/Q2, or M1/M2 with params.mirror) split into params.splits "
            "sub-instances each and interleaved in a true common-centroid "
            "cross-quad pattern, optionally enclosed by an automatically-"
            "sized guard ring -- family 4, composing families 1 and 3."
        ),
        dbu=0.001,
        validate=_diff_pair_validate,
        describe=_diff_pair_describe,
        layer_params=_diff_pair_layer_params,
    ),
    "bjt_array": _GeneratorSpec(
        name="bjt_array",
        summary=(
            "Matched vertical-bipolar (PNP/BJT) array: a common-centroid grid "
            "of identical unit devices (emitter + base-tie diffusion with "
            "contacts and local metal) sharing one base well, enclosed by a "
            "collector guard ring and a bipolar device-mark layer on PDK "
            "families whose curated deck checks one (gf180mcu's DRC_BJT) -- "
            "Epic #152 phase 4, drawn from base layers per "
            "docs/design/gen-bjt-array-spike.md."
        ),
        dbu=0.001,
        validate=_bjt_array_validate,
        describe=_bjt_array_describe,
        layer_params=_bjt_layer_params,
    ),
    "bond_pad": _GeneratorSpec(
        name="bond_pad",
        summary=(
            "Chip-boundary bond pad: a passivation opening enclosed by the "
            "resolved PDK family's own topmost routing metal, satisfying "
            "gf180mcu's only hard bond-pad DRC rule (PAD.4) -- the first "
            "generator in this family covering the I/O boundary rather than "
            "a core analog device (issue #568)."
        ),
        dbu=0.001,
        validate=_bond_pad_validate,
        describe=_bond_pad_describe,
        layer_params=_bond_pad_layer_params,
    ),
    "esd_device": _GeneratorSpec(
        name="esd_device",
        summary=(
            "Grounded-gate multi-finger ESD protection MOS: one multi-finger "
            "unit device (params.fingers gate stripes across a shared "
            "diffusion, the standard ggNMOS ESD-clamp layout idiom), "
            "optionally enclosed in an automatically-sized tap ring composing "
            "guard_ring's own ring-drawing helper (family 3), the same "
            "composition mechanism diff_pair already uses for families 1 and "
            "3, with an optional salicide_block layer -- issue #569."
        ),
        dbu=0.001,
        validate=_esd_device_validate,
        describe=_esd_device_describe,
        layer_params=_esd_device_layer_params,
    ),
}
