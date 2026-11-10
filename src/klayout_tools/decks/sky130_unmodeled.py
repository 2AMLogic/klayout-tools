"""sky130 curated-deck remainder: official rule ids the deck does not transcribe.

Split out of :mod:`.sky130` to keep that module under its file-size ceiling;
``sky130.UNMODELED_RULES`` re-exports this tuple.
"""

from __future__ import annotations

#: Official sky130A_mr.drc rule ids this curated deck knowingly does *not*
#: transcribe (issue #3031): the "2-adjacent-edges-relaxed" enclosure
#: refinements, which need a per-edge-pair primitive ``DrcRule`` does not have
#: (see the "primary rule only" notes on ``met1.enclosing.mcon.1``,
#: ``via.enclosing.met1.1`` and their ``met2``/``met3`` siblings), and the
#: width/spacing/overlap checks on the thick-oxide and marker layers (``hvi``,
#: ``hvntm``, ``hvtp``, ``hvtr``, ``pad``, ``rdl``; issue #3802), which the
#: deck gives only ongrid/angle rules. A layout can be ``klt drc --deck
#: sky130`` clean yet fail the foundry runset on exactly these, so
#: ``coverage.unmodeled_rules`` lists them rather than let a clean verdict read
#: as full coverage. The open ``sky130A_mr.drc`` defines no latch-up or ESD
#: rules, so there is no id to list for those (see ``docs/cli/drc.md``).
#: Remove an id here when its rule is added.
UNMODELED_RULES: tuple[str, ...] = (
    # Implant/npc/well rules (issue #2934) scoped to compound layer
    # expressions (diff.and(psdm), npsdm enclosing diff/tap, gate, ...) that
    # `DrcRule` cannot evaluate; ids from sky130.lydrc. Also the spacing rules
    # of sky130A_mr.drc this deck omits (issue #4282): marker/special layers
    # not modelled (dnwell, lvtn, ncm, nsm, rpm, urpm, tunm, thkox, ldntm),
    # licon spacing, areaid:core splits (poly.3, li.7) and capacitor-plate
    # separations needing two-layer checks (capm.*, cap2m.*).
    "MR_licon.SP.1",
    "cap2m.11",
    "cap2m.2b",
    "cap2m.2b_a",
    "cap2m.5",
    "capm.11",
    "capm.2b",
    "capm.2b_a",
    "capm.5",
    # ct.4 ("mcon must be covered by li") has no curated rule (issue #5233).
    "ct.4",
    "difftap.10",
    "difftap.11",
    "difftap.8",
    "difftap.9",
    "dnwell.2",
    "hvi.1",
    "hvi.2a",
    "hvntm.1",
    "hvntm.2",
    "hvtp.1",
    "hvtp.2",
    "hvtr.1",
    "hvtr.2",
    "hvtr.2_a",
    "ldntm.2",
    # li.5 is only floor-modeled by ``li1.enclosing.licon1.1`` (zero margin):
    # the official 0.08um on two adjacent edges is not checked (issue #5233).
    "li.5",
    "li.7",
    "licon.13",
    "lvtn.2",
    "m1.3ab",
    "m1.5",
    "m2.3ab",
    "m2.5",
    "m3.3ab",
    "m4.5ab",
    "n/psdm.10a",
    "n/psdm.10b",
    "n/psdm.5a",
    "n/psdm.5b",
    "n/psdm.6",
    "n/psdm.7",
    "n/psdm.8",
    "ncm.2a",
    "npc.4",
    "nsm.1",
    "pad.2",
    "poly.3",
    "rdl.1",
    "rdl.2",
    "rpm.2",
    "thkox.2",
    "tunm.2",
    "urpm.2",
    "via.5a",
    "via2.5",
    "via3.5",
    # Metal / MIM-cap / resistor / pad / special-layer triage (issue #5252, slice
    # 2/4 of #5242), each id checked against sky130A_mr.drc / sky130A.lydrc.
    # No rule in the curated deck implements the check:
    # marker-layer prohibitions and straddle rules (MR_*; MR_li.WID.4 is the
    # li:res width).
    "MR_li.WID.4",
    "MR_lvtn.OVL.2",
    "MR_rdl.CON.1",
    "MR_thkox.CON.1",
    "MR_tunm.CON.1",
    # MiM-cap plates: size/shape/straddle/via-spacing checks and the
    # "cap2m.3_a" zero-overlap variant.
    "cap2m.3_a",
    "capm.10",
    "capm.6",
    "capm.7",
    "capm.8",
    # Copper-flow-only slotting/enclosure rules of sky130A.lydrc (not
    # transcribed; the AL enclosure rules m*.4 / m4.3 are).
    "m1.11",
    "m2.11",
    "m3.11",
    "m4.11",
    "m4.15",
    # Cell-restricted met1 enclosure of mcon (specific s8 cell list).
    "m1.4a",
    "m1.4a_a",
    # Floating-metal checks (met must interact with a via); the deck has none.
    "m1.x",
    "m2.x",
    "m3.x",
    "m4.x",
    "m5.x",
    # "via must be enclosed by metal" (`via.not(met)`) and the core-vs-periphery
    # met2 enclosure split: the deck's enclosure rules only scope to vias that
    # already interact with the metal (see ``_OUTSIDE_CHECKS`` in drc.py), so
    # a via with no metal at all is not flagged here.
    "m2.4_a",
    "m2.4_b",
    "m3.4_a",
    "m4.3_a",
    "m5.3_a",
    "m2.via",
    "m3.via2",
    "m4.via3",
    "m5.via4",
    # Large-metal (>3um) spacing, 0.4um; the deck has only the 0.3um m3.2.
    "m3.3cd",
    # Fuse (mf), nsm, pwell resistor (pwres), rfdiode and rpm width/spacing/
    # enclosure checks; the deck models only rpm.1a, nsm/rpm/mf grid and angle.
    "mf.1",
    "mf.2",
    "mf.3",
    "nsm.2",
    "nsm.3",
    "nsm.4",
    "pwres.2",
    "pwres.3",
    "pwres.4",
    "pwres.5",
    "pwres.6",
    "pwres.8",
    "rfdiode.1",
    "rfdiode.2",
    "rpm.10",
    "rpm.3",
    "rpm.4",
    "rpm.5",
    "rpm.6",
    "rpm.7",
    "rpm.8",
    "rpm.9",
)
