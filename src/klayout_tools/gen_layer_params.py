"""PDK layer-parameter and design-rule lookup subsystem for :mod:`gen`.

Extracted from ``gen.py`` (issue #1875), in the series of oversized-verb-
module splits covering ``extract.py``->``extract_spef.py`` (#1195), ``lvs.py``->
``lvs_netgen.py``/``lvs_mismatch.py`` (#1803/#1721),
``gen_compose.py``->``gen_compose_routing.py`` (#1708), ``sim.py``->
``sim_remote.py`` (#1764), ``place_and_route.py``->
``place_and_route_sta.py`` (#1808), and ``gen.py``'s own earlier
``gen_pcells/`` package split (#1698/#1713)): the per-PDK-family constant
tables (``_PDK_ROLE_LAYERS`` and friends) and the small resolver functions
that read them (``_device_layer_params``, ``_pdk_family``,
``_role_layer_info``, etc.), used by every phase-2 generator in ``gen.py``
to translate a resolved PDK family into the concrete ``kdb.LayerInfo``
layer pairs and geometry floors it draws on.

This module has no top-level dependency on ``gen.py`` -- the one-way split
the parent issue documents holds exactly: nothing here calls forward into
``gen.py``'s geometry-building code. Two names genuinely belong to ``gen.py``
proper (``GenError``, used across its whole validation surface, and
``WELL_ENCLOSURE_MARGIN_UM``, a general geometry constant used well beyond
this subsystem) -- the handful of functions here that need them import them
back lazily, at call time, the same way this module's own functions already
import ``klayout.db`` lazily to avoid paying for it up front. By the time
any of these functions actually runs, ``gen`` has always finished importing
this module and is fully loaded, so the lazy import never raises.
"""

from __future__ import annotations

from typing import Any

from .pdk_models import _pdk_variant_family

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
    from .gen import WELL_ENCLOSURE_MARGIN_UM

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

#: ``cap_array``'s top-plate requires-mask slots (``cap_top_requires_<i>_*``)
#: are generated from this value and join ``gen.py``'s own
#: :data:`klayout_tools.gen._HIDDEN_PARAMS` there, since they are
#: harness-computed layer params, never part of the request schema.


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
    from .gen import GenError

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

#: ``res_array``'s flavour mask slots (``res_flavor_<i>_*``) are generated
#: from this value and join ``gen.py``'s own
#: :data:`klayout_tools.gen._HIDDEN_PARAMS` there, since they are
#: harness-computed layer params, never part of the request schema.

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
    from .gen import GenError

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
#: original poly-body geometry byte-for-byte unchanged; sky130's ``1``..``5``
#: instead draw the body on ``metN.drawing``, its own resistor-ID marker
#: (``metN.res``), and an end via/landing-pad stack connecting *down* to the
#: metal level immediately below (``li1`` for ``met1``, ``metN-1`` otherwise)
#: -- the same "contact + local-metal landing pad at each end" shape
#: :func:`_res_unit_layout` already draws for the poly case, just relocated
#: one or more levels up the stack. Landing on the layer below (never above)
#: keeps a single, uniform code path across all five sky130 levels: sky130
#: always has a metal level immediately below (down to ``li1``), while only
#: ``met1``..``met4`` have one immediately above.
#:
#: ``sg13g2`` (issue #1758) picks the *opposite* landing direction -- up, not
#: down -- because it has no sky130-``li1`` analogue: sg13g2's ``contact``
#: role (``Cont`` 6/0, ``EXTRACTION_DECK.contact``) lands ``Metal1`` on
#: poly/active, not on another routable metal, so there is no metal level
#: below ``Metal1`` for a level-1 unit's end via to land on. Landing up
#: instead (``Metal1`` -> ``Via1`` -> ``Metal2``, ``Metal2`` -> ``Via2`` ->
#: ``Metal3``) also matches this deck's own single-sided via-enclosure
#: convention: every ``vian.drc``-derived enclosure rule in
#: ``klayout_tools.decks.sg13g2`` (``metal1.enclosing.via1.1``,
#: ``metal2.enclosing.via2.1``, ...) checks only the *lower* metal's
#: enclosure of the via, exactly the ``body``-encloses-``via`` relationship a
#: landing-up level already draws, with no matching "upper metal encloses
#: via" rule ever modelled to check the ``landing`` side against.
#:
#: ``gf180mcu`` (issue #1731) lands up too, for the same reason as sg13g2:
#: its ``contact`` role (``Contact`` 33/0, ``EXTRACTION_DECK.contact``) lands
#: ``Metal1`` on poly/active, not on another routable metal, so there is no
#: metal level below ``Metal1`` to land a level-1 unit's end via on. Levels
#: 1-3 (``rm1``/``rm2``/``rm3``, ``klayout_tools.decks.gf180mcu
#: .EXTRACTION_DECK.resistors``, issue #1640) land ``Metal1`` -> ``Via1`` ->
#: ``Metal2``, ``Metal2`` -> ``Via2`` -> ``Metal3``, ``Metal3`` -> ``Via3`` ->
#: ``Metal4`` respectively. The ``tm6k``/``tm9k``/``tm11k``/``tm30k``
#: top-metal thickness family (same deck, body on ``Metal5``) is deliberately
#: *not* added as a level here: unlike ``rm1``..``rm3`` (plain two-terminal
#: devices), that family is itself caller-selectable via its own
#: ``flavour_option="metal_top"`` axis -- folding a second, independent
#: flavour selector into ``metal_level`` (which already repurposes
#: ``res_array``'s ``flavor`` param for the poly-body case, ignored entirely
#: once ``metal_level`` is non-zero) is a genuine design question, not the
#: mechanical table lookup the rest of this entry is, so it is left for a
#: follow-up rather than folded into this table by convention alone.
#:
#: Each entry's layer/datatype pairs are the *same* ones
#: ``klayout_tools.decks.sky130``/``.sg13g2``/``.gf180mcu.EXTRACTION_DECK``
#: already declare -- never a second, private map:
#:
#: - ``body``/``marker`` -- the exact ``ResistorDevice.body``/``.marker`` pair
#:   for that level's device (sky130's ``res_generic_mN``; sg13g2's
#:   ``res_metal1``/``res_metal2``; gf180mcu's ``rm1``/``rm2``/``rm3``).
#: - ``via`` -- sky130: ``EXTRACTION_DECK.vias[N - 1]``, the via connecting
#:   ``metN`` down to the level below (``mcon`` for ``met1``; ``via``/
#:   ``via2``/``via3``/``via4`` for ``met2``..``met5``). sg13g2/gf180mcu: the
#:   via connecting the body level up to the landing level (sg13g2's
#:   ``Via1``/``Via2``; gf180mcu's ``Via1``/``Via2``/``Via3``, indexed the
#:   same way -- ``EXTRACTION_DECK.vias[N - 1]`` connects ``metals[N - 1]``
#:   to ``metals[N]``, so the *same* index expression that lands sky130 down
#:   lands sg13g2/gf180mcu up).
#: - ``landing`` -- sky130: ``EXTRACTION_DECK.metals[N - 1]``, the conductor
#:   that via lands on (``li1`` for ``met1``; ``met1``..``met4`` for
#:   ``met2``..``met5``). sg13g2/gf180mcu: ``EXTRACTION_DECK.metals[N]`` (the
#:   level *above* the body -- sg13g2's ``Metal2``/``Metal3``; gf180mcu's
#:   ``Metal2``/``Metal3``/``Metal4``).
#:
#: ``sky130``, ``sg13g2``, and ``gf180mcu`` are populated today. No other
#: family exposes a drawn metal-resistor device class yet (sg13cmos5l's
#: curated deck declares none at all), so a ``metal_level`` request against
#: it still raises :class:`GenError` via :func:`_metal_res_layers` rather
#: than silently drawing unrecognised geometry.
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
    "sg13g2": {
        # klayout_tools.decks.sg13g2.EXTRACTION_DECK.resistors' res_metal1
        # (issue #1235), landing *up* through Via1 to Metal2 -- see this
        # table's own docstring above for why sg13g2 lands up rather than
        # down like sky130.
        1: {
            "body": (8, 0),  # Metal1.drawing
            "marker": (8, 29),  # Metal1.res
            "via": (19, 0),  # Via1.drawing (Metal1<->Metal2)
            "landing": (10, 0),  # Metal2.drawing
        },
        # EXTRACTION_DECK.resistors' res_metal2 (issue #1235), landing up
        # through Via2 to Metal3.
        2: {
            "body": (10, 0),  # Metal2.drawing
            "marker": (10, 29),  # Metal2.res
            "via": (29, 0),  # Via2.drawing (Metal2<->Metal3)
            "landing": (30, 0),  # Metal3.drawing
        },
        # res_metal3..res_topmetal2 are not declared here: EXTRACTION_DECK
        # .resistors itself does not recognise them yet (see
        # klayout_tools.decks.sg13g2's own module docstring, "Still
        # unrecognised" -- Metal3-TopMetal2 sit above this deck's curated
        # resistor coverage even though EXTRACTION_DECK.metals/.vias reach
        # TopMetal2, issue #1243). Adding levels 3-7 here ahead of that
        # extraction-side recognition would let `res_array` draw a device
        # `klt extract --deck sg13g2` cannot classify as anything but plain
        # interconnect -- the same "recognised but not drawable" bar this
        # table's own family-level GenError already enforces the other way
        # around (see :func:`_metal_res_layers`), applied per-level instead
        # of per-family.
    },
    "gf180mcu": {
        # klayout_tools.decks.gf180mcu.EXTRACTION_DECK.resistors' rm1 (issue
        # #1640), landing *up* through Via1 to Metal2 -- see this table's own
        # docstring above for why gf180mcu lands up rather than down like
        # sky130. metals=(Metal1, Metal2, Metal3, Metal4, Metal5),
        # vias=(Via1, Via2, Via3, Via4) -- vias[0] connects metals[0]
        # (Metal1) to metals[1] (Metal2).
        1: {
            "body": (34, 0),  # Metal1
            "marker": (110, 11),  # metal1_res
            "via": (35, 0),  # Via1 (Metal1<->Metal2)
            "landing": (36, 0),  # Metal2
        },
        # EXTRACTION_DECK.resistors' rm2, landing up through Via2 to Metal3
        # (vias[1] connects metals[1]/Metal2 to metals[2]/Metal3).
        2: {
            "body": (36, 0),  # Metal2
            "marker": (110, 12),  # metal2_res
            "via": (38, 0),  # Via2 (Metal2<->Metal3)
            "landing": (42, 0),  # Metal3
        },
        # EXTRACTION_DECK.resistors' rm3, landing up through Via3 to Metal4
        # (vias[2] connects metals[2]/Metal3 to metals[3]/Metal4).
        3: {
            "body": (42, 0),  # Metal3
            "marker": (110, 13),  # metal3_res
            "via": (40, 0),  # Via3 (Metal3<->Metal4)
            "landing": (46, 0),  # Metal4
        },
        # tm6k/tm9k/tm11k/tm30k (body on Metal5) are deliberately not added
        # here -- see this table's own docstring above for why the
        # top-metal thickness family needs its own design decision rather
        # than a mechanical level-4 entry.
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
    from .gen import GenError

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
#:
#: sg13g2 (issue #1758) needs no entry for either of its two levels either,
#: the same "generic floor already wins" reason as sky130's levels 1-4:
#: every rule `klayout_tools.decks.sg13g2` declares for the Metal1/Via1/
#: Metal2/Via2/Metal3 stack those two levels draw on is looser than the
#: generic constants --
#:
#: - ``via_min_w_um``: ``via1.width.1``/``via2.width.1`` are both 0.19um,
#:   under :data:`CONTACT_SIZE_UM` (0.22um).
#: - ``via_enclosure_min_um``: ``metal1.enclosing.via1.1``/
#:   ``metal2.enclosing.via2.1`` are 0.01um/0.005um, under
#:   :data:`ENCLOSURE_MARGIN_UM` (0.1um) -- sg13g2's own via-enclosure rules
#:   check only the *lower* metal (this table's ``body`` side for a
#:   landing-up level, see :data:`_PDK_METAL_RES_LEVELS`'s own docstring), so
#:   there is no second, ``landing``-side enclosure rule to compare against
#:   at all.
#: - ``via_space_min_um``: the stricter of ``via1.space.1``/``via2.space.1``
#:   (0.22um both) and ``metal1.space.1``/``metal2.space.1`` (0.18um/0.21um)
#:   is 0.22um, under :data:`MIN_SAME_LAYER_SPACING_UM` (0.4um).
#: - ``body_width_min_um``: ``metal1.width.1``/``metal2.width.1`` are
#:   0.16um/0.20um, under :data:`UNIT_MIN_W_UM` (0.42um).
#:
#: gf180mcu (issue #1731) needs a ``via_min_w_um`` entry at all three of its
#: levels -- unlike sky130's levels 1-4 and both sg13g2 levels, its via size
#: rule is *stricter* than the generic floor:
#:
#: - ``via_min_w_um`` 0.26um at levels 1-3 -- ``via1.width.1``/
#:   ``via2.width.1``/``via3.width.1`` (DRM 7.14 Vian rule ``Vn.1``) are all
#:   0.26um, over :data:`CONTACT_SIZE_UM` (0.22um) -- so, unlike every other
#:   populated family/level, drawing this generic 0.22um square here would be
#:   DRC-dirty against gf180mcu's own via rule.
#: - ``via_enclosure_min_um``: needs no entry -- ``metal1.enclosing.via1.1``/
#:   ``metal2.enclosing.via1.1`` (0.0um/0.01um), ``metal2.enclosing.via2.1``/
#:   ``metal3.enclosing.via2.1`` (0.01um/0.01um), and
#:   ``metal3.enclosing.via3.1``/``metal4.enclosing.via3.1`` (0.01um/0.01um)
#:   are all under :data:`ENCLOSURE_MARGIN_UM` (0.1um).
#: - ``via_space_min_um``: needs no entry -- the stricter of
#:   ``vian.space.1`` (0.26um, all three levels) and ``metalN.space.1``
#:   (0.23um for Metal1, 0.28um for Metal2/Metal3) is 0.28um, under
#:   :data:`MIN_SAME_LAYER_SPACING_UM` (0.4um).
#: - ``body_width_min_um``: needs no entry -- ``metal1.width.1``/
#:   ``metal2.width.1``/``metal3.width.1`` (0.23um/0.28um/0.28um) are all
#:   under :data:`UNIT_MIN_W_UM` (0.42um).
_PDK_METAL_RES_LEVEL_MIN_UM: dict[str, dict[int, dict[str, float]]] = {
    "sky130": {
        5: {
            "via_min_w_um": 0.8,  # via4.width.1 (via4.1_a)
            "via_enclosure_min_um": 0.31,  # met5.enclosing.via4.1 (m5.3)
            "via_space_min_um": 1.6,  # max(via4.space.1=0.8, met5.space.1=1.6)
            "body_width_min_um": 1.6,  # met5.width.1 (m5.1)
        },
    },
    "gf180mcu": {
        1: {"via_min_w_um": 0.26},  # via1.width.1 (Vn.1)
        2: {"via_min_w_um": 0.26},  # via2.width.1 (Vn.1)
        3: {"via_min_w_um": 0.26},  # via3.width.1 (Vn.1)
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


#: Per-PDK-family ``res_array`` ``metal_level`` -> the exact ``klt extract``
#: device-class name that level draws (issue #1731) -- the class-*name*
#: sibling of :data:`_PDK_METAL_RES_LEVELS` (which resolves the same
#: ``family``/``level`` pair to *layers*, never a name). Used only by
#: :func:`_resolve_expected_device_class` below, itself only consumed by
#: ``layout_plan_execute.py``'s silent-substitution guard (issue #1731) --
#: ``gen.py``'s own layout code never needs the class name, only the layers,
#: to draw geometry, so this table is intentionally *not* threaded through
#: :func:`_resistor_layer_params`. Every value here is the same
#: ``ResistorDevice.name`` :data:`_PDK_METAL_RES_LEVELS`'s own docstring
#: already cites per level -- never a second, independently-sourced name.
_PDK_METAL_RES_DEVICE_CLASS: dict[str, dict[int, str]] = {
    "sky130": {
        1: "res_generic_m1",
        2: "res_generic_m2",
        3: "res_generic_m3",
        4: "res_generic_m4",
        5: "res_generic_m5",
    },
    "sg13g2": {
        1: "res_metal1",
        2: "res_metal2",
    },
    "gf180mcu": {
        1: "rm1",
        2: "rm2",
        3: "rm3",
    },
}

#: Per-PDK-family ``res_array`` ``flavor`` -> the exact ``klt extract``
#: device-class name that flavour draws (issue #1731) when ``metal_level``
#: is ``0`` (the poly-body path) -- the class-*name* sibling of
#: :data:`_PDK_RES_FLAVOR_LAYERS` (which resolves the same ``family``/
#: ``flavor`` pair to *masks*, never a name), for the same
#: silent-substitution guard :data:`_PDK_METAL_RES_DEVICE_CLASS` serves.
#: Every value is the same device-class name :data:`_PDK_RES_FLAVOR_LAYERS`'s
#: own per-flavour comments already cite.
_PDK_RES_FLAVOR_DEVICE_CLASS: dict[str, dict[str, str]] = {
    "sky130": {
        "generic": "res_generic_po",
        "high": "res_high_po",
        "xhigh": "res_xhigh_po",
    },
    "gf180mcu": {
        "generic": "ppolyf_u",
        "1k": "ppolyf_u_1k",
        "2k": "ppolyf_u_2k",
        "3k": "ppolyf_u_3k",
    },
    "sg13g2": {
        "generic": "rsil",
        "rppd": "rppd",
        "rhigh": "rhigh",
    },
    "sg13cmos5l": {
        "generic": "rsil",
        "rsil": "rsil",
        "rppd": "rppd",
        "rhigh": "rhigh",
    },
}


def _resolve_expected_device_class(
    generator: str, family: str, params: dict[str, Any]
) -> str | None:
    """The device-class name ``generator`` draws for ``family`` under
    ``params`` -- ``None`` when this generator/family/selector combination
    has no static prediction available, which the caller (``layout_plan_
    execute.py``'s silent-substitution guard, issue #1731) must treat as
    "cannot determine," never as "confirmed mismatch."

    Only ``res_array`` is covered today -- its two independent
    device-class selectors (``flavor`` for the poly-body case,
    ``metal_level`` for the metal-body case, see :data:`_PDK_RES_FLAVOR_
    DEVICE_CLASS`/:data:`_PDK_METAL_RES_DEVICE_CLASS`) are the exact
    reproduction issue #1731 reports: a ``device_groups[]`` entry naming a
    metal-resistor ``device_class`` but routed through ``res_array`` with no
    explicit ``metal_level`` override silently draws the poly-body default
    instead. Dispatched on ``generator`` by name (mirroring
    :data:`_SIZE_PARAM_TARGETS`'s own per-generator dispatch shape in
    ``layout_plan_execute.py``) so a future generator with its own
    device-class-selecting param can add an entry here without restructuring
    this function -- no other generator's request params currently select
    between distinct recognised device classes the way ``res_array``'s do
    (``mos_array``/``diff_pair``/``bjt_array``'s ``flavor`` selects a
    transistor polarity or voltage variant, not a distinct LVS device class;
    ``cap_array``'s density flavour is a ``deck_options`` extraction-side
    concern, never a ``klt gen`` request param)."""
    if generator != "res_array":
        return None
    metal_level = params.get("metal_level", 0)
    if metal_level:
        return _PDK_METAL_RES_DEVICE_CLASS.get(family, {}).get(metal_level)
    flavor = params.get("flavor", _DEFAULT_RES_FLAVOR)
    return _PDK_RES_FLAVOR_DEVICE_CLASS.get(family, {}).get(flavor)


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
    from .gen import GenError

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
    from .gen import GenError

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
    from .gen import GenError

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
