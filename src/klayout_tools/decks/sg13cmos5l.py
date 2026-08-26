"""sg13cmos5l DRC/LVS deck: a curated starter subset (Part of #1398,
decomposed sub-issue #1400; poly resistors added by #1415).

Mirrors ``sg13g2.py``'s own history: sg13g2's first curated deck
(``decks/sg13g2.py``, issue #905) was a small, fully-verified MOS-only
starter -- 8 width/space DRC rules plus one LV MOSFET LVS device class --
grown incrementally across seven follow-on issues (#1231/#1235/#1233/#1243/
#1234/#1281) into its current ~1800-line deck. Issue #524 ("Curated SG13G2
deck ... for `klt drc`/`klt lvs`") was rejected twice by Champion for
proposing the whole deck in one PR; #905's starter -- and this module -- are
the disciplined alternative. Per #1400's own scope guard, this module
started **MOS devices only**: a single connected ``Activ``-to-``Metal1``
(cmos5l's lowest metal level) DRC stack, and MOS-only LVS device
recognition; it has since grown the first follow-on increment the way
#1231/#1235/#1233/#1243/#1234/#1281 grew sg13g2's own deck.

- **Added by #1415**: the three drawn poly resistors
  (``rsil``/``rppd``/``rhigh``) -- see ``EXTRACTION_DECK.resistors`` below.
  Not a coverage nicety: an unrecognised ``PolyRes``-marked ``GatPoly``
  body is contacted at both ends, so leaving it undeclared absorbs it into
  ordinary interconnect and **shorts the resistor's two terminals
  together**, merging two schematic nets and cascading into
  ``net.unmatched``/``device.unmatched`` for the rest of the block.
- **Still out of scope**, left for follow-on issues: the HV
  (``ThickGateOx``) MOS flavour (#1416), well/substrate tap layers (#1414),
  the ``Metal2``-``TopMetal1`` metal stack and its vias (#1417) -- which
  also gates the ``res_metal1``..``res_topmetal2`` metal-resistor family,
  see the resistor note below -- plus diodes, MoM capacitors (cmos5l has no
  MIM; see "No MIM capacitors" below), and parasitics.

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
  mos_connections,general_derivations,layers_definitions,res_extraction,
  res_derivations,res_connections,rfmos_*}.lvs``, all resolve (via a
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
- **Both the LV and HV flavours are transcribed**, mirroring ``sg13g2.py``'s
  own two-stage history (#905 shipped LV-only; ``sg13g2.py``'s HV pair was a
  dedicated follow-on, issue #1231, using the ``MOSFlavour``/
  ``mos_flavours`` mechanism) -- this deck's own HV follow-on is issue
  #1416. ``ThickGateOx`` (44/0) is a real, cited cmos5l layer this deck
  still does not check in its DRC rules (the rules transcribed below apply
  the general-case thresholds regardless of ``ThickGateOx``'s presence,
  same as sg13g2.py's own note), but it *is* now modelled for MOS
  device-recognition/model binding via ``EXTRACTION_DECK.mos_flavours`` --
  see ``UNMODELED_VOLTAGE_MARKERS`` below for what, if anything, remains
  unmodelled about that marker.
- cmos5l's **resistor** device-recognition rules (issue #1415) live in
  ``lvs/rule_decks/{res_derivations,res_extraction,res_connections}.lvs``
  -- three more of the symlinked-into-the-sibling files above, ``%include``d
  by cmos5l's *own* top-level ``lvs/sg13cmos5l.lvs``, so ``rsil``/``rppd``/
  ``rhigh`` are genuinely cmos5l devices rather than sg13g2-only ones. Their
  sheet resistances, by contrast, come from cmos5l's **own**, non-symlinked
  ``libs.tech/klayout/python/sg13cmos5l_pycell_lib/sg13cmos5l_tech.json``
  (cmos5l's own ``techName`` is ``"SG13G2_CMOS5L"``, which selects the same
  ``G2``-suffixed keys sg13g2 does; cmos5l additionally carries a ``G2C``
  key family but declares no ``*G2C_rspec``, i.e. no cmos5l-specific sheet
  rho). See ``EXTRACTION_DECK.resistors`` below for the per-entry
  transcription and its documented approximations.

No MIM capacitors: #1398 states cmos5l has "no MIM capacitors (forbidden-
layer requirement)" -- confirmed here to have no bearing on any layer this
starter touches: ``Activ``/``GatPoly``/``Metal1``/``Cont``/``NWell``/
``ThickGateOx`` (and #1415's resistor layers ``PolyRes``/``EXTBlock``/
``pSD``/``nSD``/``SalBlock``/``RES``) are unrelated to cmos5l's
MIM-forbidden rule (``rule_decks/forbidden/3_2_forbidden_cmos5l.drc``, which
blocks the ``MIM``-family layers specifically, not any layer this deck
declares). Cross-checked the other way too: none of this deck's declared
layers appear on either of cmos5l's own forbidden-layer lists
(that ``.drc``, or ``lvs/rule_decks/cmos5l_forbidden_check.lvs``'s 21-layer
table). No further action needed for this pass; noted per #1400's own
Background instruction, mirroring how ``sg13g2.py`` documents its own
declined-device investigations (e.g. its "SiGe HBTs -- declined" section).

Every DRC rule and every populated ``EXTRACTION_DECK`` provenance field
below cites a real, independently-fetched line in cmos5l's own (or, for the
symlinked-shared files, the pinned sibling ``ihp-sg13g2`` commit's) ``.drc``/
``.lvs`` source -- see ``RuleProvenance``'s own docstring in
``decks/__init__.py`` for the field shape this module's ``_cmos5l_provenance``/
``_cmos5l_lvs_provenance`` helpers below populate.

Every DRC rule below also has a golden violate/clean fixture pair in
``tests/golden_deck/sg13cmos5l/manifest.json`` (see ``tests/golden_deck/
README.md`` for the manifest schema and ``tests/golden_deck/
generate_golden_deck.py`` for how the fixtures are derived from each rule's
own ``layer``/``threshold_dbu``); that manifest is DRC-only, so the LVS
device classes below carry golden layout->netlist pair tests in
``tests/test_sg13cmos5l_deck.py`` instead.
"""

from __future__ import annotations

from . import (
    DrcRule,
    ExtractionDeck,
    MOSFlavour,
    ParasiticsDeck,
    ResistorDevice,
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
    (7, 0): "nSD.drawing",
    (7, 21): "nSD.block",
    (8, 0): "Metal1.drawing",
    (8, 2): "Metal1.pin",
    (14, 0): "pSD.drawing",
    (24, 0): "RES.drawing",
    (28, 0): "SalBlock.drawing",
    (31, 0): "NWell.drawing",
    (31, 2): "NWell.pin",
    (44, 0): "ThickGateOx.drawing",
    (111, 0): "EXTBlock.drawing",
    (128, 0): "PolyRes.drawing",
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
        "sg13_hv_pmos extract_devices calls). Issue #1416 added the HV "
        "MOSFlavour entry to EXTRACTION_DECK.mos_flavours, so a transistor "
        "drawn inside this marker now recognises and binds correctly "
        "(mirroring sg13g2.py's own #1231 follow-on) -- what remains "
        "unmodelled is this curated deck's DRC rules, which still apply "
        "the general-case thresholds to geometry regardless of "
        "ThickGateOx's presence (the channel-length-specific Gat.a1/Gat.a2 "
        "GatPoly-width rules that do read it are not transcribed here, "
        "mirroring sg13g2.py's own note)."
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
    # documents. These stay the *default* pair, cited for every transistor
    # drawn outside ThickGateOx; the thick-oxide ("-HV") pair is the
    # `mos_flavours` entry below (issue #1416).
    nfet_provenance=_cmos5l_shared_g2_provenance(
        f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_lv_nmos"
    ),
    pfet_provenance=_cmos5l_shared_g2_provenance(
        f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_lv_pmos"
    ),
    # Thick-oxide ("-HV") MOS flavour (issue #1416, the sg13cmos5l sibling
    # of sg13g2.py's own issue #1231, using the mechanism issue #1111 added
    # for gf180mcu's `Dualgate`): a transistor whose Activ island overlaps
    # ThickGateOx (44/0) is cmos5l's thick-gate-oxide device, whose real
    # upstream device-class names are `sg13_hv_nmos`/`sg13_hv_pmos` --
    #   extract_devices(mos4('sg13_hv_nmos'), { ..., 'G' => ngate_hv, ... })
    #   extract_devices(mos4('sg13_hv_pmos'), { ..., 'G' => pgate_hv, ... })
    # in the same (symlinked-but-resolved) `mos_extraction.lvs`, keyed off
    # `general_derivations.lvs`'s `ngate_hv_base =
    # ngate.and(thickgateox_drw)` / `pgate_hv_base` (and, symmetrically,
    # `ngate_lv_base = ngate.not(thickgateox_drw)` -- so the default pair
    # above is genuinely the *complement* of this flavour, which is exactly
    # the split `MOSFlavour` implements). Both names are independently
    # confirmed byte-identical to sg13g2.py's own HV pair (see the module
    # docstring's MOS-device-recognition source bullet) rather than assumed
    # identical by analogy -- `pdk_models.py`'s
    # `_MOS_MODEL_FLAVOURS[("sg13cmos5l", "sg13cmos5l")]` table binds
    # `MOSFlavour.flavour="hv"` to those two real subcircuit names under
    # `--pdk`.
    mos_flavours=(
        MOSFlavour(
            marker=(44, 0),  # ThickGateOx.drawing
            flavour="hv",
            description="cmos5l thick-gate-oxide domain (ThickGateOx 44/0)",
            nfet_provenance=_cmos5l_shared_g2_provenance(
                f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_hv_nmos"
            ),
            pfet_provenance=_cmos5l_shared_g2_provenance(
                f"{_LVS_RULE_DECKS}/mos_extraction.lvs", "sg13_hv_pmos"
            ),
        ),
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
    # ----------------------------------------------------------------- #
    # Drawn precision poly resistors (issue #1415)
    # ----------------------------------------------------------------- #
    #
    # Not a coverage nicety: an *unrecognised* resistor body is worse than
    # an uncompared one. A drawn `rppd` is a `GatPoly` strip contacted at
    # both ends, so without a `ResistorDevice` declaration the body is
    # absorbed into ordinary interconnect and the two heads are **shorted
    # together** -- collapsing two schematic nets into one, which cascades
    # into `net.unmatched` on both sides and can stop every otherwise
    # correctly-extracted device in the block from finding a
    # correspondence (issue #1415's own reproduction). `klt extract`
    # already names the shape ("the resistor-body signature ... absorbed
    # into ordinary interconnect as an unintended short"); this
    # declaration is what makes it stop happening.
    #
    # Transcribed from cmos5l's own `lvs/rule_decks/res_derivations.lvs`/
    # `res_extraction.lvs`/`res_connections.lvs` -- three more of the
    # rule-deck files cmos5l symlinks into the sibling `ihp-sg13g2`
    # checkout (see the module docstring's symlink-chain paragraph), read
    # here at the commit `.github/ihp-sg13g2.ref` pins and confirmed
    # byte-identical to the same three files fetched directly from
    # `IHP-GmbH/IHP-Open-PDK` at that commit. cmos5l's own top-level
    # `lvs/sg13cmos5l.lvs` `%include`s `res_derivations.lvs` and
    # `res_extraction.lvs` directly (and `res_connections.lvs` transitively
    # via `devices_connections.lvs`), so these are real cmos5l devices, not
    # sg13g2-only ones:
    #
    #   polyres_exclude = activ.join(pwell_block).join(nsd_block)
    #                       .join(nbulay_drw).join(thickgateox_drw)
    #                       .join(trans_drw).join(emwind_drw)
    #                       .join(emwihv_drw).join(activ_mask)
    #                       .join(recog_diode).join(recog_esd)
    #                       .join(ind_drw).join(ind_pin).join(substrate_drw)
    #   polyres_mk = polyres_drw.and(extblock_drw).interacting(gatpoly)
    #                  .not(polyres_exclude)
    #   rsil_exc   = psd_drw.join(salblock_drw).join(nsd_drw).join(nsd_block)
    #   rsil_res   = polyres_mk.and(res_drw).not(rsil_exc)
    #   rppd_res   = polyres_mk.and(psd_drw).and(salblock_drw)
    #                  .not(nsd_block).not(nsd_drw)
    #   rhigh_res  = polyres_mk.and(psd_drw).and(nsd_drw).and(salblock_drw)
    #   extract_devices(GeneralNTerminalExtractor.new('rppd', 2),
    #     { 'core' => rppd_res, 'ports' => rppd_ports, ... })  (ditto
    #     rsil/rhigh)
    #
    # Layer numbers are read from **cmos5l's own** `sg13cmos5l.lyp`, not
    # taken from `sg13g2.py` by analogy (port-checklist step 2): `GatPoly
    # .drawing` 5/0, `PolyRes.drawing` 128/0, `EXTBlock.drawing` 111/0,
    # `pSD.drawing` 14/0, `nSD.drawing` 7/0, `nSD.block` 7/21,
    # `SalBlock.drawing` 28/0, `RES.drawing` 24/0, `Activ.drawing` 1/0,
    # `ThickGateOx.drawing` 44/0 -- each cross-checked as a real
    # `<name>`/`<source>` pair there, and none of them on cmos5l's own
    # forbidden-layer lists (`lvs/rule_decks/cmos5l_forbidden_check.lvs`,
    # `drc/rule_decks/forbidden/3_2_forbidden_cmos5l.drc`).
    #
    # Sheet rhos likewise come from cmos5l's own, non-symlinked
    # `libs.tech/klayout/python/sg13cmos5l_pycell_lib/sg13cmos5l_tech.json`
    # (cited per entry below), not from `sg13g2_tech.json`. cmos5l's
    # `techParams.techName` is `"SG13G2_CMOS5L"`, which *contains* the
    # `SG13G2` substring `rppd_code.py`'s `if 'SG13G2' in SG13_TECHNOLOGY:
    # suffix = 'G2'` tests for, so the `G2`-suffixed keys are the ones a
    # cmos5l PyCell reads -- verified rather than assumed. cmos5l's JSON
    # also carries a `G2C` ("CMOS5L") key family, but *only* for
    # `_lwd`/`_rzspec`/`_ikspec`/`_ipspec`/`_rkspec` -- there is no
    # `rppdG2C_rspec`/`rhighG2C_rspec`/`rsilG2C_rspec`, i.e. no
    # cmos5l-specific sheet resistance to prefer over the `G2` values.
    #
    # Documented approximations -- identical in kind to the ones
    # `sg13g2.py`'s own poly-resistor block records, since the rule text
    # is literally the same file:
    #
    # - Upstream's `core` is the *marker* region (`polyres & extblock`,
    #   merely `interacting` GatPoly); this engine intersects it with the
    #   body layer. For a real device cell -- `polyres` drawn coincident
    #   with the poly bar it marks -- the two are the same region.
    # - `polyres_exclude` is a 14-layer join. Only `Activ` (so a marked
    #   *gate* is never mistaken for a resistor) and `ThickGateOx` are
    #   transcribed. Of the remaining twelve, four (`TRANS` 26/0, `nBuLay`
    #   32/0, `EmWind` 33/0, `EmWiHV` 156/0) are not even present in
    #   `sg13cmos5l.lyp` -- and `TRANS`/`nBuLay` are on cmos5l's own
    #   forbidden-layer list, so a design carrying them is invalid for
    #   this technology outright. The other eight (`pwell_block`,
    #   `nsd_block`, `Activ.mask`, `Recog.diode`, `Recog.esd`,
    #   `IND.drawing`, `IND.pin`, `Substrate.drawing`) are real cmos5l
    #   layers this curated deck does not model at all; `nsd_block` is
    #   nonetheless transcribed on `rsil`/`rppd`, where it is *also* a
    #   flavour-selecting term (`rsil_exc`/`rppd_res`'s own
    #   `.not(nsd_block)`), not merely a `polyres_exclude` member.
    # - Upstream additionally connects each device's `*_sub` region (the
    #   segment sized by 5 nm) to `pwell`/`iso_pwell`/`nwell_drw`
    #   (`res_connections.lvs`), i.e. a real bulk terminal.
    #   `bulk_to_substrate` below wires the equivalent `W` terminal to
    #   this deck's `substrate_net` global -- the same fallback its NMOS
    #   body already uses, since this starter models no drawn PWell and
    #   declares no `tap` layer.
    # - The PDK's own resistance model is more than sheet-rho: cmos5l's
    #   `sg13cmos5l_tech.json` feeds the same `CbResCalc` helper
    #   (`utility_functions.py`, itself symlinked into the sibling
    #   checkout) composing `l/weff*(b+1)*rspec + ... + 2/w*rzspec` with a
    #   per-flavour line-width delta (`*_lwd`) and a width-dependent
    #   contact/transition term (`*_rzspec`). Neither is expressible in
    #   this engine's `sheet_rho_ohm_sq` (+ the optional, *not*
    #   width-dependent `fixed_offset_ohm`), so `R = L/W * rspec` is a
    #   first-order transcription of the body term only -- stated here
    #   rather than silently implied.
    #
    # Metal resistors are deliberately **not** declared. The same
    # `res_extraction.lvs` also extracts `res_metal1`..`res_topmetal2`,
    # of which only five are reachable in cmos5l at all (`res_metal5`/
    # `res_topmetal2` sit on `Metal5` 67/0 and `TopMetal2` 134/0, both on
    # cmos5l's own forbidden-layer list -- its stack is
    # M1-M2-M3-M4-TopMetal1). Four of those five bodies are above this
    # starter's single-`Metal1` stack, so only `res_metal1` could be wired
    # today. Landing one member of a five-member family here and the rest
    # behind the metal-stack extension (issue #1417, this deck's own
    # sibling gap) would split a family across two increments' worth of
    # provenance; they are left together for that follow-on, mirroring
    # `sg13g2.py`'s own deferral of `res_metal3`..`res_topmetal2` behind
    # its stack-extension prerequisite.
    resistors=(
        ResistorDevice(
            name="rsil",  # upstream LVS device-class name
            body=(5, 0),  # GatPoly.drawing
            marker=(128, 0),  # PolyRes.drawing
            # `rsilG2_rspec` in cmos5l's own sg13cmos5l_tech.json (7.0;
            # `rsil_code.py` reads the `G2` key unconditionally, and the
            # non-G2 `rsil_rspec` carries the same 7.0 value, so this one
            # has no ambiguity to resolve). Independently corroborated by
            # cmos5l's own `libs.tech/ngspice/models/cornerRES.lib`
            # typical corner (`.LIB res_typ`, `rsh_rsil = 7.0`).
            sheet_rho_ohm_sq=7.0,
            requires=(
                (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
                (24, 0),  # RES      -- the silicided-resistor marker itself
            ),
            excludes=(
                (14, 0),  # pSD       -\
                (28, 0),  # SalBlock   |- upstream's `rsil_exc`
                (7, 0),  # nSD         |
                (7, 21),  # nSD.block -/
                (1, 0),  # Activ       -\ `polyres_exclude` (a marked gate is
                (44, 0),  # ThickGateOx -/ a transistor, not a resistor)
            ),
            bulk_to_substrate=True,  # upstream connects `rsil_sub` to pwell
            provenance=_cmos5l_shared_g2_provenance(
                f"{_LVS_RULE_DECKS}/res_extraction.lvs", "rsil"
            ),
        ),
        ResistorDevice(
            name="rppd",  # upstream LVS device-class name
            body=(5, 0),  # GatPoly.drawing
            marker=(128, 0),  # PolyRes.drawing
            # `rppdG2_rspec` in cmos5l's own sg13cmos5l_tech.json (260.0,
            # vs the non-G2 `rppd_rspec`'s 250.0 for the older SG13
            # flavour) -- selected because cmos5l's own `techName`,
            # `"SG13G2_CMOS5L"`, satisfies `rppd_code.py`'s `'SG13G2' in
            # SG13_TECHNOLOGY` test. Independently corroborated by
            # cmos5l's own `cornerRES.lib` (`rsh_rppd = 260.0`).
            sheet_rho_ohm_sq=260.0,
            requires=(
                (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
                (14, 0),  # pSD      -- p+ doped poly
                (28, 0),  # SalBlock -- unsalicided (vs. rsil's 7 ohm/sq)
            ),
            excludes=(
                (7, 0),  # nSD         -\ upstream's own `.not(nsd_drw)`/
                (7, 21),  # nSD.block  -/ `.not(nsd_block)` (-> rhigh)
                (1, 0),  # Activ       -\ `polyres_exclude`, see rsil above
                (44, 0),  # ThickGateOx -/
            ),
            bulk_to_substrate=True,  # upstream connects `rppd_sub` to pwell
            provenance=_cmos5l_shared_g2_provenance(
                f"{_LVS_RULE_DECKS}/res_extraction.lvs", "rppd"
            ),
        ),
        ResistorDevice(
            name="rhigh",  # upstream LVS device-class name
            body=(5, 0),  # GatPoly.drawing
            marker=(128, 0),  # PolyRes.drawing
            # cmos5l's own sg13cmos5l_tech.json reproduces sg13g2's own
            # `rhigh` self-contradiction exactly: `rhigh_rspec` is 1300.0
            # while `rhighG2_rspec` is 1360.0, and the two PyCell readers
            # disagree about which to use (`rhigh_code.py` -- symlinked
            # into the sibling checkout -- hardcodes `suffix = "G2"` for
            # its `CbResCurrent` call yet reads the *unsuffixed*
            # `rhigh_rspec` for the CDF `Rspec` field it displays, whereas
            # the shared `utility_functions.py`'s `CbResCalc`, which
            # computes the PyCell's default resistance value, hardcodes
            # `suffix = 'G2'` and so reads `rhighG2_rspec`). The tie is
            # broken by a third source cmos5l also ships, independent of
            # the PyCell parameter table above (though, like most of this
            # PDK, symlinked into the same sibling checkout):
            # `libs.tech/ngspice/models/cornerRES.lib`'s typical
            # corner (`.LIB res_typ`) sets `rsh_rhigh = 1360` -- the
            # coefficients ngspice itself simulates a `.subckt rhigh`
            # instance with. The same section's `rsh_rsil = 7.0` /
            # `rsh_rppd = 260.0` independently reproduce the two entries
            # above, corroborating `cornerRES.lib` as the value real
            # designs simulate against.
            sheet_rho_ohm_sq=1360.0,
            # `rhigh_res = polyres_mk.and(psd_drw).and(nsd_drw)
            # .and(salblock_drw)` -- unlike rsil/rppd this flavour
            # *requires* nSD present alongside pSD (the doping combination
            # its higher sheet rho comes from), which is also what
            # disambiguates it from rppd: rppd's `excludes` above subtract
            # nSD/nSD.block, so a segment carrying nSD can only ever match
            # this entry.
            requires=(
                (111, 0),  # EXTBlock -- upstream's `polyres_mk` head term
                (14, 0),  # pSD      -\ both implants present together --
                (7, 0),  # nSD       -/ upstream's `.and(psd_drw)
                #                        .and(nsd_drw)`
                (28, 0),  # SalBlock -- unsalicided, same as rppd
            ),
            excludes=(
                (1, 0),  # Activ       -\ `polyres_exclude`, see rsil/rppd
                (44, 0),  # ThickGateOx -/ above
            ),
            bulk_to_substrate=True,  # upstream connects `rhigh_sub` to pwell
            provenance=_cmos5l_shared_g2_provenance(
                f"{_LVS_RULE_DECKS}/res_extraction.lvs", "rhigh"
            ),
        ),
    ),
)

# No sheet-resistance/parallel-plate-capacitance table curated for this
# MOS-only starter pass -- parasitics are explicitly out of scope for #1400
# (deferred to the parasitics registry per its own Background note). `klt
# extract --parasitics` against the `"sg13cmos5l"` deck runs (no error),
# just reports zero R/C for every net, exactly as any deck declaring no
# `ParasiticsDeck` role does.
PARASITICS = ParasiticsDeck()
