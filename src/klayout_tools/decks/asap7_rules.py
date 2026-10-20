"""Declared rule-id contract for the ASAP7 KLayout DRC deck (#2760, #2810, #2811).

The deck itself is a KLayout DRC-DSL script, :data:`DECK_PATH`
(``asap7.drc`` next to this module), run headless through
``klt drc <gds> --engine klayout --deck-file <DECK_PATH>``. Its rule ids are
the RDB ``<category>`` names it declares, which are the ASAP7 DRM's own rule
names verbatim (DRM section 1.2.3).

This module is the single declared list of those ids. ``docs/cli/drc.md``
("ASAP7") lists the same ids, and ``tests/test_drc_asap7_deck.py`` fails if
the script, this list or the per-rule fixtures drift apart, so renaming or
dropping a rule is a visible, breaking change.

Source: ASAP7 PDK Design Rule Manual, PDK Release 1p7
(``asap7_drm_201207a.pdf``, shipped by lambdapdk v0.2.17 under
``lambdapdk/asap7/base/docs/``; BSD-3-Clause, Arizona State University).
Every :class:`Asap7Rule` names the DRM table it was transcribed from; the
DRM is cited, not copied.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The DRC-DSL deck script this module describes.
DECK_PATH = Path(__file__).with_name("asap7.drc")

#: DRM document the rules are transcribed from.
DRM_SOURCE = "asap7_drm_201207a.pdf (ASAP7 DRM, PDK Release 1p7; lambdapdk v0.2.17)"


@dataclass(frozen=True)
class Asap7Rule:
    """One rule category the deck declares.

    ``table`` is the DRM table the threshold was transcribed from.
    ``approximation`` is empty for a rule checked as written, and otherwise
    says how the check differs from the DRM text.
    """

    id: str
    table: str
    approximation: str = ""


@dataclass(frozen=True)
class Asap7RuleGap:
    """A DRM rule in a covered table (3.1-3.19) that the deck does not
    check yet."""

    id: str
    table: str
    reason: str
    tracked_by: str


def _metal_family(layer: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.12: rules stated for M1 apply to M2 and M3 as well.
    t = "Table 3.12.1"
    return (
        Asap7Rule(f"{layer}.W.1", t),
        Asap7Rule(f"{layer}.S.1", t),
        Asap7Rule(f"{layer}.S.2", t),
        Asap7Rule(f"{layer}.S.3", t),
        Asap7Rule(f"{layer}.S.4", t),
        Asap7Rule(f"{layer}.S.5", t),
        Asap7Rule(
            f"{layer}.S.6",
            t,
            "corner-to-corner read as Euclidean spacing between edges whose "
            "projections onto each other overlap by less than one database unit",
        ),
        Asap7Rule(f"{layer}.A.1", t),
    )


def _via_family(layer: str, upper: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.13: rules stated for V1 (with M1 / M2) apply to V2 and V3.
    t = "Table 3.13.1"
    return (
        Asap7Rule(
            f"{layer}.W.1",
            t,
            f"stated along the {upper} length; checked in both directions (the "
            f"via is as wide as {upper} across the track per "
            f"{layer}.{upper}.AUX.2)",
        ),
        Asap7Rule(f"{layer}.AUX.1", t),
    )


_OFFSET_0 = (
    "uses the DRM's default metal-grid offset of 0 nm (note 1); a layout "
    "built with a non-zero offset is not supported"
)


def _sadp_metal_family(layer: str, t1: str, t2: str) -> tuple[Asap7Rule, ...]:
    # DRM sections 3.14 / 3.16: rules stated for M4 (M6) apply to M5 (M7),
    # perpendicular for the vertically routed layer.
    return (
        Asap7Rule(f"{layer}.W.1", t1),
        Asap7Rule(f"{layer}.W.2", t1),
        Asap7Rule(
            f"{layer}.W.3",
            t1,
            "even multiples are enumerated up to the W.2 maximum; wider shapes "
            f"are reported by {layer}.W.2 only",
        ),
        Asap7Rule(f"{layer}.W.5", t1),
        Asap7Rule(f"{layer}.S.1", t1),
        Asap7Rule(f"{layer}.S.2", t1),
        Asap7Rule(f"{layer}.AUX.1", t2, _OFFSET_0),
        Asap7Rule(
            f"{layer}.AUX.2",
            t2,
            "applied to rectangles exactly W.1 wide across the track; " + _OFFSET_0,
        ),
        Asap7Rule(f"{layer}.AUX.3", t2),
    )


def _lele_via_family(layer: str, t: str) -> tuple[Asap7Rule, ...]:
    # DRM sections 3.15 / 3.17: rules for V4 (V6) apply to V5 (V7).
    return (
        Asap7Rule(
            f"{layer}.S.1",
            t,
            "checked between any two vias whatever their nets: the same-net "
            f"(S.1) and different-net ({layer}.S.2) thresholds are equal, so "
            "different-net pairs are reported under S.1",
        ),
        Asap7Rule(
            f"{layer}.S.3",
            t,
            "corner-to-corner read as Euclidean spacing between edges whose "
            "projections onto each other overlap by less than one database unit",
        ),
        Asap7Rule(f"{layer}.AUX.1", t),
    )


def _two_sides(text: str) -> str:
    return (
        f"the DRM says '{text}'; read like 'at least two opposite sides' "
        "(Fig. 3.15.1(b)): either opposite pair may meet the minimum"
    )


def _top_metal_family(layer: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.18: rules stated for M8 apply to M9.
    t1, t2 = "Table 3.18.1", "Table 3.18.2"
    length = "a shape's length is the longer side of its bounding box"
    return (
        Asap7Rule(f"{layer}.W.1", t1),
        Asap7Rule(f"{layer}.W.2", t1, length),
        Asap7Rule(f"{layer}.W.3", t1, length),
        Asap7Rule(f"{layer}.W.4", t1, length),
        Asap7Rule(
            f"{layer}.W.5",
            t1,
            "the DRM prints the operator of this maximum as '>='; read as '<='",
        ),
        Asap7Rule(f"{layer}.S.1", t1),
        Asap7Rule(f"{layer}.S.2", t1),
        Asap7Rule(f"{layer}.S.3", t1),
        Asap7Rule(f"{layer}.A.1", t2),
        Asap7Rule(
            f"{layer}.L.1",
            t2,
            "feature length read as polygon edge length (no definition in the DRM)",
        ),
    )


def _top_via_family(layer: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.19: rules for V8 apply to V9.
    t = "Table 3.19.1"
    return (
        Asap7Rule(f"{layer}.W.1", t),
        Asap7Rule(f"{layer}.S.1", t),
        Asap7Rule(
            f"{layer}.S.2",
            t,
            "corner-to-corner read as Euclidean spacing between edges whose "
            "projections onto each other overlap by less than one database unit",
        ),
    )


def _select_family(layer: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.7: rules stated for NSELECT apply to PSELECT, SLVT, LVT
    # and SRAMVT as well, unless otherwise stated.
    t = "Table 3.7.1"
    # The DRM lists SRAM.NSELECT.ACTIVE.EN.3 / .4 only; the other layers' SRAM
    # enclosures are the same sentence's mirrors, so they get no separate id.
    mirror = (
        ""
        if layer == "NSELECT"
        else "mirror of SRAM.NSELECT.ACTIVE.EN.3 / .4 (section 3.7 applies the "
        "table to this layer as well; the DRM lists no separate id)"
    )
    return (
        Asap7Rule(f"{layer}.W.1", t),
        Asap7Rule(f"{layer}.W.2", t),
        Asap7Rule(f"{layer}.ACTIVE.EN.1", t),
        Asap7Rule(f"{layer}.ACTIVE.EN.2", t),
        Asap7Rule(f"SRAM.{layer}.ACTIVE.EN.3", t, mirror),
        Asap7Rule(f"SRAM.{layer}.ACTIVE.EN.4", t, mirror),
        Asap7Rule(f"{layer}.GATE.EX.1", t),
        Asap7Rule(f"{layer}.GATE.EX.2", t),
    )


_PITCH_NOTE = (
    "exact pitch checked edge-to-edge for minimum-width shapes: neighbours "
    "closer than two pitches must sit exactly one pitch apart; alignment of "
    "shapes two or more pitches apart is not checked"
)

#: The connectivity the "not on the same net" rules are checked against.
_NET_MODEL = (
    "'same net' is conductor connectivity through transistor source/drain "
    "ACTIVE, SDT, LISD, LIG, GATE (not cut), V0, M1-M9 and V1-V9 only: "
    "ACTIVE is split at the GATE, the substrate and WELL are not modelled, "
    "and a shape that reaches no other conductor is a net of its own"
)

#: Every rule category the deck declares, in deck order.
RULES: tuple[Asap7Rule, ...] = (
    Asap7Rule(
        "GEOMETRY.NONORTHOGONAL",
        "Table 3.1.1",
        "applied to every drawing layer the deck reads (FEOL, MOL, M1-M9, "
        "V0-V9); the PAD layer is read only by V9.PAD.EN.2 and V9.AUX.1",
    ),
    # WELL
    Asap7Rule("WELL.W.1", "Table 3.2.1"),
    Asap7Rule("WELL.W.2", "Table 3.2.1"),
    Asap7Rule("WELL.S.1", "Table 3.2.1"),
    Asap7Rule("WELL.S.2", "Table 3.2.1"),
    Asap7Rule("WELL.A.1A", "Table 3.2.1"),
    Asap7Rule("WELL.A.1B", "Table 3.2.1"),
    Asap7Rule("WELL.GATE.EX.1", "Table 3.2.1"),
    Asap7Rule("WELL.GATE.EX.2", "Table 3.2.1"),
    # FIN
    Asap7Rule("FIN.W.1", "Table 3.3.1"),
    Asap7Rule("FIN.W.2", "Table 3.3.1"),
    Asap7Rule("FIN.S.1", "Table 3.3.1", _PITCH_NOTE),
    Asap7Rule("FIN.AUX.1", "Table 3.3.1"),
    # GATE
    Asap7Rule("GATE.W.1", "Table 3.4.1"),
    Asap7Rule("GATE.W.2", "Table 3.4.1"),
    Asap7Rule("GATE.S.1", "Table 3.4.1", _PITCH_NOTE),
    Asap7Rule("GATE.S.2", "Table 3.4.1"),
    Asap7Rule(
        "GATE.S.3",
        "Table 3.4.1",
        "centre-to-centre distance checked as edge-to-edge spacing <= 34 nm, "
        "exact for 20 nm GATE (GATE.W.1)",
    ),
    Asap7Rule("GATE.AUX.1", "Table 3.4.1"),
    Asap7Rule("GATE.ACTIVE.AUX.3", "Table 3.4.1"),
    Asap7Rule("GATE.ACTIVE.EX.1", "Table 3.4.1"),
    Asap7Rule("GATE.ACTIVE.EX.2", "Table 3.4.1"),
    Asap7Rule("GATE.ACTIVE.S.4", "Table 3.4.1"),
    # ACTIVE
    Asap7Rule("ACTIVE.FIN.EX.1", "Table 3.5.1"),
    Asap7Rule("ACTIVE.W.1", "Table 3.5.1"),
    Asap7Rule("ACTIVE.W.2", "Table 3.5.1"),
    Asap7Rule("ACTIVE.W.3", "Table 3.5.1"),
    Asap7Rule("ACTIVE.S.1", "Table 3.5.1"),
    Asap7Rule(
        "ACTIVE.S.2A",
        "Table 3.5.1",
        _NET_MODEL + "; horizontal spacing only (Table 3.5.2 note 2 calls the vertical "
        "check an error); the gap is measured between ACTIVE polygons, between "
        "the source/drain regions of ACTIVE a GATE crosses (tap ACTIVE is not "
        "a source/drain region); SRAMDRC is not excluded, as note 1 requires",
    ),
    Asap7Rule("ACTIVE.S.2B", "Table 3.5.1"),
    Asap7Rule("ACTIVE.WELL.S.4", "Table 3.5.1"),
    Asap7Rule("ACTIVE.WELL.EN.1", "Table 3.5.1"),
    Asap7Rule("SRAM.ACTIVE.WELL.S.5", "Table 3.5.1"),
    Asap7Rule("SRAM.ACTIVE.WELL.EN.2", "Table 3.5.1"),
    Asap7Rule("ACTIVE.A.1A", "Table 3.5.2"),
    Asap7Rule("ACTIVE.A.1B", "Table 3.5.2"),
    Asap7Rule("SRAM.ACTIVE.A.2A", "Table 3.5.2"),
    Asap7Rule("SRAM.ACTIVE.A.2B", "Table 3.5.2"),
    Asap7Rule("ACTIVE.AUX.1", "Table 3.5.2"),
    Asap7Rule(
        "SRAM.ACTIVE.AUX.2",
        "Table 3.5.2",
        "'do not intersect' read as sharing no area with SRAMDRC; the DRM's "
        "'completely outside' and 'may not touch' are both covered by "
        "reporting ACTIVE that touches SRAMDRC without overlapping it",
    ),
    # GCUT
    Asap7Rule("GCUT.W.1", "Table 3.6.1"),
    Asap7Rule("GCUT.ACTIVE.S.1", "Table 3.6.1"),
    Asap7Rule("GCUT.GATE.EX.1", "Table 3.6.1"),
    Asap7Rule("GCUT.GATE.S.2", "Table 3.6.1"),
    Asap7Rule("GCUT.S.3", "Table 3.6.1"),
    Asap7Rule("GCUT.AUX.1", "Table 3.6.1"),
    Asap7Rule("GCUT.AUX.2", "Table 3.6.1"),
    Asap7Rule("GCUT.AUX.3", "Table 3.6.1"),
    # NSELECT / PSELECT / SLVT / LVT / SRAMVT
    *_select_family("NSELECT"),
    *_select_family("PSELECT"),
    *_select_family("SLVT"),
    *_select_family("LVT"),
    *_select_family("SRAMVT"),
    Asap7Rule("NSELECT.PSELECT.AUX.1", "Table 3.7.1"),
    Asap7Rule("VT.AUX.2", "Table 3.7.1"),
    # SDT
    Asap7Rule("SDT.W.1", "Table 3.8.1"),
    Asap7Rule("SDT.W.2", "Table 3.8.1"),
    Asap7Rule("SDT.W.3", "Table 3.8.1"),
    Asap7Rule("SDT.S.1", "Table 3.8.1"),
    Asap7Rule("SDT.GATE.S.2", "Table 3.8.1"),
    Asap7Rule("SDT.ACTIVE.OV.1", "Table 3.8.1"),
    Asap7Rule("SDT.LISD.OV.2", "Table 3.8.1"),
    Asap7Rule("SDT.GATE.AUX.1", "Table 3.8.1"),
    Asap7Rule(
        "SDT.ACTIVE.AUX.2",
        "Table 3.8.1",
        "'coincide' read as 'lies on and overlaps an ACTIVE horizontal edge', "
        "per Fig. 3.8.1(d), which accepts SDT overhanging ACTIVE horizontally",
    ),
    Asap7Rule("SDT.ACTIVE.AUX.3", "Table 3.8.1"),
    Asap7Rule("SDT.LISD.AUX.4", "Table 3.8.1"),
    Asap7Rule("SRAM.SDT.W.4", "Table 3.8.1"),
    Asap7Rule("SRAM.SDT.ACTIVE.OV.3", "Table 3.8.1"),
    Asap7Rule("SRAM.SDT.LISD.OV.4", "Table 3.8.1"),
    # LISD
    Asap7Rule("LISD.W.1", "Table 3.9.1"),
    Asap7Rule("LISD.S.1", "Table 3.9.1"),
    Asap7Rule("LISD.S.2", "Table 3.9.1"),
    Asap7Rule("LISD.S.3", "Table 3.9.1"),
    Asap7Rule("LISD.A.1", "Table 3.9.1"),
    Asap7Rule(
        "SRAM.LISD.S.4",
        "Table 3.9.1",
        "both polygons must interact with SRAMDRC (as the DRM words it); a pair "
        "with only one in scope is covered by neither this rule nor LISD.S.3, "
        "which takes polygons lying completely outside SRAMDRC",
    ),
    Asap7Rule("SRAM.LISD.AUX.1", "Table 3.9.1"),
    # LIG
    Asap7Rule("LIG.W.1", "Table 3.10.1"),
    Asap7Rule("LIG.S.1", "Table 3.10.1"),
    Asap7Rule("LIG.S.2", "Table 3.10.1"),
    Asap7Rule("LIG.S.3", "Table 3.10.1"),
    Asap7Rule("LIG.S.4", "Table 3.10.1"),
    Asap7Rule("LIG.S.5", "Table 3.10.1"),
    Asap7Rule(
        "LIG.LISD.S.6",
        "Table 3.10.1",
        _NET_MODEL + "; spacing is Euclidean, in any direction",
    ),
    Asap7Rule(
        "LIG.LISD.S.7",
        "Table 3.10.1",
        _NET_MODEL + "; corner-to-corner read as Euclidean spacing between edges whose "
        "projections onto each other overlap by less than one database unit",
    ),
    Asap7Rule(
        "LIG.SDT.S.8",
        "Table 3.10.1",
        _NET_MODEL + "; spacing is Euclidean, in any direction",
    ),
    Asap7Rule("LIG.GATE.S.9A", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.S.9B", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.S.10", "Table 3.10.2"),
    Asap7Rule("LIG.GCUT.S.11", "Table 3.10.2"),
    Asap7Rule("LIG.A.1", "Table 3.10.2"),
    Asap7Rule("LIG.LISD.A.2", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.A.3", "Table 3.10.2"),
    Asap7Rule("SRAM.LIG.GATE.A.4", "Table 3.10.2"),
    Asap7Rule(
        "LIG.LISD.OV.1",
        "Table 3.10.3",
        "any LIG/LISD overlap is treated as 'connected together'",
    ),
    Asap7Rule("LIG.GATE.AUX.1", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.EX.1", "Table 3.10.3"),
    Asap7Rule("SRAM.LIG.AUX.2", "Table 3.10.3"),
    Asap7Rule(
        "SRAM.LIG.GATE.OV.2",
        "Table 3.10.3",
        "the DRM gives no direction; read as the horizontal overlap across the "
        "gate width, as Fig. 3.10.2(c) draws it, measured on the LIG/GATE "
        "intersection",
    ),
    # V0 (width, connectivity, LIG overlap area)
    Asap7Rule(
        "V0.W.1",
        "Table 3.11.1",
        "stated along the M1 length; checked in both directions (V0 is as wide "
        "as M1 across the track per V0.M1.AUX.3)",
    ),
    Asap7Rule("V0.AUX.1", "Table 3.11.2"),
    Asap7Rule("V0.LIG.A.1", "Table 3.11.2"),
    # M1-M3
    *_metal_family("M1"),
    *_metal_family("M2"),
    *_metal_family("M3"),
    # V1-V3 (width and connectivity)
    *_via_family("V1", "M2"),
    *_via_family("V2", "M3"),
    *_via_family("V3", "M4"),
    Asap7Rule("V2.M2.EN.1", "Table 3.13.1"),
    Asap7Rule("V3.M3.EN.1", "Table 3.13.1", "stated by Table 3.13.1 note 2"),
    Asap7Rule("V3.M4.EN.2", "Table 3.13.1"),
    # M4-M7 (Tables 3.14.1-3.14.2, 3.16.1-3.16.2)
    *_sadp_metal_family("M4", "Table 3.14.1", "Table 3.14.2"),
    *_sadp_metal_family("M5", "Table 3.14.1", "Table 3.14.2"),
    *_sadp_metal_family("M6", "Table 3.16.1", "Table 3.16.2"),
    *_sadp_metal_family("M7", "Table 3.16.1", "Table 3.16.2"),
    # V4-V7 (Tables 3.15.1, 3.17.1)
    *_lele_via_family("V4", "Table 3.15.1"),
    *_lele_via_family("V5", "Table 3.15.1"),
    *_lele_via_family("V6", "Table 3.17.1"),
    *_lele_via_family("V7", "Table 3.17.1"),
    Asap7Rule("V4.W.1", "Table 3.15.1", "checked on the via's bounding box"),
    Asap7Rule("V5.W.1", "Table 3.15.1", "checked on the via's bounding box"),
    Asap7Rule(
        "V6.W.1",
        "Table 3.17.1",
        "the DRM says 'along the length of M5'; read as M7, the metal V6 "
        "lands on (V6.M7.AUX.2); checked on the via's bounding box",
    ),
    Asap7Rule("V4.M5.AUX.2", "Table 3.15.1"),
    Asap7Rule("V5.M6.AUX.2", "Table 3.15.1"),
    Asap7Rule("V6.M7.AUX.2", "Table 3.17.1"),
    Asap7Rule("V4.M4.EN.1", "Table 3.15.1", _two_sides("on two opposite sides")),
    Asap7Rule("V4.M5.EN.2", "Table 3.15.1"),
    Asap7Rule("V5.M5.EN.1", "Table 3.15.1", _two_sides("on two opposite sides")),
    Asap7Rule("V5.M6.EN.2", "Table 3.15.1"),
    Asap7Rule("V6.M6.EN.1", "Table 3.17.1"),
    Asap7Rule("V6.M7.EN.2", "Table 3.17.1", _two_sides("on two opposite sides")),
    Asap7Rule("V7.M7.EN.1", "Table 3.17.1"),
    Asap7Rule("V7.M8.EN.2", "Table 3.17.1", _two_sides("on two opposite sides")),
    # M8-M9 (Tables 3.18.1-3.18.2)
    *_top_metal_family("M8"),
    *_top_metal_family("M9"),
    # V8-V9 (Table 3.19.1)
    *_top_via_family("V8"),
    *_top_via_family("V9"),
    Asap7Rule("V8.M8.EN.1", "Table 3.19.1"),
    Asap7Rule("V8.M9.EN.2", "Table 3.19.1"),
    Asap7Rule("V9.M9.EN.1", "Table 3.19.1"),
    Asap7Rule("V9.PAD.EN.2", "Table 3.19.1"),
    Asap7Rule("V8.AUX.1", "Table 3.19.1"),
    Asap7Rule("V9.AUX.1", "Table 3.19.1"),
)

RULE_IDS: tuple[str, ...] = tuple(r.id for r in RULES)

_SRAM = (
    "SRAMDRC-marked variant; no SRAM cells ship with the pinned PDK and the DRM "
    "says SRAM rules may be waived for other cells"
)
_GATE_DISCONTINUOUS = (
    "no distance bound, but a topological condition (a GATE column split into "
    "separate polygons, Fig. 3.4.2(a)); the DRM does not say how far two GATE "
    "polygons may overlap sideways to share a column, so the predicate needs "
    "that reading ratified before it is enforced"
)
_ACTIVE_NOTCH = (
    "no distance bound, but a topological condition (a slot cut into a "
    "vertical ACTIVE edge, Fig. 3.5.4(b)); the DRM defines neither 'notch' "
    "nor its size, so the predicate needs that reading ratified before it is "
    "enforced"
)
_TRACK = (
    "classified by routing track (same / parallel / aligned tracks); the deck "
    "has no routing-direction or track assignment"
)
_END_CAP = (
    "corner-to-corner spacing conditioned on a 5 nm metal end-cap; needs "
    "per-via end-cap classification against the upper metal"
)
_EXACT_EN = (
    "exact (==) or per-side paired enclosure ('5 & 2', '(5 & 5) | (5 & 0)') "
    "needs routing direction and a per-side enclosure predicate"
)
_LISD_EXACT_3 = (
    "'== 3 nm on at least two opposite sides' is expressible without a routing "
    "direction, but the literal '==' contradicts the pinned shipped cells (100 "
    "V0 in 57 of 212 RVT cells are enclosed by LISD by more than 3 nm on every "
    "opposite pair, e.g. 4 / 2 nm across and 27 / 36 nm along); the cells "
    "satisfy only the 'at least 3 nm' reading, so the exact check is not shipped"
)
_EXACT_W = "exact width across the metal track needs the metal routing direction"
_SADP_TRACK = (
    "defined against the routing tracks (adjacent tracks, parallel run "
    "length); the deck has no per-shape track assignment, and the DRM leaves "
    "'adjacent' and the track pitch undefined (Fig. 3.14.2 shows only min-"
    "width tracks at 2 x W.1 pitch), so any reading would be a guess"
)
_SADP_TRACK_COUNT = (
    "'spans an even number of minimum-width tracks' is, for the AUX.1 "
    "grid the deck enforces, the same condition as W.3's even multiple of "
    "the minimum width, which the deck already reports; a separate check "
    "would add no violations at the default 0 nm offset"
)
_WIDE_EDGE = (
    "'wide' is not defined, and with the default 0 nm offset every multiple "
    "of the AUX.1 grid is a track edge, so the rule has no checkable reading"
)
_VIA_NETS = (
    "needs net connectivity to tell different-net pairs from same-net ones; "
    "the threshold equals S.1's, and the deck's net-independent S.1 check "
    "reports these pairs"
)
_M8_DIRECTION = (
    "stated along / across the M8 length, but the DRM gives no M8 routing "
    "direction (Table 3.18.1 is direction-free)"
)
_WIDE_SPACING = (
    "read literally (edge length) they override M8.S.1-S.3: every edge of "
    "80 nm or more would need 80-1000 nm, so S.1's 40 nm could never apply; "
    "Fig. 3.18.1 labels S.1-S.8 by W1 / W2 width ranges, and as a width class "
    "S.5 (80-120 nm wide needs 80 nm) still contradicts S.1 (two shapes "
    "of 80 nm or more need 40 nm), so the DRM supports no consistent reading"
)
_WIDE_SPACING_S4 = (
    "read literally (edge length) it overrides M8.S.2-S.3: an edge of 60-80 "
    "nm would need 60 nm, so S.2's 43 nm and S.3's 46 nm could never apply "
    "to it; the DRM's own Fig. 3.18.1 labels S.1-S.8 by W1 / W2 width ranges "
    "and gives no reading under which S.4 and S.2-S.3 can both hold"
)


def _gaps(
    ids: tuple[str, ...], table: str, reason: str, by: str
) -> tuple[Asap7RuleGap, ...]:
    return tuple(Asap7RuleGap(i, table, reason, by) for i in ids)


def _sadp_metal_gaps(layer: str, t1: str, t2: str) -> tuple[Asap7RuleGap, ...]:
    return (
        Asap7RuleGap(f"{layer}.W.4", t1, _SADP_TRACK_COUNT, "#3155"),
        *_gaps(tuple(f"{layer}.S.{n}" for n in (3, 4, 5)), t1, _SADP_TRACK, "#3155"),
        Asap7RuleGap(f"{layer}.AUX.4", t2, _WIDE_EDGE, "#3155"),
    )


def _top_metal_gaps(layer: str) -> tuple[Asap7RuleGap, ...]:
    return (
        Asap7RuleGap(f"{layer}.S.4", "Table 3.18.1", _WIDE_SPACING_S4, "#3156"),
        *_gaps(
            tuple(f"{layer}.S.{n}" for n in range(5, 9)),
            "Table 3.18.2",
            _WIDE_SPACING,
            "#3156",
        ),
    )


def _v123_gaps(layer: str, upper: str) -> tuple[Asap7RuleGap, ...]:
    t = "Table 3.13.1"
    return (
        Asap7RuleGap(f"{layer}.S.1", t, _TRACK, "#3147"),
        *_gaps(tuple(f"{layer}.S.{n}" for n in (2, 3, 4)), t, _END_CAP, "#3147"),
        Asap7RuleGap(f"{layer}.{upper}.AUX.2", t, _EXACT_W, "#3147"),
    )


_ONE_SIDE = (
    "'on two opposite sides' with a per-side minimum (V0.LIG.EN.4 1 nm of LIG "
    "past V0) needs the LIG routing direction to pick the side pair"
)

#: DRM rules from the covered tables (3.1-3.19) the deck does not check yet.
NOT_IMPLEMENTED: tuple[Asap7RuleGap, ...] = (
    Asap7RuleGap("GATE.AUX.2", "Table 3.4.1", _GATE_DISCONTINUOUS, "#2812"),
    Asap7RuleGap("ACTIVE.AUX.3", "Table 3.5.2", _ACTIVE_NOTCH, "#2812"),
    Asap7RuleGap(
        "ACTIVE.LUP.1",
        "Table 3.5.2",
        "DRM-confirmed 30 um maximum, but the DRM defines no tap layer, "
        "distance metric or connectivity test; no sound predicate yet "
        "(evidence: docs/cli/drc.md)",
        "#2812",
    ),
    # V0 (Tables 3.11.1 / 3.11.2)
    Asap7RuleGap("V0.S.1", "Table 3.11.1", _TRACK, "#3147"),
    *_gaps(("V0.S.2", "V0.S.3", "V0.S.4"), "Table 3.11.1", _END_CAP, "#3147"),
    Asap7RuleGap("V0.M1.EN.1", "Table 3.11.1", _EXACT_EN, "#3147"),
    *_gaps(("V0.LISD.EN.2", "V0.LISD.EN.3"), "Table 3.11.2", _LISD_EXACT_3, "#3147"),
    Asap7RuleGap("V0.LIG.EN.4", "Table 3.11.2", _ONE_SIDE, "#3147"),
    Asap7RuleGap(
        "V0.LIG.AUX.2",
        "Table 3.11.2",
        "edge parallel to the LIG length needs the LIG routing direction",
        "#3147",
    ),
    Asap7RuleGap("V0.M1.AUX.3", "Table 3.11.2", _EXACT_W, "#3147"),
    # V1-V3 (Table 3.13.1; section 3.13 extends the V1 rules to V2 and V3)
    *_v123_gaps("V1", "M2"),
    Asap7RuleGap("V1.M1.EN.1", "Table 3.13.1", _EXACT_EN, "#3147"),
    Asap7RuleGap("V1.M2.EN.2", "Table 3.13.1", _EXACT_EN, "#3147"),
    *_v123_gaps("V2", "M3"),
    Asap7RuleGap("V2.M3.EN.2", "Table 3.13.1", _EXACT_EN, "#3147"),
    *_v123_gaps("V3", "M4"),
    # M4-M7 / V4-V7 (Tables 3.14-3.17)
    *_sadp_metal_gaps("M4", "Table 3.14.1", "Table 3.14.2"),
    *_sadp_metal_gaps("M5", "Table 3.14.1", "Table 3.14.2"),
    *_sadp_metal_gaps("M6", "Table 3.16.1", "Table 3.16.2"),
    *_sadp_metal_gaps("M7", "Table 3.16.1", "Table 3.16.2"),
    Asap7RuleGap("V4.S.2", "Table 3.15.1", _VIA_NETS, "#3155"),
    Asap7RuleGap("V5.S.2", "Table 3.15.1", _VIA_NETS, "#3155"),
    Asap7RuleGap("V6.S.2", "Table 3.17.1", _VIA_NETS, "#3155"),
    Asap7RuleGap("V7.S.2", "Table 3.17.1", _VIA_NETS, "#3155"),
    Asap7RuleGap("V7.W.1", "Table 3.17.1", _M8_DIRECTION, "#3155"),
    Asap7RuleGap("V7.M8.AUX.2", "Table 3.17.1", _M8_DIRECTION, "#3155"),
    # M8-M9 / V8-V9 (Tables 3.18-3.19)
    *_top_metal_gaps("M8"),
    *_top_metal_gaps("M9"),
)

#: Density disposition (#2811). The pinned DRM states no density rule: its
#: rule-type list (Table 1.2.1: A, EN, EX, GEOMETRY, L, LUP, OV, R, S, SRAM,
#: W, AUX) has no density type, and no section of chapter 3 (3.1 Geometry
#: Check through 3.19 V8-V9) or table in it gives a minimum / maximum layer
#: density, a density window or fill requirement. The deck therefore checks
#: no density, and no threshold is invented from shipped geometry. The APR
#: tech LEF's MINIMUMDENSITY / MAXIMUMDENSITY on M5 and Pad only are router
#: settings outside the DRM and are not taken as rules (docs/cli/drc.md,
#: "Density").
DENSITY_DISPOSITION = (
    "the ASAP7 DRM (" + DRM_SOURCE + ") states no density rule: Table 1.2.1 "
    "has no density rule type and no table in sections 3.1-3.19 gives a "
    "density threshold, window or fill requirement, so the deck checks none"
)

#: PAD stream layer the deck reads (``pad = input(96, 0)``): DRM mask 96
#: (Table 2.3.1). The DRM is the authority the deck is transcribed from. The
#: pinned lambdapdk v0.2.17 files do not settle the number: ``asap7.layermap``
#: names ``Pad`` only as ``Pad/SPNET|PIN|LEFPIN 95/251`` (its ``Pad/NET`` line
#: is commented out), which collides with V9's ``95``; ``asap7.lyp`` has no
#: PAD entry; ``asap7_tech.lef`` gives layer ``Pad`` no stream number. The
#: deck is not renumbered to 95 for that reason.
PAD_LAYER = (96, 0)

#: PAD-layer disposition (#3807). The DRM has no rule for PAD itself: the
#: only PAD rules are ``V9.PAD.EN.2`` and ``V9.AUX.1`` (Table 3.19.1). The
#: tech LEF's ``Pad`` width, spacing table and 20 % / 80 % density are
#: router settings outside the DRM (see ``DENSITY_DISPOSITION``) and are not
#: taken as rules.
PAD_DISPOSITION = (
    "the ASAP7 DRM (" + DRM_SOURCE + ") has no rule for the PAD layer itself "
    "(no width, spacing, area, enclosure-by-V9 or density); the deck reads "
    "PAD (stream 96/0, DRM mask 96) only as the layer enclosing V9 "
    "(V9.PAD.EN.2, V9.AUX.1), so shapes on PAD are not checked"
)

#: Unchecked areas surfaced as ``coverage.unchecked_rules`` by
#: ``klt drc --engine klayout`` when the packaged deck runs on a layout with
#: shapes on the layer: ``{id: (layer, datatype, description)}``.
UNCHECKED_AREAS: dict[str, tuple[int, int, str]] = {
    "PAD.*": (PAD_LAYER[0], PAD_LAYER[1], PAD_DISPOSITION),
}


@dataclass(frozen=True)
class Asap7CellDeviation:
    """One shipped standard cell that violates one implemented rule as the
    DRM states it, kept as an explicit, exact exception of the corpus gate.

    ``cell`` omits the ``_ASAP7_75t_<VT>`` suffix; ``citation`` is the DRM
    table / figure the rule is read from; ``libraries`` names the pinned
    ``asap7sc7p5t_28_<suffix>`` libraries the deviation occurs in.
    ``handling`` says what the deck and the gate do about it: the rule is
    kept as the DRM states it (not loosened, not waived in the deck), the
    full-corpus test expects exactly this ``(cell, rule)`` pair, and the
    clean-run CLI gate leaves only this cell out of its temporary layout.
    """

    cell: str
    rule: str
    citation: str
    rationale: str
    handling: str
    libraries: tuple[str, ...] = ("R", "L", "SL")


#: Table 3.8.1: SDT horizontal edges must coincide with ACTIVE horizontal
#: edges; Fig. 3.8.1(d) marks an SDT edge off ACTIVE's as illegal.
_SDT_TALL = (
    "SDT drawn 81 nm tall over a 54 nm ACTIVE: the SDT top edge is not on an "
    "ACTIVE horizontal edge"
)
_SDT_HANDLING = "kept as stated; expected exactly; left out of the clean run"

#: Every retained deviation of the pinned shipped cells (lambdapdk v0.2.17,
#: asap7sc7p5t_28_{R,L,SL}) from the implemented rules. docs/cli/drc.md
#: lists the same records.
SHIPPED_CELL_DEVIATION_RECORDS: tuple[Asap7CellDeviation, ...] = tuple(
    Asap7CellDeviation(
        cell,
        "SDT.ACTIVE.AUX.2",
        "Table 3.8.1, Fig. 3.8.1(d)",
        _SDT_TALL,
        _SDT_HANDLING,
    )
    for cell in (
        "AO33x2",
        "BUFx2",
        "BUFx4",
        "BUFx4f",
        "CKINVDCx9p33",
        "ICGx2p67DC",
        "ICGx4DC",
        "ICGx5p33DC",
        "ICGx6p67DC",
        "OAI22xp33",
    )
)


def _deviation_map(
    records: tuple[Asap7CellDeviation, ...],
) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for record in records:
        out[record.cell] = (*out.get(record.cell, ()), record.rule)
    return out


#: ``cell -> rule ids`` view of :data:`SHIPPED_CELL_DEVIATION_RECORDS`: the
#: exact deviation set the shipped-cell regression compares against. Names
#: are given without the ``_ASAP7_75t_<VT>`` suffix.
SHIPPED_CELL_DEVIATIONS: dict[str, tuple[str, ...]] = _deviation_map(
    SHIPPED_CELL_DEVIATION_RECORDS
)
