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

# Data tables live in gen_layer_tables.py (#3722); re-exported here.
from .gen_layer_tables import (  # noqa: F401
    _CAP_GEOMETRY_MIN_KEYS,
    _PDK_CAP_GEOMETRY_MIN_UM,
    _PDK_ROLE_LAYERS,
    CAP_BOTTOM_PLATE_MARGIN_UM,
    CAP_TOP_ESCAPE_CLEARANCE_UM,
    CONTACT_GAP_SAFE_UM,
    ESD_FINGER_WIDTH_MAX_UM,
    ESD_FINGER_WIDTH_MIN_UM,
    ESD_MAX_FINGERS_PER_RING,
    GATE_LENGTH_SAFE_MIN_UM,
    GUARD_RING_DEFAULT_CONTACTS_PER_SIDE,
    GUARD_RING_DEFAULT_PADDING_UM,
    GUARD_RING_DEFAULT_WIDTH_UM,
    PAD_OPENING_GUIDELINE_MIN_UM,
    PAD_TOP_METAL_ENCLOSURE_MIN_UM,
    SD_PAD_GATE_GAP_MIN_UM,
    UNIT_MIN_W_UM,
)
from .pdk_families import pdk_variant_family

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
#: every existing caller's gate port for no DRC benefit on families whose real
#: deck does not need it (issues #461/#492/#781 pinned that contract; sky130
#: and gf180mcu have since been moved deliberately, see their entries below).
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
#: - ``sky130`` (issue #3115): 0.15um, above the PDK's own ``poly.4`` (poly
#:   space to unrelated diffusion, 0.075um -- which this repo's curated
#:   ``sky130`` deck does not transcribe, so ``klt drc --deck sky130`` never
#:   saw the zero-distance pad overhang that Magic ``drc(full)`` flags for
#:   every gate length below the 0.42um pad width) and above ``poly.8``'s
#:   0.13um gate endcap (the stem now supplies it on the top edge, see
#:   #2936). A real, deliberate geometry change like gf180mcu's: the landing
#:   pad and the reported ``U<i>_G`` port move up by this amount; see
#:   ``docs/cli/gen.md``.
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
    "sky130": 0.15,  # poly.4 (poly to unrelated diff) is 0.075um; poly.8 endcap 0.13um
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
#: :func:`_gate_bottom_endcap_um`): sg13g2's/sg13cmos5l's curated
#: decks transcribe neither rule (sky130's gained its own endcap rule, so it
#: has an entry above, issue #2936), so every existing caller there keeps
#: byte-for-byte identical geometry.
_PDK_GATE_BOTTOM_ENDCAP_UM: dict[str, float] = {
    "gf180mcu": 0.25,  # PL.4_LV (0.22um) / DF.6_LV (0.24um)
    # poly.8 (0.13um, issue #2936), with margin; the curated deck's
    # `poly.endcap.diff.1` checks it.
    "sky130": 0.15,
}


def _gate_bottom_endcap_um(family: str) -> float:
    """Return ``family``'s gate-stripe bottom-edge endcap extension (see
    :data:`_PDK_GATE_BOTTOM_ENDCAP_UM`), or ``0.0`` for a family that
    declares none -- byte-for-byte unchanged geometry there."""
    return _PDK_GATE_BOTTOM_ENDCAP_UM.get(family, 0.0)


#: Gate-stripe *top*-edge endcap (um) the series ``_mos_unit_layout`` draws
#: as a stripe-width poly neck before the landing pad (issue #2936). Distinct
#: from :data:`_PDK_GATE_BOTTOM_ENDCAP_UM` because the top edge already has
#: the #461 landing pad, which clears the diffusion for ``L`` up to the pad
#: width only; a family whose pad already satisfies its deck (gf180mcu) is
#: left out so its geometry stays byte-for-byte unchanged.
_PDK_GATE_TOP_ENDCAP_UM: dict[str, float] = {
    "sky130": 0.15,  # poly.8 (0.13um) with margin
}


def _gate_top_endcap_um(family: str) -> float:
    """Return ``family``'s gate-stripe top-edge endcap neck length (see
    :data:`_PDK_GATE_TOP_ENDCAP_UM`), or ``0.0`` for a family that declares
    none -- byte-for-byte unchanged geometry there."""
    return _PDK_GATE_TOP_ENDCAP_UM.get(family, 0.0)


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
#: gf180mcu and, since issue #2934, sky130 declare the ``"nplus"``/``"pplus"``
#: roles) -- sg13g2's/sg13cmos5l's curated decks recognise a MOS device from
#: ``active``/``well`` alone, with no implant mask at all, so every existing
#: caller there keeps byte-for-byte identical geometry (no implant drawn).
_PDK_SD_IMPLANT_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 0.25,  # NP.5a/PP.5a (gate overlap) are 0.23um
    # sky130 (issue #2934): n/psdm.5a, "min. n/psdm enclosure diff except
    # butting edge", 0.125um (sky130A.lydrc, PDK c6d73a35).
    "sky130": 0.125,
}


#: Minimum same-layer spacing (um) between two separate source/drain implant
#: shapes, for a family whose implant boxes can land closer than that
#: between adjacent unit devices (issue #2934): sky130's ``nsdm.2``/``psdm.2``
#: is 0.38um, while the per-unit boxes (diff + 0.125um margin) sit only
#: ~0.15um apart in a ``mos_array``/``diff_pair`` row. Gaps below this are
#: closed (the shapes merge) in :func:`~klayout_tools.gen._close_implant_gaps`.
#: A family absent from this table is left untouched (``0.0``).
_PDK_SD_IMPLANT_MIN_SPACE_UM: dict[str, float] = {
    "sky130": 0.38,  # nsdm.2/psdm.2 (sky130A_mr.drc)
}


def _sd_implant_min_space_um(family: str) -> float:
    return _PDK_SD_IMPLANT_MIN_SPACE_UM.get(family, 0.0)


def _sd_implant_margin_um(family: str) -> float:
    """Return ``family``'s source/drain implant margin (see
    :data:`_PDK_SD_IMPLANT_MARGIN_UM`), or ``0.0`` for a family that declares
    none -- byte-for-byte unchanged geometry there (no implant drawn)."""
    return _PDK_SD_IMPLANT_MARGIN_UM.get(family, 0.0)


#: Margin (um) a tap/collector *ring*'s own implant
#: (:func:`_ring_tap_implant_layer`, issue #1580 -- and ``well_island``'s
#: identical ``"well_tap_implant"`` ring, issue #1421) extends beyond the
#: ring's ``Comp`` band on every edge, inner and outer alike. The
#: ring-shaped counterpart of :data:`_PDK_SD_IMPLANT_MARGIN_UM`, which does
#: the same job for a *unit device's* source/drain implant box.
#:
#: ``gf180mcu`` only (issue #2369): 0.16um, the real ``NP.5b``/``PP.5b``
#: ("Extension beyond COMP") threshold. Both #1421 and #1580 drew their
#: implant ring *exactly coincident* with the tap ring -- enough for
#: ``DF.12`` ("COMP not covered by Nplus or Pplus is forbidden"), which is a
#: coverage rule with no distance in it, but a genuine ``NP.5b``/``PP.5b``
#: violation on every edge of the band once those two rules are actually
#: checked. Unlike ``_PDK_SD_IMPLANT_MARGIN_UM``'s 0.25-for-0.23, no
#: headroom is added over the threshold: the margin is uniform on all four
#: sides of an axis-aligned band, so every ``Comp`` edge sits exactly
#: ``margin_um`` from the implant edge facing it and every corner further
#: still (``margin_um * sqrt(2)``) -- the ``euclidian`` metric
#: ``enclosed_check`` uses reports *less than* the threshold, never equal --
#: and the ring's inner edge grows *toward* whatever the ring encloses, so
#: extra margin here is spent directly out of the clearance the enclosed
#: device needs for ``NP.3a``/``PP.3a`` (implant to opposite-type COMP,
#: 0.16um).
#:
#: A family absent from this table gets ``0.0``
#: (:func:`_ring_implant_margin_um`), which
#: :func:`~klayout_tools.gen._insert_implant_ring` treats as "draw exactly
#: what :func:`~klayout_tools.gen._insert_ring` drew before" -- and no family
#: besides gf180mcu resolves a ring implant layer at all (see
#: :func:`_ring_tap_implant_layer`), so every other family's geometry is
#: byte-for-byte unchanged.
_PDK_RING_IMPLANT_MARGIN_UM: dict[str, float] = {
    "gf180mcu": 0.16,  # NP.5b/PP.5b (extension beyond COMP) are 0.16um
    # sky130 (issue #2934): n/psdm.5b, "min. n/psdm enclosure tap except
    # butting edge", 0.125um (sky130A.lydrc, PDK c6d73a35).
    "sky130": 0.125,
}


#: Families whose tap ring needs an implant even though their ``"tap"`` role
#: is a *distinct* mask from ``"active"`` (so the collision test in
#: :func:`_ring_tap_implant_layer` does not apply): sky130 draws ``tap``
#: (65/44) separately from ``diff`` (65/20), but ``n/psdm.5b`` still requires
#: ``nsdm``/``psdm`` around it and ``difftap.10``/``n/psdm.8`` tie the
#: implant type to the well underneath (issue #2934).
_DISTINCT_TAP_IMPLANT_FAMILIES = frozenset({"sky130"})


#: Margin (um) the ``npc`` box grows beyond each poly-contact licon (issue
#: #2934): 0.095um, the amount ``sky130A.tech``'s own ``cifoutput`` grows NPC
#: around ``xpc`` (the same figure :data:`_PDK_RES_FLAVOR_MASK_ENCLOSURE_UM`
#: uses for resistor heads). The official ``sky130A_mr.drc`` carries no
#: npc-enclosure-of-licon rule. A family absent here draws no ``npc``.
_PDK_NPC_MARGIN_UM: dict[str, float] = {"sky130": 0.095}

#: ``npc.2`` same-layer spacing (um); closer ``npc`` shapes are merged
#: (``sky130A_mr.drc`` marks it "should be manually merged").
_PDK_NPC_MIN_SPACE_UM: dict[str, float] = {"sky130": 0.27}


def _bjt_unit_implant_margin_um(family: str) -> float:
    """``bjt_array``'s per-unit emitter (Pplus) / base-tie (Nplus) implant
    margin (issue #3587), or ``0.0`` for a family without both implant roles
    -- byte-for-byte unchanged geometry there. Uses the ring's minimal
    NP.5b/PP.5b margin, not the wider S/D one, which would put each implant
    within NP.3/PP.3 range of the facing opposite-type pad."""
    if (
        _role_layer_info(family, "pplus") is None
        or _role_layer_info(family, "nplus") is None
    ):
        return 0.0
    return _ring_implant_margin_um(family)


#: Reach (um) of the tap-distance rule the layout generators size their tap
#: pads for (issue #3648): every active shape must lie within this distance of
#: a well/substrate tap. 20um is the 3.3V column of gf180mcu's ``DF.13`` /
#: ``DF.14`` (``comp.tap_distance.{nwell,psub}.lv.1`` in
#: :mod:`klayout_tools.decks.gf180mcu`; ``tests/test_gen.py`` pins the two
#: equal).
TAP_PAD_REACH_UM = 20.0

#: Families whose deck enforces that rule, so ``mos_array``/``diff_pair``
#: draw tap pads even with ``add_guard_ring`` off (see
#: :func:`_tap_pads_wanted`). Every other family keeps its geometry (and GDS
#: stream) byte-for-byte unchanged.
_TAP_DISTANCE_FAMILIES = frozenset({"gf180mcu"})


def _tap_pad_ring_implant_margins_um() -> frozenset[float]:
    """The ``ring_implant_margin_um`` values of the :data:`_TAP_DISTANCE_FAMILIES`.

    ``diff_pair`` resolves the ring role (so ``ring_implant_present``) for every
    family, so a PCell cannot tell a tap-distance family from that flag alone;
    its resolved ring implant margin -- family-specific and already a hidden
    param -- does (``tests/test_gen_tap_pads.py`` pins that no other family
    shares one), without adding a PCell param that would change every other
    family's GDS stream."""
    return frozenset(_ring_implant_margin_um(f) for f in _TAP_DISTANCE_FAMILIES)


def _tap_pads_wanted(family: str) -> bool:
    """Whether ``family``'s deck enforces a tap-distance rule (see
    :data:`_TAP_DISTANCE_FAMILIES`), so a ring-less array needs tap pads."""
    return family in _TAP_DISTANCE_FAMILIES


def _ring_implant_margin_um(family: str) -> float:
    """Return ``family``'s tap/collector-ring implant margin (see
    :data:`_PDK_RING_IMPLANT_MARGIN_UM`), or ``0.0`` for a family that
    declares none -- byte-for-byte unchanged geometry there."""
    return _PDK_RING_IMPLANT_MARGIN_UM.get(family, 0.0)


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


#: Per-family nwell enclosure margin (um) of the n-tap guard ring
#: (:data:`WELL_ENCLOSURE_MARGIN_UM` for a family absent here, byte-for-byte).
#:
#: ``sky130`` only (issue #3115): the PDK's ``diff/tap.10`` (nwell overlap of
#: n-tap) is 0.18um, which this repo's curated sky130 deck does not
#: transcribe; 0.2um clears it with headroom.
_PDK_RING_WELL_MARGIN_UM: dict[str, float] = {
    "sky130": 0.2,  # diff/tap.10 (nwell overlap of n-tap) is 0.18um
}


def _ring_well_margin_um(family: str) -> float:
    """Return ``family``'s n-tap ring nwell enclosure margin (see
    :data:`_PDK_RING_WELL_MARGIN_UM`), or :data:`WELL_ENCLOSURE_MARGIN_UM`."""
    from .gen import WELL_ENCLOSURE_MARGIN_UM

    return _PDK_RING_WELL_MARGIN_UM.get(family, WELL_ENCLOSURE_MARGIN_UM)


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
        # requires), plus npc (issue #3077): not in that `requires` -- the
        # curated deck never keys off it -- but the open_pdks Magic tech
        # forms `xhrpoly` only where `RPM & POLYRES & POLY & PSDM & NPC`, so
        # without it Magic sees no resistor at all.
        "high": (
            (94, 20),  # psdm -- P+ implant
            (86, 20),  # rpm  -- 300 ohm precision-resistor mask
            (95, 20),  # npc  -- Magic's `xhrpoly` needs it
        ),
        # sky130_fd_pr__res_xhigh_po_* (2 kohm/sq): psdm P+ implant + urpm
        # (EXTRACTION_DECK.resistors["res_xhigh_po"].requires). rpm/urpm are
        # mutually exclusive, so drawing urpm (not rpm) selects xhigh. npc
        # as for "high" (Magic's `uhrpoly`, issue #3077).
        "xhigh": (
            (94, 20),  # psdm -- P+ implant
            (79, 20),  # urpm -- 2 kohm precision-resistor mask
            (95, 20),  # npc  -- Magic's `uhrpoly` needs it
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


#: Per-PDK-family poly-resistor *flavour* -> the minimum ``res_array``
#: ``params.width_um`` that flavour accepts (issue #2407). :data:`UNIT_MIN_W_UM`
#: (0.42um) is a single, generator-wide *structural* floor -- sized so
#: :func:`~klayout_tools.gen._res_unit_layout`'s end contact
#: (:data:`CONTACT_SIZE_UM`, 0.22um) keeps :data:`ENCLOSURE_MARGIN_UM`
#: (0.1um, a conservative margin exceeding *both* curated decks' own real
#: contact-enclosure rules) clear of the poly edge on every side -- applied
#: unconditionally regardless of which PDK family or resistor flavour a
#: request resolves to. But some PDK-native poly-resistor flavours ship a
#: first-class fixed-width primitive narrower than that generic floor:
#: sky130's ``res_high_po``/``res_xhigh_po`` (this generator's ``"high"``/
#: ``"xhigh"`` flavours, :data:`_PDK_RES_FLAVOR_LAYERS`) each ship a
#: ``sky130_fd_pr__res_{high,xhigh}_po_0p35`` primitive -- the narrowest of
#: five discrete widths (0.35/0.69/1.41/2.85/5.73um) those two device
#: classes support -- cited by name in
#: ``klayout_tools.decks.sky130``'s own ``ResistorDevice.provenance`` for
#: both classes. 0.35um is structurally safe even under sky130's own real
#: curated-deck rule: the drawn contact still keeps ``(0.35 - 0.22) / 2 =
#: 0.065um`` of poly enclosure on every side, comfortably above
#: ``poly.enclosing.licon.1``'s actual 0.05um threshold
#: (``klayout_tools.decks.sky130``) -- it is only *below* the generic,
#: cross-family conservative :data:`ENCLOSURE_MARGIN_UM` (0.1um)
#: :data:`UNIT_MIN_W_UM` was sized against, not below what sky130's own deck
#: would actually flag.
#:
#: A family/flavour pair absent here (every entry not listed, including
#: every non-sky130 family and sky130's own ``"generic"`` flavour -- which
#: has no such fixed-width PDK primitive on record) falls back to
#: :data:`UNIT_MIN_W_UM` via :func:`_res_flavor_min_width_um`, byte-for-byte
#: this generator's pre-#2407 floor for every combination this issue does
#: not concern.
_PDK_RES_FLAVOR_MIN_W_UM: dict[str, dict[str, float]] = {
    "sky130": {
        "high": 0.35,
        "xhigh": 0.35,
    },
}


#: Context-dependent **rectangular** end-contact size per ``(family, flavor)``
#: for ``res_array``'s poly-body path (issue #2449), as ``(w_um, h_um)`` --
#: ``w_um`` along the current-flow axis, ``h_um`` across it.
#:
#: sky130's precision poly resistors (``flavor="high"``/``"xhigh"``, the only
#: ones drawn under ``rpm``/``urpm``) are exempt from the ordinary 0.17um licon
#: rule: ``sky130.lydrc`` ``licon.1b/c`` ("minimum/maximum width/length of
#: licon inside poly resistor : 2.0/0.19um") demands every such licon have
#: edges of exactly 0.19um and 2.0um, i.e. a 0.19um x 2.0um slot. The rule
#: constrains the edge lengths only, not which axis is long; the generator
#: draws the 2.0um edge across the current flow (y), so the slot spans the
#: full resistor head -- matching the foundry's own ``res_high_po``/
#: ``res_xhigh_po`` dogbone-head cells -- and the pitch to a neighbouring
#: contact (``licon.2b``/``licon.2c``: 0.35um / 0.51um) stays met by the
#: unit pitch. An empty/absent entry means the ordinary layer-keyed cut size
#: applies.
_PDK_RES_FLAVOR_END_CONTACT_UM: dict[str, dict[str, tuple[float, float]]] = {
    "sky130": {
        "high": (0.19, 2.0),
        "xhigh": (0.19, 2.0),
    },
}


#: Enclosure (um) of the whole poly resistor -- body *and* contacted end
#: heads -- by each precision-resistor requires-mask, keyed by family then
#: mask layer (issue #2449). Only applied when the request draws
#: :data:`_PDK_RES_FLAVOR_END_CONTACT_UM`'s rectangular contact: upstream
#: ``sky130A_mr.drc`` exempts a licon from ``licon.1`` only inside
#: ``(rpm | urpm) & psdm & poly.interacting(poly_rs)``, so the masks must
#: cover the end-pad licons. Values are the sky130 periphery rules
#: ``rpm.3`` (rpm/urpm enclosure of the poly resistor, 0.2um) and ``rpm.4``
#: (psdm enclosure, 0.11um); npc (issue #3077) takes the 0.095um with which
#: ``sky130A.tech``'s own ``cifoutput`` grows NPC around ``xpc`` / ``xhrpoly``
#: / ``uhrpoly`` (``npc.1``/``npc.2``'s 0.27um width/space are met because
#: neighbouring units' boxes abut, as for rpm/psdm). The resistor-ID marker
#: (``poly.res`` 66/13) stays on the body only. A mask absent here keeps the
#: body-only box.
_PDK_RES_FLAVOR_MASK_ENCLOSURE_UM: dict[str, dict[tuple[int, int], float]] = {
    "sky130": {
        (86, 20): 0.2,  # rpm  -- rpm.3
        (79, 20): 0.2,  # urpm -- rpm.3
        (94, 20): 0.11,  # psdm -- rpm.4
        (95, 20): 0.095,  # npc  -- sky130A.tech cifoutput "xpc surrounds ... 0.095um"
    },
}


#: gf180mcu salicide-block (SAB) geometry for ``res_array``'s poly body (issue
#: #3000), keyed by family. The runset's ``sab.drc`` constrains an unsalicided
#: poly resistor in two directions the body-only marker box violated:
#:
#: - ``contact_space_um``: ``SB.4``, SAB-to-contact space >= 0.15um. The SAB is
#:   drawn on the body segment, so the end head is lengthened until the
#:   contact edge sits this far from the body (the generic enclosure margin
#:   left 0.10um). Body length, and so the extracted ``L``, is unchanged.
#: - ``side_extension_um``: ``SB.9``/``SB.10`` -- the SAB must run >= 0.22um
#:   past the poly body's long edges (a SAB edge flush with the poly edge is
#:   what ``SB.10`` flagged). Folded rows are 0.4um apart, so the extended
#:   SABs of adjacent rows overlap and merge instead of tripping ``SB.2``.
#:
#: Values are from the open GF180MCU DRM / ``sab.drc`` (SB.4: 0.15um,
#: SB.9/SB.10: 0.22um). A family absent here draws the body-only SAB.
_PDK_RES_SAB_UM: dict[str, dict[str, Any]] = {
    "gf180mcu": {
        "layer": (49, 0),
        "contact_space_um": 0.15,
        "side_extension_um": 0.22,
    },
}


def _res_sab_geometry_um(
    family: str, flavor: str, metal_level: int = 0
) -> tuple[float, float]:
    """``(contact_space_um, side_extension_um)`` of the poly-body salicide
    block for ``family`` (see :data:`_PDK_RES_SAB_UM`), or ``(0.0, 0.0)`` for
    a family without one or a ``metal_level`` (metal-body) request."""
    entry = _PDK_RES_SAB_UM.get(family)
    if entry is None or metal_level:
        return (0.0, 0.0)
    if entry["layer"] not in _PDK_RES_FLAVOR_LAYERS[family].get(flavor, ()):
        return (0.0, 0.0)
    return (entry["contact_space_um"], entry["side_extension_um"])


#: PDK families whose poly resistors follow the ``abutting_heads`` topology
#: (issue #2679): ``polyres`` is the whole resistive body and the poly layer
#: is only the two abutting contact heads. Must match the families whose
#: extraction-deck poly ``ResistorDevice`` entries declare
#: ``topology="abutting_heads"`` (asserted in ``tests/test_sg13g2_deck.py``).
_PDK_RES_POLY_HEADS_ONLY: frozenset[str] = frozenset({"sg13g2"})


def _res_end_contact_um(
    family: str, flavor: str, metal_level: int = 0
) -> tuple[float, float]:
    """``(w_um, h_um)`` of ``res_array``'s rectangular precision-poly end
    contact for this request (see :data:`_PDK_RES_FLAVOR_END_CONTACT_UM`), or
    ``(0.0, 0.0)`` when the ordinary square cut applies -- always for
    ``metal_level != 0`` (metal resistors ignore ``flavor``)."""
    if metal_level:
        return (0.0, 0.0)
    return _PDK_RES_FLAVOR_END_CONTACT_UM.get(family, {}).get(flavor, (0.0, 0.0))


def _res_flavor_min_width_um(family: str, flavor: str) -> float:
    """Return the minimum ``res_array`` ``params.width_um`` accepted for
    ``flavor`` in ``family`` (see :data:`_PDK_RES_FLAVOR_MIN_W_UM`), falling
    back to :data:`UNIT_MIN_W_UM` for any family/flavour this table does not
    override."""
    return _PDK_RES_FLAVOR_MIN_W_UM.get(family, {}).get(flavor, UNIT_MIN_W_UM)


def _res_flavor_min_width_um_floor(flavor: str) -> float:
    """The loosest (smallest) :data:`_PDK_RES_FLAVOR_MIN_W_UM` floor declared
    for ``flavor`` across *every* family that names it, or
    :data:`UNIT_MIN_W_UM` if no family overrides this flavour.

    Used by :func:`~klayout_tools.gen_describe._res_array_validate`, which
    checks ``params.width_um`` before the request's ``--pdk`` family is
    resolved (the same "PDK-agnostic" constraint
    :func:`~klayout_tools.gen_describe._guard_ring_validate` documents for
    itself) -- it cannot look up one exact family's floor yet. Taking the
    loosest floor across every family that defines this flavour name keeps
    this early check a *necessary*, not *sufficient*, condition: an
    obviously-too-narrow request is still rejected immediately, before any
    PDK resolution or layout work happens, while the exact, family-specific
    floor is enforced once the family *is* known, in
    :func:`_resistor_layer_params` (via :func:`_res_flavor_min_width_um`)."""
    candidates = [
        table[flavor] for table in _PDK_RES_FLAVOR_MIN_W_UM.values() if flavor in table
    ]
    return min(candidates) if candidates else UNIT_MIN_W_UM


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


#: Per-PDK-family **fixed-size** cut/via layers (issue #2585): a layer whose
#: upstream foundry rule pins the drawn cut to one exact side -- a minimum
#: *and* a maximum -- keyed by its ``(layer, datatype)`` pair, valued in um.
#:
#: Every ``klt gen`` generator sizes its cuts off the PDK-generic
#: :data:`~klayout_tools.gen.CONTACT_SIZE_UM` (0.22um), widened per family to
#: a *minimum* where one binds (``via_min_w_um``/``cap_top_via_min_w_um``/
#: ``klt gen compose``'s deck-derived via-drop floor). Widening to a minimum is
#: only safe while every cut rule is a minimum. The layers below are not:
#: 0.22um is over the upstream maximum, so drawing it is a real DRC violation
#: whether or not this repo's curated deck can yet see it. Drawn cuts on these
#: layers are clamped *down* to the fixed size (see
#: :func:`_cut_fixed_size_um`, :func:`~klayout_tools.gen._clamp_cut_boxes` and
#: :func:`~klayout_tools.gen._fixed_cut_side_um`).
#:
#: This table only carries the fixed-size rules the curated decks do **not**
#: yet encode as ``DrcRule.threshold_max_dbu`` (issue #2370) -- the rules
#: issue #2388 still defers (sky130 ``licon1``/``mcon``).
#: :func:`_cut_fixed_size_um` reads a deck-declared ``threshold_max_dbu``
#: directly, so gf180mcu's ``contact``/``via1``-``via4`` (and any rule a later
#: backfill adds) need no entry here; ``tests/test_gen.py`` asserts that any
#: layer present in *both* sources agrees, so an entry can be deleted once its
#: deck rule carries the bound.
#:
#: - ``sky130`` ``licon1`` (66/44) 0.17um -- ``sky130A_mr.drc`` ``licon.1``
#:   ("min/max. licon length : 0.17um",
#:   ``licon.not(prec_resistor).drc(length != 0.17)``), identical to
#:   ``sky130.lydrc``'s own ``licon.1``
#:   (``licon.not(...).edges.without_length(0.17)``, "minimum/maximum width of
#:   licon : 0.17um"); this deck's ``licon1.width.1`` carries the minimum half
#:   (issue #2594). The one upstream exemption is a licon on a **precision
#:   poly resistor** (``prec_resistor = (rpm | urpm) & psdm &
#:   poly.interacting(poly_rs)``), whose cuts are instead held to 0.19um/2.0um
#:   edges by ``licon.1b/c``. ``res_array``'s ``flavor="high"``/``"xhigh"``
#:   are the only requests that draw ``rpm``/``urpm`` at all
#:   (:data:`_PDK_RES_FLAVOR_LAYERS`), and their end contacts were already
#:   drawn at neither exempt size (0.22um) before this entry, so clamping to
#:   0.17um neither introduces nor fixes that case -- it is a context-
#:   dependent size this layer-keyed table structurally cannot express;
#:   issue #2449 resolves it per request via
#:   :data:`_PDK_RES_FLAVOR_END_CONTACT_UM` (and zeroes this clamp for those
#:   requests in :func:`_resistor_layer_params`).
#: - ``sky130`` ``mcon`` (67/44) 0.17um -- ``sky130.lydrc`` ``ct.1``
#:   (``mcon.edges.without_length(0.17)``, "minimum/maximum width of mcon :
#:   0.17um"), split in ``sky130A_mr.drc`` into ``ct.1_a`` ("minimum width of
#:   mcon : 0.17um") and ``ct.1_b`` ("maximum length of mcon : 0.17um"); this
#:   deck's ``mcon.width.1`` carries the minimum half (issue #2594).
#:   ``sky130A_mr.drc`` narrows its halves to mcon outside ``areaid:ce`` and
#:   handles ring-shaped mcon separately (``ct.3``/``ct.3_a``); ``sky130.lydrc``
#:   applies the same 0.17um unconditionally, and no generator draws an
#:   ``areaid`` layer or a ring-shaped cut, so the clamp is unconditional here.
#: - sky130 ``via`` (68/44) 0.15um and sg13g2 ``Cont``/``Via1``-``Via4``
#:   (``Cnt.a``, ``V1.a``-``V4.a``) used to be listed here; issue #2388's
#:   backfill moved them into the deck as ``threshold_max_dbu``, which
#:   :func:`_cut_fixed_size_um` reads directly.
_PDK_CUT_FIXED_SIZE_UM: dict[str, dict[tuple[int, int], float]] = {
    "sky130": {
        (66, 44): 0.17,  # licon1.drawing -- licon.1 (issue #2594)
        (67, 44): 0.17,  # mcon.drawing -- ct.1 / ct.1_a+ct.1_b (issue #2594)
    },
}


def _deck_cut_max_size_um(family: str, layer: tuple[int, int]) -> float | None:
    """The tightest ``DrcRule.threshold_max_dbu`` (issue #2370) any plain
    ``"width"`` rule in ``family``'s curated deck declares for ``layer``, in
    um -- or ``None`` when the deck declares no upper size bound there (or
    ``family`` has no registered deck at all)."""
    from .decks import UnknownDeckError, get_deck, get_nominal_dbu

    try:
        rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except UnknownDeckError:
        return None
    bounds = [
        rule.threshold_max_dbu * nominal_dbu_um
        for rule in rules
        if rule.check == "width"
        and rule.layer == layer
        and rule.other_layer is None
        and rule.derived_layer is None
        and rule.threshold_max_dbu is not None
    ]
    return round(min(bounds), 6) if bounds else None


def _deck_min_width_um(family: str, layer: tuple[int, int]) -> tuple[str, float] | None:
    """``(rule id, um)`` of the plain ``"width"`` rule in ``family``'s curated
    deck with the largest minimum on ``layer`` (issue #4203), or ``None`` when
    the deck declares none (or ``family`` has no registered deck)."""
    from .decks import UnknownDeckError, get_deck, get_nominal_dbu

    try:
        rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except UnknownDeckError:
        return None
    mins = [
        (rule.id, round(rule.threshold_dbu * nominal_dbu_um, 6))
        for rule in rules
        if rule.check == "width"
        and rule.layer == layer
        and rule.other_layer is None
        and rule.derived_layer is None
        and rule.threshold_dbu > 0
    ]
    return max(mins, key=lambda m: m[1]) if mins else None


def _cut_fixed_size_um(family: str, layer: Any) -> float:
    """The fixed drawn side (um) a cut on ``layer`` must be clamped to on
    ``family`` (issue #2585), or ``0.0`` -- "no upper bound, draw the
    generator's own size unchanged" -- when neither the curated deck
    (``threshold_max_dbu``) nor :data:`_PDK_CUT_FIXED_SIZE_UM` declares one.

    ``layer`` is a ``(layer, datatype)`` pair or a ``kdb.LayerInfo`` (the form
    every ``*_layer_params`` resolver below already holds). When both sources
    declare a bound the tighter one wins; a test keeps them equal."""
    if layer is None:
        return 0.0
    pair = (
        (layer.layer, layer.datatype)
        if hasattr(layer, "datatype")
        else (int(layer[0]), int(layer[1]))
    )
    bounds = [
        bound
        for bound in (
            _deck_cut_max_size_um(family, pair),
            _PDK_CUT_FIXED_SIZE_UM.get(family, {}).get(pair),
        )
        if bound is not None
    ]
    return min(bounds) if bounds else 0.0


def mfg_grid_um(family: str, layer: Any) -> float:
    """The manufacturing grid (um) ``family``'s curated deck requires vertices
    on ``layer`` to sit on (issue #2648), or ``0.0`` when the deck declares no
    ``"ongrid"`` rule there (or ``family`` has no registered deck) -- "draw the
    generator's own coordinates unchanged".

    Read from the deck's own ``check="ongrid"`` rule (issue #2642) rather than
    restated here, so the generator and ``klt drc`` cannot disagree. ``layer``
    is a ``(layer, datatype)`` pair or a ``kdb.LayerInfo``; the coarsest
    declared grid wins."""
    if layer is None:
        return 0.0
    from .decks import UnknownDeckError, get_deck

    pair = (
        (layer.layer, layer.datatype)
        if hasattr(layer, "datatype")
        else (int(layer[0]), int(layer[1]))
    )
    try:
        rules = get_deck(family)
    except UnknownDeckError:
        return 0.0
    grids = [
        rule.grid_um
        for rule in rules
        if rule.check == "ongrid" and rule.layer == pair and rule.grid_um
    ]
    return max(grids) if grids else 0.0


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
    :func:`klayout_tools.pdk_families.pdk_variant_family` -- the package's
    single authoritative classifier (issue #2026; this call site, added by
    issue #1448, is the pattern the rest of the package was refactored onto)
    -- rather than this module's own, narrower prefix-only scan it replaces.
    This is a deliberate reuse, not a layering violation:
    ``pdk_families.py`` is a leaf module with no imports beyond the stdlib,
    so there is no import cycle. The reuse matters concretely for
    ``"ihp-sg13g2"`` -- IHP-Open-PDK's own install-directory name, and the
    exact ``variant`` string ``klt pdk find`` reports for a real fetched
    install (see ``tests/test_pdk.py``'s flat-layout tests) -- which is
    *not* a literal prefix of this module's ``"sg13g2"`` family key the way
    ``"sky130A"``/``"gf180mcuC"`` are literal prefixes of ``"sky130"``/
    ``"gf180mcu"``; a bare prefix scan here would never match it.
    :data:`klayout_tools.pdk_families.PDK_VARIANT_FAMILY_ALIASES` is what
    resolves that exact mismatch.

    Raises :class:`GenError` for any other family -- the PDK-aware
    generators draw PDK-specific layers (unlike ``resistor_strip``, which is
    PDK-agnostic and never calls this).
    """
    from .gen import GenError

    family = pdk_variant_family(variant)
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
#: sky130's single supported flavour, ``"hvi"`` (issue #1912, unblocked by
#: #1369's own citation), reuses ``klayout_tools.decks.sky130``'s curated
#: *extraction* deck's ``EXTRACTION_DECK.mos_flavours``
#: ``MOSFlavour(marker=(75, 20), flavour="hvi", ...)`` entry -- the same
#: ``hvi.drawing`` (75/20) layer that module's own docstring cross-checks
#: against three independent sources in a fetched install (``sky130.lvs``,
#: ``sky130A.lyp``, ``sky130A.tech``), and the same layer
#: ``pdk_models.py``'s ``_MOS_MODEL_FLAVOURS[("sky130", "sky130")]["hvi"]``
#: binds to the real ``sky130_fd_pr__nfet_g5v0d10v5``/``__pfet_g5v0d10v5``
#: subcircuits -- so requesting ``voltage_flavor="hvi"`` here draws the
#: identical marker the deck already extracts a device-class split on,
#: mirroring gf180mcu's/sg13g2's own "reuse the deck's own marker citation"
#: precedent above rather than inventing a new one. Before #1912, sky130 had
#: no entry here at all: this deck's own transcription cited no numbered
#: medium/high-voltage transistor marker layer (the same gap
#: :data:`_PDK_ROLE_LAYERS`'s ``esd_mark`` role still documents -- sky130's
#: ``hvtr``/``hvtp`` *implant* names remain cited only as unnumbered
#: exclusions in the official ``sky130.lvs``, never with a transcribed
#: layer/datatype pair this module could cite without guessing; ``hvi`` is a
#: distinct, numbered *identification* layer #1369 separately transcribed and
#: verified, not one of those two names). A ``voltage_flavor`` request naming
#: anything other than ``"hvi"`` still resolves to no layer on this family --
#: reported via ``drc_hints.notes``, never silently dropped (see
#: :func:`_voltage_flavor_mark_layer`). Needs no
#: :data:`_PDK_VOLTAGE_FLAVOR_MARK_MARGIN_UM` entry of its own: like
#: sg13g2/sg13cmos5l's ``"hv"`` marker, no rule in this deck's own curated DRC
#: deck reads ``hvi`` at all (`decks/sky130.py`'s own ``hvi`` layer-name
#: comment: "no DRC rule here reads hvi, so every rule still applies its
#: general-case threshold to geometry drawn inside hvi"), so the default
#: :data:`WELL_ENCLOSURE_MARGIN_UM` box never needs the gf180mcu-only
#: enlargement.
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
    "sky130": {
        "hvi": (75, 20),  # hvi.drawing -- same citation as
        # decks.sky130.EXTRACTION_DECK.mos_flavours' MOSFlavour(flavour="hvi").
        # Native-Vt NMOS (issue #3600): hvi AND lvtn. This is the primary
        # (hvi) half; the lvtn half lives in
        # :data:`_PDK_VOLTAGE_FLAVOR_EXTRA_LAYERS`.
        "native": (75, 20),
        "nvt": (75, 20),
    },
    "sg13g2": {
        "hv": (44, 0),  # ThickGateOx.drawing -- same citation as
        # decks.sg13g2.EXTRACTION_DECK.mos_flavours' MOSFlavour(flavour="hv").
    },
    "sg13cmos5l": {
        "hv": (44, 0),  # ThickGateOx.drawing -- same citation as
        # decks.sg13cmos5l.EXTRACTION_DECK.mos_flavours' MOSFlavour(flavour="hv").
    },
}


#: Second marker layer a ``voltage_flavor`` needs drawn *in addition to* its
#: :data:`_PDK_VOLTAGE_FLAVOR_LAYERS` entry (issue #3600). sky130's native-Vt
#: NMOS (``sky130_fd_pr__nfet_05v0_nvt``) is hvi AND lvtn
#: (``decks.sky130.EXTRACTION_DECK.mos_flavours``' ``MOSFlavour(flavour="nvt",
#: and_markers=((125, 44),))``), so the hvi box alone would extract as
#: ``nfet_g5v0d10v5``. ``areaid_le`` (81/60), which upstream subtracts for the
#: 5V native device, is simply never drawn.
_PDK_VOLTAGE_FLAVOR_EXTRA_LAYERS: dict[str, dict[str, tuple[int, int]]] = {
    "sky130": {
        "native": (125, 44),  # lvtn.drawing
        "nvt": (125, 44),
    },
}

#: ``(family, voltage_flavor)`` pairs with no PMOS counterpart: requesting one
#: with ``flavor="pfet"`` is a hard ``GenError`` (issue #3600).
_NFET_ONLY_VOLTAGE_FLAVORS = frozenset({("sky130", "native"), ("sky130", "nvt")})


def _voltage_flavor_extra_mark_layer(family: str, voltage_flavor: str) -> Any:
    """The ``kdb.LayerInfo`` of ``voltage_flavor``'s second marker layer in
    :data:`_PDK_VOLTAGE_FLAVOR_EXTRA_LAYERS`, or ``None`` when it needs none."""
    import klayout.db as kdb

    pair = _PDK_VOLTAGE_FLAVOR_EXTRA_LAYERS.get(family, {}).get(voltage_flavor)
    return kdb.LayerInfo(*pair) if pair is not None else None


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


#: Per-PDK-family MOS-capacitor marker layer drawn over a ``mos_array`` built
#: with ``params.mos_cap`` (issue #3283): the layer that lets a deck recognise
#: the gate-over-diffusion structure as a capacitor rather than a transistor.
#: gf180mcu's is ``MOS_CAP_MK`` (166/5), the same ``*_MK`` marker family as the
#: ``RES_MK`` (110/5), ``diode_mk`` (115/5), ``DRC_BJT`` (127/5) and
#: ``efuse_mk`` (80/5) layers this repo's gf180mcu deck already cites (the
#: deck's own ``res_exclude`` note lists ``mos_cap_mk`` by name). The 166/5
#: number matches the official gf180mcu DRC deck's own
#: ``mos_cap_mk = get_polygons(166, 5)``. A family with no entry
#: is rejected with a ``GenError`` rather than drawing an unmarked cap.
_PDK_MOS_CAP_MARK_LAYERS: dict[str, tuple[int, int]] = {
    "gf180mcu": (166, 5),  # MOS_CAP_MK
}


def _mos_cap_min_width_um(
    family: str, voltage_flavor_mark: Any
) -> tuple[float, str] | None:
    """``(min COMP width in um, deck rule id)`` that applies to a MOS
    capacitor's diffusion in ``family``'s curated deck, or ``None`` when the
    deck has no such rule. ``voltage_flavor_mark`` is the resolved
    ``voltage_flavor`` marker ``kdb.LayerInfo`` (``None`` for the default
    thin-oxide device): with one, the voltage-scoped rule that overlaps that
    marker is selected; without, the rule that excludes it (or the plain
    undifferentiated width rule on a family with no oxide split)."""
    from .decks import UnknownDeckError, get_deck, get_nominal_dbu

    active = _PDK_ROLE_LAYERS[family]["active"]
    try:
        rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except UnknownDeckError:
        return None
    mark = (
        (voltage_flavor_mark.layer, voltage_flavor_mark.datatype)
        if voltage_flavor_mark is not None
        else None
    )
    candidates = []
    for rule in rules:
        if rule.check != "width" or rule.layer != active or rule.other_layer:
            continue
        derived = rule.derived_layer
        if derived is None:
            candidates.append(rule)
        elif mark is not None and (
            derived.intersect_with == mark and derived.mode == "overlapping"
        ):
            candidates.append(rule)
        elif derived.mode == "not_interacting" and mark is None:
            candidates.append(rule)
    if not candidates:
        return None
    rule = max(candidates, key=lambda r: r.threshold_dbu)
    return round(rule.threshold_dbu * nominal_dbu_um, 6), rule.id


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

    Also resolves ``dummy_layer``/``dummy_present`` (issue #491, extended to
    gf180mcu by #2599) -- the optional PDK dummy-device marker (see
    :data:`_PDK_ROLE_LAYERS`'s ``"dummy"`` role and
    ``klayout_tools.decks.sky130``/``klayout_tools.decks.gf180mcu``'s
    ``EXTRACTION_DECK.dummy``) that ``mos_array`` draws over its
    ``dummy_cells``' gate footprint so ``klt extract``'s existing
    dummy-suppression guards (#295/#462) actually fire. ``None`` (a family
    whose curated deck declares no marker, or ``diff_pair``, which never
    populates ``dummy_cells``) is a silent no-op, matching every other
    ``*_present``-gated role here.

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
    voltage_flavor_extra_mark = (
        _voltage_flavor_extra_mark_layer(family, voltage_flavor)
        if voltage_flavor
        else None
    )
    if (family, voltage_flavor) in _NFET_ONLY_VOLTAGE_FLAVORS and (
        params.get("flavor", "nfet") != "nfet"
    ):
        from .gen import GenError

        raise GenError(
            f"params.voltage_flavor '{voltage_flavor}' is an NMOS-only device "
            f"on PDK family '{family}' (no PMOS counterpart); use "
            "flavor='nfet'"
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
    npc = _role_layer_info(family, "npc")
    npc_margin = _PDK_NPC_MARGIN_UM.get(family, 0.0)
    sd_implant_margin = _sd_implant_margin_um(family)
    # MOS capacitor (issue #3283): the marker layer must resolve and `w_um`
    # must clear the deck's own COMP min-width rule for the selected oxide,
    # else the request fails here rather than drawing an unmarked or
    # sub-rule capacitor.
    mos_cap_mark = None
    if params.get("mos_cap"):
        from .gen import GenError

        pair = _PDK_MOS_CAP_MARK_LAYERS.get(family)
        if pair is None:
            raise GenError(
                f"generator 'mos_array': params.mos_cap is not supported on "
                f"PDK family '{family}' (no MOS-capacitor marker layer is "
                "declared for it)"
            )
        mos_cap_mark = kdb.LayerInfo(*pair)
        floor = _mos_cap_min_width_um(family, voltage_flavor_mark)
        if floor is not None and params["w_um"] < floor[0]:
            raise GenError(
                f"generator 'mos_array': params.w_um must be >= {floor[0]} for "
                f"a MOS capacitor on '{family}' (deck rule '{floor[1]}', the "
                "minimum COMP width for the selected oxide)"
            )
    layer_params = {
        "active_layer": _role_layer_info(family, "active"),
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "mfg_grid_um": mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("contact")),
        "contact_fixed_size_um": _cut_fixed_size_um(
            family, _PDK_ROLE_LAYERS[family].get("contact")
        ),
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
        "voltage_flavor_extra_mark_layer": (
            voltage_flavor_extra_mark
            if voltage_flavor_extra_mark is not None
            else kdb.LayerInfo(0, 0)
        ),
        "voltage_flavor_extra_mark_present": voltage_flavor_extra_mark is not None,
        "voltage_flavor_mark_margin_um": _voltage_flavor_mark_margin_um(family),
        "gate_pad_clearance_um": _gate_pad_clearance_um(family),
        "contact_gate_offset_um": _contact_gate_extra_offset_um(family),
        "bottom_endcap_um": _gate_bottom_endcap_um(family),
        "top_endcap_um": _gate_top_endcap_um(family),
        "sd_implant_layer": (
            sd_implant if sd_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "sd_implant_present": sd_implant is not None and sd_implant_margin > 0,
        "sd_implant_margin_um": sd_implant_margin if sd_implant is not None else 0.0,
        "npc_layer": npc if npc is not None else kdb.LayerInfo(0, 0),
        "npc_present": npc is not None and npc_margin > 0,
        "npc_margin_um": npc_margin if npc is not None else 0.0,
        "npc_min_space_um": _PDK_NPC_MIN_SPACE_UM.get(family, 0.0),
        "sd_implant_min_space_um": _sd_implant_min_space_um(family),
    }
    if mos_cap_mark is not None:
        # Only `mos_array` declares these PCell params (`diff_pair` shares this
        # resolver), so they are added only for a `mos_cap` request.
        layer_params["mos_cap_mark_layer"] = mos_cap_mark
    return layer_params


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
            "mfg_grid_um": mfg_grid_um(family, levels["via"]),
            "contact_fixed_size_um": _cut_fixed_size_um(family, levels["via"]),
            "metal_layer": kdb.LayerInfo(*levels["landing"]),
            "res_mark_layer": kdb.LayerInfo(*levels["marker"]),
            "res_mark_present": True,
            **dummy_params,
        }
        for i in range(_MAX_RES_FLAVOR_LAYERS):
            resolved[f"res_flavor_{i}_layer"] = kdb.LayerInfo(0, 0)
            resolved[f"res_flavor_{i}_present"] = False
            resolved[f"res_flavor_{i}_head_enclosure_um"] = 0.0
            resolved[f"res_flavor_{i}_side_extension_um"] = 0.0
        floors = _metal_res_geometry_min_um(family, metal_level)
        resolved["metal_res_via_min_w_um"] = floors["via_min_w_um"]
        resolved["metal_res_via_enclosure_min_um"] = floors["via_enclosure_min_um"]
        resolved["metal_res_via_space_min_um"] = floors["via_space_min_um"]
        return resolved

    flavor = params.get("flavor", _DEFAULT_RES_FLAVOR)
    flavor_layers = _res_flavor_layers(family, flavor)
    min_width_um = _res_flavor_min_width_um(family, flavor)
    if params["width_um"] < min_width_um:
        from .gen import GenError

        raise GenError(
            f"generator 'res_array': params.width_um must be >= {min_width_um} "
            f"for flavor '{flavor}' on PDK family '{family}'"
        )
    mark = _role_layer_info(family, "res_mark")
    # Issue #2449: a precision-poly flavour draws its own rectangular cut, so
    # the layer-keyed ordinary fixed-size clamp (0.17um) must not shrink it.
    end_w_um, end_h_um = _res_end_contact_um(family, flavor)
    sab_contact_space_um, sab_side_ext_um = _res_sab_geometry_um(family, flavor)
    resolved = {
        "poly_layer": _role_layer_info(family, "poly"),
        "contact_layer": _role_layer_info(family, "contact"),
        "mfg_grid_um": mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("contact")),
        "contact_fixed_size_um": (
            0.0
            if end_w_um > 0.0
            else _cut_fixed_size_um(family, _PDK_ROLE_LAYERS[family].get("contact"))
        ),
        "end_contact_w_um": end_w_um,
        "end_contact_h_um": end_h_um,
        "metal_layer": _role_layer_info(family, "metal"),
        "res_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "res_mark_present": mark is not None,
        "res_poly_heads_only": family in _PDK_RES_POLY_HEADS_ONLY,
        **dummy_params,
    }
    for i in range(_MAX_RES_FLAVOR_LAYERS):
        pair = flavor_layers[i] if i < len(flavor_layers) else None
        resolved[f"res_flavor_{i}_layer"] = (
            kdb.LayerInfo(*pair) if pair is not None else kdb.LayerInfo(0, 0)
        )
        resolved[f"res_flavor_{i}_present"] = pair is not None
        # Issue #2449: precision-resistor masks cover the end heads too.
        resolved[f"res_flavor_{i}_head_enclosure_um"] = (
            _PDK_RES_FLAVOR_MASK_ENCLOSURE_UM.get(family, {}).get(pair, 0.0)
            if pair is not None and end_w_um > 0.0
            else 0.0
        )
        # Issue #3000: the SAB runs past the poly body's long edges.
        resolved[f"res_flavor_{i}_side_extension_um"] = (
            sab_side_ext_um
            if pair is not None and pair == _PDK_RES_SAB_UM.get(family, {}).get("layer")
            else 0.0
        )
    resolved["res_contact_space_um"] = sab_contact_space_um
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
        "cap_top_via_fixed_size_um": _cut_fixed_size_um(family, top_via),
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
    if family in _DISTINCT_TAP_IMPLANT_FAMILIES:
        # n-tap inside the well -> nsdm; p-tap tying the substrate -> psdm.
        return _role_layer_info(family, "nplus" if well_tie else "pplus")
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
        "mfg_grid_um": mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("contact")),
        "contact_fixed_size_um": _cut_fixed_size_um(
            family, _PDK_ROLE_LAYERS[family].get("contact")
        ),
        "metal_layer": _role_layer_info(family, "metal"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        "ring_well_margin_um": _ring_well_margin_um(family),
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
        # How far that implant extends past the ring's own `Comp` band on
        # every edge (issue #2369, NP.5b/PP.5b) -- `0.0` for a family that
        # draws no ring implant at all, which keeps its geometry
        # byte-for-byte unchanged (see `_ring_implant_margin_um`).
        "ring_implant_margin_um": (
            _ring_implant_margin_um(family) if ring_implant is not None else 0.0
        ),
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
    # Issue #3648: a family with a tap-distance rule gets tap pads even
    # without a ring, which need the ring role's tap/implant layers.
    if params.get("add_guard_ring") or _tap_pads_wanted(
        _pdk_family(pdk_info["variant"])
    ):
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
    ``Comp`` shape -- drawn on ``tap_layer`` since issue #2312 (it used to be
    drawn on ``active_layer`` directly), so the ring is the same ``tap``-role
    ring ``guard_ring``/``mos_array``/``diff_pair``/``esd_device`` already
    draw, and therefore a substrate tie an extraction deck actually
    recognises; see ``_BjtArrayPCell.produce_impl``.
    Always resolved with ``well_tie=False``: the collector ring is composed
    *outside* the shared base well's own footprint (``well_box_um`` plus
    ``BJT_COLLECTOR_GAP_UM``, see :func:`_bjt_array_layout`'s docstring), so
    it always ties the p-type substrate directly -- the real, physical
    collector terminal of a vertical PNP (P+ emitter / N+ base tie inside an
    Nwell / P+ collector = substrate, per ``docs/design/gen-bjt-array-spike.md``),
    never a well tie. The emitter/base-tie unit body is covered separately
    (issue #3587, once ``comp.coverage.1`` / ``DF.12`` started flagging bare
    ``Comp``): a P+ implant over each emitter pad and an N+ implant over each
    base-tie pad, resolved as ``unit_emitter_implant_layer``,
    ``unit_base_implant_layer`` and ``unit_implant_margin_um`` (``0.0`` = none
    drawn).
    """
    import klayout.db as kdb

    family = _pdk_family(pdk_info["variant"])
    _reject_deferred_family("bjt_array", family)
    well = _role_layer_info(family, "well")
    mark = _role_layer_info(family, "bjt_mark")
    dummy = _role_layer_info(family, "dummy")
    ring_implant = _ring_tap_implant_layer(family, well_tie=False)
    emitter_implant = _role_layer_info(family, "pplus")
    base_implant = _role_layer_info(family, "nplus")
    unit_implant_margin = _bjt_unit_implant_margin_um(family)
    return {
        "active_layer": _role_layer_info(family, "active"),
        "contact_layer": _role_layer_info(family, "contact"),
        "mfg_grid_um": mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("contact")),
        "contact_fixed_size_um": _cut_fixed_size_um(
            family, _PDK_ROLE_LAYERS[family].get("contact")
        ),
        "metal_layer": _role_layer_info(family, "metal"),
        "tap_layer": _role_layer_info(family, "tap"),
        "well_layer": well if well is not None else kdb.LayerInfo(0, 0),
        "well_present": well is not None,
        # Per-unit emitter (P+) / base-tie (N+) implants -- the unit-body
        # counterpart of the ring implant below, closing `DF.12` for the
        # unit's own `Comp` (issue #3587). Uses the ring's minimal NP.5b/PP.5b
        # margin, not `sd_implant_margin_um`: the wider S/D margin would push
        # each implant within NP.3/PP.3 range of the facing opposite-type pad.
        "unit_emitter_implant_layer": (
            emitter_implant if emitter_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "unit_base_implant_layer": (
            base_implant if base_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "unit_implant_margin_um": unit_implant_margin,
        "bjt_mark_layer": mark if mark is not None else kdb.LayerInfo(0, 0),
        "bjt_mark_present": mark is not None,
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
        # How far that implant extends past the ring's own `Comp` band on
        # every edge (issue #2369, NP.5b/PP.5b) -- `0.0` for a family that
        # draws no ring implant at all, which keeps its geometry
        # byte-for-byte unchanged (see `_ring_implant_margin_um`).
        "ring_implant_margin_um": (
            _ring_implant_margin_um(family) if ring_implant is not None else 0.0
        ),
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
        "mfg_grid_um": mfg_grid_um(family, _PDK_ROLE_LAYERS[family].get("contact")),
        "contact_fixed_size_um": _cut_fixed_size_um(
            family, _PDK_ROLE_LAYERS[family].get("contact")
        ),
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
        "top_endcap_um": _gate_top_endcap_um(family),
        "sd_implant_layer": (
            sd_implant if sd_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "sd_implant_present": sd_implant is not None and sd_implant_margin > 0,
        "sd_implant_margin_um": sd_implant_margin if sd_implant is not None else 0.0,
        "ring_implant_layer": (
            ring_implant if ring_implant is not None else kdb.LayerInfo(0, 0)
        ),
        "ring_implant_present": ring_implant is not None,
        # How far that implant extends past the ring's own `Comp` band on
        # every edge (issue #2369, NP.5b/PP.5b) -- `0.0` for a family that
        # draws no ring implant at all, which keeps its geometry
        # byte-for-byte unchanged (see `_ring_implant_margin_um`).
        "ring_implant_margin_um": (
            _ring_implant_margin_um(family) if ring_implant is not None else 0.0
        ),
    }
