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

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._layout import write_layout
from .pdk import PdkNotFoundError, find_pdk, resolve_pdk_dbu
from .pdk_models import _pdk_variant_family

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
#: resolved PDK family. ``res_array``'s own per-flavour ``res_flavor_<i>_*``
#: mask slots are appended below :data:`_PDK_RES_FLAVOR_LAYERS`, since how
#: many of them exist is derived from that table; ``cap_array``'s own
#: ``cap_top_requires_<i>_*`` slots are appended the same way below
#: :data:`_PDK_CAP_TOP_PLATE_REQUIRES` (issue #1555).
_HIDDEN_PARAMS = {
    "layer",
    "active_layer",
    "poly_layer",
    "contact_layer",
    "metal_layer",
    "tap_layer",
    "well_layer",
    "well_present",
    "metal_label_layer",
    "metal_label_present",
    "well_tap_implant_layer",
    "well_tap_implant_present",
    "well_margin_resolved_um",
    "gate_pad_clearance_um",
    # `mos_array`/`diff_pair`/`esd_device`'s issue #1577 harness-computed
    # knobs -- resolved per PDK family exactly like `gate_pad_clearance_um`
    # above, never part of the request schema.
    "voltage_flavor_mark_margin_um",
    "contact_gate_offset_um",
    "bottom_endcap_um",
    "sd_implant_layer",
    "sd_implant_present",
    "sd_implant_margin_um",
    "bjt_mark_layer",
    "bjt_mark_present",
    "res_mark_layer",
    "res_mark_present",
    "dummy_layer",
    "dummy_present",
    "pad_layer",
    "top_metal_layer",
    "esd_mark_layer",
    "esd_mark_present",
    "salicide_block_layer",
    "salicide_block_present",
    "voltage_flavor_mark_layer",
    "voltage_flavor_mark_present",
    "cap_top_plate_layer",
    "cap_bottom_plate_layer",
    "cap_top_via_layer",
    "cap_top_via_present",
    "cap_top_via_metal_layer",
    "cap_top_via_metal_present",
    # `cap_array`'s per-family geometry floors (see
    # :data:`_PDK_CAP_GEOMETRY_MIN_UM`) -- harness-computed from the resolved
    # PDK family exactly like `gate_pad_clearance_um`/
    # `well_margin_resolved_um` above, never part of the request schema.
    # `cap_top_via_metal_min_w_um` (issue #1455) was omitted from this set
    # when it landed, so it leaked into `klt gen --list`'s `cap_array`
    # params despite being absent from `docs/cli/gen.md`'s own param table;
    # listing all four together here keeps the mechanism consistent (#1555).
    "cap_bottom_plate_margin_min_um",
    "cap_top_via_min_w_um",
    "cap_top_via_metal_min_w_um",
    "cap_min_spacing_um",
    # `guard_ring`/`mos_array`/`diff_pair`/`bjt_array`/`esd_device`'s own
    # tap/collector-ring implant (issue #1580, follow-up to #1577's
    # unit-device-only `sd_implant_*` fix) -- harness-computed exactly like
    # `sd_implant_layer`/`sd_implant_present` above, never part of the
    # request schema. See :func:`_ring_tap_implant_layer`.
    "ring_implant_layer",
    "ring_implant_present",
    # `res_array`'s own metal-layer-resistor geometry floors (issue #1639),
    # harness-computed from the resolved PDK family/`params.metal_level`
    # exactly like `cap_bottom_plate_margin_min_um`/friends above -- see
    # :data:`_PDK_METAL_RES_LEVEL_MIN_UM`. `params.metal_level` itself is
    # *not* hidden -- it is a real, request-facing param, exactly like
    # `flavor`.
    "metal_res_via_min_w_um",
    "metal_res_via_enclosure_min_um",
    "metal_res_via_space_min_um",
}

#: Minimum contact/via drawn size (um) used by every phase-2 generator --
#: exceeds both curated decks' contact-size rule (sky130's ``licon1`` has no
#: size rule at all; gf180mcu's ``contact.width.1`` is 0.22um).
CONTACT_SIZE_UM = 0.22

#: The coarsest grid (um) any generator's geometry is placed on -- the
#: ``dbu=0.001`` fallback every ``_GENERATOR_SPECS`` entry below declares.
#: Used by :func:`_snap_square_box_um` to pre-round a contact's centre to
#: the manufacturing grid *before* building its box, so the later,
#: independent per-edge ``int(round(x / dbu))`` conversion in
#: :func:`_insert_boxes` can never round one edge up and the other down and
#: silently draw a contact 1 dbu narrower/shorter than ``CONTACT_SIZE_UM``
#: -- exactly the ``contact.width.1`` failure mode diagnosed in issue #685
#: (``CONTACT_SIZE_UM`` has zero DRC margin above gf180mcu's ``CO.1``
#: threshold, so a single dbu of rounding slop is enough to trip it).
#:
#: This stays a *fixed* 0.001 even though :func:`_produce` now resolves the
#: output layout's own dbu from the resolved PDK's tech LEF (issue #1496,
#: e.g. 0.0005 for gf180mcu's ``DATABASE MICRONS 2000``), and it is exactly
#: as safe there: a coordinate pre-snapped to a 0.001 grid is *exactly*
#: representable on any finer dbu that divides 0.001, so ``int(round())``
#: introduces no error at all rather than the sub-dbu slop #685 fixed. It is
#: deliberately not re-derived per-request from the resolved dbu -- doing so
#: would move geometry (a finer snap grid moves a contact's centre) on the
#: very PDKs this constant was validated against.
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

#: Minimum spacing (um) between two well regions held at **different**
#: potentials -- the rule ``well_island`` (issue #1421) enforces between its
#: own drawn well and every other well region the caller names in
#: ``params.isolate_from``. Measured *euclidian* (corner-to-corner, not
#: per-axis), matching the metric the source rule itself is written with.
#:
#: This is deliberately **not** the ``space``-rule value either curated DRC
#: deck checks today, because neither deck checks the different-potential
#: case at all:
#:
#: - ``sky130``: 1.27um, the SkyWater sign-off rule ``nwell.2`` ("Minimum
#:   spacing between N-well and N-well of different potential"), the rule the
#:   PDK's own KLayout deck codes as ``nwell.isolated(1.27, euclidian)``.
#:   This repo's curated sky130 DRC deck (``klayout_tools.decks.sky130``)
#:   transcribes **no** well-layer rule whatsoever -- see issue #1420 -- so
#:   ``klt drc --deck sky130`` cannot catch two same-flavour well islands
#:   that merged. That gap is precisely why the *generator* enforces the
#:   separation itself rather than deferring to the checker: it is the source
#:   of truth for the geometry it draws.
#: - ``gf180mcu``: 1.4um, DRM 7.4 Nwell rule ``NW.2b`` ("Min. Nwell Space
#:   (Outside DNWELL) [Different potential]", 3.3V column). This value is not
#:   invented here -- it is the *same* number ``klayout_tools.decks.gf180mcu``'s
#:   own ``nwell.space.1`` docstring already cites as the half of the
#:   ``NW.2a``/``NW.2b`` split it deliberately does not transcribe ("our
#:   engine has no connectivity/netlist information, so this uses the less
#:   strict ``NW.2a`` value"). ``klt gen`` *does* know the intended potential
#:   (the caller names it), so it can apply the strict half.
#:
#: A family absent from this table has no known different-potential well rule
#: to enforce; ``well_island`` reports that via ``drc_hints.notes`` rather
#: than silently applying another family's number (see
#: :func:`_well_island_isolation`).
_PDK_WELL_ISOLATION_UM: dict[str, float] = {
    "sky130": 1.27,  # nwell.2 (different potential), euclidian
    "gf180mcu": 1.4,  # NW.2b (different potential), 3.3V
}

#: Per-PDK-family clearance (um) the unit device's gate-poly landing pad
#: (issue #461) is held clear of the diffusion's own gate-side edge, drawn as
#: a poly stem of exactly the gate length across the gap (issue #1450). See
#: :func:`_mos_unit_layout` for the geometry.
#:
#: The landing pad is a :data:`CONTACT_SIZE_UM` + 2 * :data:`ENCLOSURE_MARGIN_UM`
#: square -- wider than the gate stripe it sits on for every realistic gate
#: length -- so it overhangs the gate on both sides. With no clearance the pad
#: abuts the diffusion at ``y == w_um``, which puts the overhang's own
#: underside edge (an edge with no active area beneath it, unlike the channel
#: portion of the gate) facing unrelated ``active`` at *zero* lateral
#: distance. sky130's and gf180mcu's curated decks transcribe no
#: poly-to-unrelated-active spacing rule at all, so that has always been --
#: and remains -- legal there; sg13g2's does, and flags it.
#:
#: A family absent from this table gets ``0.0`` (see
#: :func:`_gate_pad_clearance_um`), i.e. the pre-#1450 geometry byte-for-byte:
#: this is deliberately *not* applied universally, because lifting the pad
#: also lifts the reported ``U<i>_G`` port (its ``y``, and with
#: ``gate_contact`` the drawn contact/local-metal stack too), which would move
#: every existing sky130/gf180mcu caller's gate port for no DRC benefit on
#: those families (issues #461/#492/#781 pin that contract).
#:
#: - ``sg13g2``: 0.1um, comfortably above ``klayout_tools.decks.sg13g2``'s
#:   ``gatpoly.separation.activ.1`` (`5_8_gatpoly.drc` rule ``Gat.d``,
#:   "Min. GatPoly space to Activ", 0.07um -- the binding rule) and equal to
#:   the enclosure budget :data:`ENCLOSURE_MARGIN_UM` every other margin in
#:   this module is already sized against, so the drawn value keeps the same
#:   ~40% headroom over the threshold that the rest of the generator's
#:   constants keep over theirs. Landing the pad this way is also closer to
#:   how a real device is drawn: the gate poly extends past the channel
#:   (an endcap) rather than stopping dead on the diffusion edge.
#: - ``gf180mcu`` (issue #1577): 0.35um, above gf180mcu's own real signoff
#:   deck's ``PL.5a_LV``/``PL.5b_LV`` ("Space from field Poly2 to
#:   unrelated/related COMP", 0.1um each) *and* their ``_MV`` (``Dualgate``
#:   -enclosed, medium-voltage) counterparts (0.3um each) -- none of the four
#:   transcribed by this repo's curated ``gf180mcu`` deck, see
#:   ``klayout_tools.decks.gf180mcu``. The clearance is structural geometry
#:   (the gate landing pad's own stand-off from the diffusion edge, see
#:   :func:`_mos_unit_layout`), not conditioned on ``params.voltage_flavor``,
#:   so it must clear the larger of the two thresholds unconditionally: a
#:   plain (non-``medium_voltage``) request draws the identical unit device a
#:   ``voltage_flavor="medium_voltage"`` request does, just without the
#:   ``Dualgate`` marker layered on top (see
#:   :data:`_PDK_VOLTAGE_FLAVOR_LAYERS`) -- so the same landing pad geometry
#:   has to satisfy whichever threshold a marker drawn *around* it would
#:   trigger. Without this clearance the #461 landing pad -- wider than the
#:   gate stripe it sits on -- abuts the diffusion's top edge directly, so
#:   its own side edges face *unrelated* ``Comp`` (the same diffusion, just
#:   outside the pad's own narrower footprint) at zero lateral distance:
#:   exactly the failure mode this table's own docstring already describes
#:   for sg13g2, just never verified against gf180mcu's real deck until
#:   #1577 (this repo's curated deck transcribes no poly-to-unrelated-active
#:   spacing rule for gf180mcu at all, so the gap was invisible to ``klt drc
#:   --deck gf180mcu``). Moving gf180mcu off its pre-#1577 ``0.0`` is a
#:   deliberate, real geometry change (the landing pad -- and therefore the
#:   reported ``U<i>_G`` port -- moves further off the diffusion edge),
#:   unlike every other value in this module that stays byte-for-byte
#:   pinned; see ``docs/cli/gen.md``'s ``mos_array`` gf180mcu note for the
#:   full before/after.
_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM: dict[str, float] = {
    "sg13g2": 0.1,  # Gat.d (GatPoly space to Activ) is 0.07um
    "gf180mcu": 0.35,  # PL.5a_MV/PL.5b_MV (field Poly2 to COMP, MV) are 0.3um
}


def _gate_pad_clearance_um(family: str) -> float:
    """Return ``family``'s gate-poly-landing-pad clearance (see
    :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM`), or ``0.0`` for a family that
    declares none -- the pre-#1450 geometry, byte-for-byte."""
    return _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM.get(family, 0.0)


#: Extra clearance (um) :func:`_mos_finger_positions` inserts on *each* side
#: of every gate stripe -- exactly like :func:`_sd_pad_gate_offset_um`'s own
#: small-``l_um`` makeup padding (issue #1187), but as a *family-specific
#: floor* applied regardless of ``l_um`` (:func:`_sd_pad_gate_offset_um`
#: takes the ``max`` of the two, so whichever one asks for more wins).
#:
#: ``gf180mcu`` only (issue #1577): 0.08um, closing the gap gf180mcu's real
#: signoff deck's ``CO.7`` ("Space from COMP contact to Poly2 on COMP",
#: 0.15um -- not transcribed by this repo's curated ``gf180mcu`` deck) finds
#: on every S/D contact. Without this floor, a contact's nearest edge sits
#: exactly :data:`ENCLOSURE_MARGIN_UM` (0.1um) from the gate poly's own edge
#: -- the enclosure budget the contact's *own* pad sizing already spends, not
#: a contact-to-*gate* spacing budget, and short of CO.7's 0.15um by 0.05um.
#: 0.08um closes that with headroom (0.1 + 0.08 = 0.18um > 0.15um). A family
#: absent from this table gets ``0.0`` (see
#: :func:`_contact_gate_extra_offset_um`): sky130's/sg13g2's/sg13cmos5l's
#: curated decks all check no equivalent contact-to-gate spacing rule this
#: constant would need to close, so every existing caller there keeps
#: byte-for-byte identical geometry.
_PDK_CONTACT_GATE_EXTRA_OFFSET_UM: dict[str, float] = {
    "gf180mcu": 0.08,  # CO.7 (COMP contact to Poly2 on COMP) is 0.15um
}


def _contact_gate_extra_offset_um(family: str) -> float:
    """Return ``family``'s contact-to-gate extra offset floor (see
    :data:`_PDK_CONTACT_GATE_EXTRA_OFFSET_UM`), or ``0.0`` for a family that
    declares none -- byte-for-byte unchanged geometry there."""
    return _PDK_CONTACT_GATE_EXTRA_OFFSET_UM.get(family, 0.0)


#: Extra downward extension (um) :func:`_mos_unit_layout`/
#: :func:`_mos_unit_strapped_layout` draw on every gate stripe's *bottom*
#: edge (``y == 0`` for the bare "series" channel, ``y == diff_y0`` for the
#: strapped comb) -- a symmetric counterpart to the *top* edge's own landing
#: pad (issue #461), which already extends well past the diffusion there.
#:
#: ``gf180mcu`` only (issue #1577): 0.25um, closing two real signoff-deck
#: rules this repo's curated ``gf180mcu`` deck never transcribes, both of
#: which fire on the *bottom* edge only (the top edge's own #461 landing pad
#: already clears both, verified against the real deck):
#:
#: - ``PL.4_LV`` ("Extension beyond COMP to form Poly2 end cap", 0.22um):
#:   with no extension, the bottom-edge channel poly stops exactly flush
#:   with the diffusion's own bottom edge -- a zero-margin endcap.
#: - ``DF.6_LV`` ("Min. COMP extend beyond gate", 0.24um): the same flush
#:   coincidence reads, from COMP's own side, as COMP failing to clear the
#:   gate poly by any margin at all. Once the poly actually extends past
#:   COMP's edge (as the top edge's landing pad already does), gf180mcu's
#:   real deck does not flag this direction -- confirmed empirically (see
#:   issue #1577), not merely inferred from the two rules' stated
#:   thresholds.
#:
#: A family absent from this table gets ``0.0`` (see
#: :func:`_gate_bottom_endcap_um`): sky130's/sg13g2's/sg13cmos5l's curated
#: decks transcribe neither rule, so every existing caller there keeps
#: byte-for-byte identical geometry.
_PDK_GATE_BOTTOM_ENDCAP_UM: dict[str, float] = {
    "gf180mcu": 0.25,  # PL.4_LV (0.22um) / DF.6_LV (0.24um)
}


def _gate_bottom_endcap_um(family: str) -> float:
    """Return ``family``'s gate-stripe bottom-edge endcap extension (see
    :data:`_PDK_GATE_BOTTOM_ENDCAP_UM`), or ``0.0`` for a family that
    declares none -- byte-for-byte unchanged geometry there."""
    return _PDK_GATE_BOTTOM_ENDCAP_UM.get(family, 0.0)


#: Margin (um) :func:`_mos_unit_layout`/:func:`_mos_unit_strapped_layout`
#: grow a source/drain implant box beyond the unit device's own ``active``
#: box, on every side, when the resolved PDK family needs one (see
#: :func:`_device_layer_params`'s ``sd_implant_layer``/``sd_implant_present``
#: -- the family's ``"nplus"``/``"pplus"`` role, selected by ``flavor``).
#:
#: ``gf180mcu`` only (issue #1577): 0.25um, closing ``DF.12`` ("COMP not
#: covered by Nplus or Pplus is forbidden") -- this repo's curated
#: ``gf180mcu`` deck draws no implant over a unit device's body at all today,
#: so a bare ``Comp`` shape violates it unconditionally, independent of any
#: margin value. 0.25um is not the DF.12 margin itself (DF.12 is a coverage
#: rule, not a distance -- covering ``active`` exactly, margin ``0.0``,
#: already satisfies it) but the margin two *other* real rules need once the
#: implant exists at all and starts covering the gate the same way it covers
#: the source/drain (``NP.5a``/``PP.5a``, "Overlap of N-channel/P-channel
#: gate", 0.23um each -- the implant must either not touch the gate poly at
#: all, or enclose it with 0.23um margin; since DF.12 forces full ``active``
#: coverage, only the second option is available, and the gate spans
#: ``active``'s own full height with *zero* margin at ``y == 0``/``y ==
#: w_um``). 0.25um clears that with headroom and comfortably exceeds
#: ``NP.5b``/``PP.5b``'s smaller 0.16um COMP-extension margin too.
#:
#: A family absent from this table gets ``0.0`` (see
#: :func:`_sd_implant_margin_um`), and :func:`_device_layer_params` never
#: resolves a ``sd_implant_layer`` for one either (no family besides
#: gf180mcu declares the ``"nplus"``/``"pplus"`` roles) -- sky130's/
#: sg13g2's/sg13cmos5l's curated decks all recognise a MOS device from
#: ``active``/``well`` alone, with no implant mask at all, so every existing
#: caller there keeps byte-for-byte identical geometry (no implant drawn).
_PDK_SD_IMPLANT_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 0.25,  # NP.5a/PP.5a (gate overlap) are 0.23um
}


def _sd_implant_margin_um(family: str) -> float:
    """Return ``family``'s source/drain implant margin (see
    :data:`_PDK_SD_IMPLANT_MARGIN_UM`), or ``0.0`` for a family that declares
    none -- byte-for-byte unchanged geometry there (no implant drawn)."""
    return _PDK_SD_IMPLANT_MARGIN_UM.get(family, 0.0)


#: Margin (um) the ``voltage_flavor`` marker box (:data:`_PDK_VOLTAGE_FLAVOR_LAYERS`,
#: issue #1054) grows beyond the array/pair's own shared active footprint --
#: normally the *same* box :data:`WELL_ENCLOSURE_MARGIN_UM` sizes the
#: ``flavor="pfet"`` well shape to (they are independent params, but nothing
#: before #1577 needed the marker any bigger than the well).
#:
#: ``gf180mcu`` only (issue #1577): the real signoff deck's ``DV.6``/``DV.8``
#: ("Min. Dualgate enclose COMP" 0.24um / "Min. Dualgate enclose Poly2"
#: 0.4um) both need *more* than :data:`WELL_ENCLOSURE_MARGIN_UM` (0.15um) --
#: DV.6 because 0.15 < 0.24 outright, and DV.8 because ``Poly2`` (the #461
#: landing pad -- stood off the diffusion edge by
#: :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM`'s own gf180mcu clearance, plus
#: this issue's own :data:`_PDK_GATE_BOTTOM_ENDCAP_UM` extension on the
#: bottom edge) reaches further past the shared active footprint than the
#: well margin alone covers: the pad's top overhang is
#: ``_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM``'s gf180mcu clearance (0.35um) plus
#: ``CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM`` (0.42um) == 0.77um past the
#: diffusion edge, so the marker needs at least ``0.77 + 0.4 == 1.17um`` of
#: headroom on the top edge to enclose the *worst-case* row's own poly with
#: DV.8's own margin. 1.2um (used for every side, not just top, for one
#: uniform box -- the same "one box, one margin" shape
#: :data:`WELL_ENCLOSURE_MARGIN_UM` already uses) clears that with a little
#: headroom; verified against the real deck on ``mos_array``'s documented
#: default params (issue #1577) -- a caller who additionally raises
#: ``gate_contact``/``fingers`` beyond that default grows the landing pad's
#: own footprint further and is not verified against this fixed margin.
#:
#: A family absent from this table falls back to :data:`WELL_ENCLOSURE_MARGIN_UM`
#: (see :func:`_voltage_flavor_mark_margin_um`) -- sky130/sg13g2/sg13cmos5l's
#: own ``voltage_flavor`` markers (``esd_mark``'s ``Dualgate``/``ThickGateOx``
#: reuse, and gf180mcu's own pre-#1577 marker) never needed a bigger box, so
#: every existing caller there keeps byte-for-byte identical geometry.
_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 1.2,  # DV.8 (Dualgate enclose Poly2) is 0.4um past a
    # landing pad whose own top overhang (_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM's
    # gf180mcu clearance, 0.35um, plus CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM,
    # 0.42um) already reaches 0.77um past the shared active footprint.
}


def _voltage_flavor_mark_margin_um(family: str) -> float:
    """Return ``family``'s ``voltage_flavor`` marker box margin (see
    :data:`_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`), or
    :data:`WELL_ENCLOSURE_MARGIN_UM` (the pre-#1577 behaviour -- the same box
    the well shape uses) for a family that declares none."""
    return _PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM.get(family, WELL_ENCLOSURE_MARGIN_UM)


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
        # issue #1058; Metal4-5/`vias[2:]` still stay unexposed here (out of
        # that issue's scope, not a structural limit -- see
        # `EXTRACTION_DECK.metals`/`.vias` in `klayout_tools.decks.gf180mcu`).
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
        # with a landing pad on Metal2 in between. Metal4-5/`vias[2:]` stay
        # unexposed here (out of this issue's scope, not a structural limit
        # -- see `EXTRACTION_DECK.metals`/`.vias` in
        # `klayout_tools.decks.gf180mcu`).
        "metal3": (42, 0),  # Metal3 -- EXTRACTION_DECK.metals[2]
        "via2": (38, 0),  # Via2 -- EXTRACTION_DECK.vias[1] (Metal2<->Metal3)
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
#: - ``sg13g2`` (issue #1455): ``cap_top_via_metal_min_w_um`` 1.64um --
#:   `topmetal1.width.1` (`klayout_tools.decks.sg13g2`'s `5_22_topmetal1.drc`
#:   rule "TM1.a", "Min. TopMetal1 width"), far coarser than sky130's
#:   `met4.width.1` (0.3um) or gf180mcu's own metal-width rules the generic
#:   pad size was originally sized against. Without this floor, `cap_array`'s
#:   default-sized landing pad would violate `klt drc --deck sg13g2` on every
#:   request.
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


def _cap_geometry_min_um(family: str) -> dict[str, float]:
    """Every :data:`_CAP_GEOMETRY_MIN_KEYS` geometry floor (um) resolved for
    ``family`` (see :data:`_PDK_CAP_GEOMETRY_MIN_UM`), with ``0.0`` -- "no
    floor, draw the generic geometry" -- for each key that family does not
    override."""
    floors = _PDK_CAP_GEOMETRY_MIN_UM.get(family, {})
    return {key: floors.get(key, 0.0) for key in _CAP_GEOMETRY_MIN_KEYS}


#: Per-PDK-family extra mask layers that must *also* cover `cap_array`'s
#: top plate for the drawn stack to be recognised as that family's MiM
#: capacitor -- the capacitor sibling of :data:`_PDK_RES_FLAVOR_LAYERS`'s
#: per-flavour ``requires`` masks, and the generator-side counterpart of
#: :class:`~klayout_tools.decks.CapacitorDevice`'s own ``top_plate_requires``
#: field (which ``extract.py`` applies as a mandatory AND: every listed layer
#: must also cover the top plate for the device to be recognised).
#:
#: Like that table, these are the *same* layer/datatype pairs the curated
#: *extraction* deck keys recognition off, never a second private map. A
#: family absent from this table (sky130's `cap_mim`, sg13g2's `cap_cmim` --
#: neither sets `top_plate_requires`) draws no extra mask at all, exactly as
#: before issue #1555.
#:
#: - ``gf180mcu``: `CAP_MK` (117/5) + `MIM_L_MK` (117/10), from
#:   `klayout_tools.decks.gf180mcu.EXTRACTION_DECK.capacitors[0]`
#:   (`cap_mim_2f0_m4m5_noshield`)'s own ``top_plate_requires``, mirroring the
#:   PDK's own official derivation (`mimcap_extraction.lvs`'s ``fuse_cap =
#:   fusetop.interacting(cap_mk).interacting(mim_l_mk)``). Without them a
#:   drawn `FuseTop`/`Metal4` stack is geometrically plausible but extracts as
#:   *zero* capacitors. That deck's `top_plate_excludes` (`efuse_mk`/
#:   `plfuse`) need no counterpart here: this generator simply never draws
#:   either layer, so the exclusion is satisfied by construction. No curated
#:   *DRC* rule in that deck checks 117/5 or 117/10, so drawing them never
#:   affects `klt drc --deck gf180mcu` status -- the same "the layer is real,
#:   just uncheckable for its own purpose" situation :data:`_PDK_ROLE_LAYERS`'
#:   own `res_mark`/`bjt_mark` entries document.
_PDK_CAP_TOP_PLATE_REQUIRES: dict[str, tuple[tuple[int, int], ...]] = {
    "gf180mcu": (
        (117, 5),  # CAP_MK
        (117, 10),  # MIM_L_MK
    ),
}

#: The widest entry in :data:`_PDK_CAP_TOP_PLATE_REQUIRES` -- how many
#: ``cap_top_requires_<i>_*`` slots ``_CapArrayPCell`` declares. Derived from
#: the table rather than hard-coded (mirrors :data:`_MAX_RES_FLAVOR_LAYERS`),
#: so a future family needing more masks needs no PCell change: today ``2``
#: (gf180mcu's `CAP_MK`/`MIM_L_MK`).
_MAX_CAP_TOP_PLATE_REQUIRES = max(
    (len(layers) for layers in _PDK_CAP_TOP_PLATE_REQUIRES.values()), default=0
)

#: ``cap_array``'s top-plate requires-mask slots are generated from
#: :data:`_MAX_CAP_TOP_PLATE_REQUIRES` (which depends on the table above), so
#: -- exactly like ``res_array``'s own ``res_flavor_<i>_*`` slots -- they join
#: :data:`_HIDDEN_PARAMS` here rather than being listed literally at its own
#: definition.
_HIDDEN_PARAMS |= {
    f"cap_top_requires_{i}_{suffix}"
    for i in range(_MAX_CAP_TOP_PLATE_REQUIRES)
    for suffix in ("layer", "present")
}


def _cap_top_plate_requires(family: str) -> tuple[tuple[int, int], ...]:
    """The ordered extra mask layers that must cover ``family``'s MiM top
    plate for it to be recognised (see
    :data:`_PDK_CAP_TOP_PLATE_REQUIRES`), or an empty tuple for a family
    whose curated deck sets no ``top_plate_requires``."""
    return _PDK_CAP_TOP_PLATE_REQUIRES.get(family, ())


#: Generators whose current geometry is not wired up for a family that
#: :func:`_pdk_family` otherwise resolves successfully -- distinct from an
#: unresolvable family (:func:`_pdk_family`'s own error, e.g. a variant
#: matching no family at all) and from a per-role gap a generator already
#: checks explicitly (:func:`_cap_family_layers`'s ``cap_top_plate``/
#: ``cap_bottom_plate`` check; :func:`_bond_pad_layer_params`'s own
#: ``pad``/``top_metal`` check below). sg13g2 (issue #1448) resolves every
#: role :func:`_bjt_layer_params`/:func:`_esd_device_layer_params`/
#: :func:`_well_island_layer_params` treat as mandatory (``active``/
#: ``contact``/``metal``/``tap``, all populated above for `guard_ring`'s own
#: sake) -- so, unlike ``cap_array``/``bond_pad``, none of those three would
#: raise or crash on their own. They are deferred here as a deliberate scope
#: decision instead: this deck's `EXTRACTION_DECK` declares no bipolar
#: device recognition at all (`bjt_array`), and `esd_device`/`well_island`
#: were simply not attempted by this issue (see `docs/cli/gen.md`'s
#: "PDK-family support" section) -- not a discovered defect in either.
#:
#: sg13cmos5l (issue #1462) adds ``bjt_array``/``esd_device`` for the same
#: "no bipolar device class"/"not attempted" reasons as sg13g2's own entries,
#: plus ``well_island`` (same "not attempted" reason). ``guard_ring``/
#: ``diff_pair`` are *also* deferred here for this family -- unlike
#: sg13g2, where both resolve every mandatory role fine (``"tap"`` there is
#: real, if shared with ``"active"``): this family's own
#: :data:`_PDK_ROLE_LAYERS` entry deliberately carries no ``"tap"`` role at
#: all (see that entry's own comment), so ``guard_ring``'s ring layer
#: resolution would otherwise pass a ``None`` ``tap_layer`` straight to a
#: PCell parameter that requires a real ``kdb.LayerInfo`` -- an opaque
#: PCell-level crash rather than this module's own clear ``GenError``.
#: Deferring both here up front (via :func:`_ring_layer_params`'s own
#: ``generator_name``-parametrized call, since ``diff_pair`` composes the
#: same ring-layer resolution ``guard_ring`` uses) keeps that failure
#: legible instead.
#:
#: ``mos_array``'s own ``sg13cmos5l`` entry (issue #1493) is *conditional* in
#: a way none of the entries above are: :func:`_mos_array_layer_params` only
#: consults this table (via :func:`_ring_layer_params`'s
#: ``generator_name="mos_array"`` call) when the request actually sets
#: ``params.add_guard_ring`` -- every existing no-ring ``mos_array`` request
#: on this family (predating #1493) never reaches this check at all, so it
#: keeps working unchanged. Only a request that *does* ask for
#: ``add_guard_ring`` on this family hits this entry, turning what would
#: otherwise be the same opaque ``None``-``tap_layer`` PCell crash
#: ``guard_ring``/``diff_pair`` avoid above into this module's own clear
#: ``GenError``.
_GENERATOR_FAMILY_DEFERRED: dict[str, tuple[str, ...]] = {
    "bjt_array": ("sg13g2", "sg13cmos5l"),
    "esd_device": ("sg13g2", "sg13cmos5l"),
    "well_island": ("sg13g2", "sg13cmos5l"),
    "guard_ring": ("sg13cmos5l",),
    "diff_pair": ("sg13cmos5l",),
    "mos_array": ("sg13cmos5l",),
}


def _reject_deferred_family(generator_name: str, family: str) -> None:
    """Raise :class:`GenError` when ``generator_name`` explicitly defers
    ``family`` (see :data:`_GENERATOR_FAMILY_DEFERRED`) -- a no-op for every
    other generator/family pair."""
    if family in _GENERATOR_FAMILY_DEFERRED.get(generator_name, ()):
        raise GenError(
            f"generator '{generator_name}': PDK family '{family}' is not "
            "yet supported by this generator -- see docs/cli/gen.md's "
            "'PDK-family support' section"
        )


#: Per-PDK-family poly-resistor *flavour* -> the ordered set of extra mask
#: layers that flavour's recognised device class requires over each unit's
#: body segment, selected by ``res_array``'s ``flavor`` request param (issue
#: #463). Unlike the always-single roles in :data:`_PDK_ROLE_LAYERS`, a poly
#: resistor's recognised device *class* is chosen by which implant/precision-
#: resistor masks cover its body, and a single PDK family exposes several such
#: classes -- with a *different number* of masks per class (issue #1451): from
#: none at all (sky130's ``res_generic_po``) up to four (sg13g2's ``rhigh``).
#: Each entry is therefore a plain ordered tuple of ``(layer, datatype)``
#: pairs, drawn in full over every unit body; an empty tuple means the flavour
#: needs no mask beyond the shared ``res_mark`` marker. (Before #1451 this was
#: a fixed two-slot ``res_implant``/``res_block`` dict, which structurally
#: could not express sg13g2's three- and four-mask classes.)
#:
#: The layer/purpose numbers below are the *same* ``requires`` layers the
#: curated *extraction* decks key each flavour off -- never a second, private
#: map: sky130's ``res_high_po``/``res_xhigh_po`` from
#: ``klayout_tools.decks.sky130.EXTRACTION_DECK.resistors`` (issue #222/#299),
#: gf180mcu's ``ppolyf_u``/``ppolyf_u_1k``/``_2k``/``_3k`` (issues #369/#1550),
#: and sg13g2's ``rsil``/``rppd``/``rhigh`` (issues #1448/#1451). The
#: first-listed flavour per family is that family's default
#: (``_DEFAULT_RES_FLAVOR``), chosen so a request that never mentions
#: ``flavor`` reproduces the pre-#463 geometry
#: exactly (sky130 -> ``res_generic_po``, gf180mcu -> ``ppolyf_u``, sg13g2 ->
#: ``rsil``).
_PDK_RES_FLAVOR_LAYERS: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
    "sky130": {
        # sky130_fd_pr__res_generic_po (48.2 ohm/sq): the poly.res marker
        # alone, no implant/block -- the base, lowest-sheet-rho flavour.
        "generic": (),
        # sky130_fd_pr__res_high_po_* (319.8 ohm/sq): psdm P+ implant + rpm
        # precision-resistor mask (EXTRACTION_DECK.resistors["res_high_po"].
        # requires).
        "high": (
            (94, 20),  # psdm -- P+ implant
            (86, 20),  # rpm  -- 300 ohm precision-resistor mask
        ),
        # sky130_fd_pr__res_xhigh_po_* (2 kohm/sq): psdm P+ implant + urpm
        # (EXTRACTION_DECK.resistors["res_xhigh_po"].requires). rpm/urpm are
        # mutually exclusive, so drawing urpm (not rpm) selects xhigh.
        "xhigh": (
            (94, 20),  # psdm -- P+ implant
            (79, 20),  # urpm -- 2 kohm precision-resistor mask
        ),
    },
    "gf180mcu": {
        # ppolyf_u: gf180mcu's base drawn-poly-resistor flavour, which
        # requires both Pplus (implant) and SAB (salicide block) over the
        # RES_MK marker (issue #369).
        "generic": (
            (31, 0),  # Pplus -- P+ implant
            (49, 0),  # SAB   -- salicide block
        ),
        # High-sheet-rho flavour (issue #1550): all three named values below
        # draw identical geometry -- ppolyf_u_1k/_2k/_3k are distinguished
        # only by `klt extract --deck-option poly_res=<value>`, not by any
        # drawn layer (see `ResistorDevice(name="ppolyf_u_1k",
        # flavour_option="poly_res", ...)` in
        # `klayout_tools.decks.gf180mcu`). Each requires exactly SAB + the
        # Resistor high-sheet-rho marker -- no Pplus, matching that device's
        # own `requires` tuple precisely (Pplus is neither required nor
        # excluded there, so omitting it keeps this entry minimal).
        "1k": (
            (49, 0),  # SAB      -- unsalicided, same as generic
            (62, 0),  # Resistor -- high-sheet-rho marker
        ),
        "2k": (
            (49, 0),  # SAB
            (62, 0),  # Resistor
        ),
        "3k": (
            (49, 0),  # SAB
            (62, 0),  # Resistor
        ),
    },
    "sg13g2": {
        # All three entries below come straight from
        # `klayout_tools.decks.sg13g2.EXTRACTION_DECK.resistors`' own
        # `requires` sets, beyond the `res_mark` (PolyRes) marker every
        # flavour shares. Each class's `excludes` set is what keeps them
        # mutually unambiguous without this generator having to model
        # exclusions: rsil excludes pSD/SalBlock/nSD, and rppd excludes
        # nSD/nSD_block, so drawing exactly one flavour's `requires` set can
        # only ever match that flavour.
        #
        # rsil (7 ohm/sq): the lowest-sheet-rho class, and this family's
        # default -- `EXTRACTION_DECK.resistors["rsil"].requires`.
        "generic": (
            (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
            (24, 0),  # Res       -- the silicided-resistor marker itself
        ),
        # rppd (260 ohm/sq): p+ doped, unsalicided poly --
        # `EXTRACTION_DECK.resistors["rppd"].requires` (issue #1451). Three
        # masks; rsil's own `excludes` (pSD, SalBlock) rule it out here.
        "rppd": (
            (111, 0),  # EXTBlock
            (14, 0),  # pSD      -- p+ doped poly
            (28, 0),  # SalBlock -- unsalicided (vs. rsil's 7 ohm/sq)
        ),
        # rhigh (1360 ohm/sq): the highest-sheet-rho class --
        # `EXTRACTION_DECK.resistors["rhigh"].requires` (issue #1451). Four
        # masks: unlike rppd it requires nSD present *alongside* pSD (the
        # doping combination its higher sheet rho comes from), which is also
        # exactly what rppd's own `excludes` subtracts.
        "rhigh": (
            (111, 0),  # EXTBlock
            (14, 0),  # pSD      -\ both implants present together
            (7, 0),  # nSD       -/
            (28, 0),  # SalBlock -- unsalicided, same as rppd
        ),
    },
    "sg13cmos5l": {
        # All three entries below come straight from
        # `klayout_tools.decks.sg13cmos5l.EXTRACTION_DECK.resistors`' own
        # `requires` sets (issue #1462) -- byte-identical to `sg13g2`'s own
        # table above, since cmos5l's resistor LVS rules are themselves
        # symlinks into the pinned sibling `ihp-sg13g2` checkout (see that
        # deck module's own docstring), independently re-verified against
        # cmos5l's own `EXTRACTION_DECK.resistors` rather than assumed
        # identical by analogy.
        #
        # `"generic"` is this family's default (`_DEFAULT_RES_FLAVOR`),
        # matching every other family's own "first entry is `\"generic\"`"
        # convention -- `rsil` (7 ohm/sq) is this family's lowest-sheet-rho
        # class, `EXTRACTION_DECK.resistors["rsil"].requires`.
        "generic": (
            (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
            (24, 0),  # RES       -- the silicided-resistor marker itself
        ),
        # `"rsil"` is a deliberate alias of `"generic"` above (identical
        # mask set) -- issue #1462's own acceptance criteria and test plan
        # request `res_array`'s `flavor` param by this family's literal
        # device-class name (`flavor="rsil"`), not only via the
        # family-agnostic `"generic"` spelling every other family exposes
        # for its own lowest-sheet-rho class. Both names resolve to the same
        # `requires` set, so `flavor="rsil"` and an unspecified `flavor`
        # (which defaults to `_DEFAULT_RES_FLAVOR`, `"generic"`) draw
        # byte-identical geometry.
        "rsil": (
            (111, 0),  # EXTBlock
            (24, 0),  # RES
        ),
        # rppd (260 ohm/sq): p+ doped, unsalicided poly --
        # `EXTRACTION_DECK.resistors["rppd"].requires`. Three masks; rsil's
        # own `excludes` (pSD, SalBlock, nSD) rule it out here.
        "rppd": (
            (111, 0),  # EXTBlock
            (14, 0),  # pSD      -- p+ doped poly
            (28, 0),  # SalBlock -- unsalicided (vs. rsil's 7 ohm/sq)
        ),
        # rhigh (1360 ohm/sq): the highest-sheet-rho class --
        # `EXTRACTION_DECK.resistors["rhigh"].requires`. Four masks: unlike
        # rppd it requires nSD present *alongside* pSD (the doping
        # combination its higher sheet rho comes from).
        "rhigh": (
            (111, 0),  # EXTBlock
            (14, 0),  # pSD      -\ both implants present together
            (7, 0),  # nSD       -/
            (28, 0),  # SalBlock -- unsalicided, same as rppd
        ),
    },
}

#: The widest flavour in :data:`_PDK_RES_FLAVOR_LAYERS` -- how many
#: ``requires``-mask slots ``_ResArrayPCell`` declares (a KLayout PCell's
#: parameter list is static, so the slot count has to be fixed at class-
#: declaration time). Derived from the table rather than hard-coded, so
#: adding a flavour with more masks than any current one needs no PCell
#: change: today ``4`` (sg13g2's ``rhigh``).
_MAX_RES_FLAVOR_LAYERS = max(
    (
        len(layers)
        for family in _PDK_RES_FLAVOR_LAYERS.values()
        for layers in family.values()
    ),
    default=0,
)

#: ``res_array``'s flavour mask slots are generated from
#: :data:`_MAX_RES_FLAVOR_LAYERS` (which depends on the table above), so they
#: join :data:`_HIDDEN_PARAMS` here rather than being listed literally at its
#: own definition -- they are harness-computed layer params like every other
#: name in that set, never part of the request schema.
_HIDDEN_PARAMS |= {
    f"res_flavor_{i}_{suffix}"
    for i in range(_MAX_RES_FLAVOR_LAYERS)
    for suffix in ("layer", "present")
}

#: The default ``res_array`` flavour: the first flavour listed for each family
#: in :data:`_PDK_RES_FLAVOR_LAYERS`. Every family's first entry is keyed
#: ``"generic"``, so one default name works across families while resolving to
#: each family's own base device class.
_DEFAULT_RES_FLAVOR = "generic"


def _res_flavor_layers(family: str, flavor: str) -> tuple[tuple[int, int], ...]:
    """Return the ordered ``requires``-mask layer pairs for ``flavor`` in
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


#: Per-PDK-family metal-layer resistor levels (issue #1639): the ordered
#: ``body``/``marker``/``via``/``landing`` layer set ``res_array``'s
#: ``metal_level`` request param selects between, keyed ``1``..``5`` (sky130's
#: ``met1``..``met5``). Unlike :data:`_PDK_RES_FLAVOR_LAYERS`'s poly-body
#: flavours (which only ever add *extra* masks over one fixed poly body), a
#: metal-layer resistor's recognised device class (sky130's
#: ``res_generic_m1``..``res_generic_m5``,
#: ``klayout_tools.decks.sky130.EXTRACTION_DECK.resistors``) swaps the body
#: layer itself -- ``metal_level=0`` (the default) leaves ``res_array``'s
#: original poly-body geometry byte-for-byte unchanged; ``1``..``5`` instead
#: draw the body on ``metN.drawing``, its own resistor-ID marker
#: (``metN.res``), and an end via/landing-pad stack connecting *down* to the
#: metal level immediately below (``li1`` for ``met1``, ``metN-1`` otherwise)
#: -- the same "contact + local-metal landing pad at each end" shape
#: :func:`_res_unit_layout` already draws for the poly case, just relocated
#: one or more levels up the stack. Landing on the layer below (never above)
#: keeps a single, uniform code path across all five levels: sky130 always has
#: a metal level immediately below (down to ``li1``), while only ``met1``..
#: ``met4`` have one immediately above.
#:
#: Each entry's layer/datatype pairs are the *same* ones
#: ``klayout_tools.decks.sky130.EXTRACTION_DECK`` already declares -- never a
#: second, private map:
#:
#: - ``body``/``marker`` -- the exact ``ResistorDevice.body``/``.marker`` pair
#:   for that level's ``res_generic_mN`` entry.
#: - ``via`` -- ``EXTRACTION_DECK.vias[N - 1]``, the via connecting ``metN``
#:   down to the level below (``mcon`` for ``met1``; ``via``/``via2``/
#:   ``via3``/``via4`` for ``met2``..``met5``).
#: - ``landing`` -- ``EXTRACTION_DECK.metals[N - 1]``, the conductor that via
#:   lands on (``li1`` for ``met1``; ``met1``..``met4`` for ``met2``..
#:   ``met5``).
#:
#: Only ``sky130`` is populated today -- neither gf180mcu's nor sg13g2's
#: curated decks declare a drawn metal-resistor device class (see
#: ``klayout_tools.decks.gf180mcu``/``.sg13g2``'s own ``resistors`` tuples), so
#: a ``metal_level`` request against either raises :class:`GenError` via
#: :func:`_metal_res_layers` rather than silently drawing unrecognised
#: geometry.
_PDK_METAL_RES_LEVELS: dict[str, dict[int, dict[str, tuple[int, int]]]] = {
    "sky130": {
        1: {
            "body": (68, 20),  # met1.drawing
            "marker": (68, 13),  # met1.res
            "via": (67, 44),  # mcon.drawing (li1<->met1)
            "landing": (67, 20),  # li1.drawing
        },
        2: {
            "body": (69, 20),  # met2.drawing
            "marker": (69, 13),  # met2.res
            "via": (68, 44),  # via.drawing (met1<->met2)
            "landing": (68, 20),  # met1.drawing
        },
        3: {
            "body": (70, 20),  # met3.drawing
            "marker": (70, 13),  # met3.res
            "via": (69, 44),  # via2.drawing (met2<->met3)
            "landing": (69, 20),  # met2.drawing
        },
        4: {
            "body": (71, 20),  # met4.drawing
            "marker": (71, 13),  # met4.res
            "via": (70, 44),  # via3.drawing (met3<->met4)
            "landing": (70, 20),  # met3.drawing
        },
        5: {
            "body": (72, 20),  # met5.drawing
            "marker": (72, 13),  # met5.res
            "via": (71, 44),  # via4.drawing (met4<->met5)
            "landing": (71, 20),  # met4.drawing
        },
    },
}

#: The highest ``metal_level`` any currently-registered family supports --
#: ``res_array``'s own structural upper bound (see :func:`_res_array_validate`),
#: independent of which PDK family a given request eventually resolves to
#: (mirrors ``ESD_FINGER_WIDTH_MAX_UM``'s "generator-side structural floor,
#: not the target PDK's own rule" precedent). Derived from the table above
#: rather than hard-coded, so a future family adding a sixth level needs no
#: ``validate()`` change.
_MAX_METAL_RES_LEVEL = max(
    (level for levels in _PDK_METAL_RES_LEVELS.values() for level in levels),
    default=0,
)


def _metal_res_layers(family: str, level: int) -> dict[str, tuple[int, int]]:
    """Return ``level``'s ``body``/``marker``/``via``/``landing`` layer pairs
    for ``family`` (see :data:`_PDK_METAL_RES_LEVELS`).

    Raises :class:`GenError` for a family with no metal-resistor levels
    configured at all, or a ``level`` outside the ones that family declares --
    mirrors :func:`_res_flavor_layers`'s own unsupported-value error."""
    levels = _PDK_METAL_RES_LEVELS.get(family)
    if not levels:
        raise GenError(
            "generator 'res_array': params.metal_level requires a PDK family "
            "with a drawn metal-resistor device class -- supported families: "
            f"{', '.join(sorted(_PDK_METAL_RES_LEVELS)) or '(none)'}"
        )
    try:
        return levels[level]
    except KeyError:
        raise GenError(
            f"generator 'res_array': params.metal_level {level} is not a "
            f"recognised metal-resistor level for PDK family '{family}' -- "
            f"supported levels: {', '.join(str(n) for n in sorted(levels))}"
        ) from None


#: Per-PDK-family, per-``metal_level`` geometry floors (um) for the end
#: via/landing-pad stack :func:`_res_unit_layout` draws when ``metal_level``
#: is set (issue #1639) -- the metal-resistor sibling of
#: :data:`_PDK_CAP_GEOMETRY_MIN_UM`, applied the same ``max(generic, floor)``
#: way so a level/family absent from this table draws exactly the generic
#: :data:`CONTACT_SIZE_UM`/:data:`ENCLOSURE_MARGIN_UM`/
#: :data:`MIN_SAME_LAYER_SPACING_UM` geometry :func:`_res_unit_layout` already
#: uses for the poly case.
#:
#: Keys (all optional per level, ``0.0`` when absent -- see
#: :func:`_metal_res_geometry_min_um`):
#:
#: - ``via_min_w_um`` -- minimum drawn side of the end via connecting the
#:   body down to the landing metal.
#: - ``via_enclosure_min_um`` -- minimum enclosure of that via by *both* the
#:   body and the landing metal (the stricter of sky130's two per-via
#:   enclosure rules is used for both sides, matching this deck's own
#:   documented "primary rule only" approximation elsewhere).
#: - ``via_space_min_um`` -- minimum spacing between two adjacent unit
#:   resistors' end vias *and* between their body-layer shapes (the stricter
#:   of the two binds, since the body spans the full unit -- including both
#:   end vias -- so widening ``params.spacing_um`` widens both gaps at once),
#:   floored under ``params.spacing_um`` exactly like
#:   :data:`_PDK_CAP_GEOMETRY_MIN_UM`'s own ``cap_min_spacing_um``.
#: - ``body_width_min_um`` -- minimum drawn width (``params.width_um``) this
#:   level's own body-layer width rule requires. Unlike the three keys above,
#:   this is *never* applied to drawn geometry (``width_um`` stays exactly
#:   what the request asks for, matching every other generator's "the caller
#:   owns the primary requested dimension" convention -- see `mos_array`'s
#:   `w_um`/`cap_array`'s `plate_w_um`/`plate_h_um`) -- it is surfaced as a
#:   ``drc_hints.notes`` entry instead when the request falls short, mirroring
#:   the existing ``spacing_um``-below-margin note.
#:
#: sky130 levels 1-4 need no entry at all: every DRC rule those levels' own
#: via/enclosure/spacing/width checks impose (``mcon``/``via``/``via2``/
#: ``via3`` and their enclosing ``met1``..``met4`` rules,
#: ``klayout_tools.decks.sky130``) is already looser than the generic
#: :data:`CONTACT_SIZE_UM` (0.22um) / :data:`ENCLOSURE_MARGIN_UM` (0.1um) /
#: :data:`MIN_SAME_LAYER_SPACING_UM` (0.4um) / :data:`UNIT_MIN_W_UM` (0.42um)
#: constants every other generator already relies on. Level 5 (``met5``/
#: ``via4``) is the one exception -- sky130's top redistribution metal carries
#: markedly coarser rules than every level below it:
#:
#: - ``via_min_w_um`` 0.8um -- ``via4.width.1`` (``sky130A_mr.drc`` rule
#:   ``via4.1_a``).
#: - ``via_enclosure_min_um`` 0.31um -- ``met5.enclosing.via4.1``
#:   (``sky130A_mr.drc`` rule ``m5.3``; ``met4``'s own enclosure of the same
#:   via, ``met4.enclosing.via4.1``/``via4.4``, is looser at 0.19um, so using
#:   the stricter value for both sides is conservative, never a violation).
#: - ``via_space_min_um`` 1.6um -- the *stricter* of ``via4.space.1``
#:   (``sky130A_mr.drc`` rule ``via4.2``, 0.8um, the end-via-to-end-via gap)
#:   and ``met5.space.1`` (rule ``m5.2``, 1.6um, the met5 body-to-body gap the
#:   same ``params.spacing_um`` also controls, since the body spans the
#:   entire unit including both end vias) -- 1.6um binds.
#: - ``body_width_min_um`` 1.6um -- ``met5.width.1`` (``sky130A_mr.drc`` rule
#:   ``m5.1``).
_PDK_METAL_RES_LEVEL_MIN_UM: dict[str, dict[int, dict[str, float]]] = {
    "sky130": {
        5: {
            "via_min_w_um": 0.8,  # via4.width.1 (via4.1_a)
            "via_enclosure_min_um": 0.31,  # met5.enclosing.via4.1 (m5.3)
            "via_space_min_um": 1.6,  # max(via4.space.1=0.8, met5.space.1=1.6)
            "body_width_min_um": 1.6,  # met5.width.1 (m5.1)
        },
    },
}

#: Every geometry-floor key :data:`_PDK_METAL_RES_LEVEL_MIN_UM` may carry.
#: The first three also name the exact hidden PCell params
#: :func:`_resistor_layer_params` resolves and ``_ResArrayPCell`` declares
#: (the ``metal_res_`` prefix distinguishes them there, mirroring
#: :data:`_CAP_GEOMETRY_MIN_KEYS`'s own role) -- ``body_width_min_um`` is
#: describe()-only (see the table's own docstring above) and is never passed
#: to the PCell.
_METAL_RES_GEOMETRY_MIN_KEYS = (
    "via_min_w_um",
    "via_enclosure_min_um",
    "via_space_min_um",
    "body_width_min_um",
)


def _metal_res_geometry_min_um(family: str, level: int) -> dict[str, float]:
    """Every :data:`_METAL_RES_GEOMETRY_MIN_KEYS` geometry floor (um)
    resolved for ``family``/``level`` (see
    :data:`_PDK_METAL_RES_LEVEL_MIN_UM`), with ``0.0`` -- "no floor, draw the
    generic geometry" -- for each key that level does not override. Safe to
    call with ``level=0`` (the poly-body default, never in the table)."""
    floors = _PDK_METAL_RES_LEVEL_MIN_UM.get(family, {}).get(level, {})
    return {key: floors.get(key, 0.0) for key in _METAL_RES_GEOMETRY_MIN_KEYS}


def _pdk_family(variant: str) -> str:
    """Map a resolved PDK ``variant`` (e.g. ``"sky130A"``, ``"gf180mcuC"``,
    ``"ihp-sg13g2"``) to the layer-role family key
    (``"sky130"``/``"gf180mcu"``/``"sg13g2"``) in :data:`_PDK_ROLE_LAYERS`.

    Delegates the variant -> family classification to
    :func:`klayout_tools.pdk_models._pdk_variant_family` -- the *same* helper
    ``sim.py`` already imports directly for its own MOS-model-binding lookup
    (issue #1448) -- rather than this module's own, narrower prefix-only scan
    it replaces. This is a deliberate reuse, not a layering violation (see
    that helper's own module for the equivalent note ``netlist_digest.py``
    makes): ``pdk_models.py`` has no imports of its own beyond the stdlib, so
    there is no import cycle. The reuse matters concretely for
    ``"ihp-sg13g2"`` -- IHP-Open-PDK's own install-directory name, and the
    exact ``variant`` string ``klt pdk find`` reports for a real fetched
    install (see ``tests/test_pdk.py``'s flat-layout tests) -- which is
    *not* a literal prefix of this module's ``"sg13g2"`` family key the way
    ``"sky130A"``/``"gf180mcuC"`` are literal prefixes of ``"sky130"``/
    ``"gf180mcu"``; a bare prefix scan here would never match it.
    :data:`klayout_tools.pdk_models._PDK_VARIANT_FAMILY_ALIASES` is what
    resolves that exact mismatch.

    Raises :class:`GenError` for any other family -- the PDK-aware
    generators draw PDK-specific layers (unlike ``resistor_strip``, which is
    PDK-agnostic and never calls this).
    """
    family = _pdk_variant_family(variant)
    if family in _PDK_ROLE_LAYERS:
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


#: Families ``mos_array``'s own well-tie tap-pad geometry (issue #1473, see
#: :func:`_mos_array_well_tap_layout`) has actually been *verified* against --
#: DRC-clean under ``klt drc`` and re-extracting with an empty
#: ``unbiased_pmos_body_nets[]`` under ``klt extract``. A family is added
#: here only once that verification has actually been run, mirroring the
#: same "verified, not assumed by analogy" bar :data:`_GENERATOR_FAMILY_DEFERRED`
#: and :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` already hold themselves to.
#:
#: ``gf180mcu``'s own :data:`_PDK_ROLE_LAYERS` entry already declares a
#: ``"well_tap_implant"`` role (added by issue #1421 for ``well_island``'s own
#: use), and that family's curated deck derives an equivalent well tie the
#: *identical* way sg13cmos5l's does (``tap_nplus`` -- ``Nplus`` 32/0, see
#: ``EXTRACTION_DECK.tap_nplus`` in ``klayout_tools.decks.gf180mcu``) -- so
#: the same "well drawn, nothing ties it" gap this issue fixes for
#: ``sg13cmos5l`` plausibly reproduces on ``gf180mcu`` too. Issue #1473's own
#: friction report explicitly did not measure that ("I only have the IHP
#: families installed here"), so this table deliberately does *not* opt
#: ``gf180mcu`` in unverified -- :func:`_mos_array_well_tap_role_layer`
#: returns ``None`` for it even though the underlying role resolves, so
#: ``mos_array``'s drawn geometry and ``ports[]`` on ``gf180mcu``/``sky130``
#: stay byte-for-byte unchanged by this issue. A follow-on issue that wires
#: this up for ``gf180mcu`` should add it here once verified DRC-clean and
#: extract-clean against that family's own curated deck, never assumed.
_MOS_ARRAY_WELL_TAP_FAMILIES: frozenset[str] = frozenset({"sg13cmos5l"})


def _mos_array_well_tap_role_layer(family: str) -> Any:
    """The ``"well_tap_implant"`` role's ``kdb.LayerInfo`` for ``family``,
    scoped to :data:`_MOS_ARRAY_WELL_TAP_FAMILIES` -- ``None`` outside that
    set, even when :data:`_PDK_ROLE_LAYERS` itself declares the role for a
    different generator's own use (e.g. ``gf180mcu``, whose entry exists for
    ``well_island``, not (yet) for ``mos_array``). Shared by
    :func:`_device_layer_params` and :func:`_mos_array_describe` so both
    resolve the exact same gate."""
    if family not in _MOS_ARRAY_WELL_TAP_FAMILIES:
        return None
    return _role_layer_info(family, "well_tap_implant")


#: Fixed net-name text ``mos_array`` draws on its own well-tie tap pad's
#: metal (issue #1473), when one is drawn (see :data:`_MOS_ARRAY_WELL_TAP_FAMILIES`).
#: Unlike ``well_island``'s own opt-in ``net`` request param (empty by
#: default, drawing no label at all), ``mos_array`` has no per-request
#: net-name param -- and the whole point of drawing this pad is to satisfy
#: the *default* ``flavor='pfet'`` request's own ``klt extract
#: unbiased_pmos_body_nets[]`` check, so the label cannot be conditional on a
#: caller opting in. ``klt extract``'s own ``_detect_unbiased_pmos_body_nets``
#: (see that function's docstring) only checks whether the body net carries
#: *any* drawn label -- KLayout's ``Net.expanded_name()`` synthesizes an
#: anonymous ``"$<n>"`` placeholder purely because no ``kdb.Text`` shape sits
#: on the net's own conductor region, independent of whether that region is
#: otherwise a real, physically-tied conductor -- so the literal text here is
#: never itself read by `klt extract`/`klt gen-compose`, only its presence
#: matters. Matches the port name (``WELL_TAP``) reported alongside it for
#: easy cross-reference between `ports[]` and the extracted netlist.
_MOS_ARRAY_WELL_TAP_NET_LABEL = "WELL_TAP"


#: Per-PDK-family medium-voltage/thick-oxide transistor *flavour* -> marker
#: layer, selected by ``mos_array``'s/``diff_pair``'s ``voltage_flavor``
#: request param (issue #1054). Mirrors :data:`_PDK_RES_FLAVOR_LAYERS`'s own
#: "family -> flavour name -> layer" shape, not a new pattern -- a device's
#: recognised higher-voltage class is chosen by which marker/implant mask
#: covers its body, exactly like a poly resistor's recognised precision class
#: above.
#:
#: gf180mcu's single supported flavour, ``"medium_voltage"``, reuses
#: :data:`_PDK_ROLE_LAYERS`'s existing ``esd_mark`` citation (``Dualgate``,
#: 55/0) -- *the same* verified layer, not a second private one: that role's
#: own comment already establishes "the two 6V (``Dualgate``-marked,
#: medium-voltage) flavours are the ones the PDK's own I/O library uses for
#: its ESD clamps", so a core ``mos_array``/``diff_pair`` unit device
#: requesting the thick-oxide/medium-voltage variant gets the identical
#: marker gf180mcu's own higher-voltage transistor flavour carries.
#: ``esd_device`` keeps resolving that citation through its own
#: ``esd_mark``/``_esd_device_layer_params`` path (unconditional, no
#: ``voltage_flavor`` value to select) -- this table gives ``mos_array``/
#: ``diff_pair`` a second, opt-in way to draw the same GDS layer without
#: renaming or otherwise disturbing ``esd_device``'s existing role.
#:
#: sky130 has no entry: this deck's own transcription cites no numbered
#: medium/high-voltage transistor marker layer (the same gap
#: :data:`_PDK_ROLE_LAYERS`'s ``esd_mark`` role documents -- sky130's
#: ``hvtr``/``hvtp`` names are cited only as unnumbered exclusions in the
#: official ``sky130.lvs``, never with a transcribed layer/datatype pair this
#: module could cite without guessing). A ``voltage_flavor`` request on this
#: family resolves to no layer for *any* name -- reported via
#: ``drc_hints.notes``, never silently dropped (see
#: :func:`_voltage_flavor_mark_layer`).
#:
#: ``sg13g2``/``sg13cmos5l`` (issue #1472) both share the single flavour name
#: ``"hv"``, transcribed directly from the curated decks' own
#: ``EXTRACTION_DECK.mos_flavours`` entry rather than a second, private layer
#: map -- ``klayout_tools.decks.sg13g2``/``klayout_tools.decks.sg13cmos5l``
#: each declare exactly one ``MOSFlavour(marker=(44, 0), flavour="hv", ...)``
#: (``ThickGateOx.drawing``), and ``pdk_models.py``'s own
#: ``_MOS_MODEL_FLAVOURS[(deck_name, "sg13g2"/"sg13cmos5l")]`` table binds
#: that same ``"hv"`` key to the real ``sg13_hv_nmos``/``sg13_hv_pmos``
#: subcircuits -- so requesting ``voltage_flavor="hv"`` here draws the
#: identical marker the deck already extracts a device-class split on,
#: mirroring gf180mcu's own "reuse the deck's own marker citation" precedent
#: above rather than inventing a new one.
_PDK_VOLTAGE_FLAVOR_LAYERS: dict[str, dict[str, tuple[int, int] | None]] = {
    "gf180mcu": {
        "medium_voltage": (55, 0),  # Dualgate -- same citation as
        # _PDK_ROLE_LAYERS's "esd_mark" entry.
    },
    "sky130": {},
    "sg13g2": {
        "hv": (44, 0),  # ThickGateOx.drawing -- same citation as
        # decks.sg13g2.EXTRACTION_DECK.mos_flavours' MOSFlavour(flavour="hv").
    },
    "sg13cmos5l": {
        "hv": (44, 0),  # ThickGateOx.drawing -- same citation as
        # decks.sg13cmos5l.EXTRACTION_DECK.mos_flavours' MOSFlavour(flavour="hv").
    },
}


def _voltage_flavor_mark_layer(family: str, voltage_flavor: str) -> Any:
    """Return the ``kdb.LayerInfo`` for ``voltage_flavor`` in ``family``'s
    :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` table, or ``None`` when that family's
    curated deck has no marker layer for the requested flavour name
    (including every name on sky130 today, and an unrecognised name on any
    family).

    Deliberately never raises -- mirrors :func:`_role_layer_info`'s own
    "``None`` means absent" contract rather than :func:`_res_flavor_layers`'s
    hard-reject one, so an unresolved ``voltage_flavor`` is reported via
    ``drc_hints.notes`` (see ``_mos_array_describe``/``_diff_pair_describe``)
    instead of failing the request outright -- the same "notes it, never
    silently drops it" precedent ``esd_device``'s own optional
    ``esd_mark``/``salicide_block`` roles established."""
    import klayout.db as kdb

    pair = _PDK_VOLTAGE_FLAVOR_LAYERS.get(family, {}).get(voltage_flavor)
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
    silent no-op, matching every other ``*_present``-gated role here.

    Also resolves ``voltage_flavor_mark_layer``/``voltage_flavor_mark_present``
    (issue #1054) -- the optional medium-voltage/thick-oxide device-class
    marker selected by the request's own ``voltage_flavor`` param (see
    :data:`_PDK_VOLTAGE_FLAVOR_LAYERS`). The default, an empty string, means
    "no marker requested" and always resolves to absent -- a request that
    never mentions ``voltage_flavor`` reproduces the pre-#1054 geometry
    exactly, matching every other opt-in role here (e.g. ``esd_device``'s
    ``salicide_block``).

    Finally resolves ``gate_pad_clearance_um`` (issue #1450) -- the
    family-specific gap the unit device's gate-poly landing pad (issue #461)
    is held clear of the diffusion's own gate-side edge (see
    :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM` and :func:`_mos_unit_layout`).
    This is a *geometry* param rather than a layer one, mirroring
    ``well_island``'s own harness-resolved ``well_margin_resolved_um``: the
    landing pad is a ``CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM`` square, wider
    than the gate stripe it sits on, so it overhangs the channel gate on both
    sides -- and with no clearance the overhang's own underside edge (which,
    unlike the channel portion, has no active area beneath it to exempt it)
    faces unrelated ``active`` at *zero* lateral distance. sg13g2's curated
    deck is the only one that transcribes a rule shaped like that
    (``gatpoly.separation.activ.1``, ``Gat.d``, 0.07um poly-to-unrelated-
    active spacing), which is why ``mos_array``/``diff_pair`` were rejected
    outright on that family before #1450 -- a *verified DRC failure*, not an
    unattempted gap (``kdb.Region.separation_check`` still reports the
    overhang's step edges even after ``.merged()``, since a native KLayout
    separation check is edge-based, not whole-polygon). ``0.0`` for sky130
    keeps its drawn geometry and reported ``U<i>_G`` port (issues
    #461/#492/#781) byte-for-byte unchanged; gf180mcu now resolves a real,
    positive clearance too (issue #1577 -- see
    :data:`_PDK_GATE_PAD_ACTIVE_CLEARANCE_UM`'s own gf180mcu entry for the
    real-signoff-deck rules this closes). ``diff_pair`` composes this same
    unit-device drawing (via :func:`_diff_pair_layer_params`) and so inherits
    the resolved clearance with no separate lookup needed.

    Also resolves three more geometry/layer knobs, all issue #1577, all
    ``0.0``/absent on every family except gf180mcu (whose real signoff deck
    needs each -- see each constant's own docstring for the exact rule ids):
    ``contact_gate_offset_um`` (:func:`_contact_gate_extra_offset_um`, a
    contact-to-gate spacing floor), ``bottom_endcap_um``
    (:func:`_gate_bottom_endcap_um`, a gate-stripe bottom-edge endcap
    extension), and ``sd_implant_layer``/``sd_implant_present``/
    ``sd_implant_margin_um`` (a source/drain implant covering the unit's
    ``active`` box, selected by ``params.flavor`` between the family's
    ``"nplus"``/``"pplus"`` roles -- see :func:`_sd_implant_margin_um`)."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    well = _role_layer_info(family, "well")
    dummy = _role_layer_info(family, "dummy")
    well_tap_implant = _mos_array_well_tap_role_layer(family)
    # Only resolved when a well-tie tap pad is actually drawn for this family
    # (`well_tap_implant is not None`) -- the label exists purely to name
    # *that* pad's own net (see `_MOS_ARRAY_WELL_TAP_NET_LABEL`'s docstring),
    # never drawn on its own.
    metal_label = _role_layer_info(family, "metal_label") if well_tap_implant else None
    voltage_flavor = params.get("voltage_flavor", "")
    voltage_flavor_mark = (
        _voltage_flavor_mark_layer(family, voltage_flavor) if voltage_flavor else None
    )
    # Source/drain implant (issue #1577): the family's `"nplus"`/`"pplus"`
    # role, selected by `flavor` (`esd_device`'s own params never carry a
    # `flavor` key at all -- that generator is always NMOS-style, so the
    # `"nfet"` fallback below resolves it exactly the way `mos_array`'s own
    # `flavor` default does). `sd_implant_present` additionally requires a
    # positive resolved margin (:func:`_sd_implant_margin_um`) -- a family
    # that declares the role but not the margin (none do today) would
    # otherwise draw a zero-size implant box, and a family with neither
    # (every family besides gf180mcu) never resolves a layer here at all.
    flavor = params.get("flavor", "nfet")
    sd_implant_role = "nplus" if flavor == "nfet" else "pplus"
    sd_implant = _role_layer_info(family, sd_implant_role)
    sd_implant_margin = _sd_implant_margin_um(family)
    return {
        "active_layer": _role_layer_info(family, "active"),
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "dummy_layer": dummy if dummy is not None else kdb.LayerInfo(0, 0),
        "dummy_present": dummy is not None,
        # Well-tie implant (issue #1473): unlike every other role above, this
        # goes through :func:`_mos_array_well_tap_role_layer` rather than a
        # bare :func:`_role_layer_info` call -- it resolves the role scoped
        # to :data:`_MOS_ARRAY_WELL_TAP_FAMILIES` (verified families only,
        # `sg13cmos5l` today), not every family whose table happens to
        # declare `"well_tap_implant"` for a different generator's own use
        # (`well_island`'s `_well_island_layer_params` resolves the same role
        # unscoped, since that generator's own use of it is unaffected).
        # ``mos_array``'s own consumer is
        # :func:`_mos_array_well_tap_layout`, gated on ``flavor == 'pfet'``
        # (see ``_MosArrayPCell.produce_impl``).
        "well_tap_implant_layer": (
            well_tap_implant if well_tap_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "well_tap_implant_present": well_tap_implant is not None,
        "metal_label_layer": (
            metal_label if metal_label is not None else kdb.LayerInfo(0, 0)
        ),
        "metal_label_present": metal_label is not None,
        "voltage_flavor_mark_layer": (
            voltage_flavor_mark
            if voltage_flavor_mark is not None
            else kdb.LayerInfo(0, 0)
        ),
        "voltage_flavor_mark_present": voltage_flavor_mark is not None,
        "voltage_flavor_mark_margin_um": _voltage_flavor_mark_margin_um(family),
        "gate_pad_clearance_um": _gate_pad_clearance_um(family),
        "contact_gate_offset_um": _contact_gate_extra_offset_um(family),
        "bottom_endcap_um": _gate_bottom_endcap_um(family),
        "sd_implant_layer": (
            sd_implant if sd_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "sd_implant_present": sd_implant is not None and sd_implant_margin > 0,
        "sd_implant_margin_um": sd_implant_margin if sd_implant is not None else 0.0,
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
    drawing that class's own implant/precision-resistor masks over the same
    segment (see :data:`_PDK_RES_FLAVOR_LAYERS`). The ``flavor`` request param
    picks which flavour -- sky130's `res_generic_po` (default) needs no extra
    mask at all, while `res_high_po`/`res_xhigh_po` each need the psdm implant
    plus their own rpm/urpm mask; gf180mcu's single `ppolyf_u` needs both
    Pplus and SAB; sg13g2's `rsil`/`rppd`/`rhigh` need two/three/four masks
    respectively (issue #1451).

    The resolved masks are handed to the PCell positionally, as
    :data:`_MAX_RES_FLAVOR_LAYERS` ``res_flavor_<i>_layer``/
    ``res_flavor_<i>_present`` slot pairs -- one static slot per mask the
    *widest* flavour in the table needs, with the tail slots of a narrower
    flavour left absent. Absence follows the `well_present`/`bjt_mark_present`
    precedent (the generator omits that layer entirely).

    Also resolves ``dummy_layer``/``dummy_present`` (issue #491), the same
    optional PDK dummy-device marker :func:`_device_layer_params` resolves --
    see that function's docstring.

    ``params.metal_level`` (issue #1639), when non-zero, switches this
    generator entirely off the poly-body path above: ``poly_layer``/
    ``contact_layer``/``metal_layer`` resolve instead to that level's
    ``body``/``via``/``landing`` layers (see :data:`_PDK_METAL_RES_LEVELS`),
    ``res_mark_layer``/``res_mark_present`` resolve to that level's own
    resistor-ID marker (unconditionally present -- every entry in
    :data:`_PDK_METAL_RES_LEVELS` carries one), and every
    ``res_flavor_<i>_present`` slot is forced ``False`` -- sky130's
    ``res_generic_mN`` classes declare no ``requires`` masks at all, so
    ``params.flavor`` is simply ignored in this mode (like
    `bond_pad`'s own `via_style`-has-no-effect precedent, an unused param in
    one mode is reported, not rejected -- see :func:`_res_array_describe`).
    Also resolves the three ``metal_res_via_*`` geometry floors (see
    :data:`_PDK_METAL_RES_LEVEL_MIN_UM`/:func:`_metal_res_geometry_min_um`);
    the poly-body path (``metal_level=0``) leaves them at the PCell's own
    ``0.0`` default (omitted here), which :func:`_res_unit_layout`/
    :func:`_res_array_layout` already treat as "no floor, draw the generic
    geometry"."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    dummy = _role_layer_info(family, "dummy")
    dummy_params: dict[str, Any] = {
        "dummy_layer": dummy if dummy is not None else kdb.LayerInfo(0, 0),
        "dummy_present": dummy is not None,
    }

    metal_level = params.get("metal_level", 0)
    if metal_level:
        levels = _metal_res_layers(family, metal_level)
        resolved: dict[str, Any] = {
            "poly_layer": kdb.LayerInfo(*levels["body"]),
            "contact_layer": kdb.LayerInfo(*levels["via"]),
            "metal_layer": kdb.LayerInfo(*levels["landing"]),
            "res_mark_layer": kdb.LayerInfo(*levels["marker"]),
            "res_mark_present": True,
            **dummy_params,
        }
        for i in range(_MAX_RES_FLAVOR_LAYERS):
            resolved[f"res_flavor_{i}_layer"] = kdb.LayerInfo(0, 0)
            resolved[f"res_flavor_{i}_present"] = False
        floors = _metal_res_geometry_min_um(family, metal_level)
        resolved["metal_res_via_min_w_um"] = floors["via_min_w_um"]
        resolved["metal_res_via_enclosure_min_um"] = floors["via_enclosure_min_um"]
        resolved["metal_res_via_space_min_um"] = floors["via_space_min_um"]
        return resolved

    flavor = params.get("flavor", _DEFAULT_RES_FLAVOR)
    flavor_layers = _res_flavor_layers(family, flavor)
    mark = _role_layer_info(family, "res_mark")
    resolved = {
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "res_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "res_mark_present": mark is not None,
        **dummy_params,
    }
    for i in range(_MAX_RES_FLAVOR_LAYERS):
        pair = flavor_layers[i] if i < len(flavor_layers) else None
        resolved[f"res_flavor_{i}_layer"] = (
            kdb.LayerInfo(*pair) if pair is not None else kdb.LayerInfo(0, 0)
        )
        resolved[f"res_flavor_{i}_present"] = pair is not None
    return resolved


def _cap_family_layers(family: str) -> dict[str, tuple[int, int] | None]:
    """Return the resolved MiM-cap plate/via layer pairs for ``family`` (see
    :data:`_PDK_ROLE_LAYERS`'s ``"cap_top_plate"``/``"cap_bottom_plate"``/
    ``"cap_top_via"``/``"cap_top_via_metal"`` roles).

    Raises :class:`GenError` for a family with no ``cap_top_plate``/
    ``cap_bottom_plate`` configured -- mirrors :func:`_res_flavor_layers`'s
    own unsupported-value error. ``sky130`` (issue #1117), ``sg13g2`` (issue
    #1455, once #1454 populated that family's ``EXTRACTION_DECK.capacitors``)
    and ``gf180mcu`` (issue #1555, the follow-on #1117 deferred) are wired up
    today; a family missing these keys is a documented "not implemented yet"
    state, not a deck-authoring bug the way an unresolvable family name in
    :func:`_pdk_family` is.

    gf180mcu's stack -- alone among the three -- needs the DRM's oversized
    "virtual bottom plate" (see
    :class:`klayout_tools.decks.CapacitorDevice`'s ``bottom_plate_oversize_um``
    docstring) and two extra top-plate recognition masks; both are per-family
    *data* rather than per-family code, in :data:`_PDK_CAP_GEOMETRY_MIN_UM`
    and :data:`_PDK_CAP_TOP_PLATE_REQUIRES` respectively."""
    roles = _PDK_ROLE_LAYERS[family]
    top = roles.get("cap_top_plate")
    bottom = roles.get("cap_bottom_plate")
    if top is None or bottom is None:
        supported = ", ".join(
            name
            for name, entry in _PDK_ROLE_LAYERS.items()
            if entry.get("cap_top_plate") is not None
            and entry.get("cap_bottom_plate") is not None
        )
        raise GenError(
            f"generator 'cap_array': PDK family '{family}' has no MiM "
            "capacitor plate layers configured -- supported families: "
            f"{supported}"
        )
    return {
        "cap_top_plate": top,
        "cap_bottom_plate": bottom,
        "cap_top_via": roles.get("cap_top_via"),
        "cap_top_via_metal": roles.get("cap_top_via_metal"),
    }


def _cap_array_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``cap_array`` (a top-plate-metal-over-
    bottom-plate-metal MiM stack, plus a top-plate via/local-metal landing
    pad) -- the capacitor sibling of :func:`_resistor_layer_params`.

    ``cap_top_via_present``/``cap_top_via_metal_present`` follow the
    ``res_mark_present`` precedent even though all three families
    :func:`_cap_family_layers` resolves today (sky130, sg13g2, gf180mcu)
    always set both -- a future family that declares plates but not a
    top-plate via can still resolve cleanly, matching
    :class:`~klayout_tools.decks.CapacitorDevice`'s own ``top_plate_via``/
    ``top_plate_via_metal`` being optional fields.

    Also resolves, all of them harness-computed from the resolved family and
    never request-facing (see :data:`_HIDDEN_PARAMS`):

    - the :data:`_CAP_GEOMETRY_MIN_KEYS` geometry floors (issues #1455/#1555,
      see :func:`_cap_geometry_min_um`) -- ``0.0`` each for a family that
      overrides nothing, leaving the generic geometry untouched;
    - :data:`_MAX_CAP_TOP_PLATE_REQUIRES` ``cap_top_requires_<i>_layer``/
      ``cap_top_requires_<i>_present`` slot pairs (issue #1555), the extra
      masks that must cover the top plate for this family's own MiM device
      class to be recognised -- positionally handed to the PCell exactly like
      ``res_array``'s own ``res_flavor_<i>_*`` slots, with a family needing
      fewer masks leaving its tail slots absent."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    layers = _cap_family_layers(family)
    top_via = layers["cap_top_via"]
    top_via_metal = layers["cap_top_via_metal"]
    top_requires = _cap_top_plate_requires(family)
    resolved: dict[str, Any] = {
        "cap_top_plate_layer": kdb.LayerInfo(*layers["cap_top_plate"]),
        "cap_bottom_plate_layer": kdb.LayerInfo(*layers["cap_bottom_plate"]),
        "cap_top_via_layer": (
            kdb.LayerInfo(*top_via) if top_via is not None else kdb.LayerInfo(0, 0)
        ),
        "cap_top_via_present": top_via is not None,
        "cap_top_via_metal_layer": (
            kdb.LayerInfo(*top_via_metal)
            if top_via_metal is not None
            else kdb.LayerInfo(0, 0)
        ),
        "cap_top_via_metal_present": top_via_metal is not None,
        **_cap_geometry_min_um(family),
    }
    for i in range(_MAX_CAP_TOP_PLATE_REQUIRES):
        pair = top_requires[i] if i < len(top_requires) else None
        resolved[f"cap_top_requires_{i}_layer"] = (
            kdb.LayerInfo(*pair) if pair is not None else kdb.LayerInfo(0, 0)
        )
        resolved[f"cap_top_requires_{i}_present"] = pair is not None
    return resolved


def _ring_tap_implant_layer(family: str, well_tie: bool) -> Any:
    """The implant ``kdb.LayerInfo`` (or ``None``) that must cover a ring's
    own tap/collector ``Comp`` shape (issue #1580, follow-up to #1577's
    unit-device-only ``sd_implant_*`` fix -- see that constant's own
    docstring for the ``DF.12`` rule this closes).

    Only resolves a real layer wherever the family's own ``"tap"`` role is
    the *same* physical layer/datatype pair as its ``"active"`` role --
    a necessary, but (re-verified against the current tree, correcting
    #1580's own curation) *not sufficient* condition. ``gf180mcu`` is the
    only family that satisfies both this collision *and* declares an
    implant role (``"well_tap_implant"``/``"pplus"``) to resolve here at all
    -- its curated deck declares no dedicated tap mask
    (``_PDK_ROLE_LAYERS["gf180mcu"]["tap"] == (22, 0)``, the same pair as
    ``"active"``). ``sky130``/``sg13cmos5l`` both declare a distinct
    ``"tap"`` layer, so the collision check alone already returns ``None``
    for them. ``sg13g2`` is the corrected case: its own ``"tap"`` role
    *does* collide with ``"active"`` (both ``(1, 0)``, "no distinct tap mask
    ... derives well ties from the same Activ layer", see this family's own
    ``_PDK_ROLE_LAYERS`` entry) -- #1580's curation asserted otherwise,
    which does not hold against this tree. It still resolves ``None`` here,
    for an independent reason: this family declares neither a
    ``"well_tap_implant"`` nor a ``"pplus"`` role at all (no known
    ``DF.12``-equivalent implant-coverage rule in its curated deck to close
    -- see the ``"tap"`` role's own comment: sg13g2 recognises a tie from
    bare ``Activ ∩ nwell``, no implant mask needed), so
    :func:`_role_layer_info` already returns ``None`` for either lookup
    below regardless of the collision check. The net behaviour every
    non-gf180mcu family needs (``None``, byte-for-byte-unchanged geometry,
    exactly like every other ``*_present``-gated role #1577 added) holds for
    all three, just via two different mechanisms.

    ``well_tie`` selects which doping the ring's own tie needs -- this is
    the open design question #1580's curation left for the Builder to
    settle, with rationale, rather than treating either option as settled
    fact:

    - ``True`` (the ring is drawn *inside* an enclosing well, e.g.
      ``guard_ring``'s own ``add_well``-gated well, or ``mos_array``'s/
      ``diff_pair``'s ``flavor='pfet'`` well): reuses the *same*
      ``"well_tap_implant"`` role ``well_island`` already reuses for its own
      ring (Nplus on gf180mcu, ``_PDK_ROLE_LAYERS``'s own
      ``"well_tap_implant"`` entry) -- an n+ tie recognised as biasing the
      Nwell it sits inside, the identical precedent issue #1421 established
      and this issue's curation names as the template to follow.
    - ``False`` (no enclosing well -- the ring ties the p-type substrate
      directly): the *opposite* doping is needed instead, so this resolves
      the ``"pplus"`` role instead (the same p+ implant #1577 added for a
      ``flavor='pfet'`` unit device's own source/drain) -- reusing
      ``"well_tap_implant"`` (Nplus) here would misrepresent a bare
      substrate contact as a well tie, which a real signoff extraction deck
      would read as biasing a well that is not actually drawn.

    Neither choice is independently verified against a real gf180mcu
    signoff deck -- the same not-independently-verified caveat #1575/#1577/
    #1580 all carry (this sandbox has no such deck to check against)."""
    roles = _PDK_ROLE_LAYERS[family]
    tap_pair = roles.get("tap")
    active_pair = roles.get("active")
    if tap_pair is None or active_pair is None or tap_pair != active_pair:
        return None
    role = "well_tap_implant" if well_tie else "pplus"
    return _role_layer_info(family, role)


def _ring_layer_params(
    pdk_info: dict[str, Any],
    params: dict[str, Any],
    generator_name: str = "guard_ring",
) -> dict[str, Any]:
    """Hidden layer params for a generator drawing a guard ring
    (``guard_ring``, and the optional ring half of ``diff_pair``/
    ``mos_array``, both of which pass their own ``generator_name`` so a
    deferred family's error names the generator actually invoked rather than
    always ``'guard_ring'`` -- see :data:`_GENERATOR_FAMILY_DEFERRED`'s
    ``sg13cmos5l`` entries).

    Also resolves ``ring_implant_layer``/``ring_implant_present`` (issue
    #1580) -- the tap-ring implant :func:`_ring_tap_implant_layer` selects,
    gated on whether *this* ring is drawn inside an enclosing well:
    ``guard_ring``'s own ``params.add_well`` (default ``True``, mirroring
    ``_GuardRingPCell``'s own default) for ``generator_name == "guard_ring"``;
    otherwise (``diff_pair``/``mos_array``) the same ``params.flavor ==
    "pfet"`` condition those generators' own ``produce_impl`` already uses to
    decide whether their ring gets a well tie at all."""
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family(generator_name, family)
    well = _role_layer_info(family, "well")
    if generator_name == "guard_ring":
        well_tie = bool(params.get("add_well", True)) and well is not None
    else:
        well_tie = well is not None and params.get("flavor") == "pfet"
    ring_implant = _ring_tap_implant_layer(family, well_tie)
    return {
        "tap_layer": _role_layer_info(family, "tap"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
    }


def _diff_pair_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """``diff_pair`` composes a unit-device array and an optional guard ring
    -- union of both their hidden layer params."""
    layer_params = _device_layer_params(pdk_info, params)
    layer_params.update(
        _ring_layer_params(pdk_info, params, generator_name="diff_pair")
    )
    return layer_params


def _mos_array_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """``mos_array`` composes a unit-device array's own layer roles
    (:func:`_device_layer_params`) and, only when ``params.add_guard_ring``
    is actually requested, ``guard_ring``'s ring role
    (:func:`_ring_layer_params`, issue #1493).

    This is deliberately *conditional*, unlike :func:`_diff_pair_layer_params`/
    :func:`_esd_device_layer_params` (which always resolve the ring role,
    since a ring is those generators' own primary composition and both are
    listed in :data:`_GENERATOR_FAMILY_DEFERRED` outright for a family with
    no ``"tap"`` role). ``mos_array``'s guard ring is opt-in and defaults to
    off: resolving the ring role unconditionally would regress every
    existing no-ring ``mos_array`` request on such a family (e.g.
    ``sg13cmos5l``, which has no ``"tap"`` role but carries passing
    ``mos_array`` coverage predating this issue). ``tap_layer`` falls back to
    ``kdb.LayerInfo(0, 0)`` when the ring role is not resolved -- unused by
    ``produce_impl`` unless ``add_guard_ring`` is set, mirroring every other
    ``*_layer``-without-a-``*_present``-gate default in this module."""
    import klayout.db as kdb

    layer_params = _device_layer_params(pdk_info, params)
    if params.get("add_guard_ring"):
        layer_params.update(
            _ring_layer_params(pdk_info, params, generator_name="mos_array")
        )
    else:
        layer_params.setdefault("tap_layer", kdb.LayerInfo(0, 0))
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

    Also resolves ``ring_implant_layer``/``ring_implant_present`` (issue
    #1580, :func:`_ring_tap_implant_layer`) for the collector ring's own
    ``Comp`` shape (drawn on ``active_layer`` directly -- see
    ``_BjtArrayPCell.produce_impl``, not a separate ``tap``-role ring the way
    ``guard_ring``/``mos_array``/``diff_pair``/``esd_device`` draw theirs).
    Always resolved with ``well_tie=False``: the collector ring is composed
    *outside* the shared base well's own footprint (``well_box_um`` plus
    ``BJT_COLLECTOR_GAP_UM``, see :func:`_bjt_array_layout`'s docstring), so
    it always ties the p-type substrate directly -- the real, physical
    collector terminal of a vertical PNP (P+ emitter / N+ base tie inside an
    Nwell / P+ collector = substrate, per ``docs/design/gen-bjt-array-spike.md``),
    never a well tie. This intentionally does **not** extend implant coverage
    to the emitter/base-tie unit body itself, a separate, pre-existing,
    documented limitation (that same design doc: "curated decks check no
    implant layer ... a process-exact device would distinguish P+ emitter, N+
    base tie, and P+ collector by implant") predating both #1577 and this
    issue -- out of #1580's own scope, which is the tap/collector *ring*
    only.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("bjt_array", family)
    well = _role_layer_info(family, "well")
    mark = _role_layer_info(family, "bjt_mark")
    dummy = _role_layer_info(family, "dummy")
    ring_implant = _ring_tap_implant_layer(family, well_tie=False)
    return {
        "active_layer": _role_layer_info(family, "active"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "tap_layer": _role_layer_info(family, "tap"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "bjt_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "bjt_mark_present": mark is not None,
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
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
    family this generator supports -- unlike ``well``/``bjt_mark``/``dummy``
    above, neither is ever ``None`` *there*, so this needs no ``*_present``
    flag; a family with no curated passivation-opening/top-metal layer at all
    (e.g. ``sg13g2``, issue #1448 -- this deck's own ``LAYER_NAMES`` cites no
    pad-opening mask) raises :class:`GenError` here instead, mirroring
    :func:`_cap_family_layers`'s own explicit missing-role check rather than
    passing ``None`` through to a PCell layer param that requires a real
    ``kdb.LayerInfo``."""
    family = _pdk_family(pdk_info["variant"])
    pad = _role_layer_info(family, "pad")
    top_metal = _role_layer_info(family, "top_metal")
    if pad is None or top_metal is None:
        raise GenError(
            f"generator 'bond_pad': PDK family '{family}' has no bond-pad "
            "passivation-opening/top-metal layers configured -- supported "
            "families: gf180mcu, sky130"
        )
    return {
        "pad_layer": pad,
        "top_metal_layer": top_metal,
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

    Also resolves ``contact_gate_offset_um``/``bottom_endcap_um``/
    ``sd_implant_layer``/``sd_implant_present``/``sd_implant_margin_um``
    (issue #1577) -- the same three gf180mcu-only geometry knobs
    :func:`_device_layer_params` resolves for ``mos_array``/``diff_pair``,
    inlined here rather than delegated (this function predates -- and does
    not otherwise call -- :func:`_device_layer_params`, and always resolves
    the ``"nplus"`` implant role: ``esd_device`` has no ``flavor`` param and
    is always an NMOS-style device, matching that function's own ``"nfet"``
    fallback when ``params`` carries no ``flavor`` key). ``gate_pad_clearance_um``
    is deliberately *not* resolved here -- :func:`_esd_device_layout` has
    never forwarded it to :func:`_mos_unit_layout` (a pre-existing,
    out-of-scope gap this issue does not attempt), so leaving it unresolved
    keeps that pre-#1577 behaviour exactly as it was.

    Also resolves ``ring_implant_layer``/``ring_implant_present`` (issue
    #1580, :func:`_ring_tap_implant_layer`) for the ring's own tap shape --
    always with ``well_tie=False``, since (per this function's own docstring
    above) ``esd_device`` never draws an enclosing well at all, so its ring
    always ties the substrate directly.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("esd_device", family)
    esd_mark = _role_layer_info(family, "esd_mark")
    salicide_block = _role_layer_info(family, "salicide_block")
    sd_implant = _role_layer_info(family, "nplus")
    sd_implant_margin = _sd_implant_margin_um(family)
    ring_implant = _ring_tap_implant_layer(family, well_tie=False)
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
        "contact_gate_offset_um": _contact_gate_extra_offset_um(family),
        "bottom_endcap_um": _gate_bottom_endcap_um(family),
        "sd_implant_layer": (
            sd_implant if sd_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "sd_implant_present": sd_implant is not None and sd_implant_margin > 0,
        "sd_implant_margin_um": sd_implant_margin if sd_implant is not None else 0.0,
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
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


def _sd_pad_gate_offset_um(l_um: float, extra_offset_um: float = 0.0) -> float:
    """Extra clearance (um) :func:`_mos_finger_positions` inserts on *each*
    side of every gate stripe, between it and the adjacent S/D segment
    (issue #1187).

    ``0.0`` whenever ``l_um >= SD_PAD_GATE_GAP_MIN_UM`` -- true for every
    generator's own default (``GATE_LENGTH_SAFE_MIN_UM``, which is itself
    ``> SD_PAD_GATE_GAP_MIN_UM``) -- so existing callers at or above that
    default see byte-for-byte unchanged geometry. Below it, the offset grows
    just enough that the S/D-pad-to-pad gap across the gate
    (``l_um + 2 * offset``) reaches :data:`SD_PAD_GATE_GAP_MIN_UM` exactly,
    regardless of how small ``l_um`` itself is.

    ``extra_offset_um`` (issue #1577) is a second, independent floor: the
    resolved PDK family's :func:`_contact_gate_extra_offset_um`, needed on
    families whose real (not this repo's curated) deck checks a
    contact-to-gate spacing tighter than the small-``l_um`` makeup above
    alone guarantees. The two floors are independent asks -- a small ``l_um``
    already at the family's floor gets no *extra* padding on top, and a
    family with no floor at all (``0.0``, the default) sees exactly the
    pre-#1577 makeup-only behaviour -- so the return value is ``max``, not a
    sum, of the two.
    """
    makeup_um = max(0.0, (SD_PAD_GATE_GAP_MIN_UM - l_um) / 2.0)
    return max(makeup_um, extra_offset_um)


def _mos_finger_positions(
    l_um: float, fingers: int, extra_offset_um: float = 0.0
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """``(seg_positions, poly_positions, total_len_um)`` for a ``fingers``-
    finger MOS unit device: ``fingers + 1`` contact-sized source/drain
    segments alternating with ``fingers`` ``l_um``-wide gate stripes, laid
    out left to right from ``x = 0``.

    Shared by both finger topologies (see :func:`_mos_unit_layout`) so the
    along-the-diffusion arithmetic -- and therefore ``total_len_um``, the
    array column pitch -- is identical whether or not the fingers are
    strapped in parallel.

    Each gate stripe sits :func:`_sd_pad_gate_offset_um` clear of the S/D
    segments either side of it (issue #1187) -- ``0.0`` for any ``l_um`` at
    or above :data:`SD_PAD_GATE_GAP_MIN_UM`, so the stripe still abuts the
    segments exactly as before every caller's own default. Below that
    threshold, the offset pads the unit device's *pitch* (``total_len_um``
    grows) rather than the gate itself (``l_um`` is drawn exactly as
    requested), so the S/D local-metal pads :func:`_mos_unit_layout` draws
    flush to ``seg_positions`` stay a legal same-layer-metal-spacing gap
    apart even at a PDK's absolute minimum gate length.

    ``extra_offset_um`` (issue #1577) is forwarded to
    :func:`_sd_pad_gate_offset_um` unchanged -- see that function's own
    docstring for why it is a second, independent floor rather than added to
    the small-``l_um`` makeup.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    pad_offset_um = _sd_pad_gate_offset_um(l_um, extra_offset_um)
    seg_positions: list[tuple[float, float]] = []
    poly_positions: list[tuple[float, float]] = []
    x = 0.0
    for i in range(fingers + 1):
        seg_positions.append((x, x + contact_region_um))
        x += contact_region_um
        if i < fingers:
            x += pad_offset_um
            poly_positions.append((x, x + l_um))
            x += l_um + pad_offset_um
    return seg_positions, poly_positions, x


def _mos_unit_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    gate_contact: bool = False,
    finger_topology: str = "series",
    gate_pad_clearance_um: float = 0.0,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
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

    In the ``"series"`` shape with ``fingers == 1`` there is nothing but the
    two end segments and the one gate, so the unit reports plain
    ``s_xy``/``d_xy``/``g_xy`` exactly as before. For ``fingers > 1`` (issue
    #781, the follow-up to #777's option 1) *every* finger is padded and
    *every* terminal is exposed: ``seg_xy`` holds all ``fingers + 1`` S/D
    segment centres (west to east) and ``gate_xy`` holds all ``fingers`` gate
    pad centres, so :func:`_mos_array_describe` can report a ``U<i>_S<j>``/
    ``U<i>_D<j>`` port for every segment and a ``U<i>_G<j>`` port for every
    gate instead of leaving the interior of the chain undrivable. ``s_xy``/
    ``d_xy``/``g_xy`` still alias ``seg_xy[0]``/``seg_xy[-1]``/``gate_xy[0]``
    so single-finger callers (:func:`_diff_pair_layout`,
    :func:`_esd_device_layout`, and the ``fingers == 1`` case here) need no
    change.

    Every gate finger carries the same **poly landing pad** that extends
    past the diffusion's top edge, so a contact can be placed on the gate
    outside the channel (issue #461). Without it the gate poly shared both
    the top and bottom edge of the diffusion, leaving nowhere legal for a
    gate contact to land: one at the diff edge straddles it
    (``poly.enclosing.licon.1``/``diff.enclosing.licon.1`` violations) and
    one moved inward sits over the channel (a gate-oxide short). The pad is a
    ``contact_region_um`` square (``CONTACT_SIZE_UM`` enclosed by
    ``ENCLOSURE_MARGIN_UM`` on every side -- the same enclosure budget the
    S/D contacts use), centred on the gate ``ENCLOSURE_MARGIN_UM`` clear of
    the diffusion, and each reported gate port sits at its own pad's centre.
    Adjacent pads end up exactly ``l_um + 2 * `` :func:`_sd_pad_gate_offset_um`
    ``(l_um)`` apart -- i.e. never closer than :data:`SD_PAD_GATE_GAP_MIN_UM`
    (issue #1187), the same guarantee the S/D local-metal pads either side of
    a gate now have -- so padding every finger (not just the first) costs no
    new DRC risk, regardless of how small ``l_um`` is.

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

    ``gate_pad_clearance_um`` (issue #1450, resolved per PDK family by
    :func:`_gate_pad_clearance_um`) stands that landing pad off the
    diffusion's top edge instead of abutting it, bridging the gap with a poly
    stem of exactly ``l_um`` -- the gate stripe's own width, so the poly stays
    one connected region *and* the stem contributes no new edge at
    ``y == w_um`` for a poly-to-active separation check to measure. That
    matters because the pad is wider than the gate stripe for any realistic
    ``l_um``: with no clearance, the two overhangs' undersides sit flush on
    the diffusion's own top edge, facing unrelated ``active`` at zero
    distance. sky130/gf180mcu pass ``0.0`` (neither curated deck checks
    poly-to-unrelated-active spacing at all, and lifting the pad would move
    their long-pinned ``U<i>_G`` port), so their drawn geometry is
    byte-for-byte unchanged; sg13g2 passes a real clearance. The stem is only
    ever ``l_um`` wide, never the pad's width, so a caller's existing gate
    length still fully determines poly width here -- no new minimum-width or
    poly-space rule binds. Only the ``"series"`` shape needs this: the
    ``"parallel"`` strapped shape (:func:`_mos_unit_strapped_layout`) already
    runs every gate stripe a full :data:`MIN_SAME_LAYER_SPACING_UM` plus a
    contact region past the diffusion before widening into its comb.

    Three more knobs, all issue #1577, all resolved per PDK family
    (``0.0`` -- byte-for-byte unchanged geometry -- on every family besides
    ``gf180mcu``, whose real signoff deck needs each): ``contact_gate_offset_um``
    (:func:`_contact_gate_extra_offset_um`) is forwarded to
    :func:`_mos_finger_positions` as a second, independent floor on top of
    the small-``l_um`` makeup :func:`_sd_pad_gate_offset_um` already applies;
    ``bottom_endcap_um`` (:func:`_gate_bottom_endcap_um`) extends every gate
    stripe's *bottom* edge (``y == 0``) down past the diffusion, mirroring
    the endcap the *top* edge's own #461 landing pad already provides there;
    ``sd_implant_margin_um`` (:func:`_sd_implant_margin_um`) draws a
    ``"sd_implant"`` box -- present only when this margin is positive --
    covering the unit's own ``active`` box grown by that margin on every
    side, so `produce_impl` can lay a source/drain implant mask over the
    device body (see :func:`_device_layer_params`'s ``sd_implant_layer``/
    ``sd_implant_present``, resolved off ``params.flavor``).
    """
    if finger_topology == "parallel" and fingers > 1:
        return _mos_unit_strapped_layout(
            w_um,
            l_um,
            fingers,
            gate_contact,
            contact_gate_offset_um,
            bottom_endcap_um,
            sd_implant_margin_um,
        )

    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(
        l_um, fingers, contact_gate_offset_um
    )

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "active": [(0.0, 0.0, total_len_um, w_um)],
        "poly": [(px0, -bottom_endcap_um, px1, w_um) for (px0, px1) in poly_positions],
        "contact": [],
        "metal": [],
        "sd_implant": (
            [
                (
                    -sd_implant_margin_um,
                    -sd_implant_margin_um,
                    total_len_um + sd_implant_margin_um,
                    w_um + sd_implant_margin_um,
                )
            ]
            if sd_implant_margin_um > 0
            else []
        ),
    }
    contact_half = CONTACT_SIZE_UM / 2.0
    seg_xy: list[tuple[float, float]] = []
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, w_um))
        cx = (sx0 + sx1) / 2.0
        cy = w_um / 2.0
        boxes["contact"].append(
            (cx - contact_half, cy - contact_half, cx + contact_half, cy + contact_half)
        )
        seg_xy.append((cx, cy))

    s_xy = seg_xy[0]
    d_xy = seg_xy[-1]

    gate_xy: list[tuple[float, float]] = []
    if poly_positions:
        # Gate landing pad above *every* finger (issue #781 -- previously
        # only the first). Each pad's half-width (contact_region_um / 2) can
        # exceed l_um / 2, so it overhangs its narrow gate on both sides --
        # that is the point: the pad, not the sub-contact-width gate, is
        # what encloses the contact. It abuts its own gate at y == w_um
        # (keeping that gate's poly one connected region) and extends
        # contact_region_um past it. Adjacent pads end up exactly
        # l_um + 2 * _sd_pad_gate_offset_um(l_um) apart -- never closer than
        # SD_PAD_GATE_GAP_MIN_UM (issue #1187), the same guarantee the S/D
        # local-metal pads either side of a gate now have, so padding every
        # finger binds no new spacing rule regardless of how small l_um is.
        #
        # With `gate_contact` each pad's contact region is first pushed
        # MIN_SAME_LAYER_SPACING_UM further out (the poly simply grows into a
        # stem of the same width across that clearance, so it stays one
        # connected region): the metal pad drawn on it is a full
        # contact_region_um square, and centred on the #461 pad it would
        # share the y == w_um edge with the S/D local-metal pads either side
        # -- merging into one polygon and shorting the gate to source/drain.
        #
        # `gate_pad_clearance_um` (issue #1450) additionally lifts the whole
        # pad (and, with `gate_contact`, its stem/contact/metal stack) that
        # far off y == w_um on families whose curated deck checks
        # poly-to-unrelated-active spacing -- see the function docstring. The
        # gap is bridged by a poly box of the gate stripe's *own* width
        # (px0..px1), never the pad's: a full-width bridge would just move the
        # overhang's underside edge up by the clearance instead of removing it
        # from the diffusion's edge, and a stripe-width one adds no edge at
        # y == w_um at all (it is flush with the gate stripe below it).
        pad_half = contact_region_um / 2.0
        clearance_um = max(gate_pad_clearance_um, 0.0)
        stem_um = MIN_SAME_LAYER_SPACING_UM if gate_contact else 0.0
        pad_y0 = w_um + clearance_um
        gate_ext_um = clearance_um + stem_um + contact_region_um
        g_cy = pad_y0 + stem_um + contact_region_um / 2.0
        for px0, px1 in poly_positions:
            g_cx = (px0 + px1) / 2.0
            if clearance_um:
                boxes["poly"].append((px0, w_um, px1, pad_y0))
            boxes["poly"].append(
                (g_cx - pad_half, pad_y0, g_cx + pad_half, w_um + gate_ext_um)
            )
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
            gate_xy.append((g_cx, g_cy))
        g_xy = gate_xy[0]
        g_width_um = contact_region_um
    else:
        g_xy = (total_len_um / 2.0, w_um)
        g_width_um = l_um
        gate_ext_um = 0.0

    return {
        "total_len_um": total_len_um,
        "height_um": w_um,
        # Full drawn height including the gate landing pad *and* (issue
        # #1577) the gate stripe's own bottom-edge endcap extension -- what
        # row-to-row placement (see `_mos_array_layout`/`_diff_pair_layout`)
        # and the enclosing well box must span. Without folding
        # `bottom_endcap_um` in here too, row-to-row placement would still
        # only reserve `MIN_SAME_LAYER_SPACING_UM` between each row's *own*
        # y == 0 origin and the row above's landing pad -- but the actual
        # drawn poly now reaches `bottom_endcap_um` *below* that origin, so
        # the real gap to the previous row's poly would shrink by exactly
        # that amount, tripping gf180mcu's real `PL.3a` ("Space on
        # COMP/Field", 0.24um) same-layer poly spacing rule between the two
        # rows' unrelated gate stripes. `height_um` stays the bare diffusion
        # height because it is the S/D ports' perpendicular width.
        "bbox_height_um": w_um + gate_ext_um + bottom_endcap_um,
        "gate_ext_um": gate_ext_um,
        "boxes_um": boxes,
        "s_xy": s_xy,
        "d_xy": d_xy,
        "g_xy": g_xy,
        "g_width_um": g_width_um,
        # Every S/D segment centre (fingers + 1 of them, west to east) and
        # every gate pad centre (fingers of them) -- issue #781. `s_xy`/
        # `d_xy`/`g_xy` above alias `seg_xy[0]`/`seg_xy[-1]`/`gate_xy[0]`.
        # `gate_xy` is `[]` only when `poly_positions` is (never happens for
        # `fingers >= 1`, since `_mos_finger_positions` always yields at
        # least one gate).
        "seg_xy": seg_xy,
        "gate_xy": gate_xy,
        # Perpendicular width of the reported S/D ports. Equal to the bare
        # diffusion height here (the ports sit on the S/D pads, which span
        # it); the strapped topology reports its strap-rail height instead
        # (see `_mos_unit_strapped_layout`).
        "sd_width_um": w_um,
        "finger_topology": "series",
    }


def _mos_unit_strapped_layout(
    w_um: float,
    l_um: float,
    fingers: int,
    gate_contact: bool = False,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
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

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) mirror :func:`_mos_unit_layout`'s own three knobs of the
    same name exactly -- see that function's docstring. The gate stripe's
    bottom edge here is ``diff_y0`` (coincident with the diffusion's own
    bottom edge, the same flush-endcap gap the bare "series" shape's
    ``y == 0`` edge has), so ``bottom_endcap_um`` extends it downward from
    there instead of from ``0.0``.
    """
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    clearance_um = MIN_SAME_LAYER_SPACING_UM
    seg_positions, poly_positions, total_len_um = _mos_finger_positions(
        l_um, fingers, contact_gate_offset_um
    )

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
        "poly": [
            (px0, diff_y0 - bottom_endcap_um, px1, comb_y0)
            for (px0, px1) in poly_positions
        ],
        "contact": [],
        "metal": [
            (0.0, 0.0, total_len_um, source_rail_y1),
            (0.0, drain_rail_y0, total_len_um, drain_rail_y1),
        ],
        "sd_implant": (
            [
                (
                    -sd_implant_margin_um,
                    diff_y0 - sd_implant_margin_um,
                    total_len_um + sd_implant_margin_um,
                    diff_y1 + sd_implant_margin_um,
                )
            ]
            if sd_implant_margin_um > 0
            else []
        ),
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


def _mos_array_well_tap_layout(
    min_x0_um: float, min_y0_um: float, max_x1_um: float, max_y1_um: float
) -> dict[str, Any]:
    """Well-tie tap pad for ``mos_array``'s ``flavor='pfet'`` well, on a PDK
    family whose curated deck derives a well tie from an implant-covered
    ``"active"`` shape inside the well rather than a distinct tap mask (issue
    #1473 -- see the ``"well_tap_implant"`` role in :data:`_PDK_ROLE_LAYERS`,
    e.g. ``sg13cmos5l``'s ``nSD``/``EXTRACTION_DECK.tap_nplus``).

    One pad ties the *entire* shared well: :func:`_mos_array_layout`'s own
    ``well_box_um`` already merges every unit device's well enclosure into a
    single shape, and the curated deck's own tie derivation
    (``tap_nplus_region & active & nwell``) connects that single ``nwell``
    conductor -- and therefore every PMOS body terminal sitting in it -- to
    whatever this one pad's contact/metal reach. So this is drawn once per
    array, never once per unit device, mirroring the "one net biases the
    whole tub" fact the derivation itself relies on.

    Placed :data:`MIN_SAME_LAYER_SPACING_UM` clear of the array's own active
    bounding box (``(min_x0_um, min_y0_um, max_x1_um, max_y1_um)``, the same
    box :func:`_mos_array_layout` margins into ``well_box_um``), on the
    *same* shared ``"active"`` role every unit device's own source/drain
    diffusion uses -- comfortably inside a curated deck's own same-layer
    spacing floor (e.g. sg13cmos5l's ``activ.space.1``, 0.21um), so the
    differently-implanted tap pad never collides with a real unit device's
    diffusion under DRC."""
    contact_region_um = CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM
    x0 = max_x1_um + MIN_SAME_LAYER_SPACING_UM
    y0 = min_y0_um
    x1 = x0 + contact_region_um
    y1 = y0 + contact_region_um
    contact_half = CONTACT_SIZE_UM / 2.0
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return {
        "active": (x0, y0, x1, y1),
        "contact": (
            cx - contact_half,
            cy - contact_half,
            cx + contact_half,
            cy + contact_half,
        ),
        "metal": (x0, y0, x1, y1),
        "width_um": contact_region_um,
        "xy": (cx, cy),
    }


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
    gate_pad_clearance_um: float = 0.0,
    draw_well_tap: bool = False,
    add_guard_ring: bool = False,
    ring_gap_side: str = "",
    ring_gap_um: float = 0.0,
    ring_gap_offset_um: float = 0.0,
    ring_padding_um: float = GUARD_RING_DEFAULT_PADDING_UM,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
    voltage_flavor_mark_margin_um: float = WELL_ENCLOSURE_MARGIN_UM,
) -> dict[str, Any]:
    """A ``rows`` x ``cols`` grid of :func:`_mos_unit_layout` unit devices,
    with ``dummy`` extra unit-device columns flanking each side.

    ``finger_topology`` is forwarded to :func:`_mos_unit_layout` and decides
    whether a ``fingers > 1`` unit is one parallel folded device (the
    default) or an unstrapped series chain (issue #777).
    ``gate_pad_clearance_um`` (issue #1450) is likewise forwarded; it grows
    each unit's ``bbox_height_um``, so the row pitch and the enclosing well
    follow it without any further arithmetic here (exactly as ``gate_contact``
    already does).

    Also computes ``well_box_um`` -- a single well shape (per
    :data:`WELL_ENCLOSURE_MARGIN_UM`) enclosing every cell's (real and dummy)
    active region, the same "one shared tub for the whole matched group"
    shape :func:`_bjt_array_layout` draws for its own shared base well --
    used only when the caller requests ``flavor="pfet"``.

    ``draw_well_tap`` (issue #1473) additionally computes ``well_tap`` (see
    :func:`_mos_array_well_tap_layout`) -- the well-tie pad only a PDK
    family with a ``"well_tap_implant"`` role needs -- and grows
    ``well_box_um`` to enclose it too. ``None`` (the default, ``False``) when
    not requested, so every caller that never asks for it keeps byte-for-byte
    identical geometry.

    ``add_guard_ring`` (issue #1493) additionally encloses the array in an
    automatically-sized tap/guard ring (``_ring_layout``, the same helper
    ``diff_pair``/``bjt_array``/``esd_device`` already compose), sized off
    ``well_box_um`` -- the array's own shared-footprint box regardless of
    ``flavor`` (it is computed unconditionally above; only *drawing* it as a
    well is gated on ``flavor == 'pfet'``) -- plus ``ring_padding_um``. This
    mirrors :func:`_bjt_array_layout`'s own ring-around-``well_box_um``
    composition (there fixed at :data:`BJT_COLLECTOR_GAP_UM` rather than a
    caller-supplied padding) more closely than :func:`_diff_pair_layout`'s
    (whose own core box always starts at the origin): ``well_box_um`` can
    start at a negative offset once ``dummy`` columns extend left of column
    0, so ``ring_offset_um`` is derived from ``well_box_um``'s own corner
    rather than assumed to be ``-(ring_w + padding)``. ``ring_gap_side``/
    ``ring_gap_um``/``ring_gap_offset_um`` cut one routing opening through the
    ring (#434), exactly as they do for every other ring-composing generator.
    ``None``/``(0.0, 0.0)`` (the defaults, ``add_guard_ring=False``) when not
    requested, so every existing caller keeps byte-for-byte identical
    geometry.

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) are forwarded to :func:`_mos_unit_layout` unchanged -- see
    that function's own docstring."""
    unit = _mos_unit_layout(
        w_um,
        l_um,
        fingers,
        gate_contact,
        finger_topology,
        gate_pad_clearance_um,
        contact_gate_offset_um,
        bottom_endcap_um,
        sd_implant_margin_um,
    )
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
    # `voltage_flavor` marker box (issue #1054, margin widened by #1577):
    # independent of `well_box` -- see `_voltage_flavor_mark_margin_um`'s own
    # docstring for why gf180mcu needs a bigger margin here than the well
    # itself does.
    mark_margin = voltage_flavor_mark_margin_um
    voltage_flavor_mark_box = (
        min_x0 - mark_margin,
        min_y0 - mark_margin,
        max_x1 + mark_margin,
        max_y1 + mark_margin,
    )

    well_tap = None
    if draw_well_tap:
        well_tap = _mos_array_well_tap_layout(min_x0, min_y0, max_x1, max_y1)
        tap_box = well_tap["active"]
        well_box = (
            min(well_box[0], tap_box[0] - margin),
            min(well_box[1], tap_box[1] - margin),
            max(well_box[2], tap_box[2] + margin),
            max(well_box[3], tap_box[3] + margin),
        )

    ring = None
    ring_offset = (0.0, 0.0)
    if add_guard_ring:
        padding = ring_padding_um
        ring_w = GUARD_RING_DEFAULT_WIDTH_UM
        inner_x0 = well_box[0] - padding
        inner_y0 = well_box[1] - padding
        inner_w = (well_box[2] - well_box[0]) + 2 * padding
        inner_h = (well_box[3] - well_box[1]) + 2 * padding
        ring = _ring_layout(
            inner_w,
            inner_h,
            ring_w,
            GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
            ring_gap_side,
            ring_gap_um,
            ring_gap_offset_um,
        )
        ring_offset = (inner_x0 - ring_w, inner_y0 - ring_w)

    return {
        "unit": unit,
        "col_pitch_um": col_pitch,
        "row_pitch_um": row_pitch,
        "cells": cells,
        "dummy_cells": dummy_cells,
        "well_box_um": well_box,
        "voltage_flavor_mark_box_um": voltage_flavor_mark_box,
        "well_tap": well_tap,
        "ring": ring,
        "ring_offset_um": ring_offset,
    }


def _res_unit_layout(
    length_um: float,
    width_um: float,
    via_min_w_um: float = 0.0,
    via_enclosure_min_um: float = 0.0,
) -> dict[str, Any]:
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

    ``via_min_w_um``/``via_enclosure_min_um`` (issue #1639, ``0.0`` by
    default) are ``res_array``'s ``metal_level`` per-level geometry floors --
    see :data:`_PDK_METAL_RES_LEVEL_MIN_UM` -- applied as ``max(generic,
    floor)`` exactly like :func:`_cap_unit_layout`'s own ``*_min_*``
    arguments, so the default ``0.0`` leaves every existing (poly-body) caller
    byte-for-byte unchanged. Despite the ``poly``/``contact``/``metal`` box
    keys' names (kept as-is so ``_ResArrayPCell.produce_impl`` needs no
    change), this same geometry is what a ``metal_level`` request draws its
    ``metN`` body / end via / landing pad on -- see
    :func:`_resistor_layer_params`'s own docstring for how those roles are
    resolved to different physical layers depending on ``params.metal_level``.
    """
    contact_side_um = max(CONTACT_SIZE_UM, via_min_w_um)
    enclosure_um = max(ENCLOSURE_MARGIN_UM, via_enclosure_min_um)
    contact_region_um = contact_side_um + 2 * enclosure_um
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
    contact_half = contact_side_um / 2.0
    for sx0, sx1 in seg_positions:
        boxes["metal"].append((sx0, 0.0, sx1, width_um))
        cx = (sx0 + sx1) / 2.0
        cy = width_um / 2.0
        # Snap the centre to the dbu grid *before* deriving the edges -- an
        # un-snapped `cx +/- contact_half` lets the two edges round in
        # opposite directions when the centre lands on a half-dbu tie (e.g.
        # length_um=1.4965), silently drawing a 219nm contact against
        # gf180mcu's 220nm `contact.width.1` floor (issue #1551; same
        # failure mode `_snap_square_box_um` was introduced for in #685).
        boxes["contact"].append(_snap_square_box_um(cx, cy, contact_half, _GRID_DBU_UM))

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
    via_min_w_um: float = 0.0,
    via_enclosure_min_um: float = 0.0,
    min_spacing_um: float = 0.0,
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

    ``via_min_w_um``/``via_enclosure_min_um`` (issue #1639, forwarded
    unchanged to :func:`_res_unit_layout`) and ``min_spacing_um`` are
    ``metal_level``'s own per-level geometry floors (see
    :data:`_PDK_METAL_RES_LEVEL_MIN_UM`) -- ``min_spacing_um`` is this level's
    own minimum spacing between adjacent unit resistors' end vias, applied to
    ``spacing_um`` as ``max(spacing_um, min_spacing_um)`` exactly like
    :func:`_cap_array_layout`'s own ``min_spacing_um`` floor, and reported
    back as this function's own ``spacing_um`` so the caller (
    :func:`_res_array_describe`) can note when it widened the request. All
    three default to ``0.0``, leaving every existing (poly-body) caller
    byte-for-byte unchanged.
    """
    unit = _res_unit_layout(length_um, width_um, via_min_w_um, via_enclosure_min_um)
    effective_spacing_um = max(spacing_um, min_spacing_um)
    pitch = unit["total_len_um"] + effective_spacing_um
    # Row-to-row pitch (issue #1639): the same `min_spacing_um` floor applies
    # here too -- it is the *same* same-layer same-metal spacing rule
    # (sky130's `met5.space.1`) that binds between two body shapes whether
    # they are adjacent within one row or across a row transition, so using
    # the generic `MIN_SAME_LAYER_SPACING_UM` unconditionally here (while
    # `pitch` above already floors under `min_spacing_um`) would leave a
    # folded (`rows > 1`) met5 request DRC-dirty even though the unfolded
    # case is clean.
    row_pitch = unit["height_um"] + max(MIN_SAME_LAYER_SPACING_UM, min_spacing_um)
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
        "spacing_um": effective_spacing_um,
    }


def _cap_unit_layout(
    plate_w_um: float,
    plate_h_um: float,
    top_via_metal_min_w_um: float = 0.0,
    top_via_min_w_um: float = 0.0,
    bottom_plate_margin_min_um: float = 0.0,
) -> dict[str, Any]:
    """One unit MiM capacitor cell: a ``plate_w_um`` x ``plate_h_um``
    top-plate mark (sky130's ``capm``) centred over a larger bottom-plate
    conductor (``met3``), with a top-plate via + local-metal landing pad
    (``via3``/``met4``) centred on the top plate -- the capacitor sibling of
    :func:`_res_unit_layout`, with a single centred via/pad standing in for
    that function's two end contacts (a MiM cap has no resistive body to
    keep a via clear of, so the via lands in the middle of the plate rather
    than at either end).

    The three ``*_min_*`` arguments are per-PDK-family geometry *floors*
    (issues #1455/#1555, resolved by :func:`_cap_geometry_min_um` from
    :data:`_PDK_CAP_GEOMETRY_MIN_UM`), each applied as ``max(generic,
    floor)`` so the default ``0.0`` leaves a family that overrides nothing --
    sky130 -- byte-for-byte unchanged:

    - ``top_via_metal_min_w_um`` widens the drawn landing pad (and the escape
      stub/pad drawn on the same layer) past the generic
      ``CONTACT_SIZE_UM + 2*ENCLOSURE_MARGIN_UM`` when a family's own
      top-plate-via-metal layer carries a coarser minimum-width rule
      (sg13g2's ``TopMetal1``, 1.64um vs. sky130's ``met4``, 0.3um).
    - ``top_via_min_w_um`` widens the drawn via past ``CONTACT_SIZE_UM`` when
      the family's top-plate via has a coarser minimum size than the
      *contact* rule that constant was sized against (gf180mcu's ``Via4``,
      0.26um).
    - ``bottom_plate_margin_min_um`` widens the bottom plate past
      ``CAP_BOTTOM_PLATE_MARGIN_UM`` for a family whose bottom plate is the
      DRM's oversized "virtual bottom plate" rather than an ordinary
      conductor (gf180mcu's ``Metal4``, drawn at
      ``CapacitorDevice.bottom_plate_oversize_um`` = 1.06um so the drawn
      shape *is* the derived virtual plate).

    On top of the via-enclosing landing pad centred on the plate (which sits
    directly over the bottom plate -- unroutable in isolation, issue #1494),
    this also draws a same-layer escape: a stub running north from that
    centred pad, clear past the bottom plate's own top edge by
    :data:`CAP_TOP_ESCAPE_CLEARANCE_UM`, ending in a second landing pad
    (``top_escape_xy``) whose own bounding box no longer touches -- let alone
    overlaps -- the bottom plate. A via stack landed at ``top_escape_xy``
    using the family's own metal/via stack can step straight down without
    ever crossing the bottom plate's footprint, unlike the centred
    ``top_xy`` (kept for reference/backward compatibility) that a further
    via step would silently short to the bottom plate."""
    margin = max(CAP_BOTTOM_PLATE_MARGIN_UM, bottom_plate_margin_min_um)
    bottom_w = plate_w_um + 2 * margin
    bottom_h = plate_h_um + 2 * margin
    cx, cy = bottom_w / 2.0, bottom_h / 2.0

    via_side = max(CONTACT_SIZE_UM, top_via_min_w_um)
    via_half = via_side / 2.0
    # The landing pad keeps its `ENCLOSURE_MARGIN_UM` clearance around
    # whatever via was actually drawn -- so a family whose via floor widened
    # the cut (gf180mcu's `Via4`) widens the pad with it, rather than
    # silently eating into the enclosure. Identical to the pre-#1555
    # expression on every family whose via is the generic `CONTACT_SIZE_UM`.
    pad_side = max(via_side + 2 * ENCLOSURE_MARGIN_UM, top_via_metal_min_w_um)
    pad_half = pad_side / 2.0
    top_via_box = _snap_square_box_um(cx, cy, via_half, _GRID_DBU_UM)
    top_via_metal_box = _snap_square_box_um(cx, cy, pad_half, _GRID_DBU_UM)

    # Escape stub + landing pad (issue #1494): same layer/width as
    # `top_via_metal_box` (so it inherits that pad's own family-specific
    # minimum-width compliance, e.g. sg13g2's `TopMetal1`), running straight
    # north from the centred pad's top edge to a landing pad entirely beyond
    # `bottom_h` -- i.e. entirely outside the bottom plate's own bounding
    # box, not merely touching it.
    escape_pad_cy = bottom_h + CAP_TOP_ESCAPE_CLEARANCE_UM + pad_half
    escape_pad_box = _snap_square_box_um(cx, escape_pad_cy, pad_half, _GRID_DBU_UM)
    escape_stub_box = (
        cx - pad_half,
        top_via_metal_box[3],
        cx + pad_half,
        escape_pad_box[1],
    )

    boxes: dict[str, list[tuple[float, float, float, float]]] = {
        "bottom_plate": [(0.0, 0.0, bottom_w, bottom_h)],
        "top_plate": [(margin, margin, margin + plate_w_um, margin + plate_h_um)],
        "top_via": [top_via_box],
        "top_via_metal": [top_via_metal_box, escape_stub_box, escape_pad_box],
    }
    return {
        "total_w_um": bottom_w,
        "total_h_um": bottom_h,
        "boxes_um": boxes,
        # The landing-pad side actually used above -- read back by
        # `_cap_array_describe` for its `C<i>_TOP` port width instead of that
        # function recomputing the sizing rule, so the reported width can
        # never drift from the drawn pad as more per-family floors land
        # (issue #1555; the value is unchanged for sky130/sg13g2).
        "top_pad_w_um": pad_side,
        # Bottom-plate port: the conductor's own local-left edge, mirroring
        # `_res_unit_layout`'s `a_xy`. Legacy top-plate centre: the
        # via/landing-pad centre -- the same point `top_via`/the original
        # `top_via_metal` pad are drawn at (issue #1494: unroutable, kept
        # only for internal reference). Escape top-plate port: the landing
        # pad past the bottom plate's own bbox -- the point
        # `_cap_array_describe` reports as `C{idx}_TOP` today.
        "bot_xy": (0.0, cy),
        "top_xy": (cx, cy),
        "top_escape_xy": (cx, escape_pad_cy),
    }


def _cap_array_layout(
    plate_w_um: float,
    plate_h_um: float,
    spacing_um: float,
    num: int,
    top_via_metal_min_w_um: float = 0.0,
    top_via_min_w_um: float = 0.0,
    bottom_plate_margin_min_um: float = 0.0,
    min_spacing_um: float = 0.0,
) -> dict[str, Any]:
    """``num`` matched unit MiM capacitors (see :func:`_cap_unit_layout`) in
    a single row, spaced ``spacing_um`` apart -- the capacitor sibling of
    :func:`_res_array_layout`'s own (unfolded, single-row) layout. No
    ``rows`` folding or ``dummy`` padding yet (issue #1117 scopes this
    generator's first increment to the core plate/via geometry; both are
    natural `res_array`-parity follow-ups, not correctness gaps -- a MiM
    cap array with only a handful of matched units rarely needs either).

    ``top_via_metal_min_w_um``/``top_via_min_w_um``/
    ``bottom_plate_margin_min_um`` are forwarded to :func:`_cap_unit_layout`
    unchanged (issues #1455/#1555).

    ``min_spacing_um`` (issue #1555) is this PDK family's own floor under the
    requested ``spacing_um``: gf180mcu's ``mim.space.1`` (MIMTM.1) requires
    1.2um between the virtual bottom plates of adjacent MiM capacitors, far
    more than this generator's 0.5um default or its generic
    ``MIN_SAME_LAYER_SPACING_UM`` advisory, so a request that would otherwise
    draw a guaranteed violation is widened instead of rejected (the effective
    spacing is what :func:`_cap_array_describe` reports as
    ``drc_hints.min_spacing_um``, alongside a note). ``0.0`` -- every family
    but gf180mcu -- leaves the requested spacing exactly as asked."""
    unit = _cap_unit_layout(
        plate_w_um,
        plate_h_um,
        top_via_metal_min_w_um,
        top_via_min_w_um,
        bottom_plate_margin_min_um,
    )
    effective_spacing_um = max(spacing_um, min_spacing_um)
    pitch = unit["total_w_um"] + effective_spacing_um
    cells = [{"idx": i, "x0_um": i * pitch, "y0_um": 0.0} for i in range(num)]
    return {
        "unit": unit,
        "pitch_um": pitch,
        "cells": cells,
        "spacing_um": effective_spacing_um,
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


#: Order ``well_island`` prefers when picking which ring side carries its net
#: label text (see :func:`_well_island_label_point`). Any side still covered
#: by ring metal works electrically -- the order only makes the choice
#: deterministic, so the drawn GDS and the reported label position never
#: disagree between two runs (or between ``produce_impl`` and ``describe``).
_WELL_ISLAND_LABEL_SIDE_ORDER = ("S", "N", "W", "E")


def _well_island_label_point(ring: dict[str, Any]) -> tuple[float, float] | None:
    """Where ``well_island`` places its net-label text: the midpoint of the
    first side of ``ring`` (in :data:`_WELL_ISLAND_LABEL_SIDE_ORDER`) that is
    still covered by ring metal.

    A point on a side's own centre line always falls *inside* the drawn metal
    band, which is what makes the text attach to that conductor during
    extraction. ``None`` only when the ring has no covered side left at all
    -- structurally impossible today (``ring_gap_side`` opens at most one
    side), but handled rather than assumed.
    """
    for side in _WELL_ISLAND_LABEL_SIDE_ORDER:
        xy = ring["ports"].get(side)
        if xy is not None:
            return xy
    return None


def _well_box_um(
    ring: dict[str, Any], margin_um: float
) -> tuple[float, float, float, float]:
    """The well rectangle enclosing ``ring``'s outer box by ``margin_um`` on
    every side -- the same shape ``guard_ring``'s ``add_well`` already draws,
    factored out so ``well_island``'s isolation math and its ``produce_impl``
    can never derive it differently."""
    return (
        -margin_um,
        -margin_um,
        ring["outer_w_um"] + margin_um,
        ring["outer_h_um"] + margin_um,
    )


def _box_separation_um(
    a_um: tuple[float, float, float, float],
    b_um: tuple[float, float, float, float],
) -> float:
    """Euclidian separation (um) between two axis-aligned rectangles: ``0.0``
    when they touch or overlap, otherwise the corner-to-corner distance
    between their nearest points.

    Euclidian rather than per-axis on purpose -- it is the metric the well
    isolation rules this module enforces are themselves written with (sky130's
    ``nwell.isolated(1.27, euclidian)``; see :data:`_PDK_WELL_ISOLATION_UM`),
    and it is the *stricter* of the two at a diagonal offset, so a
    geometry this function accepts is never one the rule's own metric would
    reject.
    """
    ax0, ay0, ax1, ay1 = a_um
    bx0, by0, bx1, by1 = b_um
    dx = max(0.0, bx0 - ax1, ax0 - bx1)
    dy = max(0.0, by0 - ay1, ay0 - by1)
    return math.hypot(dx, dy)


def _parse_well_regions(
    generator: str, param_name: str, raw: Any
) -> list[dict[str, Any]]:
    """Parse a ``params.isolate_from``-shaped value into normalised well
    regions (issue #1421).

    Each entry is ``[x0, y0, x1, y1]`` or ``[x0, y0, x1, y1, "<net>"]`` -- a
    rectangle in the generated cell's **own** coordinate frame (the same frame
    the response's ``bbox_um``/``ports[]`` use), optionally tagged with the
    net that region is held at. The optional net is what makes the
    "equipotential wells may merge" case expressible: a neighbour tagged with
    the *same* net as the island's own tie is not a different-potential
    neighbour, so the isolation rule does not apply to it (see
    :func:`_well_island_isolation`).

    Corner order is normalised (``min``/``max``), so a caller who hands over a
    bbox with either corner first gets the same answer. A degenerate
    (zero-area) rectangle is rejected -- it is far more likely a caller bug
    than an intentional request, and it would silently satisfy every
    separation check by having no extent to violate one with.
    """
    if not isinstance(raw, list):
        raise GenError(
            f"generator '{generator}': params.{param_name} must be a JSON array"
        )
    regions: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        where = f"params.{param_name}[{index}]"
        if not isinstance(entry, (list, tuple)) or len(entry) not in (4, 5):
            raise GenError(
                f"generator '{generator}': {where} must be "
                "[x0_um, y0_um, x1_um, y1_um] or "
                "[x0_um, y0_um, x1_um, y1_um, net]"
            )
        coords: list[float] = []
        for value in entry[:4]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GenError(
                    f"generator '{generator}': {where}'s first four elements "
                    "must be numbers (a rectangle in um)"
                )
            coords.append(float(value))
        x0, y0, x1, y1 = coords
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if box[0] == box[2] or box[1] == box[3]:
            raise GenError(
                f"generator '{generator}': {where} is a zero-area rectangle "
                f"({coords}) -- a well region with no extent cannot be "
                "separated from anything"
            )
        net: str | None = None
        if len(entry) == 5:
            if not isinstance(entry[4], str):
                raise GenError(
                    f"generator '{generator}': {where}'s optional fifth "
                    "element must be a net name string"
                )
            net = entry[4] or None
        regions.append({"box_um": box, "net": net})
    return regions


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
    gate_pad_clearance_um: float = 0.0,
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
) -> dict[str, Any]:
    """Two matched devices (``"A"``/``"B"``), each split into ``splits``
    unit sub-instances, interleaved in a true common-centroid cross-quad
    checkerboard over a 2-row x ``splits``-col grid: ``label(row, col) = "A"
    if (row + col) is even else "B"`` -- for ``splits=2`` this is exactly the
    classic differential-pair "A B / B A" layout; it generalises the same
    way for any ``splits``, each column always holding one A and one B.

    Both rows place their unit device at the same ``x0_um = col *
    col_pitch``, so the two legs **share one x column per terminal** -- A's
    and B's ports in a given column differ only in ``y_um`` (issue #1495).
    That is a property of the interleave, not an oversight: separating the
    legs in x by a constant per-leg offset would shift one leg's x centroid
    relative to the other's, destroying exactly the gradient cancellation
    this layout exists to provide. (Nor is there a cleverer uniform-pitch
    ordering for odd ``splits``: placing ``2 * splits`` units at distinct
    multiples of one pitch and partitioning them into two equal-x-centroid
    halves needs the position sum ``splits * (2 * splits - 1)`` to be even,
    which fails for every odd ``splits``.) The caveat is documented for
    callers in ``docs/cli/gen.md`` and surfaced in ``klt gen --list``.

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
    here. ``gate_pad_clearance_um`` (issue #1450) is forwarded the same way,
    with the same knock-on effect. ``contact_gate_offset_um``/
    ``bottom_endcap_um``/``sd_implant_margin_um`` (issue #1577) are likewise
    forwarded unchanged -- see :func:`_mos_unit_layout`'s own docstring.
    """
    unit = _mos_unit_layout(
        w_um,
        l_um,
        1,
        gate_contact,
        gate_pad_clearance_um=gate_pad_clearance_um,
        contact_gate_offset_um=contact_gate_offset_um,
        bottom_endcap_um=bottom_endcap_um,
        sd_implant_margin_um=sd_implant_margin_um,
    )
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
    contact_gate_offset_um: float = 0.0,
    bottom_endcap_um: float = 0.0,
    sd_implant_margin_um: float = 0.0,
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
    itself (``diff_pair`` doesn't either; see that class's own docstring).

    ``contact_gate_offset_um``/``bottom_endcap_um``/``sd_implant_margin_um``
    (issue #1577) are forwarded to :func:`_mos_unit_layout` unchanged -- see
    that function's own docstring."""
    unit = _mos_unit_layout(
        finger_width_um,
        l_um,
        fingers,
        gate_contact,
        contact_gate_offset_um=contact_gate_offset_um,
        bottom_endcap_um=bottom_endcap_um,
        sd_implant_margin_um=sd_implant_margin_um,
    )

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
            "dbu_um": float,
            "bbox_um": {"x0": float, "y0": float, "x1": float, "y1": float},
            "device_count": int,
            "ports": [...],
            "drc_hints": {...},
            "warnings": [...],
        }

    ``dbu_um`` (issue #1496) reports the database unit the output stream was
    actually written at -- resolved from the *resolved PDK's own tech LEF*
    (``DATABASE MICRONS``) by :func:`~klayout_tools.pdk.resolve_pdk_dbu`, not
    a fixed ``0.001`` -- so a caller can confirm two blocks agree before
    handing them to ``klt gen-compose`` (which requires one shared dbu across
    every block) without a separate ``klt stats`` round-trip. Additive field,
    no ``schema_version`` bump (see ``docs/json-contract.md``).

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
        "dbu_um": dbu,
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
    if ptype == Decl.TypeList:
        # A JSON array passes through as a plain Python list -- KLayout's
        # PCell variant machinery round-trips nested lists unchanged (see
        # `_WellIslandPCell.isolate_from`). Element *shape* is the individual
        # generator's business, validated in its own `validate()` (e.g.
        # :func:`_parse_well_regions`), the same division of labour every
        # other param type here follows: this function only enforces the
        # declared JSON type.
        if not isinstance(value, list):
            raise GenError(f"generator '{generator}': params.{name} must be an array")
        return list(value)
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

    The output layout's dbu is resolved from ``pdk_info``'s own tech LEF via
    :func:`~klayout_tools.pdk.resolve_pdk_dbu` (issue #1496) -- e.g. 0.0005
    for gf180mcu's ``DATABASE MICRONS 2000`` -- so a ``klt gen`` output
    agrees, by construction, with a ``klt place_and_route`` output resolved
    against the same PDK (both derive their dbu from the same tech LEF, the
    way `place_and_route`'s own DEF/GDS merge already does per issue #1032).
    Falls back to ``spec.dbu`` (the generator's hardcoded default, historically
    always 0.001) when the resolver returns ``None`` -- a PDK whose tech LEF
    is missing or unparsable never fails a `klt gen` request that previously
    succeeded, it just keeps today's hardcoded dbu.
    """
    import klayout.db as kdb

    lib = _pcell_library()
    decl = lib.layout().pcell_declaration(spec.name)

    pcell_values = dict(resolved_params)
    pcell_values.update(spec.layer_params(pdk_info, resolved_params))

    layout = kdb.Layout()
    layout.dbu = resolve_pdk_dbu(pdk_info) or spec.dbu
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
    """Assemble every reference generator's ``PCellDeclarationHelper``
    subclass into one name -> class mapping.

    Thin composer over the per-family ``_build_<family>_pcell()`` factory
    functions below (one per generator family) -- each factory still does
    its own local ``import klayout.db as kdb`` so no factory pays the
    import cost until it's actually called, and this function's own
    ``dict[str, type[...]]`` return shape is unchanged from when all ten
    classes were defined inline here.
    """
    pcell_classes: dict[str, type[kdb.PCellDeclarationHelper]] = {}
    pcell_classes.update(_build_resistor_strip_pcell())
    pcell_classes.update(_build_mos_array_pcell())
    pcell_classes.update(_build_res_array_pcell())
    pcell_classes.update(_build_cap_array_pcell())
    pcell_classes.update(_build_guard_ring_pcell())
    pcell_classes.update(_build_well_island_pcell())
    pcell_classes.update(_build_diff_pair_pcell())
    pcell_classes.update(_build_bjt_array_pcell())
    pcell_classes.update(_build_bond_pad_pcell())
    pcell_classes.update(_build_esd_device_pcell())
    return pcell_classes


def _build_resistor_strip_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``resistor_strip`` reference generator's PCell -- a row of
    parametrized rectangles standing in for a unit-resistor string.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
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

    return {"resistor_strip": _ResistorStripPCell}


def _build_mos_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``mos_array`` reference generator's PCell -- a matched MOS
    transistor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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
        :func:`_mos_array_describe`).

        ``voltage_flavor`` (issue #1054) optionally draws a PDK
        medium-voltage/thick-oxide device-class marker (e.g. gf180mcu's
        ``Dualgate``) sized to enclose every unit device (real and dummy) --
        the same shared box ``flavor="pfet"``'s well shape already encloses.
        The default, an empty string, draws nothing (byte-for-byte unchanged
        geometry). See :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` for the flavour
        names each PDK family recognises."""

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
                "voltage_flavor",
                self.TypeString,
                "Optional medium-voltage/thick-oxide device-class marker: "
                "'' (default, no marker drawn) or a name the resolved PDK "
                "family's role-layer table recognises (e.g. 'medium_voltage' "
                "on gf180mcu, drawing its Dualgate marker). A name the family "
                "doesn't recognise draws nothing and is reported via "
                "drc_hints.notes, never silently dropped",
                default="",
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
            self.param(
                "well_tap_implant_layer",
                self.TypeLayer,
                "Well-tie implant layer drawn over the well-tap pad (only "
                "used when flavor is 'pfet', well_present, and "
                "well_tap_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_tap_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "a tap pad on the shared active role as a well tie",
                default=False,
            )
            self.param(
                "metal_label_layer",
                self.TypeLayer,
                "Label layer the well-tap pad's fixed net-name text is drawn "
                "on (only used when well_tap_implant_present and "
                "metal_label_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_label_present",
                self.TypeBoolean,
                "Whether metal_label_layer is a real label layer for the resolved PDK",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_layer",
                self.TypeLayer,
                "Medium-voltage/thick-oxide device-class marker drawing "
                "layer, sized to enclose every unit device (only used when "
                "voltage_flavor_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "voltage_flavor_mark_present",
                self.TypeBoolean,
                "Whether voltage_flavor_mark_layer is a real layer resolved "
                "for params.voltage_flavor on this PDK family",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the voltage_flavor marker box grows "
                "beyond the array's own shared active footprint (see "
                "_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM)",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )
            self.param(
                "gate_pad_clearance_um",
                self.TypeDouble,
                "Harness-resolved clearance the gate-poly landing pad keeps "
                "off the diffusion edge (see _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM)",
                default=0.0,
            )
            self.param(
                "contact_gate_offset_um",
                self.TypeDouble,
                "Harness-resolved extra S/D-contact-to-gate offset floor "
                "(see _PDK_CONTACT_GATE_EXTRA_OFFSET_UM)",
                default=0.0,
            )
            self.param(
                "bottom_endcap_um",
                self.TypeDouble,
                "Harness-resolved gate-stripe bottom-edge endcap extension "
                "(see _PDK_GATE_BOTTOM_ENDCAP_UM)",
                default=0.0,
            )
            self.param(
                "sd_implant_layer",
                self.TypeLayer,
                "Source/drain implant drawing layer (only used when "
                "sd_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "sd_implant_present",
                self.TypeBoolean,
                "Whether sd_implant_layer is a real layer this PDK family "
                "needs over each unit device's own active body",
                default=False,
            )
            self.param(
                "sd_implant_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the source/drain implant box grows "
                "beyond each unit device's own active box (only used when "
                "sd_implant_present)",
                default=0.0,
            )
            self.param(
                "add_guard_ring",
                self.TypeBoolean,
                "Enclose the array in an automatically-sized tap/guard ring "
                "(issue #1493)",
                default=False,
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
                "Padding between the array's own shared-footprint box "
                "(well_box_um) and the guard ring's inner edge (um), when "
                "add_guard_ring is set",
                default=GUARD_RING_DEFAULT_PADDING_UM,
            )
            self.param(
                "tap_layer",
                self.TypeLayer,
                "Guard ring tap drawing layer (only used when add_guard_ring is set)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Guard-ring implant drawing layer, exactly coincident with "
                "the tap ring (only used when add_guard_ring and "
                "ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the guard ring's own shape (see _ring_tap_implant_layer)",
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
            # Well-tie tap pad (issue #1473): only meaningful for a drawn
            # well (`flavor == 'pfet'` and `well_present`) on a family whose
            # curated deck needs an implant mask to recognise a tap pad on
            # the shared `active` role as a well tie (see
            # `_mos_array_well_tap_layout`'s own docstring). Resolved once,
            # up front, so `_mos_array_layout` computes (and
            # `well_box_um` encloses) the exact same geometry this
            # `produce_impl` draws below.
            draw_well_tap = (
                self.flavor == "pfet"
                and self.well_present
                and self.well_tap_implant_present
            )
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
                self.gate_pad_clearance_um,
                draw_well_tap,
                self.add_guard_ring,
                self.ring_gap_side,
                self.ring_gap_um,
                self.ring_gap_offset_um,
                self.ring_padding_um,
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
                self.voltage_flavor_mark_margin_um,
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

            # Source/drain implant (issue #1577): drawn over every real *and*
            # dummy unit device's own active body -- DF.12-style coverage
            # rules apply to every drawn Comp shape, dummy columns included.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                for c in info["cells"] + info["dummy_cells"]:
                    _insert_boxes(
                        self.cell,
                        li_sd_implant,
                        dbu,
                        unit_boxes["sd_implant"],
                        c["x0_um"],
                        c["y0_um"],
                    )

            if self.flavor == "pfet" and self.well_present:
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [info["well_box_um"]])

            if draw_well_tap and info["well_tap"] is not None:
                well_tap = info["well_tap"]
                _insert_boxes(self.cell, li_active, dbu, [well_tap["active"]])
                _insert_boxes(self.cell, li_contact, dbu, [well_tap["contact"]])
                _insert_boxes(self.cell, li_metal, dbu, [well_tap["metal"]])
                # Exactly coincident with the tap pad's own active box --
                # never a blanket over anything else -- so only the tap pad
                # itself is n+ implanted, mirroring `well_island`'s own
                # `well_tap_implant` ring precedent.
                _insert_boxes(
                    self.cell,
                    self.layout.layer(self.well_tap_implant_layer),
                    dbu,
                    [well_tap["active"]],
                )
                # Fixed net-name label (issue #1473, see
                # `_MOS_ARRAY_WELL_TAP_NET_LABEL`'s own docstring): without
                # *some* drawn label, `klt extract` always synthesizes an
                # anonymous `$<n>` net for this tie regardless of how well it
                # is physically connected, so `unbiased_pmos_body_nets[]`
                # would still fire even though the well is genuinely tied.
                if self.metal_label_present:
                    tap_cx, tap_cy = well_tap["xy"]
                    self.cell.shapes(self.layout.layer(self.metal_label_layer)).insert(
                        kdb.Text(
                            _MOS_ARRAY_WELL_TAP_NET_LABEL,
                            kdb.Trans(
                                kdb.Vector(
                                    int(round(tap_cx / dbu)), int(round(tap_cy / dbu))
                                )
                            ),
                        )
                    )

            # Automatically-sized tap/guard ring (issue #1493): composed the
            # same way `diff_pair`/`esd_device`/`bjt_array` already compose
            # `_ring_layout`, sized off `well_box_um` (the array's own
            # shared-footprint box, computed above regardless of `flavor`).
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
                # The ring's own well tie (independent of the device-array
                # well drawn above, which already merges with it on the same
                # `well_layer` -- mirrors `_diff_pair_layout`'s identical
                # "both land on the same layer, so they simply merge" ring
                # well-tie composition): only for `flavor == 'pfet'`, so the
                # default `flavor='nfet'` case never encloses an NMOS array
                # in a well the way #421's diff_pair regression test guards
                # against.
                if self.well_present and self.flavor == "pfet":
                    li_well = self.layout.layer(self.well_layer)
                    margin = WELL_ENCLOSURE_MARGIN_UM
                    ring_well_box = (
                        -margin,
                        -margin,
                        ring["outer_w_um"] + margin,
                        ring["outer_h_um"] + margin,
                    )
                    _insert_boxes(
                        self.cell, li_well, dbu, [_shift_box(ring_well_box, ox, oy)]
                    )
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- never a blanket over the enclosed array.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

            # Medium-voltage/thick-oxide device-class marker (issue #1054):
            # sized to enclose every unit device (real and dummy) --
            # independent of `flavor`, so a caller can request both a well
            # (pfet) and a voltage-flavor marker (or either alone). Its own
            # box (`voltage_flavor_mark_box_um`) is *not* the same box the
            # well shape above encloses (issue #1577): gf180mcu's real
            # signoff deck needs the marker to reach further than the well's
            # own margin does -- see `_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`'s
            # own docstring.
            if self.voltage_flavor_mark_present:
                li_voltage_flavor_mark = self.layout.layer(
                    self.voltage_flavor_mark_layer
                )
                _insert_boxes(
                    self.cell,
                    li_voltage_flavor_mark,
                    dbu,
                    [info["voltage_flavor_mark_box_um"]],
                )

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

    return {"mos_array": _MosArrayPCell}


def _build_res_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``res_array`` reference generator's PCell -- a resistor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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
                "res_generic_po on sky130, ppolyf_u on gf180mcu, rsil on "
                "sg13g2), or, on sky130, 'high' (res_high_po) / 'xhigh' "
                "(res_xhigh_po), or, on gf180mcu, '1k' / '2k' / '3k' "
                "(ppolyf_u_1k/_2k/_3k, selected at extraction time via "
                "'klt extract --deck-option poly_res='), or, on sg13g2, "
                "'rppd' (260 ohm/sq) / 'rhigh' (1360 ohm/sq) for the "
                "higher-sheet-rho flavours (issues #463/#1451/#1550)",
                default=_DEFAULT_RES_FLAVOR,
            )
            self.param(
                "metal_level",
                self.TypeInt,
                "Draw a metal-layer resistor body instead of poly: 0 "
                "(default) draws the original poly-body resistor selected "
                "by 'flavor'; 1..5 on sky130 draws that level's "
                "res_generic_mN device (met1..met5, its own resistor-ID "
                "marker, and an end via/landing-pad stack down to the metal "
                "level below) instead, ignoring 'flavor' entirely (issue "
                "#1639)",
                default=0,
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
            # One static slot per mask the *widest* flavour in
            # `_PDK_RES_FLAVOR_LAYERS` requires (issue #1451). A KLayout
            # PCell's parameter list is fixed at declaration time, so the
            # count comes from `_MAX_RES_FLAVOR_LAYERS` rather than the
            # per-request flavour; a narrower flavour simply leaves its tail
            # slots absent. Before #1451 these were two fixed
            # `res_implant`/`res_block` slots, which could not express
            # sg13g2's three-mask `rppd` / four-mask `rhigh` classes.
            for i in range(_MAX_RES_FLAVOR_LAYERS):
                self.param(
                    f"res_flavor_{i}_layer",
                    self.TypeLayer,
                    f"Flavour requires-mask #{i} (implant / precision-resistor "
                    "/ salicide-block layer, e.g. gf180mcu's Pplus or SAB) "
                    "drawn over the body segment for device recognition "
                    f"(only used when res_flavor_{i}_present)",
                    default=kdb.LayerInfo(0, 0),
                )
                self.param(
                    f"res_flavor_{i}_present",
                    self.TypeBoolean,
                    f"Whether res_flavor_{i}_layer is a real, DRC-checked "
                    "layer the resolved PDK's requested flavour requires",
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
            # `metal_level`'s own per-level geometry floors (issue #1639) --
            # harness-computed from the resolved PDK family/level exactly
            # like `cap_array`'s own `cap_top_via_min_w_um`/friends. `0.0`
            # (the poly-body default) leaves `_res_unit_layout`'s generic
            # CONTACT_SIZE_UM/ENCLOSURE_MARGIN_UM/MIN_SAME_LAYER_SPACING_UM
            # geometry unchanged -- see that function's own docstring.
            self.param(
                "metal_res_via_min_w_um",
                self.TypeDouble,
                "Minimum drawn side (um) for metal_level's end via on the "
                "resolved PDK family -- 0.0 leaves the generic "
                "CONTACT_SIZE_UM cut unchanged",
                default=0.0,
            )
            self.param(
                "metal_res_via_enclosure_min_um",
                self.TypeDouble,
                "Minimum enclosure (um) of metal_level's end via by the body "
                "and landing-pad layers on the resolved PDK family -- 0.0 "
                "leaves the generic ENCLOSURE_MARGIN_UM unchanged",
                default=0.0,
            )
            self.param(
                "metal_res_via_space_min_um",
                self.TypeDouble,
                "Minimum spacing (um) between metal_level's end vias on "
                "adjacent unit resistors on the resolved PDK family -- 0.0 "
                "draws exactly the requested spacing_um",
                default=0.0,
            )

        def display_text_impl(self) -> str:
            return (
                f"res_array(l={self.length_um},w={self.width_um},"
                f"n={self.num},rows={self.rows},flavor={self.flavor},"
                f"metal_level={self.metal_level})"
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
                self.metal_res_via_min_w_um,
                self.metal_res_via_enclosure_min_um,
                self.metal_res_via_space_min_um,
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

            # Draw the PDK resistor-ID marker, plus every requires-mask the
            # requested flavour needs (none on sky130's `res_generic_po`, two
            # on gf180mcu's `ppolyf_u` and sg13g2's `rsil`, three on sg13g2's
            # `rppd`, four on its `rhigh` -- issue #1451), over every unit's
            # body segment -- dummies included, since they are structurally
            # real unit resistors too (mirrors `mos_array`'s dummy gates,
            # which are drawn identically to real ones) -- see
            # :func:`_res_unit_layout` for why the marker box excludes the
            # contacted end pads.
            marker_boxes = unit_boxes["marker"]
            for present, layer_param in [
                (self.res_mark_present, self.res_mark_layer),
            ] + [
                (
                    getattr(self, f"res_flavor_{i}_present"),
                    getattr(self, f"res_flavor_{i}_layer"),
                )
                for i in range(_MAX_RES_FLAVOR_LAYERS)
            ]:
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

    return {"res_array": _ResArrayPCell}


def _build_cap_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``cap_array`` reference generator's PCell -- a capacitor array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    class _CapArrayPCell(kdb.PCellDeclarationHelper):
        """Unit MiM capacitor array: a row of ``num`` matched unit cells
        (see :func:`_cap_unit_layout`), each a top-plate-metal-over-
        bottom-plate-metal MiM stack with a top-plate via + local-metal
        landing pad -- the capacitor sibling of ``res_array`` (issue
        #1117)."""

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "plate_w_um",
                self.TypeDouble,
                "Unit top-plate width (um)",
                default=5.0,
            )
            self.param(
                "plate_h_um",
                self.TypeDouble,
                "Unit top-plate height (um)",
                default=5.0,
            )
            self.param(
                "spacing_um",
                self.TypeDouble,
                "Spacing between unit capacitors (um)",
                default=0.5,
            )
            self.param(
                "num", self.TypeInt, "Number of matched unit capacitors", default=4
            )
            self.param(
                "cap_top_plate_layer",
                self.TypeLayer,
                "MiM top-plate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_bottom_plate_layer",
                self.TypeLayer,
                "MiM bottom-plate drawing layer",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_layer",
                self.TypeLayer,
                "Top-plate via drawing layer (only used when cap_top_via_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_present",
                self.TypeBoolean,
                "Whether cap_top_via_layer is a real, DRC-checked layer for "
                "the resolved PDK",
                default=False,
            )
            self.param(
                "cap_top_via_metal_layer",
                self.TypeLayer,
                "Top-plate via landing-metal drawing layer (only used when "
                "cap_top_via_metal_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "cap_top_via_metal_present",
                self.TypeBoolean,
                "Whether cap_top_via_metal_layer is a real, DRC-checked "
                "layer for the resolved PDK",
                default=False,
            )
            self.param(
                "cap_top_via_metal_min_w_um",
                self.TypeDouble,
                "Minimum drawn width (um) for the top-plate via landing pad "
                "on the resolved PDK family (issue #1455) -- 0.0 for a "
                "family with no override, leaving the generic landing-pad "
                "size unchanged",
                default=0.0,
            )
            self.param(
                "cap_top_via_min_w_um",
                self.TypeDouble,
                "Minimum drawn side (um) for the top-plate via itself on the "
                "resolved PDK family (issue #1555) -- 0.0 leaves the generic "
                "CONTACT_SIZE_UM cut unchanged",
                default=0.0,
            )
            self.param(
                "cap_bottom_plate_margin_min_um",
                self.TypeDouble,
                "Minimum distance (um) the bottom plate extends past the top "
                "plate on the resolved PDK family (issue #1555) -- 0.0 "
                "leaves the generic CAP_BOTTOM_PLATE_MARGIN_UM unchanged",
                default=0.0,
            )
            self.param(
                "cap_min_spacing_um",
                self.TypeDouble,
                "Minimum spacing (um) between adjacent unit capacitors on "
                "the resolved PDK family (issue #1555) -- 0.0 draws exactly "
                "the requested spacing_um",
                default=0.0,
            )
            # One static slot per mask the *widest* family in
            # `_PDK_CAP_TOP_PLATE_REQUIRES` needs (issue #1555), mirroring
            # `_ResArrayPCell`'s own `res_flavor_<i>_*` slots: a KLayout
            # PCell's parameter list is fixed at declaration time, so the
            # count comes from `_MAX_CAP_TOP_PLATE_REQUIRES` rather than the
            # resolved family, and a family needing fewer (or no) masks
            # simply leaves its tail slots absent.
            for i in range(_MAX_CAP_TOP_PLATE_REQUIRES):
                self.param(
                    f"cap_top_requires_{i}_layer",
                    self.TypeLayer,
                    f"Top-plate requires-mask #{i} (e.g. gf180mcu's CAP_MK "
                    "or MIM_L_MK) drawn over the top plate for device "
                    f"recognition (only used when cap_top_requires_{i}_present)",
                    default=kdb.LayerInfo(0, 0),
                )
                self.param(
                    f"cap_top_requires_{i}_present",
                    self.TypeBoolean,
                    f"Whether cap_top_requires_{i}_layer is a real layer the "
                    "resolved PDK family's own MiM device class requires over "
                    "the top plate",
                    default=False,
                )

        def display_text_impl(self) -> str:
            return f"cap_array(w={self.plate_w_um},h={self.plate_h_um},n={self.num})"

        def produce_impl(self) -> None:
            dbu = self.layout.dbu
            li_top = self.layout.layer(self.cap_top_plate_layer)
            li_bottom = self.layout.layer(self.cap_bottom_plate_layer)
            info = _cap_array_layout(
                self.plate_w_um,
                self.plate_h_um,
                self.spacing_um,
                self.num,
                self.cap_top_via_metal_min_w_um,
                self.cap_top_via_min_w_um,
                self.cap_bottom_plate_margin_min_um,
                self.cap_min_spacing_um,
            )
            unit_boxes = info["unit"]["boxes_um"]
            # Every extra mask this family's own MiM device class requires
            # over the top plate (issue #1555) -- drawn exactly coincident
            # with the top plate, since `extract.py` intersects them with it
            # (`_capacitor_plate_regions`) and any shortfall would narrow the
            # recognised plate. Empty on sky130/sg13g2, whose curated decks
            # set no `top_plate_requires`; `CAP_MK`/`MIM_L_MK` on gf180mcu.
            top_requires_layers = [
                getattr(self, f"cap_top_requires_{i}_layer")
                for i in range(_MAX_CAP_TOP_PLATE_REQUIRES)
                if getattr(self, f"cap_top_requires_{i}_present")
            ]
            for c in info["cells"]:
                _insert_boxes(
                    self.cell,
                    li_bottom,
                    dbu,
                    unit_boxes["bottom_plate"],
                    c["x0_um"],
                    c["y0_um"],
                )
                _insert_boxes(
                    self.cell,
                    li_top,
                    dbu,
                    unit_boxes["top_plate"],
                    c["x0_um"],
                    c["y0_um"],
                )
                for layer_param in top_requires_layers:
                    _insert_boxes(
                        self.cell,
                        self.layout.layer(layer_param),
                        dbu,
                        unit_boxes["top_plate"],
                        c["x0_um"],
                        c["y0_um"],
                    )
                if self.cap_top_via_present:
                    li_via = self.layout.layer(self.cap_top_via_layer)
                    _insert_boxes(
                        self.cell,
                        li_via,
                        dbu,
                        unit_boxes["top_via"],
                        c["x0_um"],
                        c["y0_um"],
                    )
                if self.cap_top_via_metal_present:
                    li_via_metal = self.layout.layer(self.cap_top_via_metal_layer)
                    _insert_boxes(
                        self.cell,
                        li_via_metal,
                        dbu,
                        unit_boxes["top_via_metal"],
                        c["x0_um"],
                        c["y0_um"],
                    )

    return {"cap_array": _CapArrayPCell}


def _build_guard_ring_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``guard_ring`` reference generator's PCell -- a substrate/well
    tap ring.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Tap-ring implant drawing layer, exactly coincident with the "
                "tap ring (only used when ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the tap ring's own shape (see _ring_tap_implant_layer)",
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
                well_box = _well_box_um(info, WELL_ENCLOSURE_MARGIN_UM)
                _insert_boxes(self.cell, li_well, dbu, [well_box])
            if self.ring_implant_present:
                # Exactly coincident with the tap ring -- never a blanket
                # over the enclosed area, which would re-dope whatever a
                # caller later places inside the ring (issue #1580, mirrors
                # `well_island`'s own `well_tap_implant` ring precedent).
                _insert_ring(
                    self.cell,
                    self.layout.layer(self.ring_implant_layer),
                    dbu,
                    info["outer_box_um"],
                    info["inner_box_um"],
                    gap_box,
                )

    return {"guard_ring": _GuardRingPCell}


def _build_well_island_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``well_island`` reference generator's PCell -- an isolated well
    tap island.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    class _WellIslandPCell(kdb.PCellDeclarationHelper):
        """Named-net, isolated well/tap island (issue #1421).

        ``guard_ring``'s geometry -- the same tap ring + local-metal ring +
        evenly-spaced contacts, enclosed by a well tie -- with the two
        properties that idiom needs and a plain guard ring cannot express:

        1. **The tie carries a caller-named net.** ``params.net`` is drawn as
           a ``kdb.Text`` on the resolved family's *metal* label layer
           (``metal_label`` role) sitting on the ring's own metal band, so
           the name reaches the enclosed well only through the physical tie
           (metal -> contact -> tap -> well). Nothing is ever drawn on the
           deck's ``well_label`` layer, which would name the well polygon
           directly and make ``klt extract``'s ``devices[].nets["b"]`` report
           the intended body net even with the tie broken (the "well-label
           tautology" the issue calls out).
        2. **The island is isolated from a caller-named set of other wells.**
           ``params.isolate_from`` lists the other well regions, in this
           cell's own frame; ``params.separation_um`` optionally raises the
           required clearance above the resolved family's own
           different-potential well rule (:data:`_PDK_WELL_ISOLATION_UM`).
           The check itself lives in :func:`_well_island_isolation`, which
           runs *before* any geometry is produced -- an island that cannot
           clear its neighbours raises :class:`GenError` rather than
           emitting a well that silently merges with one of them.

        ``well_margin_resolved_um`` is the harness-computed well enclosure
        this cell actually draws: ``params.well_margin_um`` when it clears
        every neighbour, otherwise the largest value below it that does (down
        to :data:`WELL_ENCLOSURE_MARGIN_UM`, never below). See
        :func:`_well_island_isolation`.
        """

        def __init__(self) -> None:
            super().__init__()
            self.param(
                "inner_width_um",
                self.TypeDouble,
                "Width of the enclosed inner area (um)",
                default=3.0,
            )
            self.param(
                "inner_height_um",
                self.TypeDouble,
                "Height of the enclosed inner area (um)",
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
                "inner_width_um -- 0 (default) inherits contacts_per_side",
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
                "Cut one routing opening through the tap ring on this side: "
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
                "net",
                self.TypeString,
                "Net name the island's tie carries -- drawn as a text on the "
                "ring's own metal (never on the well label layer) and "
                "reported as ports[].net. '' (default) draws no label",
                default="",
            )
            self.param(
                "well_margin_um",
                self.TypeDouble,
                "Well enclosure of the tap ring (um) -- trimmed towards "
                f"{WELL_ENCLOSURE_MARGIN_UM} when a larger value would "
                "violate the requested separation from isolate_from",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )
            self.param(
                "separation_um",
                self.TypeDouble,
                "Required clearance (um, euclidian) from every isolate_from "
                "well at a different potential -- 0 (default) uses the "
                "resolved PDK family's own different-potential well rule; a "
                "value below that rule is rejected",
                default=0.0,
            )
            self.param(
                "isolate_from",
                self.TypeList,
                "Other well regions this island must stay clear of, as "
                "[x0_um, y0_um, x1_um, y1_um] or "
                "[x0_um, y0_um, x1_um, y1_um, net] entries in this cell's "
                "own coordinate frame -- an entry naming this island's own "
                "net is equipotential and is not isolated from",
                default=[],
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
                "metal_label_layer",
                self.TypeLayer,
                "Label layer the net name is drawn on (only used when "
                "metal_label_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "metal_label_present",
                self.TypeBoolean,
                "Whether metal_label_layer is a real label layer for the resolved PDK",
                default=False,
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
                "Whether well_layer is a real layer for the resolved PDK",
                default=False,
            )
            self.param(
                "well_tap_implant_layer",
                self.TypeLayer,
                "Well-tie implant layer drawn over the tap ring (only used "
                "when well_tap_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "well_tap_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the tap ring as a well tie",
                default=False,
            )
            self.param(
                "well_margin_resolved_um",
                self.TypeDouble,
                "Harness-resolved well enclosure actually drawn (see "
                "_well_island_isolation)",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )

        def display_text_impl(self) -> str:
            return f"well_island({self.net or 'unnamed'})"

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
            if self.well_tap_implant_present:
                # Exactly coincident with the tap ring -- never a blanket over
                # the enclosed area, which would re-dope whatever a caller
                # later places inside the island (see the `well_tap_implant`
                # role's own comment in `_PDK_ROLE_LAYERS`).
                _insert_ring(
                    self.cell,
                    self.layout.layer(self.well_tap_implant_layer),
                    dbu,
                    info["outer_box_um"],
                    info["inner_box_um"],
                    gap_box,
                )
            if self.well_present:
                _insert_boxes(
                    self.cell,
                    self.layout.layer(self.well_layer),
                    dbu,
                    [_well_box_um(info, self.well_margin_resolved_um)],
                )
            if self.net and self.metal_label_present:
                anchor = _well_island_label_point(info)
                if anchor is not None:
                    self.cell.shapes(self.layout.layer(self.metal_label_layer)).insert(
                        kdb.Text(
                            self.net,
                            kdb.Trans(
                                kdb.Vector(
                                    int(round(anchor[0] / dbu)),
                                    int(round(anchor[1] / dbu)),
                                )
                            ),
                        )
                    )

    return {"well_island": _WellIslandPCell}


def _build_diff_pair_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``diff_pair`` reference generator's PCell -- a differential
    pair / current mirror cell.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

    class _DiffPairPCell(kdb.PCellDeclarationHelper):
        """Differential pair / current mirror cell (spike section 4's
        family 4): two matched devices, each split into ``splits``
        sub-instances, interleaved in a true common-centroid cross-quad
        pattern (see :func:`_diff_pair_layout`) -- composes ``mos_array``'s
        unit-device drawing (family 1) and ``guard_ring``'s ring drawing
        (family 3). ``flavor="pfet"`` encloses the device pair's own active
        footprint in a well (independent of ``add_guard_ring``'s own,
        separate well tie -- see ``guard_ring``); ``flavor="nfet"`` (the
        default) draws no additional well shape (#208).

        ``voltage_flavor`` (issue #1054) optionally draws a PDK
        medium-voltage/thick-oxide device-class marker over the same device
        pair footprint the well shape above encloses, independent of
        ``flavor``. The default, an empty string, draws nothing (byte-for-
        byte unchanged geometry). See ``mos_array``'s own ``voltage_flavor``
        docstring and :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` for the flavour
        names each PDK family recognises."""

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
                "Interleaved sub-instances per device (cross-quad splits). "
                "The two legs share one x column per terminal: every column "
                "of the 2-row checkerboard holds one Q1 and one Q2 "
                "sub-instance at the same x, so Q1_<n>_S/_D/_G and that "
                "column's Q2_* ports report identical x_um and differ only "
                "in y_um by one row pitch. A one-column-per-pin floorplan "
                "(one vertical routing column per pin, one horizontal track "
                "per net) therefore cannot give the two legs' distinct nets "
                "separate columns -- see docs/cli/gen.md's diff_pair section "
                "for why this is inherent to the common-centroid interleave, "
                "and for the routing styles that do work",
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
                "voltage_flavor",
                self.TypeString,
                "Optional medium-voltage/thick-oxide device-class marker: "
                "'' (default, no marker drawn) or a name the resolved PDK "
                "family's role-layer table recognises (e.g. 'medium_voltage' "
                "on gf180mcu, drawing its Dualgate marker). A name the family "
                "doesn't recognise draws nothing and is reported via "
                "drc_hints.notes, never silently dropped",
                default="",
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
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Guard-ring implant drawing layer, exactly coincident with "
                "the tap ring (only used when add_guard_ring and "
                "ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the guard ring's own shape (see _ring_tap_implant_layer)",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_layer",
                self.TypeLayer,
                "Medium-voltage/thick-oxide device-class marker drawing "
                "layer, sized to enclose the device pair's own footprint "
                "(only used when voltage_flavor_mark_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "voltage_flavor_mark_present",
                self.TypeBoolean,
                "Whether voltage_flavor_mark_layer is a real layer resolved "
                "for params.voltage_flavor on this PDK family",
                default=False,
            )
            self.param(
                "voltage_flavor_mark_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the voltage_flavor marker box grows "
                "beyond the device pair's own shared active footprint (see "
                "_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM)",
                default=WELL_ENCLOSURE_MARGIN_UM,
            )
            self.param(
                "gate_pad_clearance_um",
                self.TypeDouble,
                "Harness-resolved clearance the gate-poly landing pad keeps "
                "off the diffusion edge (see _PDK_GATE_PAD_ACTIVE_CLEARANCE_UM)",
                default=0.0,
            )
            self.param(
                "contact_gate_offset_um",
                self.TypeDouble,
                "Harness-resolved extra S/D-contact-to-gate offset floor "
                "(see _PDK_CONTACT_GATE_EXTRA_OFFSET_UM)",
                default=0.0,
            )
            self.param(
                "bottom_endcap_um",
                self.TypeDouble,
                "Harness-resolved gate-stripe bottom-edge endcap extension "
                "(see _PDK_GATE_BOTTOM_ENDCAP_UM)",
                default=0.0,
            )
            self.param(
                "sd_implant_layer",
                self.TypeLayer,
                "Source/drain implant drawing layer (only used when "
                "sd_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "sd_implant_present",
                self.TypeBoolean,
                "Whether sd_implant_layer is a real layer this PDK family "
                "needs over each unit device's own active body",
                default=False,
            )
            self.param(
                "sd_implant_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the source/drain implant box grows "
                "beyond each unit device's own active box (only used when "
                "sd_implant_present)",
                default=0.0,
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
                self.gate_pad_clearance_um,
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
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

            # Source/drain implant (issue #1577): drawn over every unit
            # device's own active body, mirroring mos_array's own treatment.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                for c in info["cells"]:
                    _insert_boxes(
                        self.cell,
                        li_sd_implant,
                        dbu,
                        unit_boxes["sd_implant"],
                        c["x0_um"],
                        c["y0_um"],
                    )

            # Shared device-pair-footprint box (well margin), used by the
            # `flavor="pfet"` well shape below -- computed once, independent
            # of `voltage_flavor`, so a caller can request either, both, or
            # neither.
            margin = WELL_ENCLOSURE_MARGIN_UM
            device_well_box = (
                -margin,
                -margin,
                info["core_w_um"] + margin,
                info["core_h_um"] + margin,
            )

            if self.flavor == "pfet" and self.well_present:
                # Enclose the device pair's own active footprint in a well,
                # independent of the (optional) automatically-sized guard
                # ring's own well tie drawn below -- both land on the same
                # well_layer, so they simply merge into one region on any
                # boolean/DRC pass over it.
                li_well = self.layout.layer(self.well_layer)
                _insert_boxes(self.cell, li_well, dbu, [device_well_box])

            # Medium-voltage/thick-oxide device-class marker (issue #1054):
            # its *own* box (issue #1577), not the well shape's -- gf180mcu's
            # real signoff deck needs the marker to reach further than the
            # well's own margin does, see
            # `_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM`'s own docstring.
            if self.voltage_flavor_mark_present:
                li_voltage_flavor_mark = self.layout.layer(
                    self.voltage_flavor_mark_layer
                )
                mark_margin = self.voltage_flavor_mark_margin_um
                mark_box = (
                    -mark_margin,
                    -mark_margin,
                    info["core_w_um"] + mark_margin,
                    info["core_h_um"] + mark_margin,
                )
                _insert_boxes(self.cell, li_voltage_flavor_mark, dbu, [mark_box])

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
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- never a blanket over the enclosed pair.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

    return {"diff_pair": _DiffPairPCell}


def _build_bjt_array_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``bjt_array`` reference generator's PCell -- a matched BJT
    array.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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
                "ring_implant_layer",
                self.TypeLayer,
                "Collector-ring implant drawing layer, exactly coincident "
                "with the collector ring (only used when add_collector_ring "
                "and ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the collector ring's own shape (see _ring_tap_implant_layer)",
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
                if self.ring_implant_present:
                    # Exactly coincident with the collector ring's own
                    # active-layer shape (issue #1580, mirrors
                    # `well_island`'s own `well_tap_implant` ring precedent)
                    # -- never a blanket over the enclosed base well/array.
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
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

    return {"bjt_array": _BjtArrayPCell}


def _build_bond_pad_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``bond_pad`` reference generator's PCell -- a top-metal bond pad.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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

    return {"bond_pad": _BondPadPCell}


def _build_esd_device_pcell() -> dict[str, type[kdb.PCellDeclarationHelper]]:
    """Build the ``esd_device`` reference generator's PCell -- an ESD protection
    device.

    Defined inside a function (not at module scope) so importing
    ``klayout_tools.gen`` doesn't pay ``klayout.db``'s load cost until a
    caller actually runs a generator -- the same lazy-import discipline
    ``layers.py``/``render.py`` use for the same reason. See
    :func:`_build_pcell_classes`, the thin composer that merges every
    per-family factory's single-entry dict together.
    """
    import klayout.db as kdb

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
            self.param(
                "contact_gate_offset_um",
                self.TypeDouble,
                "Harness-resolved extra S/D-contact-to-gate offset floor "
                "(see _PDK_CONTACT_GATE_EXTRA_OFFSET_UM)",
                default=0.0,
            )
            self.param(
                "bottom_endcap_um",
                self.TypeDouble,
                "Harness-resolved gate-stripe bottom-edge endcap extension "
                "(see _PDK_GATE_BOTTOM_ENDCAP_UM)",
                default=0.0,
            )
            self.param(
                "sd_implant_layer",
                self.TypeLayer,
                "Source/drain implant drawing layer (only used when "
                "sd_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "sd_implant_present",
                self.TypeBoolean,
                "Whether sd_implant_layer is a real layer this PDK family "
                "needs over the unit device's own active body",
                default=False,
            )
            self.param(
                "sd_implant_margin_um",
                self.TypeDouble,
                "Harness-resolved margin the source/drain implant box grows "
                "beyond the unit device's own active box (only used when "
                "sd_implant_present)",
                default=0.0,
            )
            self.param(
                "ring_implant_layer",
                self.TypeLayer,
                "Tap-ring implant drawing layer, exactly coincident with the "
                "tap ring (only used when add_guard_ring and "
                "ring_implant_present)",
                default=kdb.LayerInfo(0, 0),
            )
            self.param(
                "ring_implant_present",
                self.TypeBoolean,
                "Whether the resolved PDK needs an implant mask to recognise "
                "the tap ring's own shape (see _ring_tap_implant_layer)",
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
                self.contact_gate_offset_um,
                self.bottom_endcap_um,
                self.sd_implant_margin_um if self.sd_implant_present else 0.0,
            )
            unit_boxes = info["unit"]["boxes_um"]
            for role, li in (
                ("active", li_active),
                ("poly", li_poly),
                ("contact", li_contact),
                ("metal", li_metal),
            ):
                _insert_boxes(self.cell, li, dbu, unit_boxes[role])

            # Source/drain implant (issue #1577): drawn over the unit
            # device's own active body, mirroring mos_array's own treatment.
            if self.sd_implant_present:
                li_sd_implant = self.layout.layer(self.sd_implant_layer)
                _insert_boxes(self.cell, li_sd_implant, dbu, unit_boxes["sd_implant"])

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
                if self.ring_implant_present:
                    # Exactly coincident with the tap ring (issue #1580,
                    # mirrors `well_island`'s own `well_tap_implant` ring
                    # precedent) -- always the substrate-tie doping (never
                    # the well-tie one), since this ring never encloses a
                    # well (see the no-well-tie note directly above).
                    _insert_ring(
                        self.cell,
                        self.layout.layer(self.ring_implant_layer),
                        dbu,
                        _shift_box(ring["outer_box_um"], ox, oy),
                        _shift_box(ring["inner_box_um"], ox, oy),
                        gap_box,
                    )

    return {"esd_device": _EsdDevicePCell}


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
    #: Fallback output database unit (um) -- used only when the resolved
    #: PDK's own tech LEF declares no ``DATABASE MICRONS`` value (or ships
    #: no readable tech LEF at all), since :func:`_produce` prefers
    #: :func:`~klayout_tools.pdk.resolve_pdk_dbu`'s PDK-derived answer
    #: (issue #1496). Every registered generator declares ``0.001``, the dbu
    #: `klt gen` unconditionally wrote at before that change.
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
    if params["ring_padding_um"] < 0:
        raise GenError("generator 'mos_array': params.ring_padding_um must be >= 0")

    # `ring_gap_side`/`ring_gap_um`/`ring_gap_offset_um` are validated against
    # a hypothetical ring (`add_guard_ring=True`) regardless of the request's
    # own `add_guard_ring` value -- the same PDK-agnostic-validate-time
    # position `_diff_pair_validate`/`_esd_device_validate`/
    # `_bjt_array_validate` already take (this function runs before the PDK
    # family is resolved, see `_GeneratorSpec.validate`'s own docstring).
    inner_w_um, inner_h_um = _auto_ring_inner_size_um(
        _mos_array_layout(
            params["w_um"],
            params["l_um"],
            params["fingers"],
            params["rows"],
            params["cols"],
            params["dummy"],
            params["topology"],
            params["gate_contact"],
            params["finger_topology"],
            add_guard_ring=True,
            ring_padding_um=params["ring_padding_um"],
        )
    )
    _validate_ring_gap(
        "mos_array",
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        inner_w_um,
        inner_h_um,
    )


def _voltage_flavor_hints(
    family: str, params: dict[str, Any], notes: list[str]
) -> dict[str, Any]:
    """``drc_hints`` fields for the optional ``voltage_flavor`` marker (issue
    #1054), shared by :func:`_mos_array_describe`/:func:`_diff_pair_describe`:
    echoes the requested flavour (``None`` when omitted) and whether a real
    marker layer was resolved for it, appending an explanatory entry to
    ``notes`` (in place) when a non-empty request resolved to no layer --
    mirrors ``esd_device``'s own ``esd_mark``/``salicide_block`` "notes it,
    never silently drops it" precedent rather than rejecting the request."""
    voltage_flavor = params["voltage_flavor"]
    mark_present = False
    if voltage_flavor:
        mark_present = _voltage_flavor_mark_layer(family, voltage_flavor) is not None
        if not mark_present:
            notes.append(
                f"params.voltage_flavor '{voltage_flavor}' has no marker layer "
                f"resolved for the resolved PDK family ('{family}') -- no marker "
                "was drawn"
            )
    return {
        "voltage_flavor": voltage_flavor or None,
        "voltage_flavor_mark_present": mark_present,
    }


def _mos_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    # Must match the same gate `_MosArrayPCell.produce_impl` uses (issue
    # #1473), so the reported `well_box_um`/well-tap port line up with the
    # geometry actually drawn.
    draw_well_tap = (
        params["flavor"] == "pfet"
        and _role_layer_info(family, "well") is not None
        and _mos_array_well_tap_role_layer(family) is not None
    )
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
        # Must match the value `_device_layer_params` threads to the PCell --
        # the reported gate-port position is defined off the landing pad this
        # clearance moves (issue #1450).
        _gate_pad_clearance_um(family),
        draw_well_tap,
        params["add_guard_ring"],
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["ring_padding_um"],
        # Must match the values `_device_layer_params` threads to the PCell
        # (issue #1577) -- `contact_gate_offset_um` moves every seg/gate
        # centre and `total_len_um` (the column pitch) the same way
        # `gate_pad_clearance_um` above moves the gate port's `y_um`.
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
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

    # #781: with `finger_topology == "series"` and `fingers > 1` the unit is
    # genuinely `fingers` transistors, so every S/D segment and every gate
    # finger is reported -- not just the two end segments and the first
    # gate. `fingers == 1` has no interior segment either way, so it keeps
    # reporting the plain `U<i>_S`/`U<i>_D`/`U<i>_G` triple, byte-for-byte
    # unchanged under both topologies (#777/#780 pinned this).
    series_fingers = params["finger_topology"] == "series" and params["fingers"] > 1

    ports = []
    if series_fingers:
        seg_xy = unit["seg_xy"]
        gate_xy = unit["gate_xy"]
        last_seg = len(seg_xy) - 1
        for c in info["cells"]:
            idx = c["idx"]
            for seg_idx, (sx, sy) in enumerate(seg_xy):
                letter = "S" if seg_idx % 2 == 0 else "D"
                if seg_idx == 0:
                    direction_deg, width_um = 180, unit["sd_width_um"]
                elif seg_idx == last_seg:
                    direction_deg, width_um = 0, unit["sd_width_um"]
                else:
                    # Interior segments are boxed in by a gate on either
                    # side, so their only free approach is from below (the
                    # gate pads sit above); their reported width is the
                    # pad's own x-width, not `w_um`.
                    direction_deg, width_um = 270, unit["g_width_um"]
                ports.append(
                    {
                        "name": f"U{idx}_{letter}{seg_idx // 2}",
                        "net": None,
                        "layer": metal_layer,
                        "x_um": c["x0_um"] + sx,
                        "y_um": c["y0_um"] + sy,
                        "width_um": width_um,
                        "direction_deg": direction_deg,
                    }
                )
            for gate_idx, (gx, gy) in enumerate(gate_xy):
                ports.append(
                    {
                        "name": f"U{idx}_G{gate_idx}",
                        "net": None,
                        "layer": gate_layer,
                        "x_um": c["x0_um"] + gx,
                        "y_um": c["y0_um"] + gy,
                        "width_um": unit["g_width_um"],
                        "direction_deg": 90,
                    }
                )
    else:
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

    # Well-tie tap port (issue #1473): one shared port for the whole array's
    # well -- `_mos_array_well_tap_layout`'s own docstring explains why a
    # single pad ties every unit device's PMOS body in the array, so this is
    # not an `U<i>_B`-per-unit port the way S/D/G are.
    if info["well_tap"] is not None:
        tap_x, tap_y = info["well_tap"]["xy"]
        ports.append(
            {
                "name": "WELL_TAP",
                "net": None,
                "layer": metal_layer,
                "x_um": tap_x,
                "y_um": tap_y,
                "width_um": info["well_tap"]["width_um"],
                "direction_deg": 0,
            }
        )

    # Automatically-sized tap/guard ring (issue #1493): reports the same
    # `TAP_<side>`/`GAP_<side>` ports `diff_pair`/`esd_device`/`bjt_array`
    # already report for their own composed rings.
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

    # Unlike `diff_pair` (`add_guard_ring` defaults `True`, so an *explicit*
    # opt-out earns a note), `mos_array`'s guard ring defaults `False` (issue
    # #1493's own regression-safety bar) -- the overwhelmingly common no-ring
    # case would otherwise gain a notes[] entry on every existing caller.
    # `_ring_gap_notes` alone still covers the "ring_gap_side set but no ring
    # drawn" case.
    notes = _ring_gap_notes(
        info["ring"] if ring_drawn else None, params["ring_gap_side"]
    )
    if params["l_um"] < GATE_LENGTH_SAFE_MIN_UM:
        notes.append(
            f"gate length below {GATE_LENGTH_SAFE_MIN_UM}um (this generator's own "
            "default): the S/D local-metal pads are automatically padded clear of "
            "the gate (issue #1187), so no S/D metal minimum-spacing violation "
            "results from this alone -- but params.l_um itself may still violate "
            "the target PDK's own poly minimum-width rule (e.g. sky130's "
            "poly.width.1 floor is 0.15um), which this generator does not check"
        )
    if params["flavor"] == "pfet" and _PDK_ROLE_LAYERS[family]["well"] is None:
        notes.append(
            f"params.flavor is 'pfet' but the resolved PDK family ('{family}') has "
            "no well layer checked by its curated DRC deck -- no well shape was drawn"
        )

    if series_fingers:
        notes.append(
            "params.finger_topology is 'series', so each unit device's "
            f"{params['fingers']} fingers are drawn unstrapped -- they extract as "
            f"{params['fingers']} transistors chained source-to-drain, not one "
            f"parallel device of width {params['fingers']} x {params['w_um']}um "
            f"(reported as U<i>_S<j>/U<i>_D<j>/U<i>_G<j> ports, one per finger)"
        )

    voltage_flavor_hints = _voltage_flavor_hints(family, params, notes)

    snapped = _grid_snapped(dbu, params["w_um"], params["l_um"])
    grid = f"{params['rows']}x{params['cols']}"
    # `flavor` is not folded into `matched_group_id` -- a matched group is
    # always one generator call by construction, so every instance in it
    # already shares one `flavor`; the id only needs to disambiguate groups
    # from each other (see the issue's Implementation Guidance item 5).
    matched_group_id = f"mos_array:{grid}:{params['topology']}"

    # #781: in "series" mode each unit really is `fingers` chained
    # transistors (matching what `klt extract` reports back), not one
    # device -- "parallel" mode still folds `fingers` into one device per
    # cell, so its device_count is unaffected.
    device_count = (
        params["rows"] * params["cols"] * (params["fingers"] if series_fingers else 1)
    )

    return {
        "device_count": device_count,
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": matched_group_id,
            "snapped_to_grid": snapped,
            "notes": notes,
            **voltage_flavor_hints,
        },
        "warnings": (
            ["one or more dimensions were rounded to the technology grid"]
            if snapped
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
    if not (0 <= params["metal_level"] <= _MAX_METAL_RES_LEVEL):
        raise GenError(
            "generator 'res_array': params.metal_level must be between 0 "
            f"(poly body, the default) and {_MAX_METAL_RES_LEVEL} -- a "
            "generator-side structural bound, independent of which PDK "
            "family the request eventually resolves to (see "
            "_metal_res_layers for the per-family/level support check)"
        )


def _res_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    metal_level = params.get("metal_level", 0)
    floors = (
        _metal_res_geometry_min_um(family, metal_level)
        if metal_level
        else {key: 0.0 for key in _METAL_RES_GEOMETRY_MIN_KEYS}
    )
    info = _res_array_layout(
        params["length_um"],
        params["width_um"],
        params["spacing_um"],
        params["num"],
        params["dummy"],
        params["rows"],
        floors["via_min_w_um"],
        floors["via_enclosure_min_um"],
        floors["via_space_min_um"],
    )
    unit = info["unit"]
    # A metal-level resistor's own body layer (metN) is already a routing
    # metal, so its ports are reported there directly -- unlike the poly-body
    # case, where `"metal"` (li1) is the practically-routable layer one level
    # *above* the raw poly terminal (see the module's own `_PDK_ROLE_LAYERS`
    # "metal" role comment). Both are electrically the same net either way
    # once `klt extract` merges connectivity through the end contact/via.
    metal_pair = (
        _metal_res_layers(family, metal_level)["body"]
        if metal_level
        else _PDK_ROLE_LAYERS[family]["metal"]
    )
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
    # The effective spacing actually drawn -- the requested `spacing_um`,
    # floored under `metal_level`'s own minimum body/end-via spacing (issue
    # #1639; only sky130's met5/via4 sets one today, so every other
    # level/family's effective spacing is exactly what was asked for --
    # mirrors `_cap_array_describe`'s own `cap_min_spacing_um` handling).
    effective_spacing_um = info["spacing_um"]
    if effective_spacing_um > params["spacing_um"]:
        notes.append(
            f"spacing_um was widened from {params['spacing_um']}um to "
            f"{effective_spacing_um}um -- metal_level {metal_level}'s own "
            "minimum body/end-via spacing rule (sky130's met5.space.1) "
            "binds above the requested value"
        )
    elif 0 <= effective_spacing_um < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    # width_um is never auto-widened (the caller owns this primary requested
    # dimension, like `mos_array`'s `w_um`/`cap_array`'s `plate_w_um` --
    # see :data:`_PDK_METAL_RES_LEVEL_MIN_UM`'s own docstring), so a request
    # under metal_level's own body-width DRC rule is surfaced here instead of
    # silently drawn non-DRC-clean or silently widened.
    if params["width_um"] < floors["body_width_min_um"]:
        notes.append(
            f"width_um ({params['width_um']}um) is below metal_level "
            f"{metal_level}'s own minimum body width "
            f"({floors['body_width_min_um']}um) on PDK family '{family}' -- "
            "the drawn body will violate that layer's own DRC width rule"
        )

    snapped = _grid_snapped(
        dbu, params["length_um"], params["width_um"], effective_spacing_um
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": effective_spacing_um,
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


def _cap_array_validate(params: dict[str, Any]) -> None:
    if params["plate_w_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'cap_array': params.plate_w_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["plate_h_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator 'cap_array': params.plate_h_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["spacing_um"] < 0:
        raise GenError("generator 'cap_array': params.spacing_um must be >= 0")
    if params["num"] < 1:
        raise GenError("generator 'cap_array': params.num must be >= 1")


def _cap_array_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    layers = _cap_family_layers(family)
    floors = _cap_geometry_min_um(family)
    top_via_metal_min_w_um = floors["cap_top_via_metal_min_w_um"]
    info = _cap_array_layout(
        params["plate_w_um"],
        params["plate_h_um"],
        params["spacing_um"],
        params["num"],
        top_via_metal_min_w_um,
        floors["cap_top_via_min_w_um"],
        floors["cap_bottom_plate_margin_min_um"],
        floors["cap_min_spacing_um"],
    )
    unit = info["unit"]
    bottom_pair = layers["cap_bottom_plate"]
    bottom_layer = {"layer": bottom_pair[0], "datatype": bottom_pair[1], "name": None}
    # The top-plate port reports the via's own landing-metal layer (`met4`)
    # when one is configured -- the terminal a downstream router would
    # actually connect to -- falling back to the bare top-plate mark
    # (`capm`) layer for a family that declares plates but no top-plate via
    # (see `_cap_family_layers`'s docstring; not exercised by sky130 today).
    top_via_metal_pair = layers["cap_top_via_metal"] or layers["cap_top_plate"]
    top_layer = {
        "layer": top_via_metal_pair[0],
        "datatype": top_via_metal_pair[1],
        "name": None,
    }
    # Read straight off the landing pad `_cap_unit_layout` actually sized
    # (issues #1455/#1555) rather than recomputing its sizing rule here, so a
    # reported `C<i>_TOP` port width can never drift from the shape on the
    # layout as more per-family floors (via size, landing-pad width) are
    # added to :data:`_PDK_CAP_GEOMETRY_MIN_UM`.
    has_top_via_metal = layers["cap_top_via_metal"] is not None
    top_port_width_um = (
        unit["top_pad_w_um"] if has_top_via_metal else params["plate_w_um"]
    )

    notes = []
    ports = []
    for c in info["cells"]:
        idx = c["idx"]
        bot_xy = unit["bot_xy"]
        ports.append(
            {
                "name": f"C{idx}_BOT",
                "net": None,
                "layer": bottom_layer,
                "x_um": c["x0_um"] + bot_xy[0],
                "y_um": c["y0_um"] + bot_xy[1],
                "width_um": unit["total_h_um"],
                "direction_deg": 180,
            }
        )
        if has_top_via_metal:
            # Escape port (issue #1494): the landing pad past the bottom
            # plate's own bbox (`_cap_unit_layout`'s `top_escape_xy`), not
            # the interior centre directly over the bottom plate -- a via
            # stack landed here can step down through the family's own
            # metal/via stack without ever crossing the bottom plate's
            # footprint. It faces due north out of the unit cell, so
            # `direction_deg` is now the same geometrically-derived value
            # `RING_SIDE_DIRECTIONS["N"]` reports for an edge-side port, not
            # a fixed placeholder.
            top_xy = unit["top_escape_xy"]
            direction_deg = RING_SIDE_DIRECTIONS["N"]
        else:
            # No top-plate-via-metal layer configured for this family (not
            # exercised by any currently-supported family -- see
            # `_cap_family_layers`'s docstring): fall back to the legacy
            # interior centre, since there is no landing-metal layer to
            # escape on. There is no single geometrically-correct outward
            # direction for an interior point, so this reports a fixed
            # value, mirroring `mos_array`'s interior gate-contact port
            # (`_mos_unit_layout`'s own `g_xy`, always `270`).
            top_xy = unit["top_xy"]
            direction_deg = 90
            notes.append(
                f"C{idx}_TOP is reported at the unit's interior centre, "
                "directly over the bottom plate -- this PDK family has no "
                "top-plate-via-metal layer to draw a routable escape pad "
                "on; a via stack landed here may short the two plates "
                "together"
            )
        ports.append(
            {
                "name": f"C{idx}_TOP",
                "net": None,
                "layer": top_layer,
                "x_um": c["x0_um"] + top_xy[0],
                "y_um": c["y0_um"] + top_xy[1],
                "width_um": top_port_width_um,
                "direction_deg": direction_deg,
            }
        )

    # The effective spacing actually drawn -- the requested `spacing_um`,
    # floored under this family's own minimum (issue #1555; only gf180mcu
    # sets one today, so every other family's effective spacing is exactly
    # what was asked for).
    effective_spacing_um = info["spacing_um"]
    if effective_spacing_um > params["spacing_um"]:
        notes.append(
            f"spacing_um was widened from {params['spacing_um']}um to "
            f"{effective_spacing_um}um -- this PDK family's own minimum MiM "
            "capacitor spacing rule (gf180mcu's mim.space.1, between the "
            "oversized virtual bottom plates of adjacent units) binds above "
            "the requested value"
        )
    elif 0 <= effective_spacing_um < MIN_SAME_LAYER_SPACING_UM:
        notes.append(
            "spacing_um is below the recommended "
            f"{MIN_SAME_LAYER_SPACING_UM}um margin -- may violate the target "
            "PDK's minimum same-layer spacing rule"
        )

    snapped = _grid_snapped(
        dbu, params["plate_w_um"], params["plate_h_um"], params["spacing_um"]
    )

    return {
        "device_count": params["num"],
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": effective_spacing_um,
            "matched_group_id": f"cap_array:{params['num']}",
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
    tap_net: str | None = None,
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

    ``tap_net`` (issue #1421) names the net every tap port carries, reported
    as each entry's ``net``. ``None`` (the default, and every caller that
    predates ``well_island``) keeps the historical ``"net": null`` -- a ring
    whose tie net the generator has no way to know. It is never applied to a
    ``GAP_*`` entry: that marks the *absence* of ring conductor, so it carries
    no net by construction.
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
                "net": tap_net,
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


def _ring_contact_gap_notes(
    inner_w_um: float, inner_h_um: float, contacts_ns: int, contacts_ew: int
) -> list[str]:
    """``drc_hints.notes`` entries for a resolved per-axis contact count that
    fits but leaves less than :data:`CONTACT_GAP_SAFE_UM` between adjacent
    contacts (issue #685).

    ``_guard_ring_validate`` only rejects a count whose contacts would
    literally overlap (see its own ``pitch <= CONTACT_SIZE_UM`` check) -- it
    can't reject on ``CONTACT_GAP_SAFE_UM`` alone since that margin's real
    violation only applies to gf180mcu (sky130's curated deck checks no
    contact-spacing rule at all), and a ``validate()`` doesn't know the
    resolved PDK family. A ``describe()`` does, so a tight-but-still-accepted
    gap is flagged here instead, per-axis, so a caller targeting gf180mcu
    knows to double-check with ``klt drc``.
    """
    notes: list[str] = []
    for label, straight, contacts in (
        ("inner_width_um", inner_w_um, contacts_ns),
        ("inner_height_um", inner_h_um, contacts_ew),
    ):
        pitch = straight / (contacts + 1)
        if pitch - CONTACT_SIZE_UM < CONTACT_GAP_SAFE_UM:
            notes.append(
                f"the resolved contact count ({contacts}) leaves less than "
                f"{CONTACT_GAP_SAFE_UM}um between adjacent contacts along {label} -- "
                "may violate the target PDK's minimum contact spacing rule"
            )
    return notes


def _guard_ring_axis_contacts(params: dict[str, Any]) -> tuple[int, int]:
    """Resolve ``params``'s per-axis contact counts: ``contacts_per_side_ns``/
    ``contacts_per_side_ew`` when set (non-zero), else ``contacts_per_side``
    inherited on that axis -- the same "0 means inherit" rule the PCell's
    ``produce_impl`` applies, kept in sync here so ``_guard_ring_validate``/
    ``_guard_ring_describe`` never disagree with what actually gets drawn."""
    contacts_ns = params["contacts_per_side_ns"] or params["contacts_per_side"]
    contacts_ew = params["contacts_per_side_ew"] or params["contacts_per_side"]
    return contacts_ns, contacts_ew


def _ring_params_validate(generator: str, params: dict[str, Any]) -> None:
    """Bounds shared by every generator whose ``params`` describe a tap ring
    directly (``guard_ring`` and ``well_island``) -- the inner-area/ring-width
    floors, the per-axis contact counts and the ring opening.

    ``generator`` only names the generator in the raised message, so a
    ``well_island`` request is never rejected in ``guard_ring``'s name.
    """
    if params["inner_width_um"] <= 0:
        raise GenError(f"generator '{generator}': params.inner_width_um must be > 0")
    if params["inner_height_um"] <= 0:
        raise GenError(f"generator '{generator}': params.inner_height_um must be > 0")
    if params["ring_width_um"] < UNIT_MIN_W_UM:
        raise GenError(
            f"generator '{generator}': params.ring_width_um must be >= {UNIT_MIN_W_UM}"
        )
    if params["contacts_per_side"] < 1:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side must be >= 1"
        )
    if params["contacts_per_side_ns"] < 0:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side_ns must be >= 0 "
            "(0 inherits params.contacts_per_side)"
        )
    if params["contacts_per_side_ew"] < 0:
        raise GenError(
            f"generator '{generator}': params.contacts_per_side_ew must be >= 0 "
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
                f"generator '{generator}': {param_name} (effectively {contacts}, "
                "from params.contacts_per_side unless overridden) does not fit "
                f"along {label}={straight}um without overlapping contacts"
            )

    _validate_ring_gap(
        generator,
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
        params["inner_width_um"],
        params["inner_height_um"],
    )


def _guard_ring_validate(params: dict[str, Any]) -> None:
    _ring_params_validate("guard_ring", params)


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
    notes.extend(
        _ring_contact_gap_notes(
            params["inner_width_um"],
            params["inner_height_um"],
            contacts_ns,
            contacts_ew,
        )
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


# --------------------------------------------------------------------------- #
# well_island (issue #1421) -- a named-net well/tap island, isolated from a
# caller-specified set of other well regions.
# --------------------------------------------------------------------------- #

#: Slack (um) allowed when comparing a measured separation (or a requested
#: one) against a rule threshold. Purely float-noise tolerance -- three orders
#: of magnitude below one dbu (:data:`_GRID_DBU_UM`), so it can never absorb a
#: real, drawable violation.
_SEPARATION_EPS_UM = 1e-9

#: How KLayout spells an *anonymous* (unlabelled) net -- ``Net.expanded_name()``
#: returns ``"$<n>"`` for a net no drawn label names, which
#: ``klayout_tools.extract`` reports (backslash-escaped, ``"\\$<n>"``) and
#: ``_detect_unbiased_pmos_body_nets`` keys its unbiased-body detection off.
#: ``well_island`` refuses to *draw* a label starting with it: a body net
#: deliberately named ``"$1"`` would be indistinguishable, downstream, from an
#: unbiased body that nobody tied at all.
_ANONYMOUS_NET_LABEL_PREFIX = "$"


def _well_island_ring(params: dict[str, Any]) -> dict[str, Any]:
    """``well_island``'s tap-ring layout, from its ``params`` -- the same
    ``_ring_layout`` call its ``produce_impl`` makes, so the isolation math,
    the reported ports and the drawn geometry all derive from one source."""
    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    return _ring_layout(
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        (contacts_ns, contacts_ew),
        params["ring_gap_side"],
        params["ring_gap_um"],
        params["ring_gap_offset_um"],
    )


def _largest_clearing_margin_um(
    ring: dict[str, Any],
    regions: list[dict[str, Any]],
    separation_um: float,
    min_margin_um: float,
    max_margin_um: float,
) -> float | None:
    """The largest well enclosure in ``[min_margin_um, max_margin_um]`` whose
    well rectangle still clears every region in ``regions`` by
    ``separation_um``, or ``None`` when even ``min_margin_um`` does not.

    The well rectangle grows with the margin and separation shrinks with it
    monotonically, so a bisection on the dbu grid finds the exact largest
    admissible value -- this is the "sizes itself to satisfy the rule"
    half of ``well_island``'s isolation contract, bounded below by
    :data:`WELL_ENCLOSURE_MARGIN_UM` (the enclosure the tap ring under it
    still needs) so it can never trade a well-spacing violation for a
    well-enclosure one.
    """

    def _clears(margin_um: float) -> bool:
        box = _well_box_um(ring, margin_um)
        return all(
            _box_separation_um(box, region["box_um"])
            >= separation_um - _SEPARATION_EPS_UM
            for region in regions
        )

    if _clears(max_margin_um):
        return max_margin_um
    if not _clears(min_margin_um):
        return None
    lo = round(min_margin_um / _GRID_DBU_UM)
    hi = round(max_margin_um / _GRID_DBU_UM)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _clears(mid * _GRID_DBU_UM):
            lo = mid
        else:
            hi = mid
    return lo * _GRID_DBU_UM


def _well_island_isolation(params: dict[str, Any], family: str) -> dict[str, Any]:
    """Resolve ``well_island``'s well geometry against its isolation request.

    Runs *before* any geometry is produced (see
    :func:`_well_island_layer_params`) and again when the response is built
    (see :func:`_well_island_describe`) -- one pure function, so the drawn
    well and the reported well/keepout rectangles can never disagree.

    The contract, in order:

    1. ``params.separation_um`` below the resolved family's own
       different-potential well rule (:data:`_PDK_WELL_ISOLATION_UM`) is a
       hard rejection -- honouring it would draw exactly the silently-too-close
       pair of wells this generator exists to prevent. ``0`` (the default)
       means "use the rule".
    2. Every ``params.isolate_from`` region **not** naming this island's own
       ``params.net`` is a different-potential neighbour and must clear the
       resolved separation. A region naming the same net is equipotential:
       the different-potential rule does not apply to it and the two wells are
       expected to tie together (reported via ``notes``, never enforced).
    3. If the requested ``params.well_margin_um`` does not clear every
       different-potential neighbour, the margin is trimmed towards
       :data:`WELL_ENCLOSURE_MARGIN_UM` until it does (reported via ``notes``
       and ``warnings``).
    4. If even the minimum margin cannot clear them, :class:`GenError` --
       never a silently merged well.

    Returns the resolved margin, the well/keepout rectangles (``None`` on a
    family with no well layer at all), and the notes/warnings the response
    should carry.
    """
    notes: list[str] = []
    warnings: list[str] = []
    well_supported = _PDK_ROLE_LAYERS[family].get("well") is not None
    rule_um = _PDK_WELL_ISOLATION_UM.get(family)
    regions = _parse_well_regions("well_island", "isolate_from", params["isolate_from"])
    own_net = params["net"] or None
    requested_um = params["separation_um"]

    if (
        requested_um > 0
        and rule_um is not None
        and requested_um < rule_um - _SEPARATION_EPS_UM
    ):
        raise GenError(
            f"generator 'well_island': params.separation_um ({requested_um}) is "
            f"below the minimum well-to-well spacing PDK family '{family}' "
            f"requires between wells at different potentials ({rule_um}um, "
            "euclidian -- see klayout_tools.gen._PDK_WELL_ISOLATION_UM for the "
            "rule citation). Raise params.separation_um to at least that value, "
            "or set it to 0 to use the rule's own minimum"
        )

    separation_um = requested_um if requested_um > 0 else rule_um
    if separation_um is None:
        notes.append(
            f"the resolved PDK family ('{family}') has no known well-to-well "
            "spacing rule for wells at different potentials, and "
            "params.separation_um is 0 -- no separation was enforced; set "
            "params.separation_um explicitly to check one"
        )

    different: list[dict[str, Any]] = []
    equipotential: list[dict[str, Any]] = []
    for region in regions:
        if own_net is not None and region["net"] == own_net:
            equipotential.append(region)
        else:
            different.append(region)

    if equipotential:
        notes.append(
            f"{len(equipotential)} params.isolate_from region(s) name this "
            f"island's own net ('{own_net}') -- they are equipotential with it, "
            "so no separation was required against them and the two wells are "
            "expected to tie together rather than isolate"
        )

    min_margin_um = WELL_ENCLOSURE_MARGIN_UM
    requested_margin_um = params["well_margin_um"]
    margin_um = requested_margin_um

    if not well_supported:
        if regions or params["net"]:
            notes.append(
                f"the resolved PDK family ('{family}') has no well layer in "
                "klayout_tools.gen._PDK_ROLE_LAYERS -- no well shape was drawn, "
                "so this island encloses no well to isolate or to name"
            )
            warnings.append(
                f"no well layer for PDK family '{family}' -- the island's tap "
                "ring was drawn but it isolates nothing"
            )
        return {
            "well_supported": False,
            "margin_um": margin_um,
            "requested_margin_um": requested_margin_um,
            "separation_um": separation_um,
            "well_box_um": None,
            "keepout_box_um": None,
            "notes": notes,
            "warnings": warnings,
        }

    ring = _well_island_ring(params)
    if different and separation_um is not None:
        resolved = _largest_clearing_margin_um(
            ring, different, separation_um, min_margin_um, requested_margin_um
        )
        if resolved is None:
            floor_box = _well_box_um(ring, min_margin_um)
            worst = min(
                different,
                key=lambda region: _box_separation_um(floor_box, region["box_um"]),
            )
            gap_um = _box_separation_um(floor_box, worst["box_um"])
            raise GenError(
                "generator 'well_island': the island's own well "
                f"{_format_box_um(floor_box)} (already at the minimum "
                f"enclosure of {min_margin_um}um) clears the "
                f"params.isolate_from region {_format_box_um(worst['box_um'])} "
                f"by only {gap_um:.4g}um, but {separation_um}um is required "
                "between wells at different potentials -- move the island, "
                "shrink params.inner_width_um/params.inner_height_um, or tie "
                "the two regions to the same net (a fifth isolate_from element) "
                "if they are meant to be equipotential"
            )
        margin_um = resolved
        if margin_um < requested_margin_um - _SEPARATION_EPS_UM:
            notes.append(
                f"params.well_margin_um ({requested_margin_um}) was trimmed to "
                f"{margin_um:.4g}um so the island's well still clears every "
                f"different-potential params.isolate_from region by "
                f"{separation_um}um"
            )
            warnings.append(
                f"well enclosure trimmed from {requested_margin_um}um to "
                f"{margin_um:.4g}um to satisfy the requested well separation"
            )

    well_box_um = _well_box_um(ring, margin_um)
    keepout_box_um = (
        None
        if separation_um is None
        else (
            well_box_um[0] - separation_um,
            well_box_um[1] - separation_um,
            well_box_um[2] + separation_um,
            well_box_um[3] + separation_um,
        )
    )
    if different and separation_um is not None:
        notes.append(
            f"well-to-well separation enforced against {len(different)} "
            f"different-potential params.isolate_from region(s): "
            f"{separation_um}um (euclidian)"
            + (
                ""
                if rule_um is None or separation_um > rule_um + _SEPARATION_EPS_UM
                else f" -- PDK family '{family}'s own different-potential well rule"
            )
        )
    return {
        "well_supported": True,
        "margin_um": margin_um,
        "requested_margin_um": requested_margin_um,
        "separation_um": separation_um,
        "well_box_um": well_box_um,
        "keepout_box_um": keepout_box_um,
        "notes": notes,
        "warnings": warnings,
    }


def _format_box_um(box_um: tuple[float, float, float, float]) -> str:
    """A compact ``(x0, y0)..(x1, y1)`` rendering of a rectangle, for error
    messages that have to name which rectangle is at fault."""
    x0, y0, x1, y1 = box_um
    return f"({x0:.4g}, {y0:.4g})..({x1:.4g}, {y1:.4g})um"


def _box_um_field(
    box_um: tuple[float, float, float, float] | None,
) -> dict[str, float] | None:
    """A rectangle as the response's own ``{x0, y0, x1, y1}`` object shape --
    the same one ``bbox_um`` already uses -- or ``None``."""
    if box_um is None:
        return None
    return {"x0": box_um[0], "y0": box_um[1], "x1": box_um[2], "y1": box_um[3]}


def _well_island_validate(params: dict[str, Any]) -> None:
    """PDK-agnostic validation for ``well_island``.

    Everything that needs the resolved PDK family -- the well-spacing rule
    itself, and the geometry check against ``params.isolate_from`` -- lives in
    :func:`_well_island_isolation`, reached through
    :func:`_well_island_layer_params` before any geometry is produced (the
    same split ``res_array``'s ``flavor`` validation already uses, since
    ``_GeneratorSpec.validate`` is handed no ``pdk_info``).
    """
    # The ring geometry is `guard_ring`'s, so its bounds are too -- reuse the
    # shared validator rather than restating (and risking drift from) the
    # same checks.
    _ring_params_validate("well_island", params)

    net = params["net"]
    if net:
        if net.strip() != net or any(character.isspace() for character in net):
            raise GenError(
                "generator 'well_island': params.net must not contain "
                "whitespace -- it is drawn as a net label and read back by "
                "`klt extract`"
            )
        if net.startswith(_ANONYMOUS_NET_LABEL_PREFIX):
            raise GenError(
                "generator 'well_island': params.net must not start with "
                f"'{_ANONYMOUS_NET_LABEL_PREFIX}' -- that prefix is how "
                "KLayout spells an *anonymous* (unlabelled) net, so a body net "
                "named this way could not be told apart from an unbiased one"
            )
    if params["well_margin_um"] < WELL_ENCLOSURE_MARGIN_UM:
        raise GenError(
            "generator 'well_island': params.well_margin_um must be >= "
            f"{WELL_ENCLOSURE_MARGIN_UM} (the well enclosure of the tap ring "
            "under it)"
        )
    if params["separation_um"] < 0:
        raise GenError("generator 'well_island': params.separation_um must be >= 0")
    _parse_well_regions("well_island", "isolate_from", params["isolate_from"])


def _well_island_describe(
    params: dict[str, Any], dbu: float, pdk_info: dict[str, Any]
) -> dict[str, Any]:
    family = _pdk_family(pdk_info["variant"])
    contacts_ns, contacts_ew = _guard_ring_axis_contacts(params)
    info = _well_island_ring(params)
    metal_pair = _PDK_ROLE_LAYERS[family]["metal"]
    metal_layer = {"layer": metal_pair[0], "datatype": metal_pair[1], "name": None}
    net = params["net"] or None

    ports = _ring_ports(
        info,
        (0.0, 0.0),
        "TAP_",
        metal_layer,
        params["ring_width_um"],
        metal_layer,
        net,
    )

    isolation = _well_island_isolation(params, family)
    notes = _ring_gap_notes(info, params["ring_gap_side"])
    notes.extend(
        _ring_contact_gap_notes(
            params["inner_width_um"],
            params["inner_height_um"],
            contacts_ns,
            contacts_ew,
        )
    )
    notes.extend(isolation["notes"])

    label_present = _PDK_ROLE_LAYERS[family].get("metal_label") is not None
    if net is None:
        notes.append(
            "params.net is empty -- no net label was drawn, so the island's "
            "body net extracts as an anonymous KLayout-synthesized net unless "
            "the caller routes and labels the tap ring themselves"
        )
    elif not label_present:
        notes.append(
            f"params.net is '{net}' but the resolved PDK family ('{family}') "
            "has no metal label layer in klayout_tools.gen._PDK_ROLE_LAYERS -- "
            "no net label was drawn"
        )

    snapped = _grid_snapped(
        dbu,
        params["inner_width_um"],
        params["inner_height_um"],
        params["ring_width_um"],
        params["well_margin_um"],
    )
    warnings = list(isolation["warnings"])
    if snapped:
        warnings.append("one or more dimensions were rounded to the technology grid")

    return {
        "device_count": len(info["contact_boxes_um"]),
        "ports": ports,
        "drc_hints": {
            "min_spacing_um": MIN_SAME_LAYER_SPACING_UM,
            "matched_group_id": None,
            "snapped_to_grid": snapped,
            "notes": notes,
            # Issue #1421's own reporting contract: the caller (or
            # `klt gen-compose`, or their own placer) gets the island's
            # resulting well rectangle and the region no *other* well may
            # enter, so a composition never has to re-derive either from raw
            # geometry.
            "well_net": net,
            "well_box_um": _box_um_field(isolation["well_box_um"]),
            "well_separation_um": isolation["separation_um"],
            "well_keepout_box_um": _box_um_field(isolation["keepout_box_um"]),
        },
        "warnings": warnings,
    }


def _well_island_layer_params(
    pdk_info: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Hidden layer params for ``well_island`` -- ``guard_ring``'s ring roles
    plus the net-label layer and (on a family with no dedicated tap layer) the
    well-tie implant.

    Also the one pre-draw hook that knows the resolved PDK family, so it is
    where :func:`_well_island_isolation` runs: an unsatisfiable isolation
    request raises :class:`GenError` from here, before any geometry exists or
    any file is written. The resolved well enclosure it computes is threaded
    to the PCell as the hidden ``well_margin_resolved_um`` param, so the drawn
    well is exactly the rectangle the response reports.
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("well_island", family)
    well = _role_layer_info(family, "well")
    metal_label = _role_layer_info(family, "metal_label")
    implant = _role_layer_info(family, "well_tap_implant")
    isolation = _well_island_isolation(params, family)
    return {
        "tap_layer": _role_layer_info(family, "tap"),
        "contact_layer": _role_layer_info(family, "contact"),
        "metal_layer": _role_layer_info(family, "metal"),
        "metal_label_layer": (
            metal_label if metal_label is not None else kdb.LayerInfo(0, 0)
        ),
        "metal_label_present": metal_label is not None,
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "well_tap_implant_layer": (
            implant if implant is not None else kdb.LayerInfo(0, 0)
        ),
        "well_tap_implant_present": implant is not None,
        "well_margin_resolved_um": isolation["margin_um"],
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

    # `gate_pad_clearance_um` is deliberately left at its default here: this
    # validator runs before the PDK family is resolved (the same PDK-agnostic
    # position `_guard_ring_validate` already documents), and a family that
    # does declare a clearance only makes the ring *taller* -- so validating
    # the caller's requested ring gap against the no-clearance inner height is
    # the conservative direction, never one that admits an unfittable gap.
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
        # See the equivalent note in `_mos_array_describe` (issue #1450).
        _gate_pad_clearance_um(family),
        # See the equivalent note in `_mos_array_describe` (issue #1577).
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
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

    voltage_flavor_hints = _voltage_flavor_hints(family, params, notes)

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
            **voltage_flavor_hints,
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
        # See the equivalent note in `_mos_array_describe` (issue #1577).
        _contact_gate_extra_offset_um(family),
        _gate_bottom_endcap_um(family),
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
    6: "list",  # PCellParameterDeclaration.TypeList -- a JSON array
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
        layer_params=_mos_array_layer_params,
    ),
    "res_array": _GeneratorSpec(
        name="res_array",
        summary=(
            "Unit resistor/capacitor array: a row of matched unit elements "
            "(poly body + contact + local-metal pads at both ends) with "
            "dummy elements at each end, per the sky130-bandgap-reference KB "
            "entry's resistor-array layout idiom -- family 2. "
            "params.metal_level (1..5 on sky130) instead draws that level's "
            "drawn metal-layer resistor (met1..met5's res_generic_mN, issue "
            "#1639) -- body + resistor-ID marker + an end via/landing-pad "
            "stack down to the metal level below."
        ),
        dbu=0.001,
        validate=_res_array_validate,
        describe=_res_array_describe,
        layer_params=_resistor_layer_params,
    ),
    "cap_array": _GeneratorSpec(
        name="cap_array",
        summary=(
            "Unit MiM capacitor array: a row of matched unit cells, each a "
            "top-plate-metal-over-bottom-plate-metal MiM stack (sky130's "
            "capm/met3) with a top-plate via + local-metal landing pad -- "
            "the capacitor sibling of res_array (issue #1117)."
        ),
        dbu=0.001,
        validate=_cap_array_validate,
        describe=_cap_array_describe,
        layer_params=_cap_array_layer_params,
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
    "well_island": _GeneratorSpec(
        name="well_island",
        summary=(
            "Named-net, isolated well/tap island: guard_ring's tap ring + "
            "well tie, with the tie bound to a caller-supplied net (drawn as "
            "a label on the ring's own metal, never on the well label layer) "
            "and a separation constraint against a caller-supplied set of "
            "other well regions -- sized to clear them or rejected outright, "
            "never silently merged. The primitive for giving two groups of "
            "same-flavour devices two different body potentials (issue "
            "#1421)."
        ),
        dbu=0.001,
        validate=_well_island_validate,
        describe=_well_island_describe,
        layer_params=_well_island_layer_params,
    ),
    "diff_pair": _GeneratorSpec(
        name="diff_pair",
        summary=(
            "Differential pair / current mirror cell: two matched devices "
            "(Q1/Q2, or M1/M2 with params.mirror) split into params.splits "
            "sub-instances each and interleaved in a true common-centroid "
            "cross-quad pattern, optionally enclosed by an automatically-"
            "sized guard ring -- family 4, composing families 1 and 3. The "
            "interleave puts one Q1 and one Q2 sub-instance in every column, "
            "so the two legs share one x column per terminal -- read "
            "docs/cli/gen.md's diff_pair section before floorplanning a "
            "one-column-per-net routing grid around it."
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
