"""sg13g2 curated-deck unmodeled registries: the voltage-domain/device-marker
notes and the untranscribed-rule ids the deck reports so an omission is loud.

Split out of :mod:`.sg13g2` verbatim to keep that module under its file-size
ceiling (issue #5916's cut-containment work needed the headroom);
``sg13g2`` re-exports all three names, and every comment below that says
"this deck", "``DECK`` above" or "``EXTRACTION_DECK`` below" refers to
:mod:`.sg13g2`, whose definitions these registries annotate.
"""

# Voltage-domain marker layer this deck draws but does not *fully* model the
# DRC/extraction scoping of (issue #552's `decks.get_unmodeled_voltage_markers`,
# the same mechanism gf180mcu.py registers its own `Dualgate` marker under).
# sg13g2's `general_derivations.lvs` splits every MOS device recognition rule
# into a "-LV" (thin, default gate oxide) and "-HV" (thick gate oxide) pair
# keyed off `thickgateox_drw` (44/0, see `ngate_lv_base`/`ngate_hv_base` in
# that file): `ngate_lv_base = ngate.not(thickgateox_drw)`, `ngate_hv_base =
# ngate.and(thickgateox_drw)` (and the PMOS analogues).
#
# What issue #1231 closed: `EXTRACTION_DECK` below now declares one
# `mos_flavours` entry keyed on this same marker (see `MOSFlavour`'s own
# docstring in `decks/__init__.py`), so a transistor drawn inside ThickGateOx
# is recognised in its own extraction pass and binds the real
# `sg13_hv_nmos`/`sg13_hv_pmos` models under `--pdk` instead of silently
# taking the thin-oxide ("-LV") ones -- and `extract.py`'s own MOS
# `voltage_domain_warnings` stops firing for this marker (it only ever warned
# about that *MOS binding* gap). This mirrors exactly what #1111 did for
# gf180mcu's `Dualgate`.
#
# What is NOT modelled, and is what this entry still warns about: every DRC
# rule in `DECK` above applies the general-case FEOL thresholds regardless of
# ThickGateOx's presence -- sg13g2's own DRM scopes several FEOL rules to the
# thick-oxide domain (e.g. `Gat.a1`/`Gat.a2`'s channel-length-specific GatPoly
# widths, themselves compound `ngate`/`pgate` derivations of GatPoly, Activ,
# NWell *and* this marker -- see the module docstring's "Scope guard"), and
# none of that is transcribed here. So the registry entry stays, with its
# description narrowed to the DRC-rule residue.
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {
    (44, 0): (
        "ThickGateOx (44/0) marks sg13g2's thick-gate-oxide ('-HV') MOS "
        "voltage domain (see general_derivations.lvs's ngate_hv_base/"
        "pgate_hv_base). EXTRACTION_DECK models it for MOS device "
        "recognition/model binding (issue #1231: sg13_hv_nmos/sg13_hv_pmos "
        "under --pdk); this curated deck's DRC rules still apply the "
        "general-case ('-LV') thresholds to geometry regardless of "
        "ThickGateOx's presence (e.g. the Gat.a1/Gat.a2 channel-length-"
        "specific GatPoly widths, not transcribed in this deck yet)."
    ),
}

# Device-recognition marker layers this deck draws but has no native
# recognition for (issue #3858), surfaced as a `klt extract` `warnings[]`
# entry via `decks.get_unmodeled_device_markers` so the omission is loud
# rather than silent. Native recognition is declined, not forgotten:
# - `esd_extraction.lvs` builds the ESD diodes (`diodevdd_*`/`diodevss_*`) as
#   `bjt3(..., Esd3Term)` with *synthesized* terminals (`*_e.middle.sized(
#   0.499.um)`, `.extents` of derived regions) -- the same size-filtered,
#   per-instance dynamic-region gap that declined `schottky_nbl1` (#1234) and
#   the SiGe HBTs (#1232); they are not a `DiodeDevice` junction-overlap case.
# - the `nmoscl_2`/`nmoscl_4` clamps are keyed on a text label interacting
#   with `Recog.esd` plus a gate-count threshold; the extraction engine has no
#   text-label recognition predicate.
# - `scr1` has no LVS extraction upstream at all (it only appears in the
#   maximal DRC deck's exclusion terms), so "extracts as no device" matches
#   the PDK's own runset.
# Operator ruling #2864 routes such devices to the PDK's own runset-backed LVS
# (`klt lvs` `"klayout-runset"`).
UNMODELED_DEVICE_MARKERS: dict[tuple[int, int], str] = {
    (99, 30): (
        "Recog.esd (99/30) marks ESD structures (nmoscl_2/nmoscl_4 clamps, "
        "scr1, diodevdd_*/diodevss_* protection diodes); this deck has no "
        "native recognition for them, so they extract as no device. Use "
        "`klt lvs` with the 'klayout-runset' backend (the PDK's own LVS "
        "runset) to compare layouts containing them."
    ),
}

#: Official ``sg13g2_maximal.drc`` rule ids this curated deck does not
#: transcribe (issue #3854): the pad rules (``Pad.*``) and the latch-up rules
#: (``LU.*``). They need layers outside the curated set (``dfpad``, EdgeSeal)
#: and compound expressions ``DrcRule`` cannot evaluate. The ``*R`` pad ids
#: are the runset's recommended-only rules (``$recommendedRules``); the
#: ``LU.*`` ids are gated by ``$latchUpRules``. ESD structures have no rule id
#: in the runset (only ``Recog.esd`` device recognition, absent from
#: ``EXTRACTION_DECK``), so nothing ESD-related can be listed here.
#: ``coverage.unmodeled_rules`` lists these so a clean verdict is not read as
#: covering pads or latch-up. Remove an id here when its rule is added.
UNMODELED_RULES: tuple[str, ...] = (
    "LU.a",
    "LU.c",
    "LU.c1",
    "LU.d",
    "LU.d1",
    "Pad.a1",
    "Pad.aR",
    "Pad.bR",
    "Pad.d",
    "Pad.d1R",
    "Pad.dR",
    "Pad.gR",
    "Pad.jR",
    "Pad.kR",
)
