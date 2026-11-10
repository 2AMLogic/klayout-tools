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
#: Also the contact/via/li slice of the native-id triage (issue #5251, parent
#: #5242): ring-shaped cuts (ct.3*, via*.3*), li/mcon IR-drop and cell-scoped li
#: rules, licon spacing/placement/coverage rules, and via variants the deck does
#: not transcribe (areaid:mt widths, 0.23um enclosures, inactive Cu back-end flow).
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
    "ct.3",
    "ct.3_a",
    "ct.3_b",
    # ct.4 ("mcon must be covered by li") has no curated rule (issue #5233).
    "ct.4",
    "ct.irdrop.1",
    "ct.irdrop.2",
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
    "li.1a",
    "li.3a",
    # li.5 is only floor-modeled by ``li1.enclosing.licon1.1`` (zero margin):
    # the official 0.08um on two adjacent edges is not checked (issue #5233).
    "li.5",
    "li.6",
    "li.7",
    "li.8",
    "licon.10",
    "licon.11",
    "licon.11a",
    "licon.11c",
    "licon.12",
    "licon.13",
    "licon.13_a",
    "licon.14",
    "licon.15",
    "licon.16",
    "licon.17",
    "licon.18",
    "licon.1_c",
    "licon.1b/c",
    "licon.2",
    "licon.2b",
    "licon.2c",
    "licon.2d",
    "licon.4",
    "licon.6",
    "licon.7",
    "licon.8a",
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
    "via.11",
    "via.12",
    "via.1b",
    "via.3",
    "via.3_a",
    "via.3_b",
    "via.4b",
    "via.5a",
    "via1.14",
    "via1.5b",
    "via2.11",
    "via2.12",
    "via2.14",
    "via2.1b",
    "via2.3",
    "via2.3_a",
    "via2.3_b",
    "via2.4a",
    "via2.5",
    "via3.11",
    "via3.12",
    "via3.13",
    "via3.5",
    "via4.3",
    "via4.3_a",
    "via4.3_b",
)
