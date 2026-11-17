"""DRM rule ids the gf180mcu deck discloses as untranscribed (data only).

Holds `UNTRANSCRIBED_RULES`, split out of :mod:`.gf180mcu` for the per-file
size ratchet (the deck module sits at its trusted line ceiling); the deck
module re-exports it, so importers are unaffected.
"""

from __future__ import annotations

#: Upstream `esd.rb` / `guard_ring.rb` rule ids this deck knowingly does not
#: transcribe, with the reason each is out of reach of the existing check kinds
#: (issue #3792). `UNMODELED_RULES` below surfaces every key (plus the MT30/MSLOT
#: ids) as `coverage.unmodeled_rules` (informational, not graded, so `status` stays
#: `clean`). Remove an id when its rule is added.
UNTRANSCRIBED_RULES: dict[str, str] = {
    "ESD.3a": "measured to NCOMP, a derived layer (COMP inside Nplus), not a drawn one",
    "ESD.3b": (
        "`esd.not_outside(pcomp)`: PCOMP is derived and the rule is a "
        "min/max-space polygon selector"
    ),
    "ESD.4a": (
        "edge-to-edge enclosure of NCOMP edges filtered by `not_interacting(pcomp)`"
    ),
    "ESD.6": "needs `tgate` edges (MOS gate recognition) and a projection check",
    "ESD.7": "`esd.not_outside(pcomp)`: PCOMP is derived",
    "ESD.9": "`esd.not_overlapping(dualgate)`: no polygon-selector check kind",
    "ESD.10": "needs LVS_IO (119/5) and a COMP & ESD derivation",
    "ESD.pl": "needs `tgate` edges (MOS gate recognition) on Dualgate poly",
    "GR.1": (
        "`guard_ring_mk.not_in(comp.interacting(guard_ring_mk))`: no "
        "polygon-selector check kind"
    ),
    "GR.3": "`guard_ring_comp.not_in(pplus)`: no polygon-selector check kind",
    "GR.4": "metal width restricted to `not_outside(guard_ring_mk)` shapes",
    "GR.6": "COMP width restricted to `not_outside(guard_ring_mk)` shapes",
    "GR.11": "`guard_ring_mk.not_interacting(pad)`: no polygon-selector check kind",
    # `ldnmos.rb` / `ldpmos.rb` (issue #4569); transcribed: MDN.1, MDN.14, MDP.6
    "MDN.2a": "connectivity-aware `conn_space` (same-potential MVSD space)",
    "MDN.2b": "connectivity-aware `conn_space` (different-potential MVSD space)",
    "MDN.3a": (
        "`mvsd.enclosed(poly_mdn)` on the derived `poly_mdn` (Poly2 & "
        "LDMOS_XTOR & Dualgate & NCOMP)"
    ),
    "MDN.3b": "max channel length on the derived `ldnmos` region (`poly_mdn - mvsd`)",
    "MDN.4a": "channel width on the derived `ldnmos` gate-end edges",
    "MDN.4b": "channel width on the derived `ldnmos` gate-end edges",
    "MDN.5ai": (
        "derived PCOMP/NCOMP interaction "
        "(`pcomp_mdn.not_interacting(ncomp)`) and MVSD polygon output"
    ),
    "MDN.5aii": (
        "derived PCOMP/NCOMP interaction (`pcomp_mdn.interacting(ncomp)`) "
        "and MVSD polygon output"
    ),
    "MDN.5b": "spacing from the derived `ldnmos_source` region",
    "MDN.5c": "sized-extents and `separation(pcomp)` selector on derived NCOMP",
    "MDN.6": "Boolean union of NCOMP/Poly2/MVSD minus Dualgate; NCOMP is derived",
    "MDN.6a": (
        "`ncomp_ld.enclosed(dualgate)`: NCOMP is a derived layer (COMP inside Nplus)"
    ),
    "MDN.7": "derived NCOMP/NGATE Boolean union minus LDMOS_XTOR",
    "MDN.7a": (
        "`dualgate.not_outside(ldmos_xtor)` polygon selector plus a "
        "`dualgate & mvsd` term"
    ),
    "MDN.8a": "connectivity-aware `conn_separation` (same-potential Nwell)",
    "MDN.8b": "connectivity-aware `conn_separation` (different-potential Nwell)",
    "MDN.9": "separation to `ncomp.not_interacting(mvsd)`: NCOMP is derived",
    "MDN.10a": (
        "width on `poly2 & dualgate & ldmos_xtor.interacting(mvsd)`, a derived region"
    ),
    "MDN.10b": "edge selection on derived NCOMP and Poly2 edges",
    "MDN.10c": "min/max (`!=`) enclosure of derived NCOMP edges",
    "MDN.10d": "min/max field-poly space on derived NCOMP and sized regions",
    "MDN.10ei": "separation from `pcomp.not_interacting(ncomp)`: derived PCOMP/NCOMP",
    "MDN.10eii": "separation from `pcomp.interacting(ncomp)`: derived PCOMP/NCOMP",
    "MDN.10f": "`interacting(..., 2)` count selector on derived Poly2 minus Nplus",
    "MDN.11": (
        "min/max overlap on the derived LDMOS_XTOR & MVSD & COMP & Poly2 & Nplus region"
    ),
    "MDN.12": "enclosure of derived NCOMP in the drain direction (`transparent` mode)",
    "MDN.13a": "finger-length selector on the derived `poly_mdn - mvsd` region",
    "MDN.13b": "count selectors on derived source/drain regions",
    "MDN.13c": "count selectors on derived source/drain regions",
    "MDN.13d": "`pcomp.holes` guard-ring covering count selectors",
    "MDN.15a": "width on the derived `comp & ldmos_xtor & dualgate & mvsd` region",
    "MDN.15b": (
        "`contact.interacting(ncomp_mdn & mvsd) - (ncomp & mvsd)`: derived NCOMP"
    ),
    "MDN.17": "`pcomp.holes` guard-ring recommendation on derived regions",
    "MDP.1": (
        "`mvpsd.enclosed(poly_mdp)` on the derived `poly_mdp` (Poly2 & "
        "PCOMP inside LDMOS_XTOR)"
    ),
    "MDP.1a": "max channel length on the derived `ldpmos` region",
    "MDP.2": "channel width on the derived `ldpmos` gate-end edges",
    "MDP.3": "non-broken Nplus guard-ring (`nplus.holes`) polygon selectors",
    "MDP.3ai": "separation from `ncomp.not_interacting(pcomp)`: derived NCOMP/PCOMP",
    "MDP.3aii": "separation from `ncomp.interacting(pcomp)`: derived NCOMP/PCOMP",
    "MDP.3b": "separation between derived NCOMP and PCOMP-in-DNWELL regions",
    "MDP.3c": "`separation(pcomp.inside(dnwell))` selector on derived NCOMP",
    "MDP.3d": "metal-gap edge-length test on derived NCOMP",
    "MDP.4": "guard-ring edge/metal-gap test on derived PCOMP",
    "MDP.4a": "separation between derived PCOMP and `dnwell & dualgate & ldmos_xtor`",
    "MDP.4b": "DNWELL edge `centers` and `pcomp.holes` selectors",
    "MDP.5": "Boolean union of PCOMP/Poly2/MVPSD minus Dualgate; PCOMP is derived",
    "MDP.5a": (
        "`pcomp.inside(ldmos_xtor).enclosed(dualgate & ldmos_xtor)`: PCOMP is derived"
    ),
    "MDP.6a": "`ldmos_xtor.not_covering(dualgate)`: no polygon-selector check kind",
    "MDP.7": "separation to `nwell.outside(ldmos_xtor)`: polygon selector on Nwell",
    "MDP.8": (
        "separation to `ncomp.outside(ldmos_xtor)`: derived NCOMP and polygon selector"
    ),
    "MDP.9a": (
        "width on `poly2.inside(dnwell & dualgate & ldmos_xtor)`, a derived region"
    ),
    "MDP.9b": "edge selection on derived Poly2 edges",
    "MDP.9c": "min/max (`== 0.2um`) enclosure of COMP by derived Poly2 edges",
    "MDP.9d": "min/max field-poly space on derived sized MVPSD & PCOMP region",
    "MDP.9ei": "separation from the derived non-butted NCOMP region",
    "MDP.9eii": "separation from the derived butted NCOMP region",
    "MDP.9f": "`interacting(..., 2)` count selector on derived Poly2 minus Pplus",
    "MDP.10": "min/max (`== 0.4um`) MVPSD overlap of the derived channel region",
    "MDP.10a": "connectivity-aware `conn_space` (different-potential MVPSD space)",
    "MDP.10b": "connectivity-aware `conn_space` (same-potential MVPSD space)",
    "MDP.11": "enclosure of PCOMP edges by MVPSD edges: PCOMP is derived",
    "MDP.12": "DNWELL enclosure of derived NCOMP guard ring",
    "MDP.13a": "finger-length selector on derived Poly2 & PCOMP region",
    "MDP.13b": "count selectors on derived source/drain regions",
    "MDP.13c": "count selectors on derived source/drain regions",
    "MDP.15": (
        "separation between DNWELLs selected by `covering(mvpsd)` polygon selector"
    ),
    "MDP.16a": "width on the derived `comp.inside(mvpsd)` region",
    "MDP.16b": "`contact.interacting(pcomp inside mvpsd)` selector: derived PCOMP",
    "MDP.17a": "latch-up guard-ring recommendation on derived MVPSD/NCOMP regions",
    "MDP.17c": "latch-up guard-ring recommendation on derived DNWELL/NCOMP regions",
}
