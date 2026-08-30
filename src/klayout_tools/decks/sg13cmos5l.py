"""sg13cmos5l DRC/LVS deck: a curated starter subset (Part of #1398,
decomposed sub-issue #1400; poly resistors added by #1415; well/substrate
taps added by #1414; metal stack extended by #1417).

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
recognition; it has since grown three follow-on increments the way
#1231/#1235/#1233/#1243/#1234/#1281 grew sg13g2's own deck.

- **Added by #1415**: the three drawn poly resistors
  (``rsil``/``rppd``/``rhigh``) -- see ``EXTRACTION_DECK.resistors`` below.
  Not a coverage nicety: an unrecognised ``PolyRes``-marked ``GatPoly``
  body is contacted at both ends, so leaving it undeclared absorbs it into
  ordinary interconnect and **shorts the resistor's two terminals
  together**, merging two schematic nets and cascading into
  ``net.unmatched``/``device.unmatched`` for the rest of the block.
- **Added by #1416**: the HV (``ThickGateOx``-gated) NMOS/PMOS flavour --
  see ``EXTRACTION_DECK.mos_flavours`` below and the LV/HV source bullet
  further down.
- **Added by #1417**: the ``Metal2``-``TopMetal1`` metal stack and its
  ``Via1``/``Via2``/``Via3``/``TopVia1`` vias -- see
  ``EXTRACTION_DECK.metals``/``.vias`` below, and the ``DECK`` entries
  transcribed from ``rule_decks/beol/{5_17_metaln,5_19_via1,5_20_vian,
  5_21_topvia1}.drc`` plus the shared ``5_22_topmetal1.drc``. Before this,
  a net routed off ``Metal1`` onto any higher level extracted as multiple
  disconnected nets (`klt extract`'s own "... outside '<deck>' deck's
  connectivity graph ..." warning), so any block verified against this
  deck had to be floorplanned planar, single-metal -- not merely
  under-checked, genuinely un-layoutable on more than one routing level.
  cmos5l's own stack tops at ``TopMetal1``: no ``Metal5``/``Via4``/
  ``TopVia2``/``TopMetal2`` (all four are on cmos5l's own LVS
  forbidden-layer list, ``lvs/rule_decks/cmos5l_forbidden_check.lvs``, and
  its DRC forbidden-layer deck,
  ``drc/rule_decks/forbidden/3_2_forbidden_cmos5l.drc`` -- independently
  confirmed, not merely inferred from the layer numbers' absence). Each new
  ``EXTRACTION_DECK.metal_labels`` entry (``Metal2.pin``/``Metal3.pin``/
  ``Metal4.pin``/``TopMetal1.pin``, datatype 2) is consistent with this
  deck's own pre-existing ``Metal1.pin`` (8/2) choice from #1400, and each
  was confirmed as a real ``<name>...pin</name>``/``<source>`` entry in a
  live ``ihp-sg13cmos5l`` checkout's own ``sg13cmos5l.lyp`` rather than
  assumed by analogy. This is a deliberate consistency choice, not a
  cmos5l-versus-sg13g2 layer-map difference: that shared ``.lyp`` layer map
  declares **both** purposes for every metal level (``Metal2.pin`` 10/2 and
  ``Metal2.text`` 10/25, and so on), and ``sg13g2.py`` picks the ``.text``
  (datatype 25) side for the same conceptual field. Whether this deck
  should switch to ``.text`` to match ``sg13g2.py`` is a separate question
  -- it would have to move ``Metal1`` too -- and is deliberately not
  settled here.
- **Added by #1414**: ``tap_nplus``/``tap_pplus`` well/substrate tap
  derivation -- see ``EXTRACTION_DECK.tap_nplus``/``.tap_pplus`` below.
  Without it every PMOS body terminal extracted onto an isolated,
  unbiased well net (``unbiased_pmos_body_nets[]``/
  ``device.body_unverified`` on every ``sg13cmos5l`` layout, regardless of
  drawn tap geometry).
- **Still out of scope**, left for follow-on issues: the
  ``res_metal1``..``res_topmetal1`` metal-resistor family (now reachable
  in principle now that #1417 lands the metal stack those bodies sit on,
  but not transcribed by this issue -- see the resistor note below), plus
  diodes and parasitics. MoM capacitors are a narrower case than "not yet
  transcribed": #1463 confirmed cmos5l *does* have a MoM capacitor family
  (``cap_cmomi``/``cap_cmomf``) but that this repo's ``CapacitorDevice``
  mechanism cannot correctly represent it -- see "MoM capacitors" below
  for the investigation and #1466 for the new device-recognition shape it
  needs.

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
- **cmos5l's Metal2-Metal4/Via1-Via3/TopVia1 BEOL DRC rule files are
  cmos5l's own, non-symlinked source -- not shared with sg13g2** (issue
  #1417). This is the opposite of what #1400's own module docstring
  (Activ/GatPoly/Metal1) established and what #1417's own curated issue
  body assumed likely without live confirmation: a real ``ihp-sg13cmos5l``
  checkout's ``libs.tech/klayout/tech/drc/rule_decks/beol/`` shows
  ``5_16_metal1.drc``/``5_22_topmetal1.drc``/``5_23_topmetal1filler.drc``/
  ``9_1_lbe.drc`` as symlinks into the sibling
  ``ihp-sg13g2`` checkout, but ``5_17_metaln.drc`` (Metal2-Metal4),
  ``5_19_via1.drc``, ``5_20_vian.drc`` (Via2-Via3), and
  ``5_21_topvia1.drc`` are ordinary, cmos5l-authored files -- confirmed by
  ``ls -la`` (regular files, not symlinks) and each file's own inline
  header comment (``5_19_via1.drc``: "IHP-SG13CMOS5L local copy (not
  symlinked from G2). Reason: G2 PR #819 added npn13g2l exclusion in V1.a.
  No HBT devices in CMOS5L, so the exclusion is removed here."). Each also
  scopes its own templated ``Mn``/``Vn`` loop to cmos5l's real, shorter
  stack (``5_17_metaln.drc``: "SG13CMOS5L: CMOS5L version - M2-M4 only (no
  M5)"; ``5_20_vian.drc``: "Via2-Via3 only (no Via4)";
  ``5_21_topvia1.drc``: "TopVia1 connects Metal4 to TopMetal1" -- cmos5l's
  own TopVia1 lands on Metal4, not sg13g2's Metal5, since cmos5l's stack
  has no Metal5). ``RuleProvenance`` for these four rule families therefore
  cites ``IHP-GmbH/ihp-sg13cmos5l`` (this repo's own commit, via
  ``_cmos5l_provenance``) rather than the shared-sibling
  ``_cmos5l_shared_g2_provenance`` every other DRC rule in this deck uses.
  Every threshold value transcribed below (``Mn_a``/``Mn_b`` 0.20/0.21um,
  ``V1_a``/``V1_b``/``V1_c`` 0.19/0.22/0.01um, ``Vn_a``/``Vn_b``/``Vn_c``
  0.19/0.22/0.005um, ``TV1_a``/``TV1_b``/``TV1_c``/``TV1_d``
  0.42/0.42/0.10/0.42um) was read from cmos5l's own
  ``sg13cmos5l_tech_default.json`` ``drc_rules`` table directly (not
  assumed identical to sg13g2's own JSON, even though the values turn out
  to coincide byte-for-value with sg13g2's -- an independently-verified
  coincidence, not a shortcut). ``5_22_topmetal1.drc`` (TopMetal1
  width/space, ``TM1_a``/``TM1_b`` 1.64um) is the exception: it *is* a
  genuine symlink into the sibling ``ihp-sg13g2`` checkout (like the
  already-transcribed ``5_16_metal1.drc``), so alone among the BEOL rule
  families this issue adds it keeps the ``_cmos5l_shared_g2_provenance``
  citation the rest of this deck's rules use.
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
``pSD``/``nSD``/``SalBlock``/``RES``, plus #1417's ``Metal2``/``Metal3``/
``Metal4``/``TopMetal1``/``Via1``/``Via2``/``Via3``/``TopVia1``) are
unrelated to cmos5l's MIM-forbidden rule
(``rule_decks/forbidden/3_2_forbidden_cmos5l.drc``, which blocks the
``MIM``-family layers specifically, not any layer this deck declares).
Cross-checked the other way too: none of this deck's declared layers
(including #1417's new metal/via levels) appear on either of cmos5l's own
forbidden-layer lists (that ``.drc``, or
``lvs/rule_decks/cmos5l_forbidden_check.lvs``'s 21-layer table) -- only
``Metal5``/``Via4``/``TopVia2``/``TopMetal2`` are, and none of those four
are declared. No further action needed for this pass; noted per #1400's own
Background instruction, mirroring how ``sg13g2.py`` documents its own
declined-device investigations (e.g. its "SiGe HBTs -- declined" section).

MoM capacitors -- investigated (#1463), deferred to a new device shape
(#1466), *not* declined: #1463 was filed on the premise that cmos5l's MIM-
forbidden rule above left MoM (metal-oxide-metal) as "the only capacitor
that family has," and asked whether cmos5l's own rule deck defines a
recognisable MoM device. It does. This repo's *vendored*
``pdks/ihp-open-pdk/ihp-sg13g2`` snapshot (pinned to release ``v0.3.0``,
upstream commit ``5cccb161f7492697cfa52eb14dc03beb00bdca9e``, 2026-03-11)
predates the relevant upstream change and shows no MoM ``extract_devices``
call, which is what #1463's own Curator investigation found and correctly
reported as inconclusive. But the two *live* commits this module's own
constants above already pin for #1417's BEOL rules --
``ihp-sg13cmos5l``'s ``607e18d4bd9214a52575c194b4181ef449f9252f`` (main,
2026-08-25) and the sibling ``ihp-sg13g2`` pin it symlinks into,
``d2cc0355f26235c777dfcc6867b390fa1e78083f`` (2026-08-11, five months
after the vendored snapshot) -- tell a different story once fetched
directly: cmos5l's own, non-symlinked top-level
``libs.tech/klayout/tech/lvs/sg13cmos5l.lvs`` explicitly
``%include``s both ``rule_decks/cap_cmomi_derivations.lvs`` and
``rule_decks/cap_cmomf_derivations.lvs`` in its "CAP DERIVATIONS" stage,
and the symlinked-shared ``cap_extraction.lvs`` (at the pinned sibling
commit) calls ``extract_devices(CapMomExtractor.new('cap_cmomi'), ...)``
and ``('cap_cmomf', ...)`` alongside the already-transcribed
``cap_cmim``/``rfcmim``/``sg13_hv_svaricap`` (#1456). ``cap_cmomi``
(interdigitated) and ``cap_cmomf`` (metal fringe/finger) are therefore
genuine cmos5l devices, not an sg13g2-only artifact cmos5l merely happens
to symlink in without using.

They are, however, structurally incompatible with this repo's
``CapacitorDevice`` mechanism (``decks/__init__.py``), which is why this
deck's ``capacitors`` stays ``()`` rather than gaining two new entries.
``CapacitorDevice``/``extract.py``'s consuming loop assume a purpose-drawn
``top_plate`` layer overlapping a *different-layer* ``bottom_plate``
conductor, handed to KLayout's native ``kdb.DeviceExtractorCapacitor`` as
two disjoint regions, with ``c_f`` computed from
``area_cap_f_um2``/``perim_cap_f_um`` against their overlap. ``cap_cmomi``/
``cap_cmomf`` (``custom_mom_extractor.lvs``'s ``CapMomExtractor``, a
``RBA::GenericDeviceExtractor`` subclass) recognise a device from a single
marker layer covering the whole footprint (``Recog.mom`` 99/39 /
``Recog.momf`` 99/40) containing exactly two metal-pin port shapes that
can land on the *same* metal level (the common ``double``/``none`` PCell
configuration) rather than on two independently-drawn plates -- there is
no "top plate over bottom plate" relationship to derive two ``kdb``
regions from. Their two terminals are told apart by *position* within the
marker, not by which layer they are on, and are declared electrically
equivalent for matching purposes. Most importantly, the real extractor
does **not** compute a capacitance value at all: its own source comment
states the value is supplied by the SPICE/Verilog-A compact model
(``C_total = density[N]*active_area + Cfeed``), with the extractor itself
only measuring ``w``/``l`` from the marker bounding box and matching
topologically -- closer to how this codebase's own MOS recognition
matches on dimensions than to how ``cap_cmim``/``rfcmim`` compute ``c_f``.
Modelling ``cap_cmomi``/``cap_cmomf`` correctly needs a new device shape
and new ``extract.py`` extraction logic (connected-component + port-by-
position, no computed value), not a ``CapacitorDevice`` entry -- filed as
#1466, which also covers ``sg13g2.py`` (the devices' native home; cmos5l
reaches them only through the shared symlink, and cmos5l's own
``TopMetal1``-topped stack means its instances can only ever populate the
``m1p``..``m4p`` ports, never ``m5p``).

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
    """Build a :class:`RuleProvenance` for anything read from cmos5l's own,
    non-symlinked source -- either the JSON threshold table
    (`sg13cmos5l_tech_default.json`) or cmos5l-authored `.drc` rule *text*.
    `source_repo`/`commit` are `ihp-sg13cmos5l`'s own (see the module-level
    constants above), `source_path`/`rule_id` per call.

    Every current call site is the rule-text kind: #1417's BEOL rules cite
    `beol/{5_17_metaln,5_19_via1,5_20_vian,5_21_topvia1}.drc` under
    `_DRC_BEOL_OWN` (see the module docstring's BEOL bullet for why those
    four files are cmos5l's own rather than symlinks into the sibling
    `ihp-sg13g2` checkout). That is a stronger claim than a threshold
    lookup -- it asserts the cited rule *text* lives in `ihp-sg13cmos5l`
    at `_IHP_SG13CMOS5L_COMMIT`, so use
    :func:`_cmos5l_shared_g2_provenance` instead whenever the cited text
    physically lives in that sibling checkout."""
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

# cmos5l's own, non-symlinked BEOL rule files (issue #1417) -- unlike
# `_DRC_BEOL` above (files genuinely symlinked into the sibling `ihp-sg13g2`
# checkout), `5_17_metaln.drc`/`5_19_via1.drc`/`5_20_vian.drc`/
# `5_21_topvia1.drc` are cmos5l's own real files (see the module docstring's
# "cmos5l's Metal2-Metal4/Via1-Via3/TopVia1 BEOL DRC rule files" bullet), so
# their path is relative to `ihp-sg13cmos5l`'s own repo root -- no
# `ihp-sg13g2/` prefix -- and their provenance is built with
# `_cmos5l_provenance` (this repo's own commit), not
# `_cmos5l_shared_g2_provenance`.
_DRC_BEOL_OWN = "libs.tech/klayout/tech/drc/rule_decks/beol"

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
    # "5.16. Metal1" -- #1400's original single-metal DRC stack; extended up
    # through TopMetal1 by #1417 below ---
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
    # --- 5.19 Via1 (beol/5_19_via1.drc) -- cmos5l's own, non-symlinked
    # local copy (see the module docstring's own note on why) --------------
    DrcRule(
        id="via1.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via1 width -- "
            "approximated as a minimum-width floor only, since this "
            "engine's width check has no maximum-width counterpart"
        ),
        layer=(19, 0),  # Via1.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_19_via1.drc rule "V1.a": via1_nseal.without_bbox_min/max(0.19um)
        # -> "5.19. V1.a : Min. and max. Via1 width: 0.19 um"
        # (cmos5l's own sg13cmos5l_tech_default.json: drc_rules['V1_a']
        # == 0.19)
        scope="5.19 Via1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_19_via1.drc", "V1.a"),
    ),
    DrcRule(
        id="via1.space.1",
        description="minimum Via1 spacing",
        layer=(19, 0),  # Via1.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_19_via1.drc rule "V1.b": via1_nseal.space(0.22um, euclidian)
        # -> "5.19. V1.b : Min. Via1 space: 0.22 um"
        # (drc_rules['V1_b'] == 0.22)
        scope="5.19 Via1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_19_via1.drc", "V1.b"),
    ),
    DrcRule(
        id="metal1.enclosing.via1.1",
        description="minimum Metal1 enclosure of Via1",
        layer=(8, 0),  # Metal1.drawing
        other_layer=(19, 0),  # Via1.drawing
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # 5_19_via1.drc rule "V1.c": via1_nseal.enclosed(metal1_drw, 0.01um,
        # euclidian) -> "5.19.  V1.c Min. Metal1 enclosure of Via1 is 0.01 um"
        # (drc_rules['V1_c'] == 0.01)
        scope="5.19 Via1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_19_via1.drc", "V1.c"),
    ),
    # --- 5.17 Metaln, Metal2 instance (beol/5_17_metaln.drc) -- cmos5l's
    # own, non-symlinked local copy, scoped "M2-M4 only (no M5)" -----------
    DrcRule(
        id="metal2.width.1",
        description="minimum Metal2 width",
        layer=(10, 0),  # Metal2.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M2.a" (met_no=2 instance of the templated
        # "Mn.a" rule): metal2_drw.width(0.20um, euclidian)
        # -> "5.17. M2.a: Min. Metal2 width: 0.20 um."
        # (drc_rules['Mn_a'] == 0.2, shared by Metal2-Metal4)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M2.a"),
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum Metal2 space or notch",
        layer=(10, 0),  # Metal2.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M2.b": metal2_drw.space(0.21um, euclidian)
        # -> "5.17. M2.b: Min. Metal2 space or notch: 0.21 um."
        # (drc_rules['Mn_b'] == 0.21)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M2.b"),
    ),
    # --- 5.20 Vian, Via2 instance (beol/5_20_vian.drc) -- cmos5l's own,
    # non-symlinked local copy, scoped "Via2-Via3 only (no Via4)" ----------
    DrcRule(
        id="via2.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via2 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1 above makes"
        ),
        layer=(29, 0),  # Via2.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_20_vian.drc rule "V2.a" (via_no=2 instance of the templated
        # "Vn.a" rule): via2_nseal.without_bbox_min/max(0.19um)
        # -> "5.20. V2.a : Min. and max. Via2 width: 0.19 um"
        # (drc_rules['Vn_a'] == 0.19, shared by Via2-Via3)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V2.a"),
    ),
    DrcRule(
        id="via2.space.1",
        description="minimum Via2 spacing",
        layer=(29, 0),  # Via2.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_20_vian.drc rule "V2.b": via2_nseal.space(0.22um, euclidian)
        # -> "5.20. V2.b : Min. Via2 space: 0.22 um"
        # (drc_rules['Vn_b'] == 0.22)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V2.b"),
    ),
    DrcRule(
        id="metal2.enclosing.via2.1",
        description="minimum Metal2 enclosure of Via2",
        layer=(10, 0),  # Metal2.drawing
        other_layer=(29, 0),  # Via2.drawing
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # 5_20_vian.drc rule "V2.c": via_lay.enclosed(metal_lay, 0.005um,
        # euclidian) -> "5.20. V2.c : Min. Metal2 enclosure of Via2 is
        # 0.005 um" (drc_rules['Vn_c'] == 0.005)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V2.c"),
    ),
    # --- 5.17 Metaln, Metal3 instance (beol/5_17_metaln.drc) ---------------
    DrcRule(
        id="metal3.width.1",
        description="minimum Metal3 width",
        layer=(30, 0),  # Metal3.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M3.a" (met_no=3 instance of "Mn.a"):
        # metal3_drw.width(0.20um, euclidian)
        # -> "5.17. M3.a: Min. Metal3 width: 0.20 um."
        # (drc_rules['Mn_a'] == 0.2)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M3.a"),
    ),
    DrcRule(
        id="metal3.space.1",
        description="minimum Metal3 space or notch",
        layer=(30, 0),  # Metal3.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M3.b": metal3_drw.space(0.21um, euclidian)
        # -> "5.17. M3.b: Min. Metal3 space or notch: 0.21 um."
        # (drc_rules['Mn_b'] == 0.21)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M3.b"),
    ),
    # --- 5.20 Vian, Via3 instance (beol/5_20_vian.drc) ---------------------
    DrcRule(
        id="via3.width.1",
        description=(
            "minimum (and, on the real rule, maximum) Via3 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1/via2.width.1 above "
            "make"
        ),
        layer=(49, 0),  # Via3.drawing
        check="width",
        threshold_dbu=190,  # 0.19 um
        # 5_20_vian.drc rule "V3.a" (via_no=3 instance of "Vn.a"):
        # via3_nseal.without_bbox_min/max(0.19um)
        # -> "5.20. V3.a : Min. and max. Via3 width: 0.19 um"
        # (drc_rules['Vn_a'] == 0.19)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V3.a"),
    ),
    DrcRule(
        id="via3.space.1",
        description="minimum Via3 spacing",
        layer=(49, 0),  # Via3.drawing
        check="space",
        threshold_dbu=220,  # 0.22 um
        # 5_20_vian.drc rule "V3.b": via3_nseal.space(0.22um, euclidian)
        # -> "5.20. V3.b : Min. Via3 space: 0.22 um"
        # (drc_rules['Vn_b'] == 0.22)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V3.b"),
    ),
    DrcRule(
        id="metal3.enclosing.via3.1",
        description="minimum Metal3 enclosure of Via3",
        layer=(30, 0),  # Metal3.drawing
        other_layer=(49, 0),  # Via3.drawing
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # 5_20_vian.drc rule "V3.c": via_lay.enclosed(metal_lay, 0.005um,
        # euclidian) -> "5.20. V3.c : Min. Metal3 enclosure of Via3 is
        # 0.005 um" (drc_rules['Vn_c'] == 0.005)
        scope="5.20 Vian",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_20_vian.drc", "V3.c"),
    ),
    # --- 5.17 Metaln, Metal4 instance (beol/5_17_metaln.drc) -- the top of
    # this file's own templated table (cmos5l has no Metal5) ---------------
    DrcRule(
        id="metal4.width.1",
        description="minimum Metal4 width",
        layer=(50, 0),  # Metal4.drawing
        check="width",
        threshold_dbu=200,  # 0.20 um
        # 5_17_metaln.drc rule "M4.a" (met_no=4 instance of "Mn.a"):
        # metal4_drw.width(0.20um, euclidian)
        # -> "5.17. M4.a: Min. Metal4 width: 0.20 um."
        # (drc_rules['Mn_a'] == 0.2)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M4.a"),
    ),
    DrcRule(
        id="metal4.space.1",
        description="minimum Metal4 space or notch",
        layer=(50, 0),  # Metal4.drawing
        check="space",
        threshold_dbu=210,  # 0.21 um
        # 5_17_metaln.drc rule "M4.b": metal4_drw.space(0.21um, euclidian)
        # -> "5.17. M4.b: Min. Metal4 space or notch: 0.21 um."
        # (drc_rules['Mn_b'] == 0.21)
        scope="5.17 Metaln",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_17_metaln.drc", "M4.b"),
    ),
    # --- 5.21 TopVia1 (beol/5_21_topvia1.drc) -- cmos5l's own, non-
    # symlinked local copy; unlike sg13g2's TopVia1 (which connects Metal5
    # to TopMetal1), cmos5l's TopVia1 connects **Metal4** to TopMetal1
    # (cmos5l's stack has no Metal5) -- confirmed by this file's own inline
    # comment ("TopVia1 connects Metal4 to TopMetal1") and its "TV1.c" rule
    # enclosing against `metal4_drw`, not `metal5_drw`. Like sg13g2's own
    # TopVia1, it carries two enclosure rules -- below (Metal4) and above
    # (TopMetal1) -- both transcribed here. --------------------------------
    DrcRule(
        id="topvia1.width.1",
        description=(
            "minimum (and, on the real rule, maximum) TopVia1 width -- "
            "approximated as a minimum-width floor only, the same "
            "min-size-only approximation via1.width.1 above makes"
        ),
        layer=(125, 0),  # TopVia1.drawing
        check="width",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.a":
        # topvia1_nseal.without_bbox_min/max(0.42um)
        # -> "5.21. TV1.a : Min. and max. TopVia1 width: 0.42 um"
        # (drc_rules['TV1_a'] == 0.42)
        scope="5.21 TopVia1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_21_topvia1.drc", "TV1.a"),
    ),
    DrcRule(
        id="topvia1.space.1",
        description="minimum TopVia1 spacing",
        layer=(125, 0),  # TopVia1.drawing
        check="space",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.b": topvia1_nseal.space(0.42um,
        # euclidian) -> "5.21. TV1.b : Min. TopVia1 space: 0.42 um"
        # (drc_rules['TV1_b'] == 0.42)
        scope="5.21 TopVia1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_21_topvia1.drc", "TV1.b"),
    ),
    DrcRule(
        id="metal4.enclosing.topvia1.1",
        description="minimum Metal4 enclosure of TopVia1",
        layer=(50, 0),  # Metal4.drawing
        other_layer=(125, 0),  # TopVia1.drawing
        check="enclosing",
        threshold_dbu=100,  # 0.10 um
        # 5_21_topvia1.drc rule "TV1.c": topvia1_nseal.enclosed(metal4_drw,
        # 0.10um, euclidian) -- "SG13CMOS5L: M4 is below TV1" (this file's
        # own inline comment) -> "5.21. TV1.c : Min. Metal4 enclosure of
        # TopVia1 is 0.10 um" (drc_rules['TV1_c'] == 0.1)
        scope="5.21 TopVia1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_21_topvia1.drc", "TV1.c"),
    ),
    DrcRule(
        id="topmetal1.enclosing.topvia1.1",
        description="minimum TopMetal1 enclosure of TopVia1",
        layer=(126, 0),  # TopMetal1.drawing
        other_layer=(125, 0),  # TopVia1.drawing
        check="enclosing",
        threshold_dbu=420,  # 0.42 um
        # 5_21_topvia1.drc rule "TV1.d": topvia1_nseal.enclosed(
        # topmetal1_drw, 0.42um, euclidian) -> "5.21. TV1.d : Min.
        # TopMetal1 enclosure of TopVia1 is 0.42 um" (drc_rules['TV1_d']
        # == 0.42)
        scope="5.21 TopVia1",
        provenance=_cmos5l_provenance(f"{_DRC_BEOL_OWN}/5_21_topvia1.drc", "TV1.d"),
    ),
    # --- 5.22 TopMetal1 (beol/5_22_topmetal1.drc) -- a genuine symlink into
    # the sibling ihp-sg13g2 checkout (unlike the four cmos5l-own rule
    # families above: 5_17_metaln/5_19_via1/5_20_vian/5_21_topvia1), so
    # alone among the BEOL rule families this issue adds it keeps the
    # shared-g2 provenance helper. Note the much coarser threshold (1.64 um,
    # vs. Metal2-Metal4's 0.20 um) -- verified against both the file's own
    # inline prose comment and cmos5l's own sg13cmos5l_tech_default.json's
    # TM1_a/TM1_b entries, not a transcription error. -------------------
    DrcRule(
        id="topmetal1.width.1",
        description="minimum TopMetal1 width",
        layer=(126, 0),  # TopMetal1.drawing
        check="width",
        threshold_dbu=1640,  # 1.64 um
        # 5_22_topmetal1.drc rule "TM1.a": topmetal1_drw.width(1.64um,
        # euclidian) -> "5.22. TM1.a: Min. TopMetal1 width: 1.64 um."
        # (drc_rules['TM1_a'] == 1.64)
        scope="5.22 TopMetal1",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_BEOL}/5_22_topmetal1.drc", "TM1.a"
        ),
    ),
    DrcRule(
        id="topmetal1.space.1",
        description="minimum TopMetal1 space or notch",
        layer=(126, 0),  # TopMetal1.drawing
        check="space",
        threshold_dbu=1640,  # 1.64 um
        # 5_22_topmetal1.drc rule "TM1.b": topmetal1_drw.space(1.64um,
        # euclidian) -> "5.22. TM1.b: Min. TopMetal1 space or notch:
        # 1.64 um." (drc_rules['TM1_b'] == 1.64)
        scope="5.22 TopMetal1",
        provenance=_cmos5l_shared_g2_provenance(
            f"{_DRC_BEOL}/5_22_topmetal1.drc", "TM1.b"
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
    (10, 0): "Metal2.drawing",
    (10, 2): "Metal2.pin",
    (14, 0): "pSD.drawing",
    (19, 0): "Via1.drawing",
    (24, 0): "RES.drawing",
    (28, 0): "SalBlock.drawing",
    (29, 0): "Via2.drawing",
    (30, 0): "Metal3.drawing",
    (30, 2): "Metal3.pin",
    (31, 0): "NWell.drawing",
    (31, 2): "NWell.pin",
    (44, 0): "ThickGateOx.drawing",
    (49, 0): "Via3.drawing",
    (50, 0): "Metal4.drawing",
    (50, 2): "Metal4.pin",
    (111, 0): "EXTBlock.drawing",
    (125, 0): "TopVia1.drawing",
    (126, 0): "TopMetal1.drawing",
    (126, 2): "TopMetal1.pin",
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
    # No distinct drawn tap layer (`tap` stays `None`) -- cmos5l's `ntap`/
    # `ptap` are derived as `Activ` intersected with `nwell`/`pwell` in
    # `general_derivations.lvs` (`ntap = nactiv.and(nwell_drw)...`,
    # `ptap_all = pactiv.and(pwell)...`), not a dedicated tap mask -- the
    # same "no distinct tap layer, shared with transistor active" shape
    # `sg13g2.py`'s own deck has (and, independently confirmed here: cmos5l's
    # `general_derivations.lvs`/`layers_definitions.lvs`/`mos_extraction.lvs`
    # are themselves symlinks into the pinned sibling `ihp-sg13g2` checkout,
    # see the module docstring, so this is the *same* derivation, not merely
    # an analogous one). Issue #1414 (the cmos5l sibling of sg13g2.py's own
    # #1273 fix, mirroring gf180mcu's #1084): rather than leaving those
    # well/substrate ties structurally unrecognisable, this deck declares
    # `tap_nplus`/`tap_pplus` -- the `nSD` (7/0) / `pSD` (14/0) implant
    # layers `mos_extraction.lvs` itself keys NMOS/PMOS source-drain off of
    # (`nsd_fet`/`psd_fet`, cited by `nfet_provenance`/`pfet_provenance`
    # above) -- so `extract.py` can *derive* an equivalent tap region: an
    # `nSD`-covered Activ shape *inside* NWell is a well tie (ties the PMOS
    # body to the well's real net -- opposite doping from the PMOS's own
    # S/D, which is `pSD`-covered Activ inside the same NWell, so the two
    # can never collide), and a `pSD`-covered Activ shape *outside* every
    # NWell is a substrate tie (ties the NMOS body to the substrate's real
    # net -- opposite doping from the NMOS's own S/D, which is `nSD`-covered
    # Activ outside NWell). A layout drawing no tie still extracts exactly
    # as it did before this issue (both `tap_nplus`/`tap_pplus` are
    # additive-only derivation inputs): the NMOS body terminal falls back to
    # `ExtractionDeck.substrate_net`'s synthesized global, and the PMOS body
    # terminal is a floating, anonymous net -- unless `well_label` (below)
    # already names it.
    tap=None,
    tap_nplus=(7, 0),  # nSD.drawing -- well-tie implant (n+ Activ inside NWell)
    tap_pplus=(14, 0),  # pSD.drawing -- substrate-tie implant (p+ Activ outside NWell)
    well_label=(31, 2),  # NWell.pin
    contact=(6, 0),  # Cont.drawing -- lands directly on Metal1, no li1-like
    # local-interconnect level (same single-first-metal-level shape as
    # sg13g2.py's own deck).
    # `metals`/`vias` (issue #1417): cmos5l's real BEOL stack, Metal1
    # through TopMetal1 -- Via1 connects Metal1 to Metal2, Via2 connects
    # Metal2 to Metal3, Via3 connects Metal3 to Metal4, TopVia1 connects
    # Metal4 to TopMetal1 (cmos5l has no Metal5, unlike sg13g2 -- see the
    # module docstring's own note, and `DECK`'s `topvia1.*` rules above).
    # No Metal5/Via4/TopVia2/TopMetal2: all four are on cmos5l's own LVS/DRC
    # forbidden-layer lists (see the module docstring), so the stack tops
    # here.
    metals=(
        (8, 0),  # Metal1.drawing
        (10, 0),  # Metal2.drawing
        (30, 0),  # Metal3.drawing
        (50, 0),  # Metal4.drawing
        (126, 0),  # TopMetal1.drawing
    ),
    vias=(
        (19, 0),  # Via1.drawing (Metal1 <-> Metal2)
        (29, 0),  # Via2.drawing (Metal2 <-> Metal3)
        (49, 0),  # Via3.drawing (Metal3 <-> Metal4)
        (125, 0),  # TopVia1.drawing (Metal4 <-> TopMetal1)
    ),
    # `metal_labels` uses the `.pin` (datatype 2) layers for every level,
    # consistent with this deck's own pre-existing `Metal1.pin` (8/2)
    # choice from #1400. Each was confirmed against a live
    # `ihp-sg13cmos5l` checkout's own `sg13cmos5l.lyp` (`Metal2.pin` 10/2,
    # `Metal3.pin` 30/2, `Metal4.pin` 50/2, `TopMetal1.pin` 126/2 -- each a
    # real `<name>...pin</name>`/`<source>` entry there) rather than
    # assumed by analogy. That layer map also declares a `.text` purpose
    # per level (datatype 25), which is the side `sg13g2.py` picks for this
    # same conceptual field -- so datatype 2 here is a consistency choice,
    # not the absence of a datatype-25 alternative (see the module
    # docstring's own note). TopVia1/Via1/Via2/Via3 are
    # cut layers, not conductors, so -- like sg13g2's own vias -- they carry
    # no `metal_labels` entry of their own.
    metal_labels=(
        (8, 2),  # Metal1.pin
        (10, 2),  # Metal2.pin
        (30, 2),  # Metal3.pin
        (50, 2),  # Metal4.pin
        (126, 2),  # TopMetal1.pin
    ),
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
    # Metal resistors are deliberately **not** declared, even now that
    # #1417 (above) lands the Metal2-TopMetal1 stack those bodies need. The
    # same `res_extraction.lvs` also extracts `res_metal1`..`res_topmetal2`,
    # of which only five are reachable in cmos5l at all (`res_metal5`/
    # `res_topmetal2` sit on `Metal5` 67/0 and `TopMetal2` 134/0, both on
    # cmos5l's own forbidden-layer list -- its stack is
    # M1-M2-M3-M4-TopMetal1, exactly what `EXTRACTION_DECK.metals` now
    # declares). All five remaining bodies (`res_metal1`..`res_topmetal1`)
    # are reachable as of this issue, but transcribing the family itself
    # (per-body sheet rho, `requires`/`excludes`, and each one's own golden
    # layout->netlist pair) is new scope this DRC/connectivity-focused issue
    # did not ask for -- left for a dedicated follow-on issue, mirroring
    # `sg13g2.py`'s own staged deferral of its metal-resistor family behind
    # its own stack-extension prerequisite.
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
