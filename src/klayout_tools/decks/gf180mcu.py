"""gf180mcu DRC deck: a curated subset of the official rule set.

Rule thresholds below are transcribed from the published GlobalFoundries
180nm MCU ("gf180mcu") **Design Rule Manual** (DRM), fetched directly from
the canonical source repository for this issue:

- https://github.com/google/gf180mcu-pdk (Apache License 2.0), commit
  ``de3240d`` (2023-05-31) — the DRM lives at
  ``docs/physical_verification/design_manual/`` as reStructuredText
  sections with the numeric rule tables as CSV (``tables_clear/*.csv``).
  Sections/tables used below:

  - ``drm_07_06.rst`` / ``tables_clear/14_COMP33_1.csv`` — 7.5 Comp (``DF.*``)
  - ``drm_07_08.rst`` / ``tables_clear/16_Poly2_42.csv`` — 7.7 Poly2 (``PL.*``)
  - ``drm_07_13.rst`` / ``tables_clear/21_Contact_56.csv`` — 7.12 Contact (``CO.*``)
  - ``drm_07_14.rst`` / ``tables_clear/22_Metaln_58.csv`` — 7.13 Metaln (``Mn.*``)

Each rule's docstring below cites the exact source rule id (e.g. ``"DF.1a"``)
and its DRM description, so values can be re-verified against a fresh
checkout of that repo at any time. Unless noted otherwise, the **3.3V
column** value is used (this deck does not model the 5V/6V high-voltage
variants).

Provenance note (why this deck isn't transcribed from a ``.lydrc`` script
the way ``sky130.py`` was): the companion KLayout-runnable DRC deck lives in
https://github.com/google/globalfoundries-pdk-libs-gf180mcu_fd_pv (also
Apache-2.0; commit ``999a6ff``, 2023-06-11), under
``klayout/drc/rule_decks/``. As open-sourced at that commit, ``main.drc``
only defines layer/connectivity setup (confirming the layer/datatype pairs
used below) and the per-feature rule files in that directory
(``drc_bjt.drc``, ``dualgate.drc``, ``hres.drc``, ``mcell.drc``, etc.) cover
specialised devices, not the core FEOL/BEOL width/space/enclosure checks
this deck curates — those aren't present as executable ``.output(...)``
statements in this snapshot of that repo. So, unlike sky130's rule ids
(transcribed verbatim from live ``.lydrc`` script lines), the values here
are transcribed from the DRM's published numeric tables instead, citing the
DRM's own rule ids (which the ``.lydrc`` deck would use for its own
``.output(...)`` calls once/if implemented).

This is *not* a full transcription of the official rule manual (hundreds of
rules across dozens of layers, plus 5V/6V variants, well/tap rules, DFM
guidelines, etc.) — mirrors the "curated starter subset" scope guard sky130
documents: width/space/enclosure checks across poly2/comp/contact/metal1,
wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) for a second PDK. Coverage is
expected to grow incrementally in follow-on issues.

Four rules below approximate the official DRM rule in some way (each is
called out again in its own docstring below); the threshold *values* used
are always the real, unmodified DRM values:

- ``comp.space.1``: the official ``DF.3a`` text scopes to a substrate-tap
  butting context; approximated as a general COMP-to-COMP spacing check on
  the whole ``comp`` drawn layer.
- ``poly2.space.1``: the official ``PL.3a`` splits into "space on COMP" vs.
  "space on field" sub-cases (context our engine can't distinguish without
  a boolean layer expression); both sub-cases share the same 3.3V value
  (0.24um), so no threshold change is needed to unify them.
- ``poly2.width.1``: the official deck holds gate poly2 (``poly2 AND comp``,
  ``PL.2``, 0.28um channel length) to a stricter minimum than general
  interconnect poly2 (``PL.1``, 0.18um); approximated using the more
  permissive ``PL.1`` value across the whole ``poly2`` drawn layer, since
  isolating gate poly2 requires the same compound layer expression our
  engine does not evaluate.
- ``contact.width.1``: ``CO.1`` specifies contact as a fixed 0.22 x 0.22um
  square (a min **and** max bound); our ``width_check`` primitive only
  supports a minimum-width lower bound, so only the min half of the rule is
  enforced here.

Layer numbers (verified against ``google/globalfoundries-pdk-libs-gf180mcu_fd_pv``'s
``klayout/drc/rule_decks/main.drc`` layer derivations, e.g. ``comp =
get_polygons(22, 0)``, and cross-checked against this repo's own gf180mcu
corpus fixtures in ``tests/corpus/golden/gf180mcu/*.layers.json``):

    Nwell     21/0
    Comp      22/0
    Pplus     31/0
    Nplus     32/0
    Poly2     30/0
    Contact   33/0
    Metal1    34/0
    Dualgate  55/0
"""

from __future__ import annotations

from . import DrcRule

# Database units are nanometres (gf180mcu streams use dbu_um = 0.001, same as
# sky130), so a threshold in micrometres times 1000 gives threshold_dbu.
DECK: list[DrcRule] = [
    DrcRule(
        id="poly2.width.1",
        description="minimum poly2 interconnect width (3.3V)",
        layer=(30, 0),  # Poly2
        check="width",
        threshold_dbu=180,  # 0.18 um
        # DRM 7.7 Poly2, rule "PL.1": "Interconnect Width (outside PLFUSE)"
        # -> 0.18 (3.3V column). Approximation: the DRM holds gate poly2
        # (poly2 AND comp, rule "PL.2") to a stricter 0.28um minimum; using
        # the more permissive PL.1 value across the whole poly2 layer since
        # isolating gate poly2 needs a boolean layer expression.
    ),
    DrcRule(
        id="poly2.space.1",
        description="minimum poly2 spacing (3.3V)",
        layer=(30, 0),  # Poly2
        check="space",
        threshold_dbu=240,  # 0.24 um
        # DRM 7.7 Poly2, rule "PL.3a": "Space on COMP / Space on Field" ->
        # both sub-cases are 0.24 (3.3V column). Approximation: the DRM
        # splits this by context (poly2 over COMP vs. over field oxide);
        # unified into one check since both values coincide.
    ),
    DrcRule(
        id="poly2.enclosing.contact.1",
        description="minimum poly2 overlap of contact",
        layer=(30, 0),  # Poly2
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.3": "Poly2 overlap of contact" -> 0.07um
    ),
    DrcRule(
        id="comp.width.1",
        description="minimum COMP (diffusion/active) width (3.3V)",
        layer=(22, 0),  # Comp
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.5 Comp, rule "DF.1a": "Min. COMP Width" -> 0.22 (3.3V column)
    ),
    DrcRule(
        id="comp.space.1",
        description="minimum COMP (diffusion/active) spacing (3.3V)",
        layer=(22, 0),  # Comp
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.5 Comp, rule "DF.3a": "Min. COMP Space. P-substrate tap
        # (PCOMP outside NWELL and DNWELL) can be butted for different
        # voltage devices..." -> 0.28 (3.3V column). Approximation: the DRM
        # text scopes this to a substrate-tap butting context; approximated
        # here as a general COMP-to-COMP spacing check on the whole drawn
        # layer, since isolating that context needs a boolean layer
        # expression (comp minus well/tap markers). Threshold value
        # unmodified.
    ),
    DrcRule(
        id="comp.enclosing.contact.1",
        description="minimum COMP overlap of contact",
        layer=(22, 0),  # Comp
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.4": "COMP overlap of contact" -> 0.07um
    ),
    DrcRule(
        id="contact.width.1",
        description="minimum contact size (approximates official min/max size rule)",
        layer=(33, 0),  # Contact
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.12 Contact, rule "CO.1": "Min/max contact size" -> 0.22um
        # (contacts are a fixed 0.22 x 0.22um square in the real deck).
        # Approximation: our width_check enforces only a minimum-width
        # lower bound; the "max" (fixed-size) half of the rule is not
        # checked. Threshold value unmodified.
    ),
    DrcRule(
        id="contact.space.1",
        description="minimum contact spacing",
        layer=(33, 0),  # Contact
        check="space",
        threshold_dbu=250,  # 0.25 um
        # DRM 7.12 Contact, rule "CO.2a": "Space" -> 0.25um
    ),
    DrcRule(
        id="metal1.width.1",
        description="minimum metal1 width",
        layer=(34, 0),  # Metal1
        check="width",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.23 (n = 1)
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum metal1 spacing",
        layer=(34, 0),  # Metal1
        check="space",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.23 (n = 1)
    ),
]

# (layer, datatype) -> upstream layer name, from gf180mcu.lyp's layer-map
# (google/globalfoundries-pdk-libs-gf180mcu_fd_pv), used only to render
# violations[].layer as e.g. "Poly2" instead of the bare "30/0" fallback.
# Unlike sky130.lyt, gf180mcu's layer map does not use "name.purpose" pairs,
# so names here are the bare upstream layer names (no ".drawing" suffix).
LAYER_NAMES: dict[tuple[int, int], str] = {
    (21, 0): "Nwell",
    (22, 0): "Comp",
    (30, 0): "Poly2",
    (31, 0): "Pplus",
    (32, 0): "Nplus",
    (33, 0): "Contact",
    (34, 0): "Metal1",
    (55, 0): "Dualgate",
}
