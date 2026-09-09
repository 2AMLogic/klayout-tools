"""Normalized PDK cross-section/stackup dump (``klt pdk stackup``, issue #1609).

A GDSII/OASIS file is 2D: it carries ``(layer, datatype)`` polygons and no z
axis at all. Every external E&M/field-solver handoff therefore needs a second
input — the process **stackup**: each conductor's elevation above the
substrate and thickness, each dielectric's z-range and relative permittivity,
and each metal's sheet resistance/conductivity. This module is that input,
keyed by resolved PDK variant, and it is what
``docs/design/em-field-sim-spike.md``'s open question ("where the sky130
stackup table itself lives as a ``klt``-owned asset ... or a new ``klt pdk
stackup`` subcommand") asked for.

Curated vs. derived: why this is not a pure live parse
------------------------------------------------------

``klt pdk em-limits`` (:func:`klayout_tools.pdk.em_limits`) is a pure live
parse of the install's tech LEFs, and deliberately owns no table — the
install's own files are the only place its data is stated. That is **not**
true here. Verified against a real ``volare``-fetched ``sky130A``
(``open_pdks c6d73a3``): the tech LEFs
(``libs.ref/*/techlef/*__{min,nom,max}.tlef``) carry ``THICKNESS``,
``RESISTANCE RPERSQ`` (routing layers), and ``RESISTANCE`` (per-cut, via
layers) — but carry **no elevation and no dielectric constant at all**. The
``CAPACITANCE CPERSQDIST``/``EDGECAPACITANCE`` values they do carry already
bake geometry and permittivity together into per-unit-area/edge coefficients,
which a field solver cannot consume as an ε_r.

So this module layers two sources, and says per field which is which:

- **Derived, live, per run** — ``thickness_um``, ``sheet_resistance_ohm_per_sq``
  (routing layers), ``via_resistance_ohm`` (cut layers), read out of the
  resolved install's own tech LEFs via
  :func:`klayout_tools.lef_header.parse_lef_header`, using the same
  family-wide tech-LEF enumeration (:func:`klayout_tools.pdk._discover_tech_lefs`)
  ``em_limits`` uses. These are **corner-dependent** in real installs (sky130's
  ``met1`` sheet resistance is 0.105/0.125/0.145 Ω/□ at min/nom/max), hence
  the ``corner`` argument.
- **Curated, checked in here** — ``z0_um``/``z1_um`` elevation and dielectric
  ``permittivity``, which no tech LEF states. Each curated entry cites the
  published, open, non-NDA'd source it was transcribed from (see
  :data:`_SKY130_STACKUP`).

An install whose family has no curated table entry raises
:class:`PdkStackupError` rather than emitting a partial stack — a stackup
missing its z axis is not a usable field-solver input, and silently emitting
one would be worse than saying so.
"""

from __future__ import annotations

import math
from typing import Any

from .lef_header import parse_lef_header
from .pdk import PdkNotFoundError, _discover_tech_lefs, _read_text, find_pdk

__all__ = [
    "PdkStackupError",
    "PdkNotFoundError",
    "DEFAULT_CORNER",
    "DEFAULT_THICKNESS_SOURCE",
    "THICKNESS_SOURCES",
    "supported_families",
    "stackup",
]

#: Default parasitic corner. Mirrors :data:`klayout_tools.pdk._NOMINAL_TECH_LEF_CORNER`
#: -- an open_pdks tech LEF's ``min``/``nom``/``max`` suffix names a *parasitic*
#: corner (routing-layer resistance/capacitance), so the nominal one is the
#: right default for a geometry+RC dump.
DEFAULT_CORNER = "nom"

#: Accepted ``thickness`` arguments, and the default.
#:
#: ``"curated"`` (default) reports the curated stack's own film thicknesses,
#: which are contiguous by construction (``z1_um`` of each level is exactly
#: the ``z0_um`` of the next). ``"tech-lef"`` reports the install's own
#: declared ``THICKNESS`` instead. They are two different published numbers
#: for the same physical film -- open_pdks states sky130's ``met1`` as 0.36 um
#: in its magic elevation table (the 3D-extraction geometry) and 0.35 um in
#: its tech LEFs (the P&R RC-estimation value) -- and mixing the tech LEF's
#: thickness into the curated elevation grid opens ~10 nm gaps between each
#: metal and the via above it, which a field solver would mesh as an open
#: circuit. Hence the default: a gap-free stack is the safer thing to hand a
#: solver, and the tech-LEF reading is never hidden (it is always reported as
#: ``lef_thickness_um``, whichever mode is selected) or unavailable (this
#: flag selects it).
THICKNESS_SOURCES = ("curated", "tech-lef")
DEFAULT_THICKNESS_SOURCE = "curated"

#: Vacuum permittivity in attofarads per micrometre (8.854e-12 F/m = 8.854
#: aF/um). Used only by the ε_r corroboration arithmetic quoted in
#: :data:`_SKY130_STACKUP`'s source notes, kept here so the check in
#: ``tests/test_pdk_stackup.py`` can re-derive it rather than trusting prose.
VACUUM_PERMITTIVITY_AF_PER_UM = 8.8541878128

#: Tolerance for calling two tech LEFs' values for the same field "equal".
#: Same rationale as :data:`klayout_tools.pdk._EM_VALUE_ABS_TOL`.
_VALUE_ABS_TOL = 1e-9

#: Tolerance for "the curated stack thickness and the tech LEF's declared
#: ``THICKNESS`` agree". Deliberately loose (1 nm) -- these are two different
#: published numbers for the same physical film (sky130's ``met1`` is 0.36 um
#: in open_pdks' magic tech file vs. 0.35 um in its tech LEFs), and the point
#: of the check is to *surface* that spread as a warning, not to hide it.
_THICKNESS_ABS_TOL = 1e-9


# --------------------------------------------------------------------------- #
# Curated data table.
#
# PROVENANCE RULE (CLAUDE.md, "Open PDKs only"): every number below is
# transcribed from a file the open PDK itself ships, or from a value stated
# in SkyWater/Google/efabless's own published open documentation. Nothing
# here is NDA'd, and nothing here is guessed -- each entry names its source.
# --------------------------------------------------------------------------- #

#: sky130 BEOL stack-up.
#:
#: **Elevation/thickness source** — ``libs.tech/magic/sky130A.tech``, the
#: ``height <types> <z0> <thickness>`` stanza of the ``cifoutput``-adjacent 3D
#: section (lines 5048-5077 of the ``open_pdks c6d73a3`` build; the same
#: stanza is present in every open_pdks sky130A/sky130B build). open_pdks is
#: efabless's own published, Apache-2.0, non-NDA sky130 distribution, and this
#: is the elevation table magic itself uses for 3D extraction, so it is the
#: authoritative open statement of the sky130 z axis. Transcribed verbatim:
#:
#:     height allli   0.9361 0.10     height v3     3.6311 0.39
#:     height mcon    1.0361 0.34     height allm4  4.0211 0.845
#:     height allm1   1.3761 0.36     height v4     4.8661 0.505
#:     height v1      1.7361 0.27     height allm5  5.3711 1.26
#:     height allm2   2.0061 0.36
#:     height v2      2.3661 0.42
#:     height allm3   2.7861 0.845
#:
#: The stanza is contiguous by construction (``z0[n+1] == z0[n] + t[n]`` for
#: every adjacent pair above — asserted in ``tests/test_pdk_stackup.py``), so
#: the dielectric slab boundaries below are exactly the conductor boundaries;
#: no independent dielectric elevation had to be invented.
#:
#: **Dielectric names/materials source** —
#: ``libs.tech/klayout/tech/xsect/sky130.xs`` (the KLayout XSection process
#: script open_pdks ships for sky130), whose process section names the same
#: films in the same order: ``bpsg_thickness = 0.94`` (pre-metal dielectric,
#: with ``li_bottom = bpsg_thickness``), then ``ild2_thickness`` …
#: ``ild6_thickness`` between successive metals, then ``passv_thickness =
#: 0.6`` of passivation over the top metal. Its boundaries agree with the
#: magic table to within 5 nm (0.94 vs 0.9361, 1.37 vs 1.3761, 2.00 vs 2.0061,
#: 2.78 vs 2.7861, 4.015 vs 4.0211, 5.365 vs 5.3711); the magic values are
#: used here so one self-consistent coordinate system is emitted.
#:
#: **GDS layer/datatype source** — ``libs.tech/klayout/tech/sky130A.map``
#: (open_pdks' own LEF/DEF→GDS layer map) cross-checked against
#: ``libs.tech/klayout/d25/sky130.lyd25``: li1 67/20, mcon 67/44, met1 68/20,
#: via 68/44, met2 69/20, via2 69/44, met3 70/20, via3 70/44, met4 71/20,
#: via4 71/44, met5 72/20.
#:
#: **Permittivity source** — 3.9, the nominal relative permittivity of the
#: SiO₂-based interlayer dielectric of an aluminium BEOL. Corroborated against
#: this same install's own ``sky130A.tech`` parallel-plate area-capacitance
#: coefficients, which are stated in aF/um² to the substrate and therefore
#: satisfy ``C_area = ε0·ε_r / z0``:
#:
#:     defaultareacap allli  locali 36.99  @ z0 0.9361  ->  ε_r = 3.91
#:     defaultareacap allm1  metal1 25.78  @ z0 1.3761  ->  ε_r = 4.01
#:     defaultareacap allm2  metal2 17.50  @ z0 2.0061  ->  ε_r = 3.97
#:
#: (``ε_r = C_area · z0 / ε0`` with ε0 = 8.854 aF/um; re-derived in
#: ``tests/test_pdk_stackup.py`` so the claim is checked, not just asserted.)
#: The passivation nitride's ε_r is **not** published in any open sky130
#: source this repo can cite, so it is emitted as ``null`` rather than
#: guessed. No loss tangent is published for any sky130 dielectric, so
#: ``loss_tangent`` is ``null`` throughout.
#:
#: **Deliberately out of scope** — FEOL conductors (poly and below). The magic
#: ``height`` stanza's FEOL entries (``dnwell``/``nwell``/``alldiff``/
#: ``allpoly``) use an origin that is not consistent with its own BEOL
#: entries (they place poly's top at 0.5062 um while placing li1's bottom at
#: 0.9361 um above a substrate the BEOL entries put at 0), so transcribing
#: them into this one coordinate system would emit a stack that is wrong
#: rather than merely incomplete. The redistribution layer (magic
#: ``mrdlc``/``mrdl``) is likewise omitted: it is absent from both the tech
#: LEFs and ``sky130A.map``, so neither its electrical model nor its GDS
#: layer could be stated here.
_SKY130_STACKUP: dict[str, Any] = {
    "family": "sky130",
    "description": "sky130 BEOL stack-up (local interconnect through met5)",
    "variant_prefixes": ("sky130",),
    "references": [
        "open_pdks <PDK_ROOT>/<variant>/libs.tech/magic/<variant>.tech"
        " -- `height <types> <z0_um> <thickness_um>` stanza (elevation,"
        " thickness) and `defaultareacap` coefficients (permittivity"
        " corroboration)",
        "open_pdks <PDK_ROOT>/<variant>/libs.tech/klayout/tech/xsect/sky130.xs"
        " -- process section (dielectric film names/order/thicknesses)",
        "open_pdks <PDK_ROOT>/<variant>/libs.tech/klayout/tech/<variant>.map"
        " -- GDS layer/datatype per routing and cut layer",
    ],
    # z = 0 is the top of the silicon substrate; every elevation below is in
    # micrometres above it.
    "substrate": {
        "name": "substrate",
        "material": "silicon",
        "z1_um": 0.0,
        "permittivity": 11.9,
        "loss_tangent": None,
        "source": (
            "relative permittivity of crystalline silicon at 300 K (textbook"
            " value, not sky130-specific); z = 0 is this module's elevation"
            " origin, the top of the substrate, matching the origin"
            " open_pdks' magic tech file uses for its BEOL `height` entries"
        ),
    },
    # (name, kind, lef_layer, gds_layer, material, z0_um, thickness_um)
    "conductors": [
        ("li1", "conductor", "li1", "67/20", "titanium nitride", 0.9361, 0.10),
        ("mcon", "via", "mcon", "67/44", "tungsten", 1.0361, 0.34),
        ("met1", "conductor", "met1", "68/20", "aluminium", 1.3761, 0.36),
        ("via", "via", "via", "68/44", "tungsten", 1.7361, 0.27),
        ("met2", "conductor", "met2", "69/20", "aluminium", 2.0061, 0.36),
        ("via2", "via", "via2", "69/44", "tungsten", 2.3661, 0.42),
        ("met3", "conductor", "met3", "70/20", "aluminium", 2.7861, 0.845),
        ("via3", "via", "via3", "70/44", "tungsten", 3.6311, 0.39),
        ("met4", "conductor", "met4", "71/20", "aluminium", 4.0211, 0.845),
        ("via4", "via", "via4", "71/44", "tungsten", 4.8661, 0.505),
        ("met5", "conductor", "met5", "72/20", "aluminium", 5.3711, 1.26),
    ],
    # (name, material, z0_um, z1_um, permittivity, note)
    #
    # A contiguous z partition from the substrate surface to the top of the
    # passivation. Conductors are *embedded in* these slabs (a conductor's
    # z-range lies inside exactly one slab), which is what a field solver
    # needs: the slab states what fills the space laterally between wires on
    # that level, not just between levels.
    "dielectrics": [
        ("pmd", "borophosphosilicate glass (BPSG)", 0.0, 0.9361, 3.9, None),
        ("ild2", "silicon dioxide", 0.9361, 1.3761, 3.9, None),
        ("ild3", "silicon dioxide", 1.3761, 2.0061, 3.9, None),
        ("ild4", "silicon dioxide", 2.0061, 2.7861, 3.9, None),
        ("ild5", "silicon dioxide", 2.7861, 4.0211, 3.9, None),
        ("ild6", "silicon dioxide", 4.0211, 5.3711, 3.9, None),
        (
            "passivation",
            "silicon nitride",
            5.3711,
            7.2311,
            None,
            "spans the met5 level (5.3711-6.6311) plus the 0.6 um nitride"
            " overcoat above it. sky130.xs deposits this nitride"
            " *conformally* over met5 (`deposit(passv_thickness,"
            " passv_thickness, mode: :square)`), so treating the whole level"
            " as one uniform slab is an approximation for widely-spaced top"
            " metal; its permittivity is null regardless, since no open"
            " sky130 source states one",
        ),
    ],
}

#: gf180mcu BEOL stack-up (issue #1616).
#:
#: **Elevation/thickness source** — ``libs.tech/magic/gf180mcuD.tech``
#: (verified against a real ``volare``-fetched ``gf180mcuD``; the same
#: filename pattern and ``height`` stanza is present, byte-for-byte on the
#: BEOL entries, in every open_pdks gf180mcuA/B/C/D build). This is
#: GlobalFoundries/Google's own published, Apache-2.0, non-NDA gf180mcu
#: distribution -- the same open_pdks tree ``_SKY130_STACKUP`` cites -- and
#: this is the elevation table magic itself uses for 3D extraction, so it is
#: the authoritative open statement of the gf180mcu z axis. Transcribed
#: verbatim (lines 2982-2990 of the ``open_pdks`` build volare fetched
#: 2026-09-09):
#:
#:     height allm1  1.23  0.55      height allm4  4.68  0.55
#:     height via    1.78  0.60      height via4   5.23  0.60
#:     height allm2  2.38  0.55      height allm5  5.83  1.0025
#:     height via2   2.93  0.60
#:     height allm3  3.53  0.55
#:     height via3   4.08  0.60
#:
#: Contiguous by construction, exactly like sky130's stanza (asserted in
#: ``tests/test_pdk_stackup.py``). Unlike sky130, gf180mcu has **no
#: local-interconnect layer** (no ``li1``/``mcon`` equivalent -- the FEOL
#: contact ``pc`` lands directly on poly/diffusion, not on a separate
#: interconnect level) and its five metal levels are a uniform 0.55 um
#: (met5 excepted, at 1.0025 um) rather than sky130's thin/thick split, so
#: this is a different stack shape, not a mechanical renumbering of
#: sky130's.
#:
#: **Dielectric names/materials source** -- **not independently confirmed**.
#: This install ships no KLayout XSection script analogous to sky130's
#: ``libs.tech/klayout/tech/xsect/sky130.xs`` (searched the full
#: ``libs.tech`` tree of a real volare-fetched gf180mcuD: no ``.xs``, no
#: ``.lyt``/``.lyp`` naming interlayer dielectric films). The slab
#: boundaries below are therefore induced purely from the ``height``
#: stanza's own contiguity (each conductor's z-range sits inside exactly
#: one slab, asserted in ``tests/test_pdk_stackup.py``, same as sky130),
#: but the film *material* name is a generic "silicon dioxide" BEOL
#: interlayer dielectric assumption for a conventional (non-low-k) 180 nm
#: process -- not a name transcribed from a gf180mcu-specific published
#: source, unlike sky130's xs-script-cited "borophosphosilicate glass"/
#: "silicon nitride". No open gf180mcu source states a passivation/overglass
#: thickness above met5 either (the ``height`` stanza's last BEOL entry is
#: met5 itself), so -- unlike sky130's ``passivation`` slab -- the top
#: dielectric here (``ild6``) stops at the top of met5 rather than
#: extending over an unstated overcoat.
#:
#: **GDS layer/datatype source** -- ``libs.tech/magic/gf180mcuD-GDS.tech``
#: (the ``calma <LAYER> <layer> <datatype>`` statements of open_pdks' own
#: magic-to-GDS layer map; gf180mcu ships no ``klayout/`` layer map
#: analogous to sky130's ``sky130A.map``, so this is the closest open,
#: non-NDA'd, install-native source): ``METAL1 34/0``, ``VIA1 35/0``,
#: ``METAL2 36/0``, ``VIA2 38/0``, ``METAL3 42/0``, ``VIA3 40/0``,
#: ``METAL4 46/0``, ``VIA4 41/0``, ``METAL5 81/0``.
#:
#: **Tech-LEF layer names** -- ``libs.ref/<cell_library>/techlef/*.tlef``
#: names its routing/cut layers ``Metal1``..``Metal5``/``Via1``..``Via4``
#: (mixed case, verified against a real ``gf180mcu_fd_sc_mcu9t5v0__nom.tlef``)
#: -- the ``lef_layer`` values below match that case exactly, since
#: :func:`_measure_tech_lefs` keys on the tech LEF's own layer name
#: verbatim.
#:
#: **Permittivity source** -- corroborated the same way ``_SKY130_STACKUP``'s
#: 3.9 is: against this same install's own ``gf180mcuD.tech``
#: ``defaultareacap`` coefficients (the first, unconditioned ``variants ()``
#: "Nominal capacitances" block, not the ``(hrhc),(lrhc)``/``(hrlc),(lrlc)``
#: corner-specific blocks later in the same file), which are stated in
#: aF/um^2 to the substrate/well plane and therefore satisfy
#: ``C_area = ε0·ε_r / z0``:
#:
#:     defaultareacap allm1 metal1 29.304  @ z0 1.23  ->  ε_r = 4.07
#:     defaultareacap allm2 metal2 15.016  @ z0 2.38  ->  ε_r = 4.04
#:     defaultareacap allm3 metal3 10.094  @ z0 3.53  ->  ε_r = 4.02
#:     defaultareacap allm4 metal4  7.602  @ z0 4.68  ->  ε_r = 4.02
#:     defaultareacap allm5 metal5  5.798  @ z0 5.83  ->  ε_r = 3.82
#:
#: (``ε_r = C_area · z0 / ε0`` with ε0 = 8.854 aF/um, re-derived in
#: ``tests/test_pdk_stackup.py``.) All five agree to within 7% of 4.0, the
#: nominal relative permittivity of a conventional (non-low-k) SiO2-based
#: BEOL interlayer dielectric -- gf180mcu is a different, older process node
#: than sky130 and this is a different (higher) nominal ε_r than sky130's
#: 3.9, not a copy of it. No open gf180mcu source states the loss tangent of
#: any dielectric, so ``loss_tangent`` is ``null`` throughout, same as
#: sky130.
#:
#: **Deliberately out of scope** -- FEOL conductors (poly and below): the
#: magic ``height`` stanza's FEOL entries (``dnwell``/``nwell,pwell``/
#: ``alldiff``/``allpoly``/``alldiffcont``/``pc``) share the same
#: origin-inconsistency problem sky130's FEOL entries have (see
#: ``_SKY130_STACKUP``'s own "Deliberately out of scope" note) -- they are
#: not transcribed here.
_GF180MCU_STACKUP: dict[str, Any] = {
    "family": "gf180mcu",
    "description": (
        "gf180mcu BEOL stack-up (metal1 through metal5, no local interconnect)"
    ),
    "variant_prefixes": ("gf180mcu",),
    "references": [
        "open_pdks <PDK_ROOT>/<variant>/libs.tech/magic/<variant>.tech"
        " -- `height <types> <z0_um> <thickness_um>` stanza (elevation,"
        " thickness) and the first (unconditioned) `variants ()`"
        " `defaultareacap` block (permittivity corroboration)",
        "open_pdks <PDK_ROOT>/<variant>/libs.tech/magic/<variant>-GDS.tech"
        " -- `calma <LAYER> <layer> <datatype>` statements (GDS"
        " layer/datatype per routing and cut layer); gf180mcu ships no"
        " KLayout XSection script analogous to sky130's `xsect/sky130.xs`,"
        " so dielectric film material names are a generic BEOL-oxide"
        " assumption, not independently confirmed per layer (see the"
        " per-entry dielectric notes)",
    ],
    # z = 0 is the top of the silicon substrate; every elevation below is in
    # micrometres above it -- the same origin convention _SKY130_STACKUP
    # uses, and the one open_pdks' magic `height` stanza itself uses.
    "substrate": {
        "name": "substrate",
        "material": "silicon",
        "z1_um": 0.0,
        "permittivity": 11.9,
        "loss_tangent": None,
        "source": (
            "relative permittivity of crystalline silicon at 300 K (textbook"
            " value, not gf180mcu-specific); z = 0 is this module's elevation"
            " origin, the top of the substrate, matching the origin"
            " open_pdks' magic tech file uses for its BEOL `height` entries"
        ),
    },
    # (name, kind, lef_layer, gds_layer, material, z0_um, thickness_um)
    "conductors": [
        ("met1", "conductor", "Metal1", "34/0", "aluminium", 1.23, 0.55),
        ("via1", "via", "Via1", "35/0", "tungsten", 1.78, 0.60),
        ("met2", "conductor", "Metal2", "36/0", "aluminium", 2.38, 0.55),
        ("via2", "via", "Via2", "38/0", "tungsten", 2.93, 0.60),
        ("met3", "conductor", "Metal3", "42/0", "aluminium", 3.53, 0.55),
        ("via3", "via", "Via3", "40/0", "tungsten", 4.08, 0.60),
        ("met4", "conductor", "Metal4", "46/0", "aluminium", 4.68, 0.55),
        ("via4", "via", "Via4", "41/0", "tungsten", 5.23, 0.60),
        ("met5", "conductor", "Metal5", "81/0", "aluminium", 5.83, 1.0025),
    ],
    # (name, material, z0_um, z1_um, permittivity, note)
    #
    # A contiguous z partition from the substrate surface to the top of
    # met5. Conductors are *embedded in* these slabs, same invariant
    # _SKY130_STACKUP's dielectrics satisfy (asserted in
    # tests/test_pdk_stackup.py).
    "dielectrics": [
        (
            "pmd",
            "silicon dioxide",
            0.0,
            1.23,
            4.0,
            "material name is a generic pre-metal-dielectric assumption --"
            " no XSection script or comparable open gf180mcu source states"
            " it (see the curated-table provenance note)",
        ),
        ("ild2", "silicon dioxide", 1.23, 2.38, 4.0, None),
        ("ild3", "silicon dioxide", 2.38, 3.53, 4.0, None),
        ("ild4", "silicon dioxide", 3.53, 4.68, 4.0, None),
        ("ild5", "silicon dioxide", 4.68, 5.83, 4.0, None),
        (
            "ild6",
            "silicon dioxide",
            5.83,
            6.8325,
            4.0,
            "spans only the met5 level (5.83-6.8325); unlike"
            " _SKY130_STACKUP's top `passivation` slab, this does not"
            " extend over a passivation/overglass overcoat -- no open"
            " gf180mcu source states one's thickness, and this module never"
            " invents an unstated elevation",
        ),
    ],
}

#: Every curated family, keyed by family name. Adding a family is a pure data
#: change plus its own provenance block -- no logic below is family-specific.
_STACKUPS: dict[str, dict[str, Any]] = {
    "gf180mcu": _GF180MCU_STACKUP,
    "sky130": _SKY130_STACKUP,
}


class PdkStackupError(Exception):
    """Raised when a PDK install resolves but no curated stackup covers it.

    Deliberately distinct from :class:`klayout_tools.pdk.PdkNotFoundError`:
    "there is no PDK here" and "there is a PDK here, but this repo has not
    curated its cross-section" are different problems with different fixes,
    even though both surface as the same exit code 1 error envelope.
    """


def supported_families() -> list[str]:
    """Every PDK family a curated stackup table exists for, sorted."""
    return sorted(_STACKUPS)


def _resolve_family(variant: str) -> dict[str, Any]:
    """Map a resolved variant name (``sky130A``) to its curated family table.

    Matching is by documented variant prefix rather than an exhaustive
    variant list so a new same-family variant (a future ``sky130C``) is
    covered without a code change, while a genuinely different process
    (``gf180mcuD``, ``ihp-sg13g2``) still fails loudly.
    """
    lowered = variant.lower()
    for table in _STACKUPS.values():
        if any(lowered.startswith(prefix) for prefix in table["variant_prefixes"]):
            return table
    raise PdkStackupError(
        f"no curated stackup for PDK variant '{variant}': this repo curates"
        f" cross-section data (elevation, dielectric permittivity) per PDK"
        f" family, and tech LEFs do not carry those fields, so an uncurated"
        f" variant cannot be reported at all. Curated families:"
        f" {', '.join(supported_families())}"
    )


def _measure_tech_lefs(
    sources: list[dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """Parse every tech LEF in ``sources``, keyed by LEF layer name.

    Each value is one entry per source that declared the layer, carrying the
    live-derived fields this module reports: ``thickness_um``,
    ``resistance_rpersq`` (routing layers) and ``resistance_ohms`` (per cut,
    via layers).
    """
    measured: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        text = _read_text(source["tech_lef"])
        if text is None:
            continue
        for layer in parse_lef_header(text)["layers"]:
            measured.setdefault(layer["name"], []).append(
                {
                    "cell_library": source["cell_library"],
                    "corner": source["corner"],
                    "tech_lef": source["tech_lef"],
                    "thickness_um": layer["thickness_um"],
                    "resistance_rpersq": layer["resistance_rpersq"],
                    "resistance_ohms": layer["resistance_ohms"],
                }
            )
    return measured


def _reduce(
    entries: list[dict[str, Any]], field: str, *, pick: str
) -> tuple[float | None, bool | None]:
    """Reduce one live-derived field across every tech LEF that declared it.

    Returns ``(value, agrees)``. ``agrees`` is ``None`` when no source
    declared the field at all, ``True`` when every source that did agrees
    within :data:`_VALUE_ABS_TOL`, else ``False``.

    When sources within one corner disagree, ``pick`` selects the **more
    resistive** (pessimistic) reading, so a disagreement never silently
    flatters the resulting electrical model: ``"min"`` for a thickness (a
    thinner film is more resistive), ``"max"`` for a resistance. This mirrors
    :func:`klayout_tools.pdk._summarize_em_field`'s ``conservative`` pick,
    which likewise resolves cross-library disagreement in the safe direction
    rather than by file ordering. The disagreement itself is always reported
    (``agrees``, plus a top-level warning), never only papered over.
    """
    present = [entry[field] for entry in entries if entry[field] is not None]
    if not present:
        return None, None
    agrees = all(
        math.isclose(value, present[0], abs_tol=_VALUE_ABS_TOL) for value in present
    )
    return (min(present) if pick == "min" else max(present)), agrees


def _conductivity(
    sheet_resistance_ohm_per_sq: float | None, thickness_um: float | None
) -> float | None:
    """Bulk conductivity in S/m from a sheet resistance and the **emitted**
    thickness: ``sigma = 1 / (R_s * t)``.

    Deliberately paired with the ``thickness_um`` this command emits (not with
    the tech LEF's own declared thickness, when the two differ): a field
    solver meshes the emitted geometry, so pairing sigma with the emitted
    thickness is what makes the solved sheet resistance reproduce the PDK's
    own ``RESISTANCE RPERSQ``. ``None`` when either input is missing or zero.
    """
    if not sheet_resistance_ohm_per_sq or not thickness_um:
        return None
    return 1.0 / (sheet_resistance_ohm_per_sq * thickness_um * 1e-6)


def stackup(
    variant: str | None = None,
    root: str | None = None,
    corner: str = DEFAULT_CORNER,
    thickness: str = DEFAULT_THICKNESS_SOURCE,
) -> dict[str, Any]:
    """Report a resolved PDK variant's cross-section/stackup (issue #1609).

    Resolves one PDK install/variant exactly as :func:`klayout_tools.pdk.find_pdk`
    does (same ``variant``/``root`` arguments, same :class:`PdkNotFoundError`
    when nothing resolves), maps it to its curated family table (see the
    module docstring for the curated-vs-derived split), and overlays the
    live-derived electrical fields read out of the install's own tech LEFs at
    the requested parasitic ``corner``.

    Returns a dict matching the documented JSON schema (``docs/cli/pdk.md``)::

        {
            "schema_version": 1,
            "pdk": <variant name>,
            "root": <absolute install root>,
            "family": <curated family name>,
            "corner": <requested parasitic corner>,
            "thickness_source": "curated" | "tech-lef",  # requested
            "available_corners": [<corner>, ...],
            "curated_source": {"description": str, "references": [str, ...]},
            "sources": [{"cell_library", "corner", "tech_lef"}, ...],
            "substrate": {
                "name", "material", "z1_um", "permittivity", "loss_tangent",
                "source"
            },
            "conductors": [
                {
                    "name", "kind", "material", "lef_layer", "gds_layer",
                    "z0_um", "z1_um", "thickness_um", "thickness_source",
                    "curated_thickness_um", "lef_thickness_um",
                    "sheet_resistance_ohm_per_sq", "via_resistance_ohm",
                    "conductivity_S_per_m", "agrees"
                },
                ...
            ],
            "dielectrics": [
                {"name", "material", "z0_um", "z1_um", "thickness_um",
                 "permittivity", "loss_tangent", "note"},
                ...
            ],
            "warnings": [str, ...],
        }

    ``conductors``/``dielectrics`` are both emitted in ascending-z order, and
    ``dielectrics`` is a contiguous partition of z from the substrate surface
    (``z0_um == 0``) to the top of the passivation, with conductors embedded
    inside those slabs (see :data:`_SKY130_STACKUP`).

    ``thickness`` selects which of the two published film thicknesses drives
    the emitted geometry (``thickness_um`` and therefore ``z1_um``) --
    ``"curated"`` (default, gap-free) or ``"tech-lef"`` (the install's own
    declared ``THICKNESS``); see :data:`THICKNESS_SOURCES` for why the
    default is the curated one. Both readings are always reported
    (``curated_thickness_um``/``lef_thickness_um``) whichever is selected,
    and a disagreement between them is always warned about, so neither is
    ever hidden. ``thickness_source`` reports which one the emitted geometry
    actually used -- requesting ``"tech-lef"`` for a layer no tech LEF
    declares a ``THICKNESS`` for falls back to ``"curated"`` rather than
    emitting a null-thickness layer.

    Raises :class:`PdkNotFoundError` when no install resolves,
    :class:`PdkStackupError` when one resolves but its family has no curated
    table, and :class:`ValueError` for an unknown ``thickness`` argument.
    """
    if thickness not in THICKNESS_SOURCES:
        raise ValueError(
            f"unknown thickness source '{thickness}':"
            f" expected one of {', '.join(THICKNESS_SOURCES)}"
        )
    info = find_pdk(variant=variant, root=root)
    table = _resolve_family(info["variant"])

    libs_ref = info["assets"]["libs_ref"]
    all_sources = _discover_tech_lefs(libs_ref) if libs_ref is not None else []
    available_corners = sorted({source["corner"] for source in all_sources})
    sources = [source for source in all_sources if source["corner"] == corner]

    warnings: list[str] = []
    if not all_sources:
        warnings.append(
            "no tech LEF found under this install's libs.ref: thickness and"
            " resistance fall back to the curated table and conductivity is"
            " unavailable"
        )
    elif not sources:
        warnings.append(
            f"no tech LEF for corner '{corner}' (available:"
            f" {', '.join(available_corners)}): thickness and resistance fall"
            f" back to the curated table and conductivity is unavailable"
        )

    measured = _measure_tech_lefs(sources)

    conductors: list[dict[str, Any]] = []
    for name, kind, lef_layer, gds_layer, material, z0, curated_t in table[
        "conductors"
    ]:
        entries = measured.get(lef_layer, []) if lef_layer else []
        lef_thickness, thickness_agrees = _reduce(entries, "thickness_um", pick="min")
        sheet_resistance, resistance_agrees = _reduce(
            entries, "resistance_rpersq", pick="max"
        )
        via_resistance, via_agrees = _reduce(entries, "resistance_ohms", pick="max")

        if thickness == "tech-lef" and lef_thickness is not None:
            effective_t, thickness_source = lef_thickness, "tech-lef"
        else:
            effective_t, thickness_source = curated_t, "curated"

        if lef_thickness is not None and not math.isclose(
            lef_thickness, curated_t, abs_tol=_THICKNESS_ABS_TOL
        ):
            warnings.append(
                f"{name}: tech LEF THICKNESS {lef_thickness:g} um differs from"
                f" the curated stack thickness {curated_t:g} um"
                + (
                    "; z1_um follows the tech LEF, so this level is not flush"
                    " with the next curated z0_um"
                    if thickness_source == "tech-lef"
                    else "; z1_um follows the curated (gap-free) value"
                )
            )
        for field, agrees in (
            ("THICKNESS", thickness_agrees),
            ("RESISTANCE RPERSQ", resistance_agrees),
            ("RESISTANCE", via_agrees),
        ):
            if agrees is False:
                warnings.append(
                    f"{name}: tech LEFs at corner '{corner}' disagree on"
                    f" {field}; the more resistive value is reported"
                )

        conductors.append(
            {
                "name": name,
                "kind": kind,
                "material": material,
                "lef_layer": lef_layer,
                "gds_layer": gds_layer,
                "z0_um": z0,
                "z1_um": z0 + effective_t,
                "thickness_um": effective_t,
                "thickness_source": thickness_source,
                "curated_thickness_um": curated_t,
                "lef_thickness_um": lef_thickness,
                "sheet_resistance_ohm_per_sq": sheet_resistance,
                "via_resistance_ohm": via_resistance,
                "conductivity_S_per_m": _conductivity(sheet_resistance, effective_t),
                "agrees": {
                    "thickness_um": thickness_agrees,
                    "sheet_resistance_ohm_per_sq": resistance_agrees,
                    "via_resistance_ohm": via_agrees,
                },
            }
        )

    dielectrics = [
        {
            "name": name,
            "material": material,
            "z0_um": z0,
            "z1_um": z1,
            "thickness_um": z1 - z0,
            "permittivity": permittivity,
            "loss_tangent": None,
            "note": note,
        }
        for name, material, z0, z1, permittivity, note in table["dielectrics"]
    ]

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "family": table["family"],
        "corner": corner,
        "thickness_source": thickness,
        "available_corners": available_corners,
        "curated_source": {
            "description": table["description"],
            "references": list(table["references"]),
        },
        "sources": sources,
        "substrate": dict(table["substrate"]),
        "conductors": conductors,
        "dielectrics": dielectrics,
        "warnings": warnings,
    }
