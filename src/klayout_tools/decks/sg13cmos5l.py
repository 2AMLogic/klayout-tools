"""sg13cmos5l DRC/LVS deck: a curated *MOS-only starter* subset (Part of
#1398, decomposed sub-issue #1400).

Mirrors ``sg13g2.py``'s own history: sg13g2's first curated deck
(``decks/sg13g2.py``, issue #905) was a small, fully-verified MOS-only
starter -- 8 width/space DRC rules plus one LV MOSFET LVS device class --
grown incrementally across seven follow-on issues (#1231/#1235/#1233/#1243/
#1234/#1281) into its current ~1800-line deck. Issue #524 ("Curated SG13G2
deck ... for `klt drc`/`klt lvs`") was rejected twice by Champion for
proposing the whole deck in one PR; #905's starter -- and this module -- are
the disciplined alternative. Per #1400's own scope guard, this module is
**MOS devices only**: a single connected ``Activ``-to-``Metal1`` (cmos5l's
lowest metal level) DRC stack, and MOS-only LVS device recognition.
Resistors, diodes, MoM capacitors (cmos5l has no MIM -- see "No MIM
capacitors" below), the ``Metal1``-``Metal4``+``TopMetal1`` metal stack, and
parasitics are all explicitly out of scope here, left for follow-on issues
the way #1231/#1235/#1233/#1243/#1234/#1281 grew sg13g2's own deck.

Source, read directly from a real ``ihp-sg13cmos5l`` install (standalone
clone of https://github.com/IHP-GmbH/ihp-sg13cmos5l, Apache-2.0), **not**
inherited from ``sg13g2.py`` by analogy, per #1400's own Background note --
every value below was independently re-verified against this deck's own
on-disk source:

- ``ihp-sg13cmos5l`` at commit ``607e18d4bd9214a52575c194b4181ef449f9252f``
  (``main``, 2026-08-25) supplies this deck's own, non-symlinked files:
  layer numbers, cross-checked against both
  ``libs.tech/klayout/tech/sg13cmos5l.lyp``'s ``<name>...drawing</name>``/
  ``<source>`` entries and the ``<dbu>0.001</dbu>`` declared in
  ``libs.tech/klayout/tech/sg13cmos5l.lyt`` (matching ``NOMINAL_DBU_UM``
  below), and DRC threshold *values*, read from
  ``libs.tech/klayout/tech/drc/rule_decks/sg13cmos5l_tech_default.json``
  (this deck's own ``drc_rules['<Key>']`` value table -- a real cmos5l file,
  not shared with sg13g2).
- **cmos5l's Activ/GatPoly/Metal1 DRC-DSL rule scripts and its MOS LVS
  device-recognition scripts are themselves literal symlinks into a sibling
  ``ihp-sg13g2`` checkout**, not independent cmos5l source text --
  ``libs.tech/klayout/tech/drc/rule_decks/feol/{5_5_activ,5_7_thickgateox,
  5_8_gatpoly}.drc`` and ``.../beol/5_16_metal1.drc``, plus
  ``libs.tech/klayout/tech/lvs/rule_decks/{mos_extraction,mos_derivations,
  mos_connections,general_derivations,rfmos_*}.lvs``, all resolve (via a
  relative ``../../../../.../ihp-sg13g2/...`` symlink target) to that
  sibling repo -- physical confirmation, not just #1398's "aligned... with
  G2 (per IHP's release notes)" prose, that cmos5l's FEOL/MOS-LVS layer set
  is byte-identical to sg13g2's own for exactly the rules this starter
  transcribes. Which upstream commit those symlinks resolve to is itself
  pinned by ``ihp-sg13cmos5l``'s own
  ``.github/ihp-sg13g2.ref`` -- ``d2cc0355f26235c777dfcc6867b390fa1e78083f``
  ("Commit of IHP-GmbH/IHP-Open-PDK that CI pairs this PDK with... Pinning
  it means a change on the G2 side cannot break this repo without someone
  bumping this file") -- fetched directly from
  ``https://github.com/IHP-GmbH/IHP-Open-PDK`` at that exact commit to read
  the real rule/device text the dangling local symlinks (this standalone
  clone has no sibling ``ihp-sg13g2`` checkout) could not otherwise resolve.
  This is a **different, newer** commit than ``sg13g2.py``'s own pinned
  ``5cccb161f7492697cfa52eb14dc03beb00bdca9e`` (the ``v0.3.0`` release tag)
  -- confirmed independently rather than assumed identical: every rule value
  cited below (``Act.a``/``Act.b`` 0.15/0.21um, ``Gat.a``/``Gat.b``
  0.13/0.18um, ``M1.a``/``M1.b`` 0.16/0.18um) matches ``sg13g2.py``'s own
  citations byte-for-value despite the newer commit, but the rule *text*
  itself has diverged cosmetically (e.g. ``Act.b``/``M1.b`` now read
  ``activ_drw.join(activ_filler).space(...)``/``metal1_drw.join(
  metal1_filler).space(...)`` -- an explicit drawing+filler join -- where
  ``sg13g2.py``'s v0.3.0-era citation shows a plain ``activ_drw.space(...)``
  ). ``RuleProvenance.source_repo``/``.source_path`` below cite
  ``IHP-GmbH/IHP-Open-PDK``'s ``ihp-sg13g2/...`` path (the real content
  location) rather than the dangling cmos5l-side symlink path, with this
  paragraph as the record of the symlink chain that makes that the correct
  citation for a deck registered as ``"sg13cmos5l"``.
- cmos5l's MOS device-recognition rules
  (``lvs/rule_decks/mos_extraction.lvs``) declare **both LV and HV NMOS/PMOS
  flavours** -- ``sg13_lv_nmos``/``sg13_lv_pmos`` (thin gate oxide) and
  ``sg13_hv_nmos``/``sg13_hv_pmos`` (``ThickGateOx`` 44/0-gated thick gate
  oxide) -- independently confirmed from cmos5l's own (symlinked-but-
  resolved) ``.lvs`` source rather than inherited from #1398's prose or
  ``sg13g2.py`` by analogy, exactly as #1400 asked. ``rfmos_extraction.lvs``
  (also present, also symlinked to the same sibling) additionally declares
  RF-flavoured devices -- out of scope for this MOS-only baseline, mirroring
  sg13g2.py's own un-transcribed RF-FET note.
- **Only the LV flavour is transcribed by this starter**, mirroring
  ``sg13g2.py``'s own #905 starter scope exactly (#905 shipped LV-only;
  ``sg13g2.py``'s HV pair was a dedicated follow-on, issue #1231, using the
  ``MOSFlavour``/``mos_flavours`` mechanism). ``ThickGateOx`` (44/0) is a
  real, cited cmos5l layer this deck neither checks (the DRC rules
  transcribed below apply the general-case thresholds regardless of
  ``ThickGateOx``'s presence, same as sg13g2.py's own note) nor models for
  MOS device-recognition/model binding -- see ``UNMODELED_VOLTAGE_MARKERS``
  below. Adding the HV flavour (the ``sg13cmos5l`` sibling of sg13g2.py's
  own issue #1231) is left for a follow-on issue.

No MIM capacitors: #1398 states cmos5l has "no MIM capacitors (forbidden-
layer requirement)" -- confirmed here to have no bearing on any layer this
MOS-only starter touches: ``Activ``/``GatPoly``/``Metal1``/``Cont``/``NWell``/
``ThickGateOx`` are unrelated to cmos5l's MIM-forbidden rule
(``rule_decks/forbidden/3_2_forbidden_cmos5l.drc``, which blocks the
``MIM``-family layers specifically, not any layer this deck declares). No
further action needed for this pass; noted per #1400's own Background
instruction, mirroring how ``sg13g2.py`` documents its own declined-device
investigations (e.g. its "SiGe HBTs -- declined" section).

Every DRC rule and every populated ``EXTRACTION_DECK`` provenance field
below cites a real, independently-fetched line in cmos5l's own (or, for the
symlinked-shared files, the pinned sibling ``ihp-sg13g2`` commit's) ``.drc``/
``.lvs`` source -- see ``RuleProvenance``'s own docstring in
``decks/__init__.py`` for the field shape this module's ``_cmos5l_provenance``/
``_cmos5l_lvs_provenance`` helpers below populate.

Every rule below also has a golden violate/clean fixture pair in
``tests/golden_deck/sg13cmos5l/manifest.json`` (see ``tests/golden_deck/
README.md`` for the manifest schema and ``tests/golden_deck/
generate_golden_deck.py`` for how the fixtures are derived from each rule's
own ``layer``/``threshold_dbu``).
"""

from __future__ import annotations

from . import (
    DrcRule,
    ExtractionDeck,
    ParasiticsDeck,
    RuleProvenance,
)

# `RuleProvenance.source_repo`/`.commit` for cmos5l's own (non-symlinked)
# files -- the JSON threshold-value table and the .lyp/.lyt layer
# definitions (see the module docstring's first source bullet).
_IHP_SG13CMOS5L_REPO = "IHP-GmbH/ihp-sg13cmos5l"
_IHP_SG13CMOS5L_COMMIT = "607e18d4bd9214a52575c194b4181ef449f9252f"  # main, 2026-08-25

# `RuleProvenance.source_repo`/`.commit` for the rule/device *text* itself --
# physically a sibling `ihp-sg13g2` checkout, resolved via the commit
# `ihp-sg13cmos5l`'s own `.github/ihp-sg13g2.ref` pins (see the module
# docstring's second source bullet for the full symlink-chain explanation).
_IHP_OPEN_PDK_REPO = "IHP-GmbH/IHP-Open-PDK"
_IHP_OPEN_PDK_G2_PIN_COMMIT = "d2cc0355f26235c777dfcc6867b390fa1e78083f"


def _cmos5l_provenance(source_path: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a value read from cmos5l's own,
    non-symlinked source (the JSON threshold table) -- `source_repo`/
    `commit` are `ihp-sg13cmos5l`'s own (see the module-level constants
    above), `source_path`/`rule_id` per call."""
    return RuleProvenance(
        source_repo=_IHP_SG13CMOS5L_REPO,
        source_path=source_path,
        rule_id=rule_id,
        commit=_IHP_SG13CMOS5L_COMMIT,
    )


def _cmos5l_shared_g2_provenance(source_path: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a DRC/LVS rule whose *text* lives
    in the sibling `ihp-sg13g2` checkout cmos5l's own rule_decks symlink to
    (see the module docstring's symlink-chain paragraph) -- `source_path` is
    the real content location (`ihp-sg13g2/libs.tech/...`, the same
    convention `sg13g2.py`'s own citations use), `commit` is the pin
    `ihp-sg13cmos5l/.github/ihp-sg13g2.ref` declares (not `sg13g2.py`'s own,
    older `v0.3.0`-tag commit -- a different, independently-verified
    snapshot)."""
    return RuleProvenance(
        source_repo=_IHP_OPEN_PDK_REPO,
        source_path=source_path,
        rule_id=rule_id,
        commit=_IHP_OPEN_PDK_G2_PIN_COMMIT,
    )


_DRC_FEOL = "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/feol"
_DRC_BEOL = "ihp-sg13g2/libs.tech/klayout/tech/drc/rule_decks/beol"
_LVS_RULE_DECKS = "ihp-sg13g2/libs.tech/klayout/tech/lvs/rule_decks"

# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001, matching `sg13cmos5l.lyt`'s own
# `<dbu>0.001</dbu>`), so a threshold in micrometres times 1000 gives
# threshold_dbu. `run_drc()` rescales threshold_dbu by NOMINAL_DBU_UM /
# layout.dbu at run time, so the deck still gives correct results against a
# layout written at a different dbu (see DrcRule's docstring).
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
        # -> "5.5. Act.a : Min. Activ width : 0.15 µm" (cmos5l's own
        # sg13cmos5l_tech_default.json: drc_rules['Act_a'] = 0.15)
        scope="5.5 Activ",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_FEOL}/5_5_activ.drc",
            "Act.a",
        ),
    ),
    DrcRule(
        id="activ.space.1",
        description="minimum Activ (drawing + filler) space or notch",
        layer=(1, 0),  # Activ.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # rule_decks/feol/5_5_activ.drc rule "Act.b":
        # actb_l1 = activ_drw.join(activ_filler).space(act_b_value.um, euclidian)
        # -> "5.5. Act.b : Min. Activ (drawing + filler) space or notch:
        # 0.21 µm." (drc_rules['Act_b'] = 0.21). This curated rule checks
        # only the drawn `Activ.drawing` layer (no `activ_filler` join) --
        # the same "primary drawn layer only, filler-join out of scope"
        # approximation `metal1.space.1` below documents for `M1.b`.
        scope="5.5 Activ",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_FEOL}/5_5_activ.drc",
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
        # -> "5.8. Gat.a : Min. GatPoly width: 0.13 µm"
        # (drc_rules['Gat_a'] = 0.13)
        scope="5.8 GatPoly",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_FEOL}/5_8_gatpoly.drc",
            "Gat.a",
        ),
    ),
    DrcRule(
        id="gatpoly.space.1",
        description="minimum GatPoly (drawing + filler) space or notch",
        layer=(5, 0),  # GatPoly.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # rule_decks/feol/5_8_gatpoly.drc rule "Gat.b":
        # gatb_l1 = gatpoly_drw.join(gatpoly_filler).space(gat_b_value.um, euclidian)
        # -> "5.8. Gat.b : Min. GatPoly (drawing + filler) space or notch:
        # 0.18 µm." (drc_rules['Gat_b'] = 0.18). Drawn-layer-only
        # approximation, same note as `activ.space.1` above.
        scope="5.8 GatPoly",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_FEOL}/5_8_gatpoly.drc",
            "Gat.b",
        ),
    ),
    # --- Metal1 (lowest metal level), rule_decks/beol/5_16_metal1.drc,
    # "5.16. Metal1" -- this MOS-only starter's DRC stack stops here
    # (#1400: "a single connected Activ->lowest-metal DRC stack") ---
    DrcRule(
        id="metal1.width.1",
        description="minimum Metal1 width",
        layer=(8, 0),  # Metal1.drawing
        check="width",
        threshold_dbu=160,  # 0.16 um
        # rule_decks/beol/5_16_metal1.drc rule "M1.a":
        # m1_a_l = metal1_drw.width(m1_a_value.um, euclidian)
        # -> "5.16. M1.a: Min. Metal1 width: 0.16 μm." (drc_rules['M1_a']
        # = 0.16)
        scope="5.16 Metal1",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_BEOL}/5_16_metal1.drc",
            "M1.a",
        ),
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum Metal1 (drawing + filler) space or notch",
        layer=(8, 0),  # Metal1.drawing
        check="space",
        threshold_dbu=180,  # 0.18 um
        # rule_decks/beol/5_16_metal1.drc rule "M1.b":
        # m1_b_l = metal1_drw.join(metal1_filler).space(m1_b_value.um, euclidian)
        # -> "5.16. M1.b: Min. Metal1 (drawing + filler) space or notch:
        # 0.18 μm." (drc_rules['M1_b'] = 0.18). Drawn-layer-only
        # approximation, same note as `activ.space.1` above.
        scope="5.16 Metal1",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_BEOL}/5_16_metal1.drc",
            "M1.b",
        ),
    ),
]

# `(layer, datatype) -> "name.purpose"` display names for every layer this
# deck's DECK/EXTRACTION_DECK reads, cross-checked against
# `sg13cmos5l.lyp`'s own `<name>...drawing</name>`/`<source>` entries (see
# the module docstring).
LAYER_NAMES: dict[tuple[int, int], str] = {
    (1, 0): "Activ.drawing",
    (1, 2): "Activ.pin",
    (5, 0): "GatPoly.drawing",
    (5, 2): "GatPoly.pin",
    (6, 0): "Cont.drawing",
    (8, 0): "Metal1.drawing",
    (8, 2): "Metal1.pin",
    (31, 0): "NWell.drawing",
    (31, 2): "NWell.pin",
    (44, 0): "ThickGateOx.drawing",
}

# Voltage-domain marker layer this deck draws but does not model the DRC/
# extraction scoping of (issue #552's `decks.get_unmodeled_voltage_markers`,
# mirroring `sg13g2.py`'s own registration of the same real, cited layer).
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {
    (44, 0): (
        "ThickGateOx (44/0) marks cmos5l's thick-gate-oxide ('-HV') MOS "
        "voltage domain (see general_derivations.lvs's ngate_hv_base/"
        "pgate_hv_base -- confirmed present in cmos5l's own (symlinked-but-"
        "resolved) LVS source, mos_extraction.lvs's sg13_hv_nmos/"
        "sg13_hv_pmos extract_devices calls). This MOS-only starter models "
        "neither side of that split yet: EXTRACTION_DECK recognises only "
        "the LV (thin-gate-oxide) NMOS/PMOS pair (mirroring sg13g2.py's own "
        "#905 starter scope; the HV pair is left for a follow-on issue, the "
        "sg13cmos5l sibling of sg13g2.py's issue #1231), and this curated "
        "deck's DRC rules apply the general-case thresholds to geometry "
        "regardless of ThickGateOx's presence (the channel-length-specific "
        "Gat.a1/Gat.a2 GatPoly-width rules that do read it are not "
        "transcribed here, mirroring sg13g2.py's own note)."
    ),
}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #

EXTRACTION_DECK = ExtractionDeck(
    active=(1, 0),  # Activ.drawing
    poly=(5, 0),  # GatPoly.drawing
    nwell=(31, 0),  # NWell.drawing
    # MOS device-recognition provenance (independently confirmed against
    # cmos5l's own mos_extraction.lvs, see the module docstring): the LV
    # (thin, 1.2V-class gate oxide) devices --
    #   extract_devices(mos4('sg13_lv_nmos'),
    #     { 'SD' => nsd_fet, 'G' => ngate_lv, 'tS' => nsd_fet,
    #       'tD' => nsd_fet, 'tG' => poly_con, 'W' => pwell })
    #   extract_devices(mos4('sg13_lv_pmos'),
    #     { 'SD' => psd_fet, 'G' => pgate_lv, 'tS' => psd_fet,
    #       'tD' => psd_fet, 'tG' => poly_con, 'W' => nwell_drw })
    # -- both extracted off this deck's own `active`/`poly`/`nwell` split
    # (NMOS = active outside nwell, PMOS = active inside nwell), the same
    # approximation of `general_derivations.lvs`'s fuller
    # `ngate_lv_base.not(nmos_exc)` exclusion chain (RF-FET/latchup-guard/
    # moscap markers) that `sg13g2.py`'s own `nfet_provenance` note
    # documents. The HV pair (`sg13_hv_nmos`/`sg13_hv_pmos`) is real and
    # independently confirmed present (see `UNMODELED_VOLTAGE_MARKERS`
    # above) but not transcribed by this MOS-only starter.
    nfet_provenance=_cmos5l_shared_g2_provenance(
        f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_lv_nmos"
    ),
    pfet_provenance=_cmos5l_shared_g2_provenance(
        f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_lv_pmos"
    ),
    # No distinct drawn tap layer -- cmos5l's `ntap`/`ptap` are derived as
    # `Activ` intersected with `nwell`/`pwell` in `general_derivations.lvs`
    # (`ntap = nactiv.and(nwell_drw)...`, `ptap_all = pactiv.and(pwell)...`),
    # not a dedicated tap mask -- the same "no distinct tap layer" shape
    # `sg13g2.py`'s own deck has. The NMOS body terminal falls back to
    # `ExtractionDeck.substrate_net`'s synthesized global.
    tap=None,
    well_label=(31, 2),  # NWell.pin
    contact=(6, 0),  # Cont.drawing -- lands directly on Metal1, no li1-like
    # local-interconnect level (same single-first-metal-level shape as
    # sg13g2.py's own deck).
    metals=((8, 0),),  # Metal1.drawing -- starter single-level stack
    metal_labels=((8, 2),),  # Metal1.pin
    vias=(),  # len(metals) - 1 == 0, no via level yet
)

# No sheet-resistance/parallel-plate-capacitance table curated for this
# MOS-only starter pass -- parasitics are explicitly out of scope for #1400
# (deferred to the parasitics registry per its own Background note). `klt
# extract --parasitics` against the `"sg13cmos5l"` deck runs (no error),
# just reports zero R/C for every net, exactly as any deck declaring no
# `ParasiticsDeck` role does.
PARASITICS = ParasiticsDeck()
