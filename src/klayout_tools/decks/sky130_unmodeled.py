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
    # diff/tap width, gate, abutment, spacing and areaid:en checks (#5254);
    # only difftap.1/.3 are transcribed.
    "difftap.1_a",
    "difftap.1_b",
    "difftap.1_c",
    "difftap.2",
    "difftap.4",
    "difftap.5",
    "difftap.6",
    "difftap.7",
    "extd.1",
    "extd.2",
    # lvtn/ncm/ldntm/tunm/thkox/moduleCut marker-layer rules needing compound
    # expressions the deck does not model (#5254).
    "ldntm.1",
    "lvtn.1",
    "lvtn.12",
    "lvtn.13",
    "lvtn.1a",
    "lvtn.3a",
    "lvtn.3b",
    "lvtn.4b",
    "lvtn.9",
    "moduleCut.1",
    "ncm.1",
    "ncm.2",
    "ncm.3",
    "ncm.5",
    "ncm.6",
    "ncm.7",
    "ncm.x.3",
    "thkox.1",
    "tunm.1",
    "tunm.3",
    "tunm.4",
    "tunm.5",
    "tunm.6a",
    "tunm.7",
    # nsdm/psdm areaid:ce/periphery, parallel-edge and overlap-over-active
    # sub-rules (#5254); only nsdm.1/psdm.1/n-psdm.1 are transcribed.
    "nsdm.2",
    "nsdm.3",
    "nsdm.4",
    "nsdm.5",
    "nsdm.6",
    "nsdm.6a",
    "nsdm_psdm_overlap",
    "psdm.2",
    "psdm.3",
    "psdm.4",
    "psdm.5",
    "psdm.6",
    "psdm.6a",
    # photodiode (areaid.po) rules: the deck models no photodiode layer (#5254).
    "photo.10",
    "photo.2",
    "photo.3",
    "photo.4",
    "photo.5.6",
    "photo.7",
    "photo.8",
    "photo.9",
    # poly rules needing gate/diff/tap-relative expressions (#5254); only
    # poly.1a/.2/.8 are transcribed.
    "poly.10",
    "poly.11",
    "poly.12",
    "poly.15",
    "poly.1b",
    "poly.4",
    "poly.5",
    "poly.6",
    "poly.7",
    "poly.9",
)
