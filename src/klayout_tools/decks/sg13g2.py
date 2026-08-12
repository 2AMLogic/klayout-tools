"""SG13G2 (IHP-Open-PDK) DRC/LVS deck: a curated *starter* subset.

Epic #711 Phase 3b ("compile" an SG13G2 deck, the second PDK-generality
proof after gf180mcu's Phase 3a). Per
``docs/design/deck-compiler-proposal.md`` (landed via issue #742, and
reconfirmed on issue #747's curator correction), "compile" in this epic
never meant "build a native-source-to-`DrcRule` parser" -- that proposal's
own §4/§5 concluded a parser would just hit the same checker-vocabulary
wall (no ``area``/``density``/``antenna`` check kind, no compound-layer
boolean expressions, no connectivity-aware checks) the hand-curated
``sky130.py``/``gf180mcu.py`` decks already document, for either PDK's real
source shape. "Compile" means: **hand-transcribe rules from the real PDK
source, each carrying a machine-readable** :class:`~klayout_tools.decks.RuleProvenance`
**citation and a golden violate/pass (or layout->netlist) pair** -- the
same discipline ``sky130.py``/``gf180mcu.py`` already use, with the newer
structured ``RuleProvenance`` field (issue #747/#868) instead of a prose-only
citation.

Source, fetched via ``scripts/fetch-ihp-sg13g2.sh``:

- https://github.com/IHP-GmbH/IHP-Open-PDK (Apache License 2.0), release tag
  ``v0.3.0``, commit ``5cccb161f7492697cfa52eb14dc03beb00bdca9e`` (the tag's
  own commit, confirmed via
  ``git ls-remote https://github.com/IHP-GmbH/IHP-Open-PDK refs/tags/v0.3.0``,
  the same reproducible-pin discipline ``sky130.py``/``gf180mcu.py`` already
  use for their own upstream commits).
- ``ihp-sg13g2/libs.tech/klayout/tech/drc/ihp-sg13g2.drc`` is SG13G2's
  top-level DRC-DSL script -- but unlike sky130's single self-contained
  ``sky130.lydrc``, every one of its ``%include rule_decks/...`` lines is
  **commented out** in the shipped release (confirmed by reading the file:
  every ``%include`` under "INCLUDES" is prefixed ``#``). The real rule
  content lives in the per-topic fragment files those includes would pull
  in, under ``ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/{feol,beol,...}/*.drc``,
  assembled at run time by a separate Python wrapper
  (``ihp-sg13g2/libs.tech/klayout/tech/drc/run_drc.py``) -- structurally the
  same "fragmented deck + assembly wrapper, no single ready-to-run file"
  shape ``gf180mcu.py``'s own provenance note documents for gf180mcu (see
  ``docs/cli/drc.md``'s "Engine" -> "klayout" limitation), **not** sky130's
  shape. This is why this deck's rules below cite the specific
  ``rule_decks/<topic>/<file>.drc`` fragment each rule's own
  ``.output(...)`` statement actually lives in, not the inert top-level
  ``ihp-sg13g2.drc``, and why ``--engine klayout`` cross-checking this deck
  against the real native SG13G2 deck (the way ``tests/golden_deck``'s tier
  3 does for sky130) is deferred for the same reason it already is for
  gf180mcu -- no single file this repo's ``--engine klayout`` resolver can
  point ``klayout`` at.
- Numeric threshold values are read from each rule's own JSON-driven
  ``drc_rules['<Key>']`` lookup (e.g. ``drc_rules['M1_a']``) via
  ``ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/sg13g2_tech_default.json``,
  the deck's own default value table -- the same "value keyed by a JSON
  table, transcribe the resolved number" shape gf180mcu's DRM CSV tables
  have, rather than a value hard-coded in the ``.drc`` script text itself.
- Layer numbers are read from
  ``ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/layers_def.drc``
  (``get_polygons(<layer>, <datatype>)`` calls, e.g. ``activ_drw =
  get_polygons(1, 0)``) and cross-checked against the display names in
  ``ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyp`` (e.g. ``Activ.drawing``
  at ``1/0``) and the ``<symbols>`` block of
  ``ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyt``.

**This is a starter subset, not a port of SG13G2's full design rule
manual** (which, like sky130/gf180mcu's, spans hundreds of rules across
FEOL/BEOL/PIN/FORBIDDEN/geometry categories -- see the commented-out
``%include`` list in ``ihp-sg13g2.drc`` for the full topic set this deck
does not attempt). Per
``docs/design/deck-compiler-proposal.md`` §6 ("First rule class:
width/spacing (retrofit, not new coverage)"), this pilot is scoped to
**``width``/``space`` checks only** -- the two check kinds requiring zero
new check-primitive work (``DrcRule.check`` already fully supports both;
``enclosing``/``separation`` and the not-yet-implemented
``area``/``density``/``antenna`` kinds are out of scope here, exactly as
they were for the sky130/gf180mcu width/space pilot in issue #747) -- across
four layers whose width/space rules are themselves simple, single-layer
``.width(...)``/``.space(...)`` calls with no compound derivation:
``Activ`` (diffusion), ``GatPoly`` (poly), ``Metal1``, and ``Metal2``.

Every rule below also has a golden violate/clean fixture pair in
``tests/golden_deck/sg13g2/manifest.json`` (see ``tests/golden_deck/README.md``
for the manifest schema and ``tests/golden_deck/generate_golden_deck.py``
for how the fixtures are derived from each rule's own
``layer``/``threshold_dbu``).

**Cross-check against issue #524** ("Curated SG13G2 (IHP-Open-PDK) DRC/LVS
deck for klt drc / klt lvs" -- a hand-written SG13G2 deck this compiled one
would normally be cross-checked against): as of this module, #524 is still
**open**, unmerged, carrying ``loom:operator-only`` -- there is no existing
hand-written SG13G2 deck in this repo to cross-check against. Per Epic #711
Phase 3b's own acceptance criteria, this is stated explicitly here (and in
this issue's PR) rather than silently skipped.

**Not transcribed in this pass** (real, cited SG13G2 rules, left for a
follow-on that either extends this width/space pilot to more layers or adds
the ``enclosing``/``separation``/``area`` check kinds needed to express
them faithfully):

- ``Gat.d`` ("Min. GatPoly space to Activ", 0.07um,
  ``rule_decks/feol/5_8_gatpoly.drc``) -- a two-layer ``separation`` rule,
  out of scope for this width/space-only pilot (mirrors why sky130/gf180mcu's
  own width/space pilot left every ``enclosing``/``separation`` rule for a
  later slice, per the deck-compiler proposal's own sequencing).
- ``V1.a`` ("Min. and max. Via1 width", 0.19um,
  ``rule_decks/beol/5_19_via1.drc``) -- a fixed-square via rule (both a
  floor *and* a ceiling on the same dimension, via
  ``without_bbox_min``/``without_bbox_max``), the same "min-and-max bound"
  shape ``docs/design/deck-compiler-proposal.md`` §4 documents as
  inexpressible in ``width_check``'s min-only vocabulary for sky130's
  ``via.width.1``/gf180mcu's ``CO.1``/``Vn.1`` -- not modelled here for the
  identical, already-documented reason.
- ``M1.e``/``M1.f``/``M1.g``/``M1.i`` and their ``Mn.*`` analogues
  (conditional wide-line/45-degree-bend spacing variants) -- each scopes to
  a *sub-population* of the layer (edges/segments meeting a length/angle
  condition), not the whole drawn layer the way ``M1.a``/``M1.b`` do; the
  same "primary rule only, conditional refinements out of scope" approximation
  sky130.py/gf180mcu.py already document for their own conditional variants.

MOSFET LVS device recognition (``EXTRACTION_DECK`` below, Epic #711 Phase 2's
``RuleProvenance``-carrying model) is transcribed from a **different**
upstream file than the DRC rules above:
``ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs``,
which declares four MOSFET flavours (``sg13_lv_nmos``/``sg13_lv_pmos``,
``sg13_hv_nmos``/``sg13_hv_pmos``, split by ``thickgateox_drw`` coverage of
the gate) via KLayout's own ``mos4(...)`` LVS-DSL extractor. This curated
deck -- like ``sky130.py``'s/``gf180mcu.py``'s ``ExtractionDeck`` -- has no
per-flavour marker-layer field: ``extract.py`` recognises exactly one NMOS
and one PMOS class off the deck's own ``active``/``poly``/``nwell`` fields
(``NMOS = active - nwell``, ``PMOS = active & nwell``, KLayout's standard
well-splitting idiom -- see ``ExtractionDeck``'s own docstring in
``decks/__init__.py``). ``nfet_provenance``/``pfet_provenance`` below cite
the **LV** (low-voltage, thin-gate-oxide) flavour as the one this generic,
no-voltage-marker recognition actually corresponds to -- the same "cite the
one flavour the generic label represents, not every voltage/threshold
variant the real PDK draws" approximation ``sky130.py``'s own
``nfet_provenance`` note documents for ``sky130_fd_pr__nfet_01v8`` (the real
SG13G2 LVS deck's own ``ngate_lv``/``pgate_lv`` derivation additionally
excludes several RF-FET/latchup-guard marker regions this curated deck does
not model -- see ``mos_derivations.lvs``'s ``mos_exclude``/``rfnmos_exc``
chain -- an approximation of the same shape, not a new one).

SG13G2 draws NMOS/PMOS device contacts (``Cont``, ``6/0``) landing directly
on ``Metal1`` (``8/0``) -- there is no separate local-interconnect layer the
way sky130's ``li1`` sits between ``licon1`` and ``met1``, the same
single-first-metal-level shape gf180mcu's ``Contact``/``Metal1`` pair has.
This curated deck accordingly declares ``contact=(6, 0)`` landing on a
single ``metals=((8, 0),)`` level (``Metal1`` only) -- a starter connectivity
stack, not SG13G2's full ``Metal1``-``TopMetal2`` routing stack (``Metal2``
through ``TopMetal2`` and their landing vias, all real, cited layers -- see
``layers_def.drc``/``sg13g2.lyt``'s own ``<connectivity>`` block -- are left
for a follow-on that extends this stack the same way issue #619 grew
sky130's ``metals``/``vias`` from ``li1``/``met1`` up through ``met5``).
SG13G2 draws no distinct substrate/well-tie diffusion layer separate from
``Activ`` (``ptap``/``ntap`` are derived as ``Activ`` intersected with
``pwell``/``NWell``, not a dedicated tap mask) -- the same "no distinct tap
layer" shape gf180mcu's shared ``Comp`` layer has, so ``tap`` is left
``None`` and the NMOS body terminal falls back to
``ExtractionDeck.substrate_net``'s synthesized global, exactly as gf180mcu's
deck already does.

``PARASITICS`` is left at its all-``None``/empty default: no public SG13G2
sheet-resistance/parallel-plate-capacitance table has been curated for this
starter pass (unlike sky130.py's/gf180mcu.py's fully-populated tables, each
sourced from a public magic tech file this repo has not yet located/verified
for SG13G2) -- ``klt extract --parasitics`` against the ``"sg13g2"`` deck
runs (no error), just reports zero R/C for every net, exactly as any deck
declaring no ``ParasiticsDeck`` role does (see ``ParasiticsDeck``'s own
docstring in ``decks/__init__.py``). A follow-on that curates real SG13G2
parasitics coefficients should populate this the same way issue #216/#547
did for sky130/gf180mcu.
"""

from __future__ import annotations

from . import (
    DrcRule,
    ExtractionDeck,
    ParasiticsDeck,
    RuleProvenance,
)

# `RuleProvenance.source_repo`/`.commit` shared by every SG13G2 DRC rule's
# provenance below (issue #747's structured-citation pattern, applied here
# for Epic #711 Phase 3b) -- the release tag this deck's rule values and
# layer numbers were fetched/verified against via
# `scripts/fetch-ihp-sg13g2.sh` (see the module docstring).
_IHP_OPEN_PDK_REPO = "IHP-GmbH/IHP-Open-PDK"
_IHP_OPEN_PDK_COMMIT = "5cccb161f7492697cfa52eb14dc03beb00bdca9e"  # tag v0.3.0


def _sg13g2_provenance(source_path: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for an SG13G2 DRC rule --
    `source_repo`/`commit` shared across the whole deck (see the
    module-level constants above), `source_path`/`rule_id` per call."""
    return RuleProvenance(
        source_repo=_IHP_OPEN_PDK_REPO,
        source_path=source_path,
        rule_id=rule_id,
        commit=_IHP_OPEN_PDK_COMMIT,
    )


# LVS device-recognition rules are transcribed from a different upstream
# file than the DRC rules above -- `mos_extraction.lvs`, not `ihp-sg13g2.drc`
# or its rule_decks fragments -- per the module docstring's "MOSFET LVS
# device recognition" section.
_LVS_SOURCE_PATH = "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"


def _sg13g2_lvs_provenance(rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for an SG13G2 LVS device-recognition
    entry -- `source_repo`/`source_path`/`commit` shared across every
    `EXTRACTION_DECK` device entry (see the module-level constants above),
    `rule_id` (the official upstream `mos4(...)` device-class name, e.g.
    `"sg13_lv_nmos"`) per call."""
    return RuleProvenance(
        source_repo=_IHP_OPEN_PDK_REPO,
        source_path=_LVS_SOURCE_PATH,
        rule_id=rule_id,
        commit=_IHP_OPEN_PDK_COMMIT,
    )


# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001, matching `sg13g2.lyt`'s own `<dbu>0.001</dbu>`),
# so a threshold in micrometres times 1000 gives threshold_dbu. `run_drc()`
# rescales threshold_dbu by NOMINAL_DBU_UM / layout.dbu at run time, so the
# deck still gives correct results against a layout written at a different
# dbu (see DrcRule's docstring).
NOMINAL_DBU_UM = 0.001

DECK: list[DrcRule] = [
    # --- Activ (diffusion), rule_decks/feol/5_5_activ.drc, "5.5. Activ" ---
    DrcRule(
        id="activ.width.1",
        description="minimum Activ width",
        layer=(1, 0),  # Activ.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # rule_decks/feol/5_5_activ.drc rule "Act.a":
        # acta_l1 = activ_drw.width(act_a_value.um, euclidian)
        # -> "Act.a : Min. Activ width : 0.15 um" (drc_rules['Act_a'] = 0.15
        # in sg13g2_tech_default.json)
        scope="5.5 Activ",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol/5_5_activ.drc",
            "Act.a",
        ),
    ),
    DrcRule(
        id="activ.space.1",
        description="minimum Activ space or notch",
        layer=(1, 0),  # Activ.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # rule_decks/feol/5_5_activ.drc rule "Act.b":
        # actb_l1 = activ_drw.space(act_b_value.um, euclidian)
        # -> "Act.b : Min. Activ space or notch: 0.21 um."
        # (drc_rules['Act_b'] = 0.21)
        scope="5.5 Activ",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol/5_5_activ.drc",
            "Act.b",
        ),
    ),
    # --- GatPoly, rule_decks/feol/5_8_gatpoly.drc, "5.8. GatPoly" ---
    DrcRule(
        id="gatpoly.width.1",
        description="minimum GatPoly width",
        layer=(5, 0),  # GatPoly.drawing
        check="width",
        threshold_dbu=130,  # 0.13 um
        # rule_decks/feol/5_8_gatpoly.drc rule "Gat.a":
        # gata_l1 = gatpoly_drw.width(gat_a_value.um, euclidian)
        # -> "Gat.a : Min. GatPoly width: 0.13 um" (drc_rules['Gat_a'] = 0.13)
        scope="5.8 GatPoly",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol/5_8_gatpoly.drc",
            "Gat.a",
        ),
    ),
    DrcRule(
        id="gatpoly.space.1",
        description="minimum GatPoly space or notch",
        layer=(5, 0),  # GatPoly.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # rule_decks/feol/5_8_gatpoly.drc rule "Gat.b":
        # gatb_l1 = gatpoly_drw.space(gat_b_value.um, euclidian)
        # -> "Gat.b :Min. GatPoly space or notch: 0.18 um."
        # (drc_rules['Gat_b'] = 0.18)
        scope="5.8 GatPoly",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol/5_8_gatpoly.drc",
            "Gat.b",
        ),
    ),
    # --- Metal1, rule_decks/beol/5_16_metal1.drc, "5.16. Metal1" ---
    DrcRule(
        id="metal1.width.1",
        description="minimum Metal1 width",
        layer=(8, 0),  # Metal1.drawing
        check="width",
        threshold_dbu=160,  # 0.16 um
        # rule_decks/beol/5_16_metal1.drc rule "M1.a":
        # m1_a_l = metal1_drw.width(m1_a_value.um, euclidian)
        # -> "M1.a: Min. Metal1 width: 0.16 um." (drc_rules['M1_a'] = 0.16)
        scope="5.16 Metal1",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol/5_16_metal1.drc",
            "M1.a",
        ),
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum Metal1 space or notch",
        layer=(8, 0),  # Metal1.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # rule_decks/beol/5_16_metal1.drc rule "M1.b":
        # m1_b_l = metal1_drw.space(m1_b_value.um, euclidian)
        # -> "M1.b: Min. Metal1 space or notch: 0.18um." (drc_rules['M1_b']
        # = 0.18)
        scope="5.16 Metal1",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol/5_16_metal1.drc",
            "M1.b",
        ),
    ),
    # --- Metal2, rule_decks/beol/5_17_metaln.drc, "5.17 Metaln" (a
    # templated per-metal-level loop over Metal2-Metal5; only the Metal2
    # ("n = 2") iteration is transcribed here, matching this deck's
    # single-metal-level `EXTRACTION_DECK.metals` starter scope) ---
    DrcRule(
        id="metal2.width.1",
        description="minimum Metal2 width",
        layer=(10, 0),  # Metal2.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # rule_decks/beol/5_17_metaln.drc rule "M2.a" (the met_no=2
        # iteration of the templated `mets_lay.each_with_index` loop):
        # mn_a_l = met_lay.width(mn_a_value.um, euclidian)
        # -> "M2.a: Min. Metal2 width: 0.2 um." (drc_rules['Mn_a'] = 0.2,
        # shared by every Metal2-Metal5 level)
        scope="5.17 Metaln",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol/5_17_metaln.drc",
            "M2.a",
        ),
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum Metal2 space or notch",
        layer=(10, 0),  # Metal2.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # rule_decks/beol/5_17_metaln.drc rule "M2.b":
        # mn_b_l = met_lay.space(mn_b_value.um, euclidian)
        # -> "M2.b: Min. Metal2 space or notch: 0.21 um." (drc_rules['Mn_b']
        # = 0.21)
        scope="5.17 Metaln",
        provenance=_sg13g2_provenance(
            "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol/5_17_metaln.drc",
            "M2.b",
        ),
    ),
]

# `(layer, datatype) -> "name.purpose"` display names for every layer this
# deck's DECK/EXTRACTION_DECK reads, cross-checked against sg13g2.lyp's own
# `<name>...drawing</name>`/`<source>` entries (see the module docstring).
LAYER_NAMES: dict[tuple[int, int], str] = {
    (1, 0): "Activ.drawing",
    (1, 2): "Activ.pin",
    (5, 0): "GatPoly.drawing",
    (5, 2): "GatPoly.pin",
    (6, 0): "Cont.drawing",
    (8, 0): "Metal1.drawing",
    (8, 2): "Metal1.pin",
    (10, 0): "Metal2.drawing",
    (31, 0): "NWell.drawing",
    (31, 2): "NWell.pin",
}

# Voltage-domain marker layers this deck draws but does not model the DRC/
# extraction scoping of (issue #552's `decks.get_unmodeled_voltage_markers`).
# SG13G2's real PDK has a `thickgateox_drw` (`44/0`) high-voltage
# gate-oxide marker with its own DRC column (see `Gat.a1`/`Gat.a2`'s
# thin/thick-oxide split in `rule_decks/feol/5_8_gatpoly.drc`), but this
# curated deck's `LAYER_NAMES` above does not name it as a recognised layer
# at all yet -- so, mirroring sky130.py's own empty table for the same
# reason, there is nothing to register here until a follow-on issue adds
# `thickgateox_drw` as a named layer first. Empty, not omitted, so
# `get_unmodeled_voltage_markers("sg13g2")` returns `{}` explicitly.
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #

EXTRACTION_DECK = ExtractionDeck(
    active=(1, 0),  # Activ.drawing
    poly=(5, 0),  # GatPoly.drawing
    nwell=(31, 0),  # NWell.drawing
    # MOS device-recognition provenance (Epic #711 Phase 2's RuleProvenance
    # model, applied here for Phase 3b): `mos_extraction.lvs`'s LV (thin,
    # 1.2V-class gate oxide) devices --
    #   extract_devices(mos4('sg13_lv_nmos'),
    #     { 'SD' => nsd_fet, 'G' => ngate_lv, 'tS' => nsd_fet,
    #       'tD' => nsd_fet, 'tG' => poly_con, 'W' => pwell })
    #   extract_devices(mos4('sg13_lv_pmos'),
    #     { 'SD' => psd_fet, 'G' => pgate_lv, 'tS' => psd_fet,
    #       'tD' => psd_fet, 'tG' => poly_con, 'W' => nwell_drw })
    # -- both extracted off this deck's own `active`/`poly`/`nwell` split
    # (NMOS = active outside nwell, PMOS = active inside nwell -- see the
    # module docstring's "MOSFET LVS device recognition" section for why the
    # real PDK's own more elaborate `ngate_lv`/`rfnmos_exc`/latchup-guard
    # exclusion chain is approximated this way, and for the HV
    # (`sg13_hv_nmos`/`sg13_hv_pmos`) flavour this citation does not cover).
    nfet_provenance=_sg13g2_lvs_provenance("sg13_lv_nmos"),
    pfet_provenance=_sg13g2_lvs_provenance("sg13_lv_pmos"),
    # No distinct tap layer (module docstring) -- the NMOS body terminal
    # falls back to `substrate_net`'s synthesized global, mirroring
    # gf180mcu's shared-`Comp`-layer deck.
    tap=None,
    well_label=(31, 2),  # NWell.pin
    contact=(6, 0),  # Cont.drawing -- lands directly on Metal1, no li1-like
    # local-interconnect level (module docstring).
    metals=((8, 0),),  # Metal1.drawing -- starter single-level stack
    metal_labels=((8, 2),),  # Metal1.pin
    vias=(),  # len(metals) - 1 == 0, no via level yet (module docstring)
)

# No public SG13G2 sheet-resistance/parallel-plate-capacitance table
# curated yet for this starter pass -- see the module docstring's
# "PARASITICS" paragraph. `klt extract --parasitics` against `"sg13g2"`
# runs cleanly, reporting zero R/C for every net, exactly as any deck
# declaring no role here does.
PARASITICS = ParasiticsDeck()
