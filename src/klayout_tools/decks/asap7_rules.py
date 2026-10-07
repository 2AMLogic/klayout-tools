"""Declared rule-id contract for the ASAP7 KLayout DRC deck (issue #2760).

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
    """A DRM rule in a phase-1 table that the deck does not check yet."""

    id: str
    table: str
    reason: str
    tracked_by: str


def _select_family(layer: str) -> tuple[Asap7Rule, ...]:
    # DRM section 3.7: rules stated for NSELECT apply to PSELECT, SLVT, LVT
    # and SRAMVT as well, unless otherwise stated.
    t = "Table 3.7.1"
    return (
        Asap7Rule(f"{layer}.W.1", t),
        Asap7Rule(f"{layer}.W.2", t),
        Asap7Rule(f"{layer}.ACTIVE.EN.1", t),
        Asap7Rule(f"{layer}.ACTIVE.EN.2", t),
        Asap7Rule(f"{layer}.GATE.EX.1", t),
        Asap7Rule(f"{layer}.GATE.EX.2", t),
    )


_PITCH_NOTE = (
    "exact pitch checked edge-to-edge for minimum-width shapes: neighbours "
    "closer than two pitches must sit exactly one pitch apart; alignment of "
    "shapes two or more pitches apart is not checked"
)

#: Every rule category the phase-1 deck declares, in deck order.
RULES: tuple[Asap7Rule, ...] = (
    Asap7Rule(
        "GEOMETRY.NONORTHOGONAL",
        "Table 3.1.1",
        "applied to the FEOL/MOL layers only until the BEOL phase (#2810)",
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
    Asap7Rule("ACTIVE.W.3", "Table 3.5.1"),
    Asap7Rule("ACTIVE.S.1", "Table 3.5.1"),
    Asap7Rule("ACTIVE.S.2B", "Table 3.5.1"),
    Asap7Rule("ACTIVE.WELL.S.4", "Table 3.5.1"),
    Asap7Rule("ACTIVE.WELL.EN.1", "Table 3.5.1"),
    Asap7Rule("ACTIVE.A.1A", "Table 3.5.2"),
    Asap7Rule("ACTIVE.A.1B", "Table 3.5.2"),
    Asap7Rule("ACTIVE.AUX.1", "Table 3.5.2"),
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
    # LISD
    Asap7Rule("LISD.W.1", "Table 3.9.1"),
    Asap7Rule("LISD.S.1", "Table 3.9.1"),
    Asap7Rule("LISD.S.2", "Table 3.9.1"),
    Asap7Rule("LISD.S.3", "Table 3.9.1"),
    Asap7Rule("LISD.A.1", "Table 3.9.1"),
    # LIG
    Asap7Rule("LIG.W.1", "Table 3.10.1"),
    Asap7Rule("LIG.S.1", "Table 3.10.1"),
    Asap7Rule("LIG.S.2", "Table 3.10.1"),
    Asap7Rule("LIG.S.3", "Table 3.10.1"),
    Asap7Rule("LIG.S.4", "Table 3.10.1"),
    Asap7Rule("LIG.S.5", "Table 3.10.1"),
    Asap7Rule("LIG.GATE.S.9A", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.S.9B", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.S.10", "Table 3.10.2"),
    Asap7Rule("LIG.GCUT.S.11", "Table 3.10.2"),
    Asap7Rule("LIG.A.1", "Table 3.10.2"),
    Asap7Rule("LIG.LISD.A.2", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.A.3", "Table 3.10.2"),
    Asap7Rule(
        "LIG.LISD.OV.1",
        "Table 3.10.3",
        "any LIG/LISD overlap is treated as 'connected together'",
    ),
    Asap7Rule("LIG.GATE.AUX.1", "Table 3.10.2"),
    Asap7Rule("LIG.GATE.EX.1", "Table 3.10.3"),
    # V0 (phase 1: width and connectivity only)
    Asap7Rule(
        "V0.W.1",
        "Table 3.11.1",
        "stated along the M1 length; checked in both directions (V0 is as wide "
        "as M1 across the track per V0.M1.AUX.3)",
    ),
    Asap7Rule("V0.AUX.1", "Table 3.11.2"),
)

RULE_IDS: tuple[str, ...] = tuple(r.id for r in RULES)

_SRAM = (
    "SRAMDRC-marked variant; no SRAM cells ship with the pinned PDK and the DRM "
    "says SRAM rules may be waived for other cells"
)
_NETS = "needs net connectivity (different-net spacing)"
_MULTIPLE = "integer-multiple width has no modulo predicate in the DRC DSL"
_NO_BOUND = "the DRM gives no distance bound to check against"
_V0_M1 = "defined against M1 tracks or as an exact enclosure; lands with the M1 rules"

#: DRM rules from the phase-1 tables (3.1-3.11) the deck does not check yet.
NOT_IMPLEMENTED: tuple[Asap7RuleGap, ...] = (
    Asap7RuleGap("GATE.AUX.2", "Table 3.4.1", _NO_BOUND, "#2812"),
    Asap7RuleGap("ACTIVE.W.2", "Table 3.5.1", _MULTIPLE, "#2812"),
    Asap7RuleGap("ACTIVE.S.2A", "Table 3.5.1", _NETS, "#2812"),
    Asap7RuleGap("SRAM.ACTIVE.WELL.S.5", "Table 3.5.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.ACTIVE.WELL.EN.2", "Table 3.5.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.ACTIVE.A.2A", "Table 3.5.2", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.ACTIVE.A.2B", "Table 3.5.2", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.ACTIVE.AUX.2", "Table 3.5.2", _SRAM, "#2812"),
    Asap7RuleGap("ACTIVE.AUX.3", "Table 3.5.2", _NO_BOUND, "#2812"),
    Asap7RuleGap(
        "ACTIVE.LUP.1",
        "Table 3.5.2",
        "latch-up distance (30 um) needs tap/device classification",
        "#2812",
    ),
    Asap7RuleGap("SRAM.NSELECT.ACTIVE.EN.3", "Table 3.7.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.NSELECT.ACTIVE.EN.4", "Table 3.7.1", _SRAM, "#2812"),
    Asap7RuleGap("SDT.W.3", "Table 3.8.1", _MULTIPLE, "#2812"),
    Asap7RuleGap("SRAM.SDT.W.4", "Table 3.8.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.SDT.ACTIVE.OV.3", "Table 3.8.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.SDT.LISD.OV.4", "Table 3.8.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.LISD.S.4", "Table 3.9.1", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.LISD.AUX.1", "Table 3.9.1", _SRAM, "#2812"),
    Asap7RuleGap("LIG.LISD.S.6", "Table 3.10.1", _NETS, "#2812"),
    Asap7RuleGap("LIG.LISD.S.7", "Table 3.10.1", _NETS, "#2812"),
    Asap7RuleGap("LIG.SDT.S.8", "Table 3.10.1", _NETS, "#2812"),
    Asap7RuleGap("SRAM.LIG.GATE.A.4", "Table 3.10.2", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.LIG.AUX.2", "Table 3.10.3", _SRAM, "#2812"),
    Asap7RuleGap("SRAM.LIG.GATE.OV.2", "Table 3.10.3", _SRAM, "#2812"),
    Asap7RuleGap("V0.S.1", "Table 3.11.1", _V0_M1, "#2810"),
    Asap7RuleGap("V0.S.2", "Table 3.11.1", _V0_M1, "#2810"),
    Asap7RuleGap("V0.S.3", "Table 3.11.1", _V0_M1, "#2810"),
    Asap7RuleGap("V0.S.4", "Table 3.11.1", _V0_M1, "#2810"),
    Asap7RuleGap("V0.M1.EN.1", "Table 3.11.1", _V0_M1, "#2810"),
    Asap7RuleGap("V0.LISD.EN.2", "Table 3.11.2", _V0_M1, "#2810"),
    Asap7RuleGap("V0.LISD.EN.3", "Table 3.11.2", _V0_M1, "#2810"),
    Asap7RuleGap("V0.LIG.EN.4", "Table 3.11.2", _V0_M1, "#2810"),
    Asap7RuleGap("V0.LIG.A.1", "Table 3.11.2", _V0_M1, "#2810"),
    Asap7RuleGap("V0.LIG.AUX.2", "Table 3.11.2", _V0_M1, "#2810"),
    Asap7RuleGap("V0.M1.AUX.3", "Table 3.11.2", _V0_M1, "#2810"),
)

#: Shipped standard cells (lambdapdk v0.2.17, asap7sc7p5t_28_{R,L,SL}) that
#: violate a phase-1 rule as the DRM states it. In each, SDT is drawn 81 nm
#: tall over a 54 nm ACTIVE, so its top edge does not lie on ACTIVE's
#: (Table 3.8.1, Fig. 3.8.1(d)). The rule is not loosened; the deviation is
#: listed here and in docs/cli/drc.md. Names are given without the
#: ``_ASAP7_75t_<VT>`` suffix.
SHIPPED_CELL_DEVIATIONS: dict[str, tuple[str, ...]] = {
    cell: ("SDT.ACTIVE.AUX.2",)
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
}
