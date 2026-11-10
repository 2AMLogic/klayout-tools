"""sky130 curated-deck remainder: official rule ids the deck does not transcribe.

Split out of :mod:`.sky130` to keep that module under its file-size ceiling;
``sky130.UNMODELED_RULES`` re-exports this tuple.
"""

from __future__ import annotations

#: Rows whose provenance cites the native sky130A output ids
#: (``<stem>_OFFGRID`` / ``<stem>_angle``, the ids ``klt drc`` coverage is
#: diffed against, issue #5253) instead of the shared ``x.1b`` / ``x.2*`` /
#: ``x.3a`` message ids. Each row's stem equals the native output stem. Other
#: rows keep the message id until their triage slice of #5242 lands.
_NATIVE_OUTPUT_ID_STEMS: frozenset[str] = frozenset(
    {
        "dnwell",
        "hvi",
        "hvntm",
        "hvtp",
        "hvtr",
        "nwell",
        "pwbm",
        "pwde",
        "pwell_rs",
        "uhvi",
        "vhvi",
        "vpp",
    }
)


def native_output_id(stem: str, suffix: str, fallback: str) -> str:
    """``<stem>_<suffix>`` for a native-triaged stem, else the shared ``fallback``."""
    return f"{stem}_{suffix}" if stem in _NATIVE_OUTPUT_ID_STEMS else fallback


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
    # Sorted (tests/test_drc_sky130_spacing.py enforces it), so per-group
    # reasons live here rather than inline:
    # - Implant/npc/well rules (issue #2934) scoped to compound layer
    #   expressions (diff.and(psdm), npsdm enclosing diff/tap, gate, ...) that
    #   `DrcRule` cannot evaluate; ids from sky130.lydrc. Also the spacing rules
    #   of sky130A_mr.drc this deck omits (issue #4282): marker/special layers
    #   not modelled (dnwell, lvtn, ncm, nsm, rpm, urpm, tunm, thkox, ldntm),
    #   licon spacing, areaid:core splits (poly.3, li.7) and capacitor-plate
    #   separations needing two-layer checks (capm.*, cap2m.*).
    # - ct.4 ("mcon must be covered by li") has no curated rule (issue #5233).
    # - li.5 is only floor-modeled by ``li1.enclosing.licon1.1`` (zero margin):
    #   the official 0.08um on two adjacent edges is not checked (issue #5233).
    # - difftap.*: diff/tap width, gate, abutment, spacing and areaid:en checks
    #   (#5254); only difftap.1/.3 are transcribed. extd.* likewise.
    # - lvtn/ncm/ldntm/tunm/thkox/moduleCut marker-layer rules needing compound
    #   expressions the deck does not model (#5254).
    # - nsdm/psdm areaid:ce/periphery, parallel-edge and overlap-over-active
    #   sub-rules (#5254); only nsdm.1/psdm.1/n-psdm.1 are transcribed.
    # - photo.*: photodiode (areaid.po) rules; the deck models no photodiode
    #   layer (#5254).
    # - poly.*: rules needing gate/diff/tap-relative expressions (#5254); only
    #   poly.1a/.2/.8 are transcribed.
    # - Metal / MIM-cap / resistor / pad / special-layer triage (#5252, slice 2/4
    #   of #5242), each id checked against sky130A_mr.drc / sky130A.lydrc: marker
    #   layer prohibitions (MR_*), MiM-cap plate size/shape/via-spacing checks,
    #   copper-flow-only slotting (m*.11, m4.15), cell-restricted m1.4a/_a,
    #   floating-metal m*.x, via-without-metal m*.via*/m*.*_a/_b, large-metal
    #   m3.3cd, and mf/nsm/pwres/rfdiode/rpm width/spacing/enclosure checks.
    "MR_li.WID.4",
    "MR_licon.SP.1",
    "MR_lvtn.OVL.2",
    "MR_rdl.CON.1",
    "MR_thkox.CON.1",
    "MR_tunm.CON.1",
    "cap2m.11",
    "cap2m.2b",
    "cap2m.2b_a",
    "cap2m.3_a",
    "cap2m.5",
    "capm.10",
    "capm.11",
    "capm.2b",
    "capm.2b_a",
    "capm.5",
    "capm.6",
    "capm.7",
    "capm.8",
    "ct.4",
    "difftap.10",
    "difftap.11",
    "difftap.1_a",
    "difftap.1_b",
    "difftap.1_c",
    "difftap.2",
    "difftap.4",
    "difftap.5",
    "difftap.6",
    "difftap.7",
    "difftap.8",
    "difftap.9",
    "dnwell.2",
    "extd.1",
    "extd.2",
    "hvi.1",
    "hvi.2a",
    "hvntm.1",
    "hvntm.2",
    "hvtp.1",
    "hvtp.2",
    "hvtr.1",
    "hvtr.2",
    "hvtr.2_a",
    "ldntm.1",
    "ldntm.2",
    "li.5",
    "li.7",
    "licon.13",
    "lvtn.1",
    "lvtn.12",
    "lvtn.13",
    "lvtn.1a",
    "lvtn.2",
    "lvtn.3a",
    "lvtn.3b",
    "lvtn.4b",
    "lvtn.9",
    "m1.11",
    "m1.3ab",
    "m1.4a",
    "m1.4a_a",
    "m1.5",
    "m1.x",
    "m2.11",
    "m2.3ab",
    "m2.4_a",
    "m2.4_b",
    "m2.5",
    "m2.via",
    "m2.x",
    "m3.11",
    "m3.3ab",
    "m3.3cd",
    "m3.4_a",
    "m3.via2",
    "m3.x",
    "m4.11",
    "m4.15",
    "m4.3_a",
    "m4.5ab",
    "m4.via3",
    "m4.x",
    "m5.3_a",
    "m5.via4",
    "m5.x",
    "mf.1",
    "mf.2",
    "mf.3",
    "moduleCut.1",
    "n/psdm.10a",
    "n/psdm.10b",
    "n/psdm.5a",
    "n/psdm.5b",
    "n/psdm.6",
    "n/psdm.7",
    "n/psdm.8",
    "ncm.1",
    "ncm.2",
    "ncm.2a",
    "ncm.3",
    "ncm.5",
    "ncm.6",
    "ncm.7",
    "ncm.x.3",
    "npc.4",
    "nsdm.2",
    "nsdm.3",
    "nsdm.4",
    "nsdm.5",
    "nsdm.6",
    "nsdm.6a",
    "nsdm_psdm_overlap",
    "nsm.1",
    "nsm.2",
    "nsm.3",
    "nsm.4",
    "pad.2",
    "photo.10",
    "photo.2",
    "photo.3",
    "photo.4",
    "photo.5.6",
    "photo.7",
    "photo.8",
    "photo.9",
    "poly.10",
    "poly.11",
    "poly.12",
    "poly.15",
    "poly.1b",
    "poly.3",
    "poly.4",
    "poly.5",
    "poly.6",
    "poly.7",
    "poly.9",
    "psdm.2",
    "psdm.3",
    "psdm.4",
    "psdm.5",
    "psdm.6",
    "psdm.6a",
    "pwres.2",
    "pwres.3",
    "pwres.4",
    "pwres.5",
    "pwres.6",
    "pwres.8",
    "rdl.1",
    "rdl.2",
    "rfdiode.1",
    "rfdiode.2",
    "rpm.10",
    "rpm.2",
    "rpm.3",
    "rpm.4",
    "rpm.5",
    "rpm.6",
    "rpm.7",
    "rpm.8",
    "rpm.9",
    "thkox.1",
    "thkox.2",
    "tunm.1",
    "tunm.2",
    "tunm.3",
    "tunm.4",
    "tunm.5",
    "tunm.6a",
    "tunm.7",
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


def native_offgrid_aliases() -> dict[str, str]:
    """Native ``sky130A.lydrc`` OFFGRID output ids -> implementing deck rule (#5254).

    The deck cites the official ``x.1b``/``x.2*``/``x.3a`` rule id in
    ``provenance.rule_id``, but the native ``.output()`` id is
    ``<layer>_OFFGRID`` / ``<layer>_angle``, so the native-vs-deck coverage diff
    (``tests/test_drc_sky130_unmodeled_vs_native.py``) needs this map to see the
    rule as implemented. Derived from ``sky130._OFFGRID_ROWS``, never hand-listed.
    """
    from .sky130 import _OFFGRID_ROWS  # lazy: sky130 imports this module

    return {
        **{f"{stem}_OFFGRID": f"{stem}.ongrid.1" for stem, *_ in _OFFGRID_ROWS},
        **{
            f"{stem}_angle": f"{stem}.angle.1"
            for stem, _layer, limit, _rid in _OFFGRID_ROWS
            if limit is not None
        },
    }
