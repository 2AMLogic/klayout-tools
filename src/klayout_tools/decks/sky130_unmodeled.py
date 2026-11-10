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
_CORE_UNMODELED_RULES: tuple[str, ...] = (
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
)

#: Native sky130A ids of the high-voltage / well / varactor / vpp families that
#: the deck knowingly does not model (triage slice 3/4 of issue #5242).
_HV_WELL_UNMODELED_RULES: tuple[str, ...] = (
    # Depletion NMOS/PMOS (denmos/depmos) rules: the deck has no depletion-device
    # marker layers or gate-derived layers (issue #5253).
    "denmos.1",
    "denmos.10",
    "denmos.11",
    "denmos.12",
    "denmos.13",
    "denmos.2",
    "denmos.3",
    "denmos.4",
    "denmos.5",
    "denmos.6",
    "denmos.7",
    "denmos.8",
    "denmos.9a",
    "denmos.9b",
    "depmos.1",
    "depmos.10",
    "depmos.11",
    "depmos.12",
    "depmos.2",
    "depmos.3",
    "depmos.4",
    "depmos.5",
    "depmos.6",
    "depmos.7",
    "depmos.8",
    "depmos.9a",
    "depmos.9b",
    # dnwell/pwde/pwbm/pwdem width, spacing and enclosure rules: dnwell, pwde,
    # pwbm and the deep-pwell marker are only ongrid/angle-checked; the deck has
    # no width/space/enclosure rule on them (issue #5253).
    "dnwell.1",
    "dnwell.3",
    "dnwell.4",
    "dnwell.5",
    "pwbm",
    "pwbm.4",
    "pwde.1",
    "pwde.2",
    "pwdem.3",
    "pwdem.4",
    "pwdem.5",
    # nwell.4-.7 need dnwell/uhvi/areaid:po/tap compound layer expressions
    # (nwell.1 is nwell.width.1) (issue #5253).
    "nwell.4",
    "nwell.5",
    "nwell.6",
    "nwell.7",
    # High-voltage (hv.*, hvdifftap.*, hvpoly.*, hvnwel*) rules over hvi-derived
    # gate/diff/nwell layers that DrcRule cannot evaluate (issue #5253).
    "hv.diff.1",
    "hv.diff.2",
    "hv.diff.3a",
    "hv.nwell.1",
    "hv.poly.2",
    "hv.poly.3",
    "hv.poly.4",
    "hv.poly.6",
    "hvdifftap.14",
    "hvdifftap.14a",
    "hvdifftap.15a",
    "hvdifftap.15b",
    "hvdifftap.16",
    "hvdifftap.17",
    "hvdifftap.18",
    "hvdifftap.19",
    "hvdifftap.20",
    "hvdifftap.21",
    "hvdifftap.22",
    "hvdifftap.23",
    "hvdifftap.24",
    "hvdifftap.25",
    "hvnwel.9",
    "hvnwell.8",
    "hvpoly.13",
    "hvpoly.14",
    # hvi/hvtp/hvntm/uhvi/vhvi spacing, enclosure and overlap rules; the deck
    # gives these marker layers only ongrid/angle rules (see issue #3802, #5253).
    "hvi.2",
    "hvi.4",
    "hvntm.3",
    "hvntm.4",
    "hvntm.5",
    "hvntm.6a",
    "hvntm.9",
    "hvtp",
    "hvtp.3",
    "hvtp.4",
    "hvtp.5",
    "uhvi.1",
    "uhvi.2",
    "uhvi.3",
    "uhvi.4",
    "uhvi.5",
    "uhvi.6",
    "uhvi.7",
    "vhvi.1",
    "vhvi.2",
    "vhvi.3",
    "vhvi.5",
    "vhvi.7",
    # Varactor (varac.*) rules need varac marker and tap-minus-poly edge
    # expressions (issue #5253).
    "varac.1",
    "varac.2",
    "varac.3",
    "varac.4",
    "varac.5",
    "varac.6",
    "varac.7",
    # vpp capacitor-marker rules; vpp is only ongrid/angle-checked (issue #5253).
    "vpp.1",
    "vpp.13",
    "vpp.3",
    "vpp.4",
    "vpp.5",
    "vpp.5a",
    "vpp.5b",
    "vpp.5c",
    "vpp.8",
    "vpp.9",
)

UNMODELED_RULES: tuple[str, ...] = tuple(
    sorted(_CORE_UNMODELED_RULES + _HV_WELL_UNMODELED_RULES)
)
