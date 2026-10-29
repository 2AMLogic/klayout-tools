"""gf180mcu DRC deck: a curated subset of the official rule set.

Rule thresholds below are transcribed from the published GlobalFoundries
180nm MCU ("gf180mcu") **Design Rule Manual** (DRM), fetched directly from
the canonical source repository for this issue:

- https://github.com/google/gf180mcu-pdk (Apache License 2.0), commit
  ``de3240d`` (2023-05-31) — the DRM lives at
  ``docs/physical_verification/design_manual/`` as reStructuredText
  sections with the numeric rule tables as CSV (``tables_clear/*.csv``).
  Sections/tables used below:

  - ``drm_07_05.rst`` / ``tables_clear/13_Nwell31.csv`` — 7.4 Nwell (``NW.*``)
  - ``drm_07_06.rst`` / ``tables_clear/14_COMP33_1.csv`` — 7.5 Comp (``DF.*``)
  - ``drm_07_08.rst`` / ``tables_clear/16_Poly2_42.csv`` — 7.7 Poly2 (``PL.*``)
  - ``drm_07_09.rst`` / ``tables_clear/17_Nplus_44.csv`` — 7.8 Nplus (``NP.*``)
  - ``drm_07_10.rst`` / ``tables_clear/18_Pplus_48.csv`` — 7.9 Pplus (``PP.*``)
    — the two implant sections (issue #2369). Their tables publish a single
    ``LAYOUT RULE`` column each (no 3.3V/5V split), and the two are
    transcribed independently rather than one assumed to mirror the other:
    the values do coincide, but ``PP.3b``/``PP.3c`` order their sub-cases
    the opposite way round from ``NP.3b``/``NP.3c``. Only ``NP.1``/``NP.2``/
    ``NP.3a``/``NP.5a``/``NP.5b`` and their ``PP.*`` mirrors are
    transcribed; the per-context splits (``NP.3b``-``NP.3e``, ``NP.4a``/
    ``NP.4b``, ``NP.5c``/``NP.5d``, ``NP.6``-``NP.12``, and the ``PP.*``
    equivalents) each key off a ``DNWELL``/``LVPWELL``/``SAB``/butting-edge
    context this deck's curated layer set does not draw.
  - ``drm_07_13.rst`` / ``tables_clear/21_Contact_56.csv`` — 7.12 Contact (``CO.*``)
  - ``drm_07_14.rst`` / ``tables_clear/22_Metaln_58.csv`` — 7.13 Metaln (``Mn.*``,
    ``n = 1 to 5``, i.e. ``Metal1``-``Metal5``)
  - ``drm_07_15.rst`` / ``tables_clear/23_Vian_59.csv`` — 7.14 Vian (``Vn.*``,
    ``n = 1 to 5`` in the DRM; only ``n = 1 to 4`` (``Via1``-``Via4``) are
    modeled here, matching this deck's existing ``Via1``-``Via4`` layer
    coverage (issue #220) -- this deck's curated layer set has no drawn
    ``Via5``/``n = 5`` layer (issue #546)
  - ``drm_07_16.rst`` / ``tables_clear/24_MetalTop_61.csv`` — 7.15 MetalTop
    (``MT.*``) — **not** part of the 7.13 Metaln table above: ``MetalTop`` is
    a separate drawn layer with its own DRM section and its own rule ids/
    values (``MT.1``/``MT.2a``), not an ``n = 6`` row of ``Mn.*``.
  - ``drm_10_4_2.rst`` / ``tables_clear/35_MIM2_88.csv`` — 10.4.2 MIM
    (Metal-insulator-Metal) Capacitor, Option B (``MIMTM.*``), from the
    "10.4 MIM Capacitor" section (itself under the "10.0 Analog Device
    Related Rules" chapter)
  - ``drm_10_07.rst`` / ``tables_clear/38_DRC_BJT_103.csv`` — 10.7 DRC_BJT Mark
    Layer (``BJT.*``), from the "10.0 Analog Device Related Rules" chapter —
    the DRM's vertical NPN/PNP bipolar rule category.
  - ``drm_09_01.rst`` / ``tables_clear/29_BondPad1_70.csv`` — 9.1 Bond Pad
    (``PAD.*``), section 9.0's *hard, coded* rule table (as opposed to 9.2's
    ``tables_clear/29_BondPad2_70.csv``, a *guideline* table -- not
    transcribed here, see the "PAD.4" rule's own docstring below).

Each rule's docstring below cites the exact source rule id (e.g. ``"DF.1a"``)
and its DRM description, so values can be re-verified against a fresh
checkout of that repo at any time. Unless noted otherwise, the **3.3V
column** value is used (this deck does not model the 5V/6V high-voltage
variants).

The one exception, as of issue #1110, is the ``Comp`` width/space pair: the
DRM's ``DF.1a``/``DF.3a`` each publish two materially different columns
(0.22/0.30 um and 0.28/0.36 um) selected by whether the geometry is drawn
inside ``Dualgate`` (55/0), gf180mcu's 5V/6V thick-oxide marker, and this
deck now ships **both** halves of each as a rule pair -- ``comp.width.1``/
``comp.space.1`` (the ``_LV`` halves, scoped to ``Comp`` polygons touching
no ``Dualgate``) and ``comp.width.mv.1``/``comp.space.mv.1`` (the ``_MV``
halves, scoped to ``Comp`` polygons overlapping it), via
:attr:`~klayout_tools.decks.DerivedLayer.mode`. Every *other* rule below
still encodes the 3.3V column only, and ``UNMODELED_VOLTAGE_MARKERS`` at the
bottom of this module keeps ``Dualgate`` registered for exactly that
residue.

``CO.*``/``Vn.*`` re-derivation note (issue #551): the nine
conductor-over-cut enclosure rules below (``metal1.enclosing.contact.1``
plus the eight ``metalN.enclosing.viaM.1`` rules) are the one family here
whose values were re-derived from *executable* rule-deck code rather than
from the DRM tables alone. A current PDK build ships them -- unlike the
``999a6ff`` DRC-only snapshot the provenance note below describes -- so each
cites both its DRM rule id and the statement it was re-derived from in
``libs.tech/klayout/tech/drc/rule_decks/{contact,via}.rb`` of a real fetched
install (``ciel enable --pdk gf180mcu
f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7``; re-verified 2026-10-16 against that
tree, where the older ``c6d73a3`` build kept the same statements in
``libs.tech/klayout/drc/rule_decks/{contact,via1,via2,via3,via4}.drc`` -- the
thresholds and boolean forms are identical, only the file layout and the Ruby
registry wrapper changed). The hash is the ``open_pdks`` build that vendors
the ``google/globalfoundries-pdk-libs-gf180mcu_fd_pv`` decks, not a commit of
that repo. Every one of those
statements has the form ``cut.enclosed(conductor, d, euclidian) OR
cut.not(conductor)`` -- exactly the pair of conditions our ``"enclosing"``
check reports (marginal facing-edge enclosure plus ``_run_check``'s
``outside_region`` zero-overlap escape term, see ``drc.py``) -- so none of
the nine is an approximation.

The four ``Comp`` voltage-split rules (``comp.width.1``/``comp.width.mv.1``/
``comp.space.1``/``comp.space.mv.1``, issue #1110) cite the same real
fetched install, for the same reason: the DRM's numeric tables publish the
two threshold columns, but only ``rule_decks/comp.drc`` states *how* the
columns are selected -- ``comp_3p3v = comp.not_interacting(v5_xtor)
.not_interacting(dualgate)`` and ``comp_56v = comp.overlapping(dualgate)``,
which are precisely the ``"not_interacting"``/``"overlapping"``
:class:`~klayout_tools.decks.DerivedLayer` modes those four rules use, and
which is also where the ``_LV``/``_MV`` rule-id suffixes come from. Their
``provenance`` therefore cites ``comp.drc`` rather than the DRM CSV the
other ``DF.*``-family entries cite.

Their **end-of-line** companions (``CO.6a``/``CO.6b``, ``Vn.3c``/``Vn.3d``,
``Vn.4b``/``Vn.4c``) are deliberately **not** transcribed (disclosed in
``coverage.unmodeled_rules``, issue #3968): each conditions a
0.06um margin on a narrow-metal (< 0.34um) end-of-line predicate, expressed
in the PDK deck via ``first_edges``/``second_edges``/``extended_in``
edge-set operations that :class:`~klayout_tools.decks.DrcRule`'s
(layer, other_layer, check, threshold) vocabulary cannot express at all --
the same class of gap as sky130's untranscribed holes-area rules
(``m1.7``/``m2.7``/``m3.7``/``m4.7``/``m5.7``), and left for a follow-on
that extends the vocabulary rather than approximated with a wrong
threshold.

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
rules across dozens of layers, plus 5V/6V variants, DFM guidelines, etc.) —
mirrors the "curated starter subset" scope guard sky130 documents:
width/space/enclosure checks across poly2/comp/contact/metal1 (extended, per
#188, to metal2/metal3/metal5/metaltop and the MiM capacitor stack, per #546
to via1-via4 size/spacing, and per #551 to the conductor-over-cut enclosures
that tie the two together), plus a
first increment of well/substrate-tap coverage (Nwell), one bipolar
(BJT)-specific device rule, and one bond-pad rule (``PAD.4``, issue #545),
wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) for a second PDK. Coverage is
expected to grow incrementally in follow-on issues (as of #2369 the
Nplus/Pplus implant layers are covered for the five context-free rule
shapes named in the ``7.8 Nplus``/``7.9 Pplus`` citation above -- their
per-context splits, LVPWELL/DNWELL, the remaining BJT rules that key off
DNWELL/LVPWELL, 5V/6V variants, and DFM guidelines all remain uncovered). ``MIMTM.2``'s
sized/derived-layer need (see its own note below) is closed as of #345 via
:class:`~klayout_tools.decks.DerivedLayer`; ``MIMTM.1``'s equivalent need is
closed as of #1033, the same way.

``klt extract``'s MiM-capacitor device recognition (``EXTRACTION_DECK.capacitors``
below, issue #225) is transcribed from a *different*, LVS-specific source
than the DRC rules above: the DRM's "10.4 MIM Capacitor" section publishes
only the DRC-checkable geometry rules (``MIMTM.*``, above), not a
device-recognition derivation or a capacitance-per-area coefficient. Both
come from ``globalfoundries-pdk-libs-gf180mcu_fd_pv``'s companion **KLayout
LVS** deck (a sibling of the DRC deck cited above, under ``klayout/lvs/``
rather than ``klayout/drc/``) — unlike the DRC deck, this was not present in
the ``999a6ff`` DRC-only snapshot cited above, so it is instead cited from a
real fetched PDK install: ``volare enable gf180mcu
c6d73a35f524070e85faff4a6a9eef49553ebc2b`` (the same commit ``open_pdks``
itself; its ``gf180mcu/Makefile.in`` copies ``${GF180MCU_PV_PATH}/klayout/lvs/*``
from that verification-library fetch), giving
``libs.tech/klayout/lvs/rule_decks/mimcap_derivations.lvs`` (the "virtual
bottom plate" geometry derivation), ``mimcap_extraction.lvs`` (the
``extract_devices(capacitor(name, area_cap, class), {...})`` call and its
``2.0e-15`` F/um² coefficient) and ``layers_definitions.lvs`` (the
``topmin1_metal``/``CAP_MK``/``mim_l_mk`` layer roles). Cross-checked
against two more independent sources in the same open_pdks build:
``gf180mcu-pdk``'s own ``docs/analog/model_parameters/LV/tables_clear/08_MIM.csv``
(a device-model table publishing the same three selectable MiM densities,
"1.0fF/um2 MIM" / "1.5fF/um2 MIM" / "2.0fF/um2 MIM"), and ``open_pdks``'
own ``gf180mcu/gf180mcu.json`` PDK-variant description string, which
literally reads ``"Global Foundries 0.18um MCU CMOS, 2fF MiM + 1k high
sheet rho poly"`` for this built variant (confirming ``2.0`` fF/um² -- not
``1.0``/``1.5`` -- is the one this specific PDK build ships).

``area_cap_f_um2``/``perim_cap_f_um`` (issue #512) are refined from the LVS
deck's own ``2.0e-15`` F/um² above to the *simulation* model's own
two-term law, ``c_c0 = c_cox * c_AREA + c_capsw * c_PERI``, transcribed from
the same ``volare enable gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b``
fetch's ``libs.tech/ngspice/sm141064.ngspice``, ``.subckt cap_mim_2f0fF``
(the "2.0fF/um² MiM" variant matching the LVS deck's own default cited
above): ``c_cox = 1.99e-3`` F/m² = ``1.99e-15`` F/um² (the area term --
0.5% below the LVS deck's rounded ``2.0e-15`` nominal-density label) and
``c_capsw = 2.383e-10`` F/m = ``2.383e-16`` F/um (the previously-unmodelled
perimeter/fringe term). Both device-recognition *geometry* (this deck's own
job) and the simulation model's *coefficients* trace to the same PDK
snapshot, so using the model card's own numbers here -- rather than the
LVS-extraction script's separately-rounded restatement of them -- keeps
``klt extract``'s reported ``c_f`` consistent with what the same drawn
geometry simulates as.

The other two selectable MiM densities cited above -- ``cap_mim_1f0fF``
(1.0 fF/um²) and ``cap_mim_1f5fF`` (1.5 fF/um²) -- are now caller-selectable
via ``EXTRACTION_DECK.capacitors``' ``flavour_option="mim_cap"``/``flavours``
(issue #1151), the capacitor sibling of ``ppolyf_u_1k``'s own
``flavour_option="poly_res"`` below: all three densities are drawn as the
*exact same* ``FuseTop``-over-``Metal4`` geometry (``mimcap_extraction.lvs``'s
``case MIM_CAP`` selects among them purely by a build-time deck variable, not
by any additional drawn layer), so a design that legitimately commits to the
1.0/1.5 fF/um² option has no geometric marker this deck could key off --
before #1151 it either failed DRC (routing the top-plate via around the
bottom plate to dodge the resulting false short) or passed DRC while
silently shorting its two plate nets together, exactly the way a `ppolyf_u`
segment marked for an unmodelled sheet-rho flavour would extract as a short
before #299/#595. ``klt extract --deck-option
mim_cap=cap_mim_1f0_m4m5_noshield`` (or ``...=cap_mim_1f5_m4m5_noshield``)
now selects the matching flavour explicitly instead. Their coefficients are
the same ``sm141064.ngspice`` file's ``.subckt cap_mim_1f0fF``/
``cap_mim_1f5fF``, at the library's own ``.LIB mimcap_typical``
corner (``mim_corner_1p0fF=1``/``mim_corner_1p5fF=1``, i.e. no corner
scaling -- the same "typical" corner ``cap_mim_2f0fF`` above is read at):
``cap_mim_1f0fF``'s ``c_cox = 0.987e-3`` F/m² = ``9.87e-16`` F/um² /
``c_capsw = 3.3e-10`` F/m = ``3.3e-16`` F/um (LVS deck's own rounded
``1.0e-15`` nominal-density label, ``cap_mim_1f0_m4m5_noshield``), and
``cap_mim_1f5fF``'s ``c_cox = 1.47e-3`` F/m² = ``1.47e-15`` F/um² /
``c_capsw = 3.79e-10`` F/m = ``3.79e-16`` F/um (LVS deck's own rounded
``1.5e-15`` nominal-density label, ``cap_mim_1f5_m4m5_noshield``) -- both
confirmed directly against a real fetched
``volare enable gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b`` install's
``libs.tech/ngspice/sm141064.ngspice``, and against that same install's
``libs.tech/klayout/lvs/rule_decks/mimcap_extraction.lvs``, whose ``MIM_OPTION
'B'``/``METAL_LEVEL '5LM'`` branch names ``cap_mim_1f0_m4m5_noshield``
(``MIM_CAP == '1'``) and ``cap_mim_1f5_m4m5_noshield`` (``MIM_CAP == '1.5'``)
as the sibling device-class names on the exact same ``Metal4``/``Via4``/
``Metal5`` stack the deck's default ``cap_mim_2f0_m4m5_noshield`` entry
already models -- the same ``mimtm_virtual``/``fuse_cap`` terminal pair, only
the ``extract_devices(capacitor(name, area_cap, class), ...)`` call's own
``name``/``area_cap`` arguments differ per density. Like every other
declared flavour in this deck, selecting ``mim_cap`` never changes *which*
geometry is recognised (still exactly one drawn ``FuseTop``-over-``Metal4``
overlap, never duplicated) -- only what device it is reported as and what
capacitance it reports.

Twenty-five rules below approximate the official DRM rule in some way (each
is called out again in its own docstring below); the threshold *values* used
are always the real, unmodified DRM values:

- ``comp.space.1``/``comp.space.mv.1``: the official ``DF.3a_LV``/
  ``DF.3a_MV`` text scopes to a substrate-tap butting context, and the PDK
  deck further excludes the one-time-programmable marker ``otp_mk``
  (173/5); approximated as a general COMP-to-COMP spacing check within each
  rule's own voltage-scoped ``comp`` region.
- ``comp.width.1``/``comp.space.1``: the PDK deck's own thin-oxide region is
  ``comp_3p3v = comp.not_interacting(v5_xtor).not_interacting(dualgate)``;
  these two model the ``dualgate`` half of that (issue #1110) but not the
  ``v5_xtor`` (112/1) half, which this deck's curated layer set does not
  draw -- so ``Comp`` marked only by ``v5_xtor`` is checked here against the
  3.3V column, exactly as it was before the ``Dualgate`` split.
- ``comp.width.mv.1``: the PDK deck's ``DF.1a_MV`` additionally excludes the
  LDMOS drain markers ``mvsd`` (210/0) / ``mvpsd`` (11/39), which this
  deck's curated layer set does not draw -- so a 5V LDMOS drift region is
  checked here where the official rule would not check it (conservative: a
  false positive on a device this deck cannot recognise, never a missed
  violation).
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
- ``contact.width.1``/``via1.width.1``/``via2.width.1``/``via3.width.1``/
  ``via4.width.1``: ``CO.1`` (fixed 0.22 x 0.22um contact) and ``Vn.1``
  (fixed 0.26 x 0.26um via) are min **and** max size rules, and **both
  halves are now enforced** (issue #2370) -- this was previously listed here
  as a min-only approximation. The max half is measured as a bounding-box
  size bound (``DrcRule.threshold_max_dbu``, see ``_run_width_max_check`` in
  ``drc.py``), not as an inverted ``width_check``, so an oversized square, an
  elongated bar, and an L-shaped cut are all flagged. The one residual
  conservatism: a *non-axis-aligned* cut (e.g. a 45-degree-rotated square) is
  measured by its axis-aligned envelope rather than its true size, so it can
  be flagged while nominally within the fixed size -- these rules' official
  geometry is an axis-aligned square, so no realistic drawn cut hits this.
- ``via1.space.1``/``via2.space.1``/``via3.space.1``/``via4.space.1``: the
  official ``Vn.2a``/``Vn.2b`` split via-to-via spacing by array density
  (``Vn.2a`` 0.26um for an ordinary two-via space, ``Vn.2b`` a tighter 0.36um
  inside a >=4x4 via array); our engine's ``space_check`` primitive has no
  array-density context, so this is approximated using only the less strict
  ``Vn.2a`` value across the whole via drawn layer -- this means vias inside
  a genuine >=4x4 array, spaced between 0.26um and 0.36um apart, will **not**
  be flagged even though the real DRM's ``Vn.2b`` would flag them (the same
  class of context-collapsing approximation as ``nwell.space.1`` below).
- ``nwell.space.1``: the official ``NW.2a``/``NW.2b`` split Nwell-to-Nwell
  spacing by net-potential context (``NW.2a`` 0.6um for equipotential wells
  that may later be merged, ``NW.2b`` 1.4um for wells at different
  potentials); our engine has no connectivity/netlist information, only
  geometry, so it cannot distinguish the two contexts. Approximated using
  the less strict ``NW.2a`` (equipotential) value across the whole ``Nwell``
  drawn layer — this means two Nwell shapes at genuinely different
  potentials, spaced between 0.6um and 1.4um apart, will **not** be flagged
  even though the real DRM's ``NW.2b`` would flag them.
- ``nwell.enclosing.comp.1``: the official ``DF.4d`` scopes to NCOMP (the
  ``comp`` drawn layer where it overlaps ``Nplus``, i.e. specifically an
  Nwell tap) rather than all of ``comp``; approximated as Nwell enclosing
  the whole ``comp`` drawn layer, since isolating NCOMP requires the same
  boolean layer expression (``comp AND Nplus``) our engine does not
  evaluate.
- ``nplus.space.pcomp.1``/``pplus.space.ncomp.1``: the official ``NP.3a``/
  ``PP.3a`` are one of four well-context splits of the same
  implant-to-opposite-diffusion spacing rule -- ``NP.3b``/``NP.3c`` (and
  their ``PP.*`` mirrors) publish the same 0.16um where the opposite COMP
  sits within 0.43um of an ``LVPWELL``/``Nwell`` edge but a looser 0.08um
  further away, selected by a ``DNWELL``/``LVPWELL`` boolean our engine
  cannot evaluate. Approximated by applying the stricter 0.16um everywhere
  (the conservative direction: a 0.08-0.16um gap in the far context is
  over-flagged, no real violation is missed) -- note this is the *opposite*
  choice from ``nwell.space.1``/``via*.space.1`` above, which take the less
  strict value of their own splits, because here the geometric context that
  selects the looser value is the rarer one. Both also drop the PDK deck's
  own butted-COMP exclusion (``nplus.not_interacting(ncomp_butted)``, the
  zero-space ``NP.3d``/``NP.3e`` cases), so a deliberately butted NCOMP/
  PCOMP pair is flagged here where the official rule permits it. The
  *checked region* is the real ``PCOMP``/``NCOMP`` boolean via
  :class:`~klayout_tools.decks.DerivedLayer`, not raw ``Comp`` -- see the
  rules' own comments for why an unscoped version would be actively wrong.
  **Well-distance split (issue #3484):** the ``PCOMP`` side of ``NP.3`` is no
  longer collapsed -- ``nplus.space.pcomp.1`` is the near-well half (a
  ``PCOMP`` within 0.429um of an ``Nwell``, or touching a ``Dnwell``, at
  0.16um) and ``nplus.space.pcomp.far.1`` (``NP.3cii``, 0.08um) the exact
  complement, selected with ``DerivedLayer.context``. The ``Dnwell``/
  ``LVPWELL`` relaxation ``NP.3bii`` is still not expressed (strict). The
  mirrored ``pplus.space.ncomp.1`` keeps the flat 0.16um.
- ``nplus.enclosing.poly2.1``/``pplus.enclosing.poly2.1``: the official
  ``NP.5a``/``PP.5a`` scope to ``ngate``/``pgate``, which the PDK deck's own
  ``main.drc`` derives from four drawn layers plus two well/marker
  subtractions (``ngate = (comp AND nplus).not(nwell OR dnwell_n) AND
  ((poly2 AND comp).not(res_mk))``, and the ``pgate`` mirror) --
  ``DerivedLayer`` expresses a two-layer boolean only; approximated with the
  two-layer gate region (``poly2 AND comp``), so
  *every* transistor gate is measured against both implants. A gate of one
  channel type drawn within 0.23um of the opposite implant is therefore
  flagged where the official rule would not look at it -- conservative (a
  false positive on geometry no ``NP.5a``/``PP.5a`` violation can hide in),
  never a missed violation.
- ``nplus.enclosing.comp.1``/``pplus.enclosing.comp.1``: the official
  ``NP.5b``/``PP.5b`` exclude butting edges (``nplus.edges.not
  (nplus_butted_edges)``) and are relaxed to as little as 0.02um by the
  ``NP.5c``/``NP.5d`` (``PP.5c``/``PP.5d``) well-context splits for a tap
  further than 0.43um from an ``LVPWELL``/``Nwell`` edge; neither
  sub-threshold split is expressible here, so the strict 0.16um is applied
  to every NCOMP/PCOMP edge this rule actually checks -- **except** (issue
  #3484) for ``pplus.enclosing.comp.1``'s PCOMP, which is now split by
  ``DerivedLayer.context``: PCOMP within 0.429um of an ``Nwell`` or touching a
  ``Dnwell`` stays on the strict 0.16um, the rest moves to
  ``pplus.enclosing.comp.far.1`` (``PP.5di``, 0.02um). ``NP.5c``/``NP.5d`` and
  ``PP.5ci`` remain unexpressed (strict). They also measure
  with KLayout's default ``euclidian`` metric where the official rules use
  ``projection`` -- again the conservative direction (a corner distance the
  official rule ignores can be reported, never the reverse). **Unlike
  ``NP.5b``, whose two sub-cases both resolve to the same 0.16um regardless
  of well context, ``PP.5b`` itself only ever applies to a PCOMP shape
  "(1) Inside NWELL (2) outside LVPWELL but inside DNWELL"** -- a PCOMP
  shape with no ``Nwell``/``Dnwell`` context anywhere instead falls under
  ``PP.5di``/``PP.5dii`` at the much looser 0.02-0.16um range above,
  structurally incapable of being correctly checked at a flat 0.16um
  (issue #2634, confirmed a false positive on ordinary ``ppolyf_u_*``
  poly-resistor P+ substrate taps against a real fetched
  ``rule_decks/pplus.drc``). ``pplus.enclosing.comp.1`` therefore also
  carries ``requires_any_layer=((21, 0), (12, 0))`` (``Nwell``, ``Dnwell``):
  a layout drawing neither anywhere skips the whole rule rather than
  misapplying it, while a layout drawing either keeps the approximations
  above unchanged. ``nplus.enclosing.comp.1`` needs no equivalent gate.
- ``comp.coverage.1`` (``DF.12``): evaluated flat (no deep/hierarchical
  mode) on merged regions, as the upstream ``comp.rb`` expression
  ``comp.not_interacting(schottky_diode).not(nplus).not(pplus)``. A COMP
  polygon that touches or overlaps a Schottky-diode marker (241/0) anywhere
  is exempted whole, exactly as upstream; an absent implant or marker layer
  is an empty region, so unimplanted COMP is reported rather than skipped.
  The well/voltage partitions of ``DF.13``/``DF.14`` are not involved. The rule
  is not voltage-split in the DRM, hence ``voltage_independent``.
- ``comp.tap_distance.nwell.{lv,mv}.1`` / ``comp.tap_distance.psub.{lv,mv}.1``
  (DRM ``DF.13_LV``/``DF.13_MV`` and ``DF.14_LV``/``DF.14_MV``,
  ``check="tap_distance"``, issues #3607, #3793): the 3.3 V (20 um) and 5 V/6 V
  (15 um) columns; the ``latchup_mk`` marker rules are not declared. Evaluated
  flat on merged regions as a ``klayout.db.Region`` translation of the
  upstream ``comp.rb`` operation sequence
  (``docs/design/gf180mcu-comp-checks-spike.md``). Recorded approximations:
  **D1** selection is of *whole* active polygons that do not interact with the
  tap reach region, so a long active with one end near a tap is legal, as
  upstream; **D2** ``DF.13`` clips every growth step with the raw ``Nwell``
  layer exactly as upstream, not with ``Nwell`` plus ``DNWELL`` -- a
  DNWELL-only well therefore flags its PCOMP; **D3** the LV selection (touching
  neither ``v5_xtor`` nor ``dualgate``) and the MV one (overlapping
  ``dualgate``) are not complements, so active touching ``v5_xtor`` alone is
  covered by neither; **D7** only upstream's ``flat`` mode was compared, not
  its ``deep``/tiled modes. ``DF.13`` measures with octagon steps and
  ``DF.14`` with a Euclidean separation, so the two rules do not share one
  boundary metric.
- ``bjt.separation.comp.1``: the official ``BJT.3`` scopes to COMP
  "unrelated" to the BJT device (i.e. excludes COMP that is itself part of
  the same bipolar device, a connectivity/netlist notion); our engine has no
  connectivity information, only geometry, so it is approximated as a
  separation check against every ``comp`` shape on the layout, which may
  over-flag COMP that is legitimately part of the same BJT structure.
- ``mim.space.1``: scoped to the real virtual bottom plate as of #1033 (see
  the note immediately below), so it no longer over-flags ordinary
  ``Metal4`` routing. One narrow approximation remains, in the "adjacent
  MiM" half of ``MIMTM.1`` only: plate-to-plate spacing is measured between
  the *whole* ``Metal4`` polygons that carry a virtual bottom plate, not
  between the sized-``FuseTop`` plate outlines themselves, so two
  neighbouring MiM capacitors whose plate metals face each other across
  routing tails (metal that is part of neither virtual plate) closer than
  1.2um are flagged even though the plates proper clear the rule. This is
  deliberately the conservative direction, and is confined to ``Metal4``
  that already touches a ``FuseTop`` shape -- measuring the clipped plate
  outlines instead would falsely split one *shared* bottom plate carrying
  two top plates more than 2 x 1.06um apart into two "plates" with a gap
  between them, reporting a violation across continuous metal. Threshold
  value unmodified.

``MIMTM.1`` (transcribed below as ``mim.space.1``) and ``MIMTM.2``
("min. MiM bottom-plate overlap of ``Via4``", 0.4um, transcribed below as
``mim.enclosing.via4.1``, issue #345) both scope to the MiM capacitor's
"virtual bottom plate" (per the DRM's own note: ``FuseTop`` sized/oversized
by 1.06um, intersected with ``Metal4``) rather than raw ``Metal4``, and are
both rewritten to use :class:`~klayout_tools.decks.DerivedLayer`
(``klayout_tools/decks/__init__.py``) to enforce that scope --
``mim.enclosing.via4.1`` since #345, and ``mim.space.1`` since #1033 (it
previously approximated ``MIMTM.1`` as a general ``Metal4``-to-``Metal4``
``space`` check across the whole drawn layer -- flagging *any* two ordinary
``Metal4`` shapes closer than 1.2um apart, PDN power-stripe or routing
geometry included, regardless of whether the layout drew a MiM capacitor
anywhere -- because ``DerivedLayer`` only derives a checked *region* and
``space_check`` is a single-region primitive; #1033 closes this gap by
rewriting the rule as a ``separation`` check between the derived virtual
bottom plate and the rest of ``Metal4`` instead, see
``mim.space.1``'s own rule comment below and ``drc.py``'s
``run_drc()``/``other_region`` handling for the "other" side's own
whole-polygon exclusion of any ``Metal4`` shape overlapping the raw,
unsized ``FuseTop`` region).

``MIMTM.1``'s text covers spacing to the bottom plate metal "whether
adjacent MiM **or** routing metal", and that whole-polygon exclusion drops
the *adjacent MiM* half out of the ``separation`` check's "other" side (a
neighbouring cap's plate metal overlaps ``FuseTop`` too, so it is excluded
along with this rule's own plate). ``run_drc()`` therefore measures that
half separately, as a peer-to-peer ``isolated_check`` among the excluded
plate-bearing polygons, reported under the same ``mim.space.1`` rule id --
``isolated_check`` (spacing between *different* polygons) rather than
``space_check``, so a slot cut into one cap's own wide plate metal, a
routine density/stress-relief feature and not "spacing to another bottom
plate", is not reported. See ``tests/test_drc.py``'s
``test_run_drc_gf180mcu_mim_space_adjacent_mim_violation`` (the covered
half), ``..._slotted_bottom_plate_clean`` and
``..._shared_plate_two_top_plates_clean`` (the two false positives that
choice avoids), and the ``mim.space.1`` bullet above for the residual
conservatism it accepts in exchange.

An *unscoped* approximation of ``MIMTM.2`` (checking raw ``Metal4``
enclosure of every ``Via4``) would have been actively wrong, not merely
conservative like ``mim.space.1``'s pre-#1033 approximation: ordinary
``Metal4``-to-``Metal5`` routing vias use a much smaller enclosure than a
MiM cap's virtual bottom plate requires, so a blanket "``Metal4`` must
enclose every ``Via4`` by 0.4um" check would flag legitimate routing vias
throughout *any* layout using ``Metal4``/``Via4``/``Metal5`` at all, not
just genuine MiM structures -- that is exactly the false-positive risk
``DerivedLayer`` exists to avoid: the checked region is only the part of
``Metal4`` that already overlaps a ``FuseTop`` shape somewhere, oversized by
the DRM's own 1.06um margin, so ordinary routing with no MiM cap anywhere
nearby has an empty derived region and never trips this rule (see
``tests/test_drc.py``'s
``test_run_drc_gf180mcu_mim_enclosing_via4_ordinary_routing_clean`` negative
control, and its ``mim.space.1`` sibling
``test_run_drc_gf180mcu_mim_space_ordinary_routing_no_mim_devices_clean``,
added by #1033).

The DRM's "10.4 MIM Capacitor" section defines two mutually-exclusive
process options (a PDK is wired for one or the other, never both): Option A
(``MIM.*``, bottom plate on ``Metal2``, for a 3-metal-layer process variant)
and Option B (``MIMTM.*`` above, bottom plate on ``Metal(n-1)`` of an
n-metal stack, for a 4-or-more-metal-layer process variant). This deck's
``Metal1``-``Metal5`` (``Mn.*``) coverage models the 5-metal-layer gf180mcu
variant -- confirmed by ``globalfoundries-pdk-libs-gf180mcu_fd_pv``'s
``main.drc``, whose connectivity (``topmin1_metal = metal4`` /
``top_via = via4`` when ``top_metal = metal5``) matches Option B's bottom
plate landing on ``Metal4`` for that stack, the same stack the issue's own
reproducer layout uses (``Metal4``/``FuseTop``/``Via4``/``Metal5``) -- so
only Option B (``MIMTM.*``) is transcribed above, and is the default for
``klt extract``. Option A's ``MIM.*`` DRC rule set is not duplicated here, but
its *extraction* is selectable (issue #2920): ``--deck-option mim_option=A``
recognises the ``Metal2``/``FuseTop``/``Via2``/``Metal3`` stack and the
``cap_mim_*_m2m3_noshield`` device names (see ``_MIM_OPTION_A_CAPACITORS``).

Layer numbers (verified against ``google/globalfoundries-pdk-libs-gf180mcu_fd_pv``'s
``klayout/drc/rule_decks/main.drc`` layer derivations, e.g. ``comp =
get_polygons(22, 0)``, cross-checked against this repo's own gf180mcu
corpus fixtures in ``tests/corpus/golden/gf180mcu/*.layers.json`` where
present, and (for layers absent from both -- ``DRC_BJT``, and the
metal2-metal5/metaltop/MiM-stack layers added by #188) against the DRM's own
``tables_clear/06_Drawn_layer10.csv`` drawn-layer/GDS-number table; the
#188 additions are *also* independently confirmed present in ``main.drc``
itself, e.g. ``metaltop_drawn = get_polygons(53, 0)`` and ``mim_l_mk =
get_polygons(117, 10)``. The ``Via1``/``Via2``/``Via3`` numbers added by #220
(to populate ``EXTRACTION_DECK``'s inter-metal connectivity) come from the
same ``main.drc`` via-layer derivations -- ``via1 = get_polygons(35, 0)``,
``via2 = get_polygons(38, 0)``, ``via3 = get_polygons(40, 0)`` -- which sit
alongside the already-transcribed ``via4 = get_polygons(41, 0)``):

    Nwell     21/0
    Comp      22/0
    Pplus     31/0
    Nplus     32/0
    Poly2     30/0
    Contact   33/0
    Metal1    34/0
    Via1      35/0
    Metal2    36/0
    Via2      38/0
    Metal3    42/0
    Via3      40/0
    Metal4    46/0
    Via4      41/0
    Metal5    81/0
    MetalTop  53/0
    FuseTop   75/0
    Dualgate  55/0
    DRC_BJT   127/5
    MIM_L_MK  117/10
"""

from __future__ import annotations

import dataclasses

from . import (
    BipolarDevice,
    CapacitorDevice,
    CapacitorFlavour,
    CapacitorVariant,
    CompCoverageSpec,
    CutRC,
    DerivedLayer,
    DiodeDevice,
    DrcRule,
    ExtractionDeck,
    LayerRC,
    MosCapacitorDevice,
    MOSFlavour,
    ParasiticsDeck,
    ResistorDevice,
    ResistorFlavour,
    RuleProvenance,
    TapDistanceSpec,
    UnsupportedDeviceMarker,
)

# `RuleProvenance.source_repo`/`.commit` shared by every gf180mcu width/space
# rule's provenance below (issue #747, piloted on this deck's 26 width/space
# rules only) -- the same `google/gf180mcu-pdk` DRM repo/commit the module
# docstring's own top-of-file provenance note already cites for every rule
# transcribed from the published DRM tables (as opposed to the nine
# `CO.*`/`Vn.*` conductor-over-cut enclosure rules re-derived from a
# *different* repo/commit, `globalfoundries-pdk-libs-gf180mcu_fd_pv` -- as
# of issue #1110 that set includes the four width/space `Comp` voltage-split
# rules alongside the nine `CO.*`/`Vn.*` enclosure rules, so it is no longer
# true that "none of those are width/space checks"; those four use
# `_gf180mcu_klayout_deck_provenance` below instead of this constant pair).
_GF180MCU_PDK_REPO = "google/gf180mcu-pdk"
_GF180MCU_PDK_COMMIT = "de3240d"

# DRM section (`DrcRule.scope`, issue #566) -> the numeric CSV table under
# `docs/physical_verification/design_manual/tables_clear/` this deck's
# width/space rule values were transcribed from, per the module docstring's
# own section/table citation list above. "9.1 Bond Pad" and "10.7 DRC_BJT
# Mark Layer" (issue #904, Epic #711 Phase 3a) extend this to the two DRM
# sections the module docstring's own citation list already names
# (`tables_clear/29_BondPad1_70.csv`, `tables_clear/38_DRC_BJT_103.csv`) but
# that had no `_GF180MCU_DRM_CSV` entry yet -- needed to backfill
# `pad.enclosing.metal5.1`/`bjt.separation.comp.1`'s provenance below.
_DRM_TABLES_DIR = "docs/physical_verification/design_manual/tables_clear"
_GF180MCU_DRM_CSV: dict[str, str] = {
    "7.4 Nwell": f"{_DRM_TABLES_DIR}/13_Nwell31.csv",
    "7.5 Comp": f"{_DRM_TABLES_DIR}/14_COMP33_1.csv",
    "7.7 Poly2": f"{_DRM_TABLES_DIR}/16_Poly2_42.csv",
    "7.8 Nplus": f"{_DRM_TABLES_DIR}/17_Nplus_44.csv",
    "7.9 Pplus": f"{_DRM_TABLES_DIR}/18_Pplus_48.csv",
    "7.12 Contact": f"{_DRM_TABLES_DIR}/21_Contact_56.csv",
    "7.13 Metaln": f"{_DRM_TABLES_DIR}/22_Metaln_58.csv",
    "7.14 Vian": f"{_DRM_TABLES_DIR}/23_Vian_59.csv",
    "7.15 MetalTop": f"{_DRM_TABLES_DIR}/24_MetalTop_61.csv",
    "10.4.2 MIM Option B": f"{_DRM_TABLES_DIR}/35_MIM2_88.csv",
    "9.1 Bond Pad": f"{_DRM_TABLES_DIR}/29_BondPad1_70.csv",
    "10.7 DRC_BJT Mark Layer": f"{_DRM_TABLES_DIR}/38_DRC_BJT_103.csv",
}


def _gf180mcu_provenance(scope: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a DRM-table-sourced gf180mcu
    rule -- `source_path` from `scope`'s own DRM section via
    `_GF180MCU_DRM_CSV`, everything else shared across the whole deck (see
    the module-level constants above). Originally scoped to the 26
    width/space rules piloted by issue #747; issue #904 (Epic #711 Phase 3a)
    reuses it unchanged for the DRM-table-sourced subset of the deck's
    remaining enclosing/separation rules (the other subset -- the nine
    `CO.*`/`Vn.*` conductor-over-cut rules re-derived from *executable* rule
    -deck code rather than a DRM table, plus the four `Comp` voltage-split
    rules issue #1110 re-derived the same way -- uses
    `_gf180mcu_klayout_deck_provenance` below instead)."""
    return RuleProvenance(
        source_repo=_GF180MCU_PDK_REPO,
        source_path=_GF180MCU_DRM_CSV[scope],
        rule_id=rule_id,
        commit=_GF180MCU_PDK_COMMIT,
    )


# `RuleProvenance` for the nine `CO.*`/`Vn.*` conductor-over-cut enclosure
# rules re-derived from *executable* rule-deck code (issue #551's own
# "CO.*/Vn.* re-derivation note", a few lines below) rather than from the DRM
# tables `_gf180mcu_provenance` above cites -- issue #904 (Epic #711 Phase
# 3a) backfills these nine rules' `provenance`. `source_repo` is the same
# companion KLayout-runnable DRC deck the module docstring's own top-of-file
# "Provenance note" paragraph already names
# (`google/globalfoundries-pdk-libs-gf180mcu_fd_pv`); `commit` is the
# specific `ciel enable --pdk gf180mcu` pin the re-derivation note itself cites
# for these nine rules -- *not* the `999a6ff` DRC-only snapshot the
# top-of-file note describes for the bulk of this deck's width/space/
# enclosure transcription (that snapshot predates these nine rules, per the
# re-derivation note's own "unlike the 999a6ff DRC-only snapshot" wording).
_GF180MCU_KLAYOUT_DRC_DECK_REPO = "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
_GF180MCU_KLAYOUT_DRC_DECK_COMMIT = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"


def _gf180mcu_klayout_deck_provenance(source_file: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a rule re-derived from the real,
    executable `libs.tech/klayout/tech/drc/rule_decks/*.rb` rather than a DRM
    table: the nine `CO.*`/`Vn.*` conductor-over-cut enclosure rules
    (issue #551's re-derivation note, `{contact,via}.rb`) and the four `Comp`
    voltage-split rules (issue #1110, `comp.rb` -- the
    only published statement of *how* the DRM's `_LV`/`_MV` threshold
    columns are selected). `source_file` is the bare file stem (e.g.
    `"contact"`, `"via"`, `"comp"`)."""
    return RuleProvenance(
        source_repo=_GF180MCU_KLAYOUT_DRC_DECK_REPO,
        source_path=f"libs.tech/klayout/tech/drc/rule_decks/{source_file}.rb",
        rule_id=rule_id,
        commit=_GF180MCU_KLAYOUT_DRC_DECK_COMMIT,
    )


# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001, same as sky130), so a threshold in micrometres
# times 1000 gives threshold_dbu. `run_drc()` rescales threshold_dbu by
# NOMINAL_DBU_UM / layout.dbu at run time, so the deck still gives correct
# results against a layout written at a different dbu (see DrcRule's
# docstring).
NOMINAL_DBU_UM = 0.001

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
        scope="7.7 Poly2",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.7 Poly2", "PL.1"),
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
        scope="7.7 Poly2",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.7 Poly2", "PL.3a"),
    ),
    DrcRule(
        id="poly2.enclosing.contact.1",
        description="minimum poly2 overlap of contact",
        layer=(30, 0),  # Poly2
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.3": "Poly2 overlap of contact" -> 0.07um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.3"),
    ),
    DrcRule(
        id="comp.width.1",
        description="minimum COMP (diffusion/active) width outside Dualgate (3.3V)",
        layer=(22, 0),  # Comp
        check="width",
        threshold_dbu=220,  # 0.22 um
        # DRM 7.5 Comp, rule "DF.1a_LV": "Min. COMP Width" -> 0.22 (3.3V
        # column). Voltage-scoped as of issue #1110: the checked region is
        # `Comp` polygons that touch no `Dualgate` (55/0) geometry at all,
        # the PDK's own `comp_3p3v` derivation -- the 5V/6V half is
        # `comp.width.mv.1` below. Before #1110 this checked the *whole*
        # `Comp` layer at 0.22, i.e. thick-oxide geometry against the
        # thin-oxide column (issue #552's reproducer).
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp
            sized_by_um=0.0,  # unsized marker, as in the PDK's own deck
            intersect_with=(55, 0),  # Dualgate
            mode="not_interacting",
        ),
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.1a_LV"),
    ),
    DrcRule(
        id="comp.width.mv.1",
        description="minimum COMP (diffusion/active) width inside Dualgate (5V/6V)",
        layer=(22, 0),  # Comp
        check="width",
        threshold_dbu=300,  # 0.30 um
        # DRM 7.5 Comp, rule "DF.1a_MV": "Min. COMP Width" -> 0.30 (5V/6V
        # column), the `_MV` half of the pair `comp.width.1` above is the
        # `_LV` half of (issue #1110, closing issue #552's DRC gap). The
        # checked region is whole `Comp` polygons that overlap `Dualgate`
        # (55/0) -- the PDK's own `comp_56v` derivation. Approximation: the
        # PDK deck's `DF.1a_MV` additionally excludes polygons under the
        # LDMOS drain markers `mvsd` (210/0) / `mvpsd` (11/39), neither of
        # which is in this deck's curated layer set, so a 5V LDMOS drift
        # region drawn on those markers is checked here where the official
        # rule would not check it (the conservative direction -- a false
        # positive on a device this deck cannot recognise, never a missed
        # violation). Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp
            sized_by_um=0.0,  # unsized marker, as in the PDK's own deck
            intersect_with=(55, 0),  # Dualgate
            mode="overlapping",
        ),
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.1a_MV"),
    ),
    DrcRule(
        id="comp.space.1",
        description="minimum COMP (diffusion/active) spacing outside Dualgate (3.3V)",
        layer=(22, 0),  # Comp
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.5 Comp, rule "DF.3a_LV": "Min. COMP Space. P-substrate tap
        # (PCOMP outside NWELL and DNWELL) can be butted for different
        # voltage devices..." -> 0.28 (3.3V column). Voltage-scoped as of
        # issue #1110 the same way `comp.width.1` above is: the checked
        # region is `Comp` polygons touching no `Dualgate` (55/0) geometry
        # (the PDK's own `comp_3p3v`); the 5V/6V half is `comp.space.mv.1`
        # below. Approximation (unchanged by #1110): the DRM text scopes
        # this to a substrate-tap butting context, and the PDK deck further
        # excludes `otp_mk` (173/5); approximated here as a general
        # COMP-to-COMP spacing check across the voltage-scoped region, since
        # isolating that context needs well/tap marker layers this deck's
        # curated layer set does not draw. Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp
            sized_by_um=0.0,  # unsized marker, as in the PDK's own deck
            intersect_with=(55, 0),  # Dualgate
            mode="not_interacting",
        ),
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.3a_LV"),
    ),
    DrcRule(
        id="comp.space.mv.1",
        description="minimum COMP (diffusion/active) spacing inside Dualgate (5V/6V)",
        layer=(22, 0),  # Comp
        check="space",
        threshold_dbu=360,  # 0.36 um
        # DRM 7.5 Comp, rule "DF.3a_MV": same rule text as "DF.3a_LV" above
        # -> 0.36 (5V/6V column), issue #1110. The checked region is whole
        # `Comp` polygons overlapping `Dualgate` (55/0) -- the PDK's own
        # `comp_56v`. Measures spacing *within* that marked set only, exactly
        # as the PDK deck's own `comp_56v.not(otp_mk).space(0.36.um)` does
        # (an `_LV`-to-`_MV` spacing pair is not this rule, and is not a
        # published DRM rule either). Same `otp_mk` (173/5) approximation as
        # `comp.space.1` above. Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp
            sized_by_um=0.0,  # unsized marker, as in the PDK's own deck
            intersect_with=(55, 0),  # Dualgate
            mode="overlapping",
        ),
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.3a_MV"),
    ),
    DrcRule(
        id="comp.enclosing.contact.1",
        description="minimum COMP overlap of contact",
        layer=(22, 0),  # Comp
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=70,  # 0.07 um
        # DRM 7.12 Contact, rule "CO.4": "COMP overlap of contact" -> 0.07um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.4"),
    ),
    DrcRule(
        id="contact.width.1",
        description="min/max contact size (fixed 0.22 x 0.22 um square)",
        layer=(33, 0),  # Contact
        check="width",
        threshold_dbu=220,  # 0.22 um
        threshold_max_dbu=220,  # 0.22 um -- same value: a *fixed*-size rule
        # DRM 7.12 Contact, rule "CO.1": "Min/max contact size" -> 0.22um
        # (contacts are a fixed 0.22 x 0.22um square in the real deck).
        # Both halves are enforced (issue #2370): `threshold_dbu` is the
        # minimum-width lower bound (`Region.width_check`), and the equal
        # `threshold_max_dbu` is the fixed-size upper bound, measured as a
        # bounding-box size so an over-long contact *bar* -- legal facing-edge
        # width, illegal size -- is caught too (see `_run_width_max_check` in
        # `drc.py`). Threshold value unmodified.
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.1"),
    ),
    DrcRule(
        id="contact.space.1",
        description="minimum contact spacing",
        layer=(33, 0),  # Contact
        check="space",
        threshold_dbu=250,  # 0.25 um
        # DRM 7.12 Contact, rule "CO.2a": "Space" -> 0.25um
        scope="7.12 Contact",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.12 Contact", "CO.2a"),
    ),
    DrcRule(
        id="metal1.enclosing.contact.1",
        description="minimum metal1 overlap of contact",
        layer=(34, 0),  # Metal1 -- the conductor *above* the cut
        other_layer=(33, 0),  # Contact
        check="enclosing",
        threshold_dbu=5,  # 0.005 um
        # DRM 7.12 Contact, rule "CO.6": "Metal1 overlap of contact" ->
        # 0.005um. Re-derived from the PDK's own executable rule deck (see
        # the "CO.*/Vn.* re-derivation" note in the module docstring):
        # `rule_decks/contact.drc`'s CO.6 is
        # `contact.enclosed(metal1, 0.005.um, euclidian) OR
        # contact.not(metal1)` -- exactly the pair of conditions our
        # "enclosing" check reports (marginal facing-edge enclosure plus the
        # zero-overlap escape term, see `_run_check`'s `outside_region` in
        # `drc.py`), so this transcription is exact, not an approximation.
        # The end-of-line companions "CO.6a"/"CO.6b" are *not* modeled -- see
        # the module docstring's end-of-line coverage note (issue #551) and
        # `_METAL_EOL_UNMODELED_RULES` (issue #3968).
        provenance=_gf180mcu_klayout_deck_provenance("contact", "CO.6"),
    ),
    DrcRule(
        id="via1.width.1",
        description="min/max via1 size (fixed 0.26 x 0.26 um square)",
        layer=(35, 0),  # Via1
        check="width",
        threshold_dbu=260,  # 0.26 um
        threshold_max_dbu=260,  # 0.26 um -- same value: a *fixed*-size rule
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um (each via is a fixed 0.26 x 0.26um square in the real
        # deck). Both halves are enforced (issue #2370) -- the same
        # min-plus-bounding-box-max pairing as contact.width.1's own CO.1
        # note above. Threshold value unmodified.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via1.space.1",
        description="minimum via1 spacing (approximates official 4x4-array rule)",
        layer=(35, 0),  # Via1
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: the DRM tightens this to 0.36um ("Vn.2b") inside a
        # >=4x4 via array; our space_check primitive has no array-density
        # context, so this uses the ordinary two-via "Vn.2a" threshold only
        # -- same class of context-collapsing approximation
        # nwell.space.1/comp.space.1 already document. Threshold value
        # unmodified.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal1.enclosing.via1.1",
        description="minimum metal1 overlap of via1 (the conductor below the cut)",
        layer=(34, 0),  # Metal1
        other_layer=(35, 0),  # Via1
        check="enclosing",
        threshold_dbu=0,  # 0.0 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V1.3a": "metal1 overlap of via1"
        # -> 0.0um, i.e. the cut must land on metal1 but needs no margin.
        # Re-derived from `rule_decks/via1.drc`, whose V1.3a is the bare
        # `via1.not(metal1)` -- exactly this deck's zero-threshold
        # "enclosing" form (`enclosing_check(..., 0)` reports nothing, and
        # the rule's whole content is carried by `_run_check`'s
        # `outside_region` escape term, see `drc.py`). Via1 is the one via
        # level whose *lower* conductor carries a 0.0um requirement; Via2-
        # Via4 use 0.01um ("Vn.3b"), below.
        provenance=_gf180mcu_klayout_deck_provenance("via", "V1.3a"),
    ),
    DrcRule(
        id="metal2.enclosing.via1.1",
        description="minimum metal2 overlap of via1 (the conductor above the cut)",
        layer=(36, 0),  # Metal2
        other_layer=(35, 0),  # Via1
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V1.4a": "metal2 overlap of via1"
        # -> 0.01um. Re-derived from `rule_decks/via1.drc`, whose V1.4a is
        # `via1.enclosed(metal2, 0.01.um, euclidian) OR via1.not(metal2)` --
        # exactly the pair of conditions our "enclosing" check reports.
        # The end-of-line companions "V1.4b"/"V1.4c" are not modeled (see
        # the module docstring's end-of-line coverage note).
        provenance=_gf180mcu_klayout_deck_provenance("via", "V1.4a"),
    ),
    DrcRule(
        id="via2.width.1",
        description="min/max via2 size (fixed 0.26 x 0.26 um square)",
        layer=(38, 0),  # Via2
        check="width",
        threshold_dbu=260,  # 0.26 um
        threshold_max_dbu=260,  # 0.26 um -- same value: a *fixed*-size rule
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Both halves enforced: see via1.width.1's note above.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via2.space.1",
        description="minimum via2 spacing (approximates official 4x4-array rule)",
        layer=(38, 0),  # Via2
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal2.enclosing.via2.1",
        description="minimum metal2 overlap of via2 (the conductor below the cut)",
        layer=(36, 0),  # Metal2
        other_layer=(38, 0),  # Via2
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V2.3b": "metal2 overlap of via2"
        # -> 0.01um. Re-derived from `rule_decks/via2.drc`, whose V2.3b is
        # `via2.not(metal2) OR via2.enclosed(metal2, 0.01.um, euclidian)` --
        # exactly the pair of conditions our "enclosing" check reports.
        provenance=_gf180mcu_klayout_deck_provenance("via", "V2.3b"),
    ),
    DrcRule(
        id="metal3.enclosing.via2.1",
        description="minimum metal3 overlap of via2 (the conductor above the cut)",
        layer=(42, 0),  # Metal3
        other_layer=(38, 0),  # Via2
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V2.4a": "metal3 overlap of via2"
        # -> 0.01um. Re-derived from `rule_decks/via2.drc` (same
        # `enclosed(...) OR not(...)` form as V2.3b above).
        provenance=_gf180mcu_klayout_deck_provenance("via", "V2.4a"),
    ),
    DrcRule(
        id="via3.width.1",
        description="min/max via3 size (fixed 0.26 x 0.26 um square)",
        layer=(40, 0),  # Via3
        check="width",
        threshold_dbu=260,  # 0.26 um
        threshold_max_dbu=260,  # 0.26 um -- same value: a *fixed*-size rule
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Both halves enforced: see via1.width.1's note above.
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via3.space.1",
        description="minimum via3 spacing (approximates official 4x4-array rule)",
        layer=(40, 0),  # Via3
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal3.enclosing.via3.1",
        description="minimum metal3 overlap of via3 (the conductor below the cut)",
        layer=(42, 0),  # Metal3
        other_layer=(40, 0),  # Via3
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V3.3b": "metal3 overlap of via3"
        # -> 0.01um. Re-derived from `rule_decks/via3.drc` (same
        # `not(...) OR enclosed(...)` form as V2.3b above).
        provenance=_gf180mcu_klayout_deck_provenance("via", "V3.3b"),
    ),
    DrcRule(
        id="metal4.enclosing.via3.1",
        description="minimum metal4 overlap of via3 (the conductor above the cut)",
        layer=(46, 0),  # Metal4
        other_layer=(40, 0),  # Via3
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V3.4a": "metal4 overlap of via3"
        # -> 0.01um. Re-derived from `rule_decks/via3.drc`.
        provenance=_gf180mcu_klayout_deck_provenance("via", "V3.4a"),
    ),
    DrcRule(
        id="via4.width.1",
        description="min/max via4 size (fixed 0.26 x 0.26 um square)",
        layer=(41, 0),  # Via4
        check="width",
        threshold_dbu=260,  # 0.26 um
        threshold_max_dbu=260,  # 0.26 um -- same value: a *fixed*-size rule
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.1": "Min/max Vian size" ->
        # 0.26um. Both halves enforced: see via1.width.1's note above. Via4
        # (41/0) was previously referenced only as mim.enclosing.via4.1's
        # other_layer (MiM top-plate overlap, DRM "MIMTM.2"); this rule adds
        # coverage of Via4's own size, which that reference alone did not
        # provide (issue #546).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.1"),
    ),
    DrcRule(
        id="via4.space.1",
        description="minimum via4 spacing (approximates official 4x4-array rule)",
        layer=(41, 0),  # Via4
        check="space",
        threshold_dbu=260,  # 0.26 um
        # DRM 7.14 Vian (n = 1 to 5), rule "Vn.2a": "Space" -> 0.26um.
        # Approximation: see via1.space.1's note above (no array-density
        # context; the tighter 4x4-array "Vn.2b" 0.36um threshold is not
        # checked). Via4 (41/0) was previously referenced only as
        # mim.enclosing.via4.1's other_layer; this rule adds coverage of
        # Via4's own spacing (issue #546).
        scope="7.14 Vian",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.14 Vian", "Vn.2a"),
    ),
    DrcRule(
        id="metal4.enclosing.via4.1",
        description="minimum metal4 overlap of via4 (the conductor below the cut)",
        layer=(46, 0),  # Metal4
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V4.3b": "metal4 overlap of via4"
        # -> 0.01um. Re-derived from `rule_decks/via4.drc`. Distinct from
        # `mim.enclosing.via4.1` (MIMTM.2, 0.4um) below: that one is the MiM
        # capacitor's *virtual bottom plate* overlap of Via4, scoped via
        # `DerivedLayer` to Metal4 that already overlaps FuseTop; this one is
        # the ordinary BEOL routing rule for raw Metal4, which every Via4 --
        # MiM or routing -- must also satisfy (a genuine MiM bottom plate
        # clears 0.01um by two orders of magnitude, so the two never
        # conflict).
        provenance=_gf180mcu_klayout_deck_provenance("via", "V4.3b"),
    ),
    DrcRule(
        id="metal5.enclosing.via4.1",
        description="minimum metal5 overlap of via4 (the conductor above the cut)",
        layer=(81, 0),  # Metal5
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=10,  # 0.01 um
        # DRM 7.14 Vian (n = 1 to 5), rule "V4.4a": "metal5 overlap of via4"
        # -> 0.01um. Re-derived from `rule_decks/via4.drc`. Scoped, like
        # `pad.enclosing.metal5.1`, to the 5LM variant this deck models
        # (Metal5 as the conductor above Via4).
        provenance=_gf180mcu_klayout_deck_provenance("via", "V4.4a"),
    ),
    DrcRule(
        id="metal1.width.1",
        description="minimum metal1 width",
        layer=(34, 0),  # Metal1
        check="width",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.23 (n = 1)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal1.space.1",
        description="minimum metal1 spacing",
        layer=(34, 0),  # Metal1
        check="space",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.23 (n = 1)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal2.width.1",
        description="minimum metal2 width",
        layer=(36, 0),  # Metal2
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal2.space.1",
        description="minimum metal2 spacing",
        layer=(36, 0),  # Metal2
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal3.width.1",
        description="minimum metal3 width",
        layer=(42, 0),  # Metal3
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal3.space.1",
        description="minimum metal3 spacing",
        layer=(42, 0),  # Metal3
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal4.width.1",
        description="minimum metal4 width",
        layer=(46, 0),  # Metal4
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal4.space.1",
        description="minimum metal4 spacing",
        layer=(46, 0),  # Metal4
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metal5.width.1",
        description="minimum metal5 width",
        layer=(81, 0),  # Metal5
        check="width",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.1": "Width" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.1"),
    ),
    DrcRule(
        id="metal5.space.1",
        description="minimum metal5 spacing",
        layer=(81, 0),  # Metal5
        check="space",
        threshold_dbu=280,  # 0.28 um
        # DRM 7.13 Metaln (n = 1 to 5), rule "Mn.2a": "Space" -> 0.28 (2 <= n <= 5)
        scope="7.13 Metaln",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.13 Metaln", "Mn.2a"),
    ),
    DrcRule(
        id="metaltop.width.1",
        description="minimum metaltop width (standard 6K angstrom thickness)",
        layer=(53, 0),  # MetalTop
        check="width",
        threshold_dbu=360,  # 0.36 um
        # DRM 7.15 MetalTop, rule "MT.1": "Width" -> 0.36 (standard/unstarred
        # value; the DRM's "0.36/0.44*" lists a second value for the 9K/11K
        # angstrom MetalTop thickness option, which -- like the 5V/6V variant
        # elsewhere in this deck -- is not modeled here).
        scope="7.15 MetalTop",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.15 MetalTop", "MT.1"),
    ),
    DrcRule(
        id="metaltop.space.1",
        description="minimum metaltop spacing (standard 6K angstrom thickness)",
        layer=(53, 0),  # MetalTop
        check="space",
        threshold_dbu=380,  # 0.38 um
        # DRM 7.15 MetalTop, rule "MT.2a": "Space" -> 0.38 (standard/
        # unstarred value; see metaltop.width.1's note on the thickness
        # option not modeled here).
        scope="7.15 MetalTop",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.15 MetalTop", "MT.2a"),
    ),
    DrcRule(
        id="mim.space.1",
        description=(
            "minimum MiM virtual bottom plate spacing to other bottom-plate-"
            "or-routing metal4 (fusetop-sized metal4, scoped to genuine MiM "
            "structures)"
        ),
        layer=(46, 0),  # Metal4 -- reporting identity, matches the sibling MiM rules
        other_layer=(46, 0),  # Metal4 -- the "other" side of the separation check
        check="separation",
        threshold_dbu=1200,  # 1.2 um
        derived_layer=DerivedLayer(
            base=(75, 0),  # FuseTop, sized (oversized) below
            sized_by_um=1.06,
            intersect_with=(46, 0),  # Metal4
        ),
        # DRM 10.4.2 MIM Option B, rule "MIMTM.1": "Minimum MiM bottom plate
        # spacing to the bottom plate metal (whether adjacent MiM or routing
        # metal)" -> 1.2um, per the DRM's own note scoped to the "virtual
        # bottom plate" (FuseTop sized by 1.06um AND Metal4 restricted to
        # wherever it already overlaps FuseTop) rather than raw Metal4 -- see
        # `DerivedLayer`'s docstring and the module docstring's own
        # `mim.space.1` note (issue #1033, mirroring `mim.enclosing.via4.1`'s
        # #345 fix for the sibling rule) for why an unscoped check here used
        # to over-flag: a blanket "Metal4-to-Metal4 spacing" check trips on
        # any ordinary Metal4 routing/PDN geometry spaced tighter than 1.2um,
        # regardless of whether a MiM capacitor is anywhere on the layout.
        # `run_drc()`'s dispatch (`drc.py`) additionally scopes the
        # "separation" check's `other_layer` side to exclude Metal4 that is
        # itself part of the same virtual-bottom-plate construction (issue
        # #1033), so this rule reports zero violations for a design with no
        # recognised MiM structures -- and measures the rule's "whether
        # adjacent MiM" half, which that exclusion would otherwise drop, as a
        # peer-to-peer `isolated_check` among those excluded plate-bearing
        # polygons, reported under this same rule id. Threshold value
        # unmodified.
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("10.4.2 MIM Option B", "MIMTM.1"),
    ),
    DrcRule(
        id="mim.enclosing.fusetop.1",
        description="minimum MiM bottom plate (metal4) overlap of top plate (fusetop)",
        layer=(46, 0),  # Metal4
        other_layer=(75, 0),  # FuseTop
        check="enclosing",
        threshold_dbu=600,  # 0.6 um
        # DRM 10.4.2 MIM Option B, rule "MIMTM.3": "Minimum MiM bottom plate
        # overlap of Top plate" -> 0.6um.
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("10.4.2 MIM Option B", "MIMTM.3"),
    ),
    DrcRule(
        id="mim.enclosing.via4.1",
        description=(
            "minimum MiM virtual bottom plate overlap of via4 (fusetop-sized "
            "metal4, scoped to genuine MiM structures)"
        ),
        layer=(46, 0),  # Metal4 -- reporting identity, matches the two rules above
        other_layer=(41, 0),  # Via4
        check="enclosing",
        threshold_dbu=400,  # 0.4 um
        derived_layer=DerivedLayer(
            base=(75, 0),  # FuseTop, sized (oversized) below
            sized_by_um=1.06,
            intersect_with=(46, 0),  # Metal4
        ),
        # DRM 10.4.2 MIM Option B, rule "MIMTM.2": "Minimum MiM bottom plate
        # overlap of Via4" -> 0.4um, per the DRM's own note scoped to the
        # "virtual bottom plate" (FuseTop sized by 1.06um AND Metal4
        # restricted to wherever it already overlaps FuseTop) rather than raw
        # Metal4 -- see DerivedLayer's docstring and the module docstring's
        # own MIMTM.2 note (issue #345) for why an unscoped check here would
        # be actively wrong (it would flag ordinary Metal4/Via4/Metal5
        # routing throughout any layout, ordinary interconnect this deck's
        # `metals`/`vias` stack draws everywhere), not merely conservative.
        scope="10.4.2 MIM Option B",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("10.4.2 MIM Option B", "MIMTM.2"),
    ),
    DrcRule(
        id="pad.enclosing.metal5.1",
        description="minimum top-metal (Metal5) overlap of pad opening",
        layer=(81, 0),  # Metal5 -- the enclosing layer, 5LM variant only
        other_layer=(37, 0),  # Pad (passivation opening)
        check="enclosing",
        threshold_dbu=2000,  # 2.0 um
        # DRM 9.1 Bond Pad, rule "PAD.4": "Top layer metal overlap of pad
        # opening" -> 2.0um. The only hard, coded bond-pad rule (9.2's
        # PAD.1/PAD.2/PAD.5-PAD.20 are a guideline table, out of scope here).
        # Scoped to the 5LM variant this deck already exclusively models
        # (Metal5/81,0 as top metal) -- 6LM's MetalTop/53,0 case is not
        # covered; this deck has no variant-selection mechanism today.
        scope="9.1 Bond Pad",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("9.1 Bond Pad", "PAD.4"),
    ),
    DrcRule(
        id="nwell.space.1",
        description="minimum Nwell spacing (equipotential, 3.3V)",
        layer=(21, 0),  # Nwell
        check="space",
        threshold_dbu=600,  # 0.6 um
        # DRM 7.4 Nwell, rule "NW.2a": "Min. Nwell Space (Outside DNWELL)
        # [Equi-potential], Merge if the space is less than" -> 0.6 (3.3V
        # column). Approximation: the DRM splits this by net-potential
        # context ("NW.2a" equipotential 0.6um vs. "NW.2b" different-
        # potential 1.4um); our engine has no connectivity/netlist
        # information, so this uses the less strict "NW.2a" value across the
        # whole Nwell drawn layer. Threshold value unmodified.
        scope="7.4 Nwell",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.4 Nwell", "NW.2a"),
    ),
    DrcRule(
        id="nwell.enclosing.comp.1",
        description="minimum Nwell enclosure of an Nwell tap (COMP), 3.3V",
        layer=(21, 0),  # Nwell
        other_layer=(22, 0),  # Comp
        check="enclosing",
        threshold_dbu=120,  # 0.12 um
        # DRM 7.5 Comp, rule "DF.4d": "Min. (Nwell overlap of NCOMP) outside
        # DNWELL" -> 0.12 (3.3V column). Approximation: the DRM scopes this
        # to NCOMP (comp AND Nplus, i.e. specifically an Nwell substrate tap)
        # rather than all of comp; approximated as Nwell enclosing the whole
        # comp drawn layer, since isolating NCOMP requires the same boolean
        # layer expression our engine does not evaluate. Threshold value
        # unmodified.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.5 Comp", "DF.4d"),
    ),
    DrcRule(
        id="comp.coverage.1",
        description=(
            "COMP not covered by Nplus or Pplus is forbidden "
            "(except COMP under the Schottky-diode marking)"
        ),
        layer=(22, 0),  # Comp -- the checked layer
        check="comp_coverage",
        threshold_dbu=0,  # no distance: a coverage rule
        region_spec=CompCoverageSpec(
            covered_by_any=((32, 0), (31, 0)),  # Nplus, Pplus
            exempt_markers=((241, 0),),  # Schottky_diode
        ),
        # DRM 7.5 Comp, rule "DF.12": "COMP not covered by Nplus or Pplus is
        # forbidden (except those COMP under marking)". Re-derived from the
        # PDK's executable `comp.rb`: `comp.not_interacting(schottky_diode)
        # .not(nplus).not(pplus)`. See the module docstring's "Known
        # approximations" note on `comp.coverage.1`.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.12"),
        # DF.12 publishes no 3.3V/5V column split, so it cannot have read the
        # wrong voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="comp.tap_distance.nwell.lv.1",
        description=(
            "Max distance of an Nwell tap (NCOMP inside Nwell) from PCOMP inside "
            "Nwell: 20um (3.3V)"
        ),
        layer=(22, 0),  # Comp -- the checked layer
        check="tap_distance",
        threshold_dbu=0,  # no dbu threshold: the distance is in the spec
        region_spec=TapDistanceSpec(
            well_domain="inside",
            voltage="lv",
            max_distance_um=20.0,
            nwell=(21, 0),
            dnwell=(12, 0),
            lvpwell=(204, 0),
            nplus=(32, 0),
            pplus=(31, 0),
            res_mk=(110, 5),
            dualgate=(55, 0),
            v5_xtor=(112, 1),
        ),
        # DRM 7.5 Comp, rule "DF.13": "Max distance of Nwell tap (NCOMP inside
        # Nwell) from (PCOMP inside Nwell)" (3.3V column, 20 um). Re-derived from
        # the PDK's executable `comp.rb` (pactive not interacting
        # v5_xtor/dualgate/the 20um tap reach); see the module docstring's "Known
        # approximations" note on `comp.tap_distance.nwell.lv.1`.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.13_LV"),
    ),
    DrcRule(
        id="comp.tap_distance.psub.lv.1",
        description=(
            "Max distance of a substrate tap (PCOMP outside Nwell) from NCOMP "
            "outside Nwell: 20um (3.3V)"
        ),
        layer=(22, 0),  # Comp -- the checked layer
        check="tap_distance",
        threshold_dbu=0,  # no dbu threshold: the distance is in the spec
        region_spec=TapDistanceSpec(
            well_domain="outside",
            voltage="lv",
            max_distance_um=20.0,
            nwell=(21, 0),
            dnwell=(12, 0),
            lvpwell=(204, 0),
            nplus=(32, 0),
            pplus=(31, 0),
            res_mk=(110, 5),
            dualgate=(55, 0),
            v5_xtor=(112, 1),
        ),
        # DRM 7.5 Comp, rule "DF.14": "Max distance of substrate tap (PCOMP
        # outside Nwell) from (NCOMP outside Nwell)" (3.3V column, 20 um).
        # Re-derived from the PDK's executable `comp.rb` (nactive minus a diamond
        # 20um ptap filter, refined by sep(ptap, 20um)); see the module
        # docstring's "Known approximations" note on `comp.tap_distance.psub.lv.1`.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.14_LV"),
    ),
    DrcRule(
        id="comp.tap_distance.nwell.mv.1",
        description=(
            "Max distance of an Nwell tap (NCOMP inside Nwell) from PCOMP inside "
            "Nwell: 15um (5V/6V)"
        ),
        layer=(22, 0),  # Comp -- the checked layer
        check="tap_distance",
        threshold_dbu=0,  # no dbu threshold: the distance is in the spec
        region_spec=TapDistanceSpec(
            well_domain="inside",
            voltage="mv",
            max_distance_um=15.0,
            nwell=(21, 0),
            dnwell=(12, 0),
            lvpwell=(204, 0),
            nplus=(32, 0),
            pplus=(31, 0),
            res_mk=(110, 5),
            dualgate=(55, 0),
            v5_xtor=(112, 1),
        ),
        # DRM 7.5 Comp, rule "DF.13": "Max distance of Nwell tap (NCOMP inside
        # Nwell) from (PCOMP inside Nwell)" (5V/6V column, 15 um). Re-derived
        # from the PDK's executable `comp.rb` (pactive overlapping dualgate and
        # not interacting the 15um tap reach); see the module docstring's
        # "Known approximations" note on `comp.tap_distance.nwell.mv.1`.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.13_MV"),
    ),
    DrcRule(
        id="comp.tap_distance.psub.mv.1",
        description=(
            "Max distance of a substrate tap (PCOMP outside Nwell) from NCOMP "
            "outside Nwell: 15um (5V/6V)"
        ),
        layer=(22, 0),  # Comp -- the checked layer
        check="tap_distance",
        threshold_dbu=0,  # no dbu threshold: the distance is in the spec
        region_spec=TapDistanceSpec(
            well_domain="outside",
            voltage="mv",
            max_distance_um=15.0,
            nwell=(21, 0),
            dnwell=(12, 0),
            lvpwell=(204, 0),
            nplus=(32, 0),
            pplus=(31, 0),
            res_mk=(110, 5),
            dualgate=(55, 0),
            v5_xtor=(112, 1),
        ),
        # DRM 7.5 Comp, rule "DF.14": "Max distance of substrate tap (PCOMP
        # outside Nwell) from (NCOMP outside Nwell)" (5V/6V column, 15 um).
        # Re-derived from the PDK's executable `comp.rb` (nactive overlapping
        # dualgate minus a diamond 15um ptap filter, refined by sep(ptap,
        # 15um)); see the module docstring's "Known approximations" note on
        # `comp.tap_distance.psub.mv.1`.
        scope="7.5 Comp",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("comp", "DF.14_MV"),
    ),
    DrcRule(
        id="nplus.width.1",
        description="minimum Nplus implant width",
        layer=(32, 0),  # Nplus
        check="width",
        threshold_dbu=400,  # 0.4 um
        # DRM 7.8 Nplus, rule "NP.1": "Width" -> 0.4 (the implant tables
        # publish a single column, no 3.3V/5V split). Cross-checked against
        # the real fetched install's own executable deck (issue #2369):
        # `rule_decks/nplus.drc`'s `nplus.width(0.4.um, euclidian)`, the
        # same metric our `width_check` uses by default. Not an
        # approximation.
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.8 Nplus", "NP.1"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="nplus.space.1",
        description="minimum Nplus implant spacing",
        layer=(32, 0),  # Nplus
        check="space",
        threshold_dbu=400,  # 0.4 um
        # DRM 7.8 Nplus, rule "NP.2": "Space" -> 0.4. Cross-checked against
        # `rule_decks/nplus.drc`'s `nplus.space(0.4.um, euclidian)` (issue
        # #2369) -- the same primitive/metric our `space_check` uses. Not an
        # approximation.
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.8 Nplus", "NP.2"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="dualgate.width.1",
        description="minimum Dualgate (5V/6V thick-oxide marker) width",
        layer=(55, 0),  # Dualgate
        check="width",
        threshold_dbu=700,  # 0.7 um
        # Official `rule_decks/dualgate.drc` rule "DV.5": `dualgate.width(0.7.um,
        # euclidian)` -- the same primitive/metric `width_check` uses. Not an
        # approximation (issue #3791).
        scope="7.5 Comp",  # no Dualgate DRM table in _GF180MCU_DRM_CSV (#566)
        provenance=_gf180mcu_klayout_deck_provenance("dualgate", "DV.5"),
        voltage_independent=True,
    ),
    DrcRule(
        id="dualgate.space.1",
        description="minimum Dualgate (5V/6V thick-oxide marker) spacing",
        layer=(55, 0),  # Dualgate
        check="space",
        threshold_dbu=440,  # 0.44 um
        # Official `rule_decks/dualgate.drc` rule "DV.2": `dualgate.space(0.44.um,
        # euclidian)` (issue #3791).
        scope="7.5 Comp",  # no Dualgate DRM table in _GF180MCU_DRM_CSV (#566)
        provenance=_gf180mcu_klayout_deck_provenance("dualgate", "DV.2"),
        voltage_independent=True,
    ),
    DrcRule(
        id="nplus.space.pcomp.1",
        description="minimum Nplus implant space to PCOMP (p-type diffusion)",
        layer=(32, 0),  # Nplus -- reporting identity, shared with the rules above
        other_layer=(32, 0),  # Nplus -- the "other" side of the separation check
        check="separation",
        threshold_dbu=160,  # 0.16 um
        # DRM 7.8 Nplus, rule "NP.3a": "Space to PCOMP for PCOMP: (1) Inside
        # Nwell (2) Outside LVPWELL but inside DNWELL" -> 0.16 (issue
        # #2369). The checked region is the real PCOMP boolean (`Comp` AND
        # `Pplus`, the PDK deck's own `pcomp` derivation in
        # `rule_decks/main.drc`) via `DerivedLayer`, *not* raw `Comp`:
        # `Nplus` covers NCOMP by construction (rule NP.5b below), so an
        # unscoped `Nplus`-to-`Comp` separation check would report a
        # zero-distance violation on every single NMOS in any layout -- the
        # "actively wrong, not merely conservative" class of approximation
        # `mim.enclosing.via4.1`'s own note describes, not an acceptable one.
        # Two approximations remain, both documented in the module
        # docstring's "known approximations" list: (a) the well context is
        # collapsed -- NP.3b/NP.3c publish the same 0.16 for PCOMP near a
        # well edge but a looser 0.08 further away, and our engine cannot
        # evaluate the `lvpwell`/`dnwell` boolean context that selects
        # between them, so the stricter 0.16 is applied everywhere
        # (conservative: over-flags a 0.08-0.16 gap in the far context,
        # never misses a real violation); (b) the PDK deck excludes `Nplus`
        # interacting with a butted NCOMP (`np3_nplus =
        # nplus.not_interacting(ncomp_butted)`, the NP.3d/NP.3e zero-space
        # butting cases), which needs a three-layer boolean our engine has
        # no vocabulary for -- so a deliberately butted NCOMP/PCOMP pair is
        # flagged here where the official rule permits it. Threshold value
        # unmodified.
        derived_layer=DerivedLayer(
            base=(31, 0),  # Pplus, unsized -- `pcomp = comp AND pplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
            context_layers=((21, 0), (12, 0)),  # Nwell, Dnwell
            context_sized_by_um=0.429,  # the PDK deck's own `sized(0.429.um)`
            context="near",
        ),
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        # Cited from the executable rule deck rather than the DRM's own CSV
        # (the `comp.drc`/`CO.*`/`Vn.*` precedent above): the table publishes
        # the 0.16 value, but only `rule_decks/nplus.drc` states the PCOMP
        # derivation this rule's `derived_layer` models.
        provenance=_gf180mcu_klayout_deck_provenance("nplus", "NP.3a"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="nplus.space.pcomp.far.1",
        description=(
            "minimum Nplus implant space to PCOMP (p-type diffusion) at least "
            "0.429um from any Nwell/Dnwell"
        ),
        layer=(32, 0),  # Nplus -- reporting identity, shared with the rules above
        other_layer=(32, 0),  # Nplus -- the "other" side of the separation check
        check="separation",
        threshold_dbu=80,  # 0.08 um
        # DRM 7.8 Nplus, rule "NP.3cii": "Space to PCOMP: For Outside DNWELL:
        # (ii) For PCOMP space to Nwell >= 0.43um" -> 0.08 (issue #3484), i.e.
        # `np3_nplus.not(dnwell).separation(pcomp.not(dnwell).not(
        # nwell_n_dn.sized(0.429.um)), 0.08.um, euclidian)` in
        # `rule_decks/nplus.drc`. The far half of the split whose near half is
        # `nplus.space.pcomp.1` above (`DerivedLayer.context`): a PCOMP shape
        # (`Comp` AND `Pplus`) touching no `Nwell`/`Dnwell` grown by 0.429um is
        # held to 0.08 instead of 0.16 -- the substrate tap of an unmodified
        # PDK endcap/filltie cell sits ~0.48um from its Nwell and is Pplus-
        # enclosed with an Nplus 0.08um away, which the flat 0.16 mis-flagged.
        # Errs strict: the DNWELL branches (`NP.3bii`, inside LVPWELL) are not
        # relaxed (any PCOMP within reach of a `Dnwell` stays on the 0.16
        # near rule), and selection is whole-polygon, so a PCOMP only partly
        # within reach stays on the near rule. Same butting approximation as
        # the near rule.
        derived_layer=DerivedLayer(
            base=(31, 0),  # Pplus, unsized -- `pcomp = comp AND pplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
            context_layers=((21, 0), (12, 0)),  # Nwell, Dnwell
            context_sized_by_um=0.429,
            context="far",
        ),
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("nplus", "NP.3cii"),
        voltage_independent=True,
    ),
    DrcRule(
        id="nplus.enclosing.poly2.1",
        description="minimum Nplus implant overlap of the N-channel gate region",
        layer=(32, 0),  # Nplus -- reporting identity, shared with the rules above
        other_layer=(32, 0),  # Nplus -- the enclosing side of the `enclosed` check
        check="enclosed",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.8 Nplus, rule "NP.5a": "Overlap of N-channel gate" -> 0.23
        # (issue #2369). Spelled as an `"enclosed"` check whose checked
        # region is the transistor *gate* (`Poly2` AND `Comp`, via
        # `DerivedLayer`) and whose enclosing `other_layer` is `Nplus` --
        # the same direction as the PDK deck's own
        # `ngate.enclosed(nplus, 0.23.um, euclidian)` in
        # `rule_decks/nplus.drc`, including its second `ngate.not_outside
        # (nplus).not(nplus)` term, which is exactly `_run_check`'s
        # `outside_region` zero-overlap-escape term (see `drc.py`, #318).
        # The plain `"enclosing"` spelling (Nplus enclosing the raw `Poly2`
        # drawn layer) would be actively wrong rather than conservative:
        # every gate's poly2 runs off the diffusion to route, so it escapes
        # the implant by design and would be flagged on every transistor in
        # every layout. Approximation: the official `ngate` is
        # `nactive.and(tgate)` in the PDK deck's own `main.drc` -- i.e.
        # `(comp AND nplus).not(nwell OR dnwell_n)` intersected with
        # `(poly2 AND comp).not(res_mk)`, four drawn layers plus two
        # subtractions, where `DerivedLayer` expresses a two-layer boolean
        # only -- so the checked region is every
        # gate -- P-channel gates included. A PMOS gate drawn within 0.23um
        # of unrelated `Nplus` is therefore flagged here where the official
        # rule would not look at it (conservative: a false positive on
        # geometry no NP.5a violation can hide in, never a missed
        # violation). Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp, unsized -- `gate = poly2 AND comp`
            sized_by_um=0.0,
            intersect_with=(30, 0),  # Poly2
        ),
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("nplus", "NP.5a"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="nplus.enclosing.comp.1",
        description="minimum Nplus implant extension beyond NCOMP (n-type diffusion)",
        layer=(32, 0),  # Nplus -- reporting identity, shared with the rules above
        other_layer=(32, 0),  # Nplus -- the enclosing side of the `enclosed` check
        check="enclosed",
        threshold_dbu=160,  # 0.16 um
        # DRM 7.8 Nplus, rule "NP.5b": "Extension beyond COMP for the COMP
        # (1) inside LVPWELL (2) outside Nwell and DNWELL" -> 0.16 (issue
        # #2369), i.e. `ncomp.edges.enclosed(nplus_edges, 0.16.um,
        # projection)` in `rule_decks/nplus.drc`. Spelled the same way here:
        # the checked region is the real NCOMP boolean (`Comp` AND `Nplus`,
        # via `DerivedLayer`) and the enclosing `other_layer` is `Nplus`, so
        # only diffusion this implant actually covers is measured -- an
        # unscoped `Nplus`-encloses-`Comp` check would instead flag every
        # PCOMP that happens to touch `Nplus` (and, via the zero-overlap
        # escape term, the whole of any COMP polygon shared by a butted
        # NCOMP/PCOMP pair). Two approximations: (a) the PDK deck excludes
        # butting edges (`np5b_nplus_slct = nplus.edges.not
        # (nplus_butted_edges)`) and scopes by well context (NP.5c/NP.5d
        # relax the extension to 0.02 for a tap far from an LVPWELL/Nwell
        # edge), neither expressible here, so the strict 0.16 is applied to
        # every NCOMP edge including a butted one; (b) the official check
        # uses the `projection` metric while our `enclosed_check` uses
        # KLayout's default `euclidian`, which additionally measures
        # corner-to-corner distances -- the conservative direction (it can
        # report a corner the official rule ignores, never the reverse).
        # Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(32, 0),  # Nplus, unsized -- `ncomp = comp AND nplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
        ),
        scope="7.8 Nplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("nplus", "NP.5b"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.width.1",
        description="minimum Pplus implant width",
        layer=(31, 0),  # Pplus
        check="width",
        threshold_dbu=400,  # 0.4 um
        # DRM 7.9 Pplus, rule "PP.1": "Width" -> 0.4 -- the `Pplus` mirror of
        # `nplus.width.1` above, transcribed from the Pplus table in its own
        # right rather than assumed symmetric (issue #2369), and
        # cross-checked against `rule_decks/pplus.drc`'s
        # `pplus.width(0.4.um, euclidian)`. Not an approximation.
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.9 Pplus", "PP.1"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.space.1",
        description="minimum Pplus implant spacing",
        layer=(31, 0),  # Pplus
        check="space",
        threshold_dbu=400,  # 0.4 um
        # DRM 7.9 Pplus, rule "PP.2": "Space" -> 0.4, cross-checked against
        # `rule_decks/pplus.drc`'s `pplus.space(0.4.um, euclidian)` (issue
        # #2369). Not an approximation.
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_provenance("7.9 Pplus", "PP.2"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.space.ncomp.1",
        description="minimum Pplus implant space to NCOMP (n-type diffusion)",
        layer=(31, 0),  # Pplus -- reporting identity, shared with the rules above
        other_layer=(31, 0),  # Pplus -- the "other" side of the separation check
        check="separation",
        threshold_dbu=160,  # 0.16 um
        # DRM 7.9 Pplus, rule "PP.3a": "Space to NCOMP for NCOMP (1) inside
        # LVPWELL (2) outside NWELL and DNWELL" -> 0.16 (issue #2369) --
        # the `Pplus` mirror of `nplus.space.pcomp.1` above, with NCOMP
        # (`Comp` AND `Nplus`) as the derived checked region. Note the
        # mirror is *not* a pure transposition of the Nplus table: PP.3b/
        # PP.3c order their sub-cases the other way round from NP.3b/NP.3c,
        # which is why each side is transcribed from its own DRM table.
        # Same two approximations as its Nplus twin (collapsed well context,
        # butting cases PP.3d/PP.3e not excluded) -- see that rule's comment
        # and the module docstring's "known approximations" list. Threshold
        # value unmodified.
        derived_layer=DerivedLayer(
            base=(32, 0),  # Nplus, unsized -- `ncomp = comp AND nplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
        ),
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("pplus", "PP.3a"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.enclosing.poly2.1",
        description="minimum Pplus implant overlap of the P-channel gate region",
        layer=(31, 0),  # Pplus -- reporting identity, shared with the rules above
        other_layer=(31, 0),  # Pplus -- the enclosing side of the `enclosed` check
        check="enclosed",
        threshold_dbu=230,  # 0.23 um
        # DRM 7.9 Pplus, rule "PP.5a": "Overlap of P-channel gate" -> 0.23
        # (issue #2369), i.e. `rule_decks/pplus.drc`'s
        # `pplus.enclosing(pgate, 0.23.um, euclidian)` plus its
        # `pgate.not_outside(pplus).not(pplus)` escape term. The `Pplus`
        # mirror of `nplus.enclosing.poly2.1` above, and carrying the same
        # approximation: the checked region is every gate (`Poly2` AND
        # `Comp`), N-channel gates included, since the official `pgate` is
        # `pactive.and(tgate)` -- `(comp AND pplus).and(nwell OR dnwell_n)`
        # intersected with `(poly2 AND comp).not(res_mk)`, beyond a
        # two-layer `DerivedLayer` boolean. Threshold value unmodified.
        derived_layer=DerivedLayer(
            base=(22, 0),  # Comp, unsized -- `gate = poly2 AND comp`
            sized_by_um=0.0,
            intersect_with=(30, 0),  # Poly2
        ),
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("pplus", "PP.5a"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.enclosing.comp.1",
        description="minimum Pplus implant extension beyond PCOMP (p-type diffusion)",
        layer=(31, 0),  # Pplus -- reporting identity, shared with the rules above
        other_layer=(31, 0),  # Pplus -- the enclosing side of the `enclosed` check
        check="enclosed",
        threshold_dbu=160,  # 0.16 um
        # DRM 7.9 Pplus, rule "PP.5b": "Extension beyond COMP for COMP (1)
        # Inside NWELL (2) outside LVPWELL but inside DNWELL" -> 0.16 (issue
        # #2369), i.e. `pcomp.edges.enclosed(pplus_edges, 0.16.um,
        # projection)` in `rule_decks/pplus.drc`. The `Pplus` mirror of
        # `nplus.enclosing.comp.1` above, with PCOMP (`Comp` AND `Pplus`) as
        # the derived checked region, and carrying the same two
        # approximations (butting edges not excluded, `euclidian` rather
        # than `projection` metric). Threshold value unmodified.
        #
        # UNLIKE `nplus.enclosing.comp.1` above, this rule's 0.16um
        # threshold is NOT context-independent -- confirmed a false
        # positive by issue #2634. The official `PP.5b` only covers `Pplus`
        # "(1) Inside NWELL (2) outside LVPWELL but inside DNWELL"
        # (`pp5b_pplus = pp5b_pplus_slct.and(nwell).join(
        # pp5b_pplus_slct.not(lvpwell).and(dnwell))` in `pplus.drc`); a
        # `PCOMP` shape with no `Nwell`/`Dnwell` context anywhere instead
        # falls under `PP.5di`/`PP.5dii` ("outside DNWELL"), whose own
        # threshold is only 0.02um unless the shape sits within 0.429um of
        # a pure (non-`Dnwell`-overlapping) `Nwell` -- exactly the ordinary
        # poly-resistor P+ substrate-tap case gf180-ldo's `divider`/
        # `passives` cells hit, each already DRC-clean on the PDK's own
        # deck. (`nplus.enclosing.comp.1`'s mirror, `NP.5b`, has no such
        # split: its own two sub-cases -- "(1) inside LVPWELL (2) outside
        # Nwell and DNWELL" -- both resolve to the same 0.16um, verified
        # directly against a real fetched `rule_decks/{nplus,pplus}.drc`:
        # the identical 0.11um-extension geometry reports 0 `PP.5b` findings
        # but 4 `NP.5b` findings.) Modelling `PP.5di`/`PP.5dii`'s own
        # 0.429um-proximity split needs a three-input derivation this
        # engine's `DerivedLayer` has no mode for (see its docstring), so
        # `requires_any_layer` below narrows scope instead: a layout that
        # draws neither `Nwell` nor `Dnwell` anywhere has no PCOMP shape
        # `PP.5b` can apply to, so the whole rule is skipped rather than
        # misapplied at this flat, too-strict threshold. A layout that
        # draws either layer anywhere keeps this rule's pre-#2634 behaviour
        # unchanged (still approximate per the two approximations above,
        # just no longer firing on well-free layouts).
        requires_any_layer=((21, 0), (12, 0)),  # Nwell, Dnwell
        derived_layer=DerivedLayer(
            base=(31, 0),  # Pplus, unsized -- `pcomp = comp AND pplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
            context_layers=((21, 0), (12, 0)),  # Nwell, Dnwell
            context_sized_by_um=0.429,  # the PDK deck's own `sized(0.429.um)`
            context="near",
        ),
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("pplus", "PP.5b"),
        # The 7.8 Nplus/7.9 Pplus tables publish a single column (no
        # 3.3V/5V split), so this rule can never have read the wrong
        # voltage column -- see `DrcRule.voltage_independent`.
        voltage_independent=True,
    ),
    DrcRule(
        id="pplus.enclosing.comp.far.1",
        description=(
            "minimum Pplus implant extension beyond PCOMP (p-type diffusion) "
            "at least 0.429um from any Nwell/Dnwell"
        ),
        layer=(31, 0),  # Pplus -- reporting identity, shared with the rules above
        other_layer=(31, 0),  # Pplus -- the enclosing side of the `enclosed` check
        check="enclosed",
        threshold_dbu=20,  # 0.02 um
        # DRM 7.9 Pplus, rule "PP.5di": "Extension beyond COMP: For Outside
        # DNWELL (i) For Pplus to NWELL space >= 0.43um for Pfield or LVPWELL
        # tap" -> 0.02 (issue #3484), i.e. `pcomp.outside(dnwell).edges.not(
        # nplus_edges).not(nwell_n_dn.sized(0.429.um))` enclosed by Pplus
        # edges at 0.02 in `rule_decks/pplus.drc`. The far half of the split
        # whose near half is `pplus.enclosing.comp.1` above
        # (`DerivedLayer.context`; the strict `PP.5dii`/`PP.5b` 0.16 stays
        # there). Errs strict exactly like `nplus.space.pcomp.far.1`: PCOMP
        # within reach of any `Dnwell` (the `PP.5ci`/`PP.5cii` branches) is
        # not relaxed, and selection is whole-polygon. Euclidian metric where
        # the official rule is `projection`, as for the near rule.
        derived_layer=DerivedLayer(
            base=(31, 0),  # Pplus, unsized -- `pcomp = comp AND pplus`
            sized_by_um=0.0,
            intersect_with=(22, 0),  # Comp
            context_layers=((21, 0), (12, 0)),  # Nwell, Dnwell
            context_sized_by_um=0.429,
            context="far",
        ),
        scope="7.9 Pplus",  # DRM section this rule is transcribed from (#566)
        provenance=_gf180mcu_klayout_deck_provenance("pplus", "PP.5di"),
        voltage_independent=True,
    ),
    DrcRule(
        id="bjt.separation.comp.1",
        description="minimum space of DRC_BJT (bipolar device mark layer) to COMP",
        layer=(127, 5),  # DRC_BJT
        other_layer=(22, 0),  # Comp
        check="separation",
        threshold_dbu=100,  # 0.1 um
        # DRM 10.7 DRC_BJT Mark Layer, rule "BJT.3": "Minimum space of
        # DRC_BJT layer to unrelated COMP" -> 0.1um. Approximation: the DRM
        # scopes this to COMP "unrelated" to the BJT device (a connectivity
        # notion); our engine has no connectivity information, so this
        # checks against every comp shape, which may over-flag COMP that is
        # legitimately part of the same BJT structure. Threshold value
        # unmodified.
        scope="10.7 DRC_BJT Mark Layer",  # DRM section transcribed from (#566)
        provenance=_gf180mcu_provenance("10.7 DRC_BJT Mark Layer", "BJT.3"),
        #
        # Follow-up (issue #223): `DRC_BJT` is now *also* consumed for
        # device-recognition purposes (not just this DRC mark/separation
        # check) -- see `EXTRACTION_DECK.bipolars` below, which uses it as
        # the `marker` layer of a `klt extract` bipolar device-recognition
        # entry.
    ),
]

# ---------------------------------------------------------------------------
# Manufacturing-grid rules -- the PDK runset's own `OFFGRID` group (issue #3001)
# ---------------------------------------------------------------------------
#
# `rule_decks/geom.drc` opens with `if OFFGRID`, and `run_drc.py` turns that
# group on by default (`--no_offgrid` disables it). Until issue #3001 this
# curated deck transcribed none of it, so `klt drc --deck gf180mcu` reported
# `clean` for geometry the PDK's own runset fails (`contact_OFFGRID` on a
# guard-ring cell from `klt gen mos_array`) -- the gf180mcu twin of sky130's
# #2642.
#
# Unlike sky130's group, gf180mcu's is **grid-only**: `geom.drc` has 100
# `ongrid(0.005)` calls and no `with_angle` call (its `<layer>_angle` rules are
# `edges.without_angle(0).without_angle(45)...`, an edge-direction check that
# no `DrcRule` check kind expresses), so no `"angle"` rule is emitted here.
# The grid is the same literal `0.005` on every call.
#
# One row per curated layer (`LAYER_NAMES` below) that `geom.drc` grid-checks,
# in the runset's own order. Metal-stack conditionals in the runset: `Via2`/
# `Metal3` are checked for 3LM+, `Via3`/`Metal4` for 4LM+, `Via4`/`Metal5` for
# 5LM+ and `MetalTop` for 6LM only; this deck draws one fixed five-metal
# stack, so all of them are transcribed. A rule is simply skipped, as for any
# other missing layer, when the stream carries none of it.

#: The manufacturing grid every `ongrid` call in `geom.drc` passes, in
#: micrometres (`layer.ongrid(0.005)`, one literal on every call site).
OFFGRID_GRID_UM = 0.005

#: ``(rule-id stem, (layer, datatype), runset layer variable)`` per layer
#: `geom.drc`'s `if OFFGRID` block grid-checks, in that block's line order.
_OFFGRID_ROWS: tuple[tuple[str, tuple[int, int], str], ...] = (
    ("comp", (22, 0), "comp"),
    ("dnwell", (12, 0), "dnwell"),
    ("nwell", (21, 0), "nwell"),
    ("dualgate", (55, 0), "dualgate"),
    ("poly2", (30, 0), "poly2"),
    ("nplus", (32, 0), "nplus"),
    ("pplus", (31, 0), "pplus"),
    ("sab", (49, 0), "sab"),
    ("resistor", (62, 0), "resistor"),
    ("fusetop", (75, 0), "fusetop"),
    ("plfuse", (125, 5), "plfuse"),
    ("efuse_mk", (80, 5), "efuse_mk"),
    ("res_mk", (110, 5), "res_mk"),
    ("cap_mk", (117, 5), "cap_mk"),
    ("diode_mk", (115, 5), "diode_mk"),
    ("drc_bjt", (127, 5), "drc_bjt"),
    ("mim_l_mk", (117, 10), "mim_l_mk"),
    ("contact", (33, 0), "contact"),
    ("metal1", (34, 0), "metal1"),
    ("via1", (35, 0), "via1"),
    ("metal2", (36, 0), "metal2"),
    ("via2", (38, 0), "via2"),
    ("metal3", (42, 0), "metal3"),
    ("via3", (40, 0), "via3"),
    ("metal4", (46, 0), "metal4"),
    ("via4", (41, 0), "via4"),
    ("metal5", (81, 0), "metal5"),
    ("metaltop", (53, 0), "metaltop"),
    ("pad", (37, 0), "pad"),
)

#: Runset layers `geom.drc` grid-checks that have no curated GDS number in
#: `LAYER_NAMES`, so no rule can be emitted for them -- recorded so the
#: omission is explicit rather than inferred. Keyed by runset layer variable.
#: Excludes the derived `*_dummy`/`*_label`/`*_blk`/`*_slot`/`*_res` metal
#: variants, which `geom.drc` also grid-checks and which are likewise
#: undrawn here.
OFFGRID_UNCHECKED_LAYERS: dict[str, str] = {
    "lvpwell": "LVPWELL (204/0) -- not in this deck's curated layer set",
    "esd": "ESD (24/0) -- drawn for ESD.* rules; no ongrid rule emitted for it",
    "fhres": "FHRES (227/0) -- not in this deck's curated layer set",
    "mvsd": "MVSD (210/0) -- not in this deck's curated layer set",
    "mvpsd": "MVPSD (11/39) -- not in this deck's curated layer set",
    "nat": "NAT (5/0) -- not in this deck's curated layer set",
    "v5_xtor": "V5_XTOR (112/1) -- not in this deck's curated layer set",
    "via5": "Via5 -- six-metal stack only; this deck has no Via5 layer",
}


def _offgrid_rules() -> list[DrcRule]:
    """Build one `"ongrid"` `DrcRule` per `_OFFGRID_ROWS` row (issue #3001)."""
    return [
        DrcRule(
            id=f"{stem}.ongrid.1",
            description=(
                f"{stem} vertices must be on the {OFFGRID_GRID_UM}um manufacturing grid"
            ),
            layer=layer,
            check="ongrid",
            threshold_dbu=0,  # unused by "ongrid" -- see grid_um
            grid_um=OFFGRID_GRID_UM,
            # geom.drc OFFGRID group: `<layer>.ongrid(0.005)` ->
            # "<layer>_OFFGRID : OFFGRID vertex on <layer>"
            scope="OFFGRID",
            provenance=_gf180mcu_klayout_deck_provenance(
                "geom", f"{runset_name}_OFFGRID"
            ),
            # One grid for every layer and voltage domain: there is no
            # second column to have misread (#552).
            voltage_independent=True,
        )
        for stem, layer, runset_name in _OFFGRID_ROWS
    ]


DECK += _offgrid_rules()

# ---------------------------------------------------------------------------
# Salicide-block (`SB.*`) rules -- `rule_decks/sab.drc` (issue #3001)
# ---------------------------------------------------------------------------
#
# `sab.drc` defines `SB.1`-`SB.16` over the SAB layer (49/0), under `if FEOL`
# (on by default in `run_drc.py`). Until issue #3001 this deck carried SAB as a
# layer name only, so a poly-resistor cell with SAB too close to a contact
# (`SB.4`) or a poly that stops short of the SAB edge (`SB.10`) read `clean`.
#
# Transcribed (the check kinds that express them exactly):
#
# - `SB.1`/`SB.2`: SAB width / space, 0.42um. The runset measures `SB.2` on
#   `sab.outside(otp_mk)`; `OTP_MK` is not a curated layer, so every SAB shape
#   is checked (the same set, in a stream with no OTP mark).
# - `SB.4`: `sab.separation(contact, 0.15, euclidian)`. The runset also ORs in
#   `sab & contact` (SAB *over* a contact); `Region.separation_check` reports
#   nothing for overlapping shapes, and that case is `SB.8` ("non-salicided
#   contacts are forbidden"), which is not transcribed -- see below.
# - `SB.10`: `sab.enclosed(poly_sab, 0.22, euclidian)` with
#   `poly_sab = poly2.interacting(sab)`, i.e. poly must extend 0.22um past the
#   SAB edge. Declared `report_escapes=False`: the DSL statement has no
#   zero-overlap escape term, and a correct poly resistor's SAB overhangs the
#   poly sideways, which the default #318 escape term would flag.
#
# Not transcribed, with the reason each is out of reach of the existing check
# kinds (or of this deck's curated layer set):
#
# - `SB.3`, `SB.5a`, `SB.5b`, `SB.9`, `SB.15a`, `SB.15b`: scoped to "unrelated"
#   COMP / Poly2 / Nplus-Pplus (`not_overlapping(sab)`, `interacting` chains).
#   The only whole-polygon selectors a `DerivedLayer` offers are
#   `not_interacting`/`overlapping`, which differ from `not_overlapping` on
#   touching shapes, and a derived-layer `"separation"` against the same drawn
#   layer is reserved for the MiM construction (`mim.space.1`).
# - `SB.6`, `SB.7`, `SB.11`: `comp.interacting(sab)` / `enclosed` / `overlap`
#   with `euclidian` polygon outputs, including a zero-overlap escape term
#   the default engine would add to the wrong side.
# - `SB.8`: `contact & sab` is a Boolean AND with no check-kind equivalent.
# - `SB.12`: needs `ESD_MK`, not a curated layer.
# - `SB.13`: min area 2um^2 on `sab.outside(otp_mk)`; not yet transcribed
#   (follow-up -- expressible as an `"area"` rule).
# - `SB.14a`/`SB.14b`/`SB.16`: need `pgate`/`tgate`/`LVS_IO`, i.e. MOS gate
#   recognition, which is device-extraction context, not a drawn layer.
_SAB = (49, 0)  # SAB
_SB_PROVENANCE_FILE = "sab"

DECK += [
    DrcRule(
        id="sab.width.1",
        description="minimum SAB width",
        layer=_SAB,
        check="width",
        threshold_dbu=420,  # 0.42 um
        # sab.drc "SB.1": `sab.width(0.42.um, euclidian)`
        scope="SB",
        provenance=_gf180mcu_klayout_deck_provenance(_SB_PROVENANCE_FILE, "SB.1"),
        voltage_independent=True,
    ),
    DrcRule(
        id="sab.space.1",
        description="minimum SAB spacing",
        layer=_SAB,
        check="space",
        threshold_dbu=420,  # 0.42 um
        # sab.drc "SB.2": `sab_out_otp.space(0.42.um, euclidian)` -- every SAB
        # shape here, since OTP_MK is not a curated layer.
        scope="SB",
        provenance=_gf180mcu_klayout_deck_provenance(_SB_PROVENANCE_FILE, "SB.2"),
        voltage_independent=True,
    ),
    DrcRule(
        id="sab.separation.contact.1",
        description="minimum SAB spacing to contact",
        layer=_SAB,
        other_layer=(33, 0),  # Contact
        check="separation",
        threshold_dbu=150,  # 0.15 um
        # sab.drc "SB.4": `sab_out_otp.separation(contact, 0.15.um,
        # euclidian)` (plus the `sab & contact` overlap term, see above).
        scope="SB",
        provenance=_gf180mcu_klayout_deck_provenance(_SB_PROVENANCE_FILE, "SB.4"),
        voltage_independent=True,
    ),
    DrcRule(
        id="poly2.enclosing.sab.1",
        description="minimum Poly2 extension beyond related SAB",
        layer=(30, 0),  # Poly2
        other_layer=_SAB,
        check="enclosing",
        threshold_dbu=220,  # 0.22 um
        report_escapes=False,
        # sab.drc "SB.10": `sab.enclosed(poly_sab, 0.22.um, euclidian)` with
        # `poly_sab = poly2.interacting(sab)`; `a.enclosed(b)` is
        # `b.enclosing(a)`, so Poly2 is the enclosing layer here. Edges of
        # Poly2 that no SAB edge faces produce nothing, which is what the
        # `interacting` pre-filter achieves in the DSL.
        scope="SB",
        provenance=_gf180mcu_klayout_deck_provenance(_SB_PROVENANCE_FILE, "SB.10"),
        voltage_independent=True,
    ),
]

# ---------------------------------------------------------------------------
# ESD implant (`ESD.*`) and guard-ring marker (`GR.*`) rules -- `esd.rb`,
# `guard_ring.rb` (issue #3792)
# ---------------------------------------------------------------------------
#
# Until issue #3792 this deck carried neither group, so a grounded-gate NMOS
# clamp with a sub-0.6um ESD implant, or a COMP closer than 10um to a
# GUARD_RING_MK marker, read `clean` with those rules never evaluated.
#
# Transcribed (the check kinds that express them exactly, from the drawn
# layers ESD 24/0 and GUARD_RING_MK 167/5):
#
# - `ESD.1`/`ESD.2`: ESD width / space, 0.6um.
# - `ESD.4b`: `esd.overlap(comp, 0.45)`.
# - `ESD.5a`/`ESD.5b`: min ESD area / min area of a hole in ESD, 0.49um^2.
# - `ESD.8`: `esd.separation(nplus.or(pplus), 0.3)`, one rule per implant
#   (a union of two layers has no single-layer spelling; the violating
#   distances are the same).
# - `GR.2`: `<layer>.separation(guard_ring_mk, 10)` for COMP, Nwell, Poly2 and
#   Metal1-5/MetalTop (the runset gates the metal levels on the stack depth;
#   an absent level is simply inapplicable here).
#
# Everything else in the two files is in `UNTRANSCRIBED_RULES` below, with the
# reason, and `klt drc` lists each in `coverage.unmodeled_rules` so a clean
# report cannot be read as covering them (informational; `status` is unchanged).
_ESD = (24, 0)  # ESD
_GUARD_RING_MK = (167, 5)  # GUARD_RING_MK
_ESD_PROVENANCE_FILE = "esd"
_GR_PROVENANCE_FILE = "guard_ring"

DECK += [
    DrcRule(
        id="esd.width.1",
        description="minimum ESD implant width",
        layer=_ESD,
        check="width",
        threshold_dbu=600,  # 0.6 um
        # esd.rb "ESD.1": `esd.width(0.6.um, euclidian)`
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.1"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.space.1",
        description="minimum ESD implant spacing",
        layer=_ESD,
        check="space",
        threshold_dbu=600,  # 0.6 um
        # esd.rb "ESD.2": `esd.space(0.6.um, euclidian)`
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.2"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.overlap.comp.1",
        description="minimum overlap of an ESD implant edge to a COMP",
        layer=_ESD,
        other_layer=(22, 0),  # Comp
        check="overlap",
        threshold_dbu=450,  # 0.45 um
        # esd.rb "ESD.4b": `esd.overlap(comp, 0.45.um, euclidian)`
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.4b"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.area.1",
        description="minimum ESD implant area",
        layer=_ESD,
        check="area",
        threshold_dbu=0,  # unused by "area" -- see area_min_dbu2
        area_min_dbu2=490_000,  # 0.49 um^2
        # esd.rb "ESD.5a": `esd.with_area(nil, 0.49.um)`
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.5a"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.holes_area.1",
        description="minimum field area enclosed by ESD implant",
        layer=_ESD,
        check="area",
        threshold_dbu=0,  # unused by "area" -- see area_min_dbu2
        area_min_dbu2=490_000,  # 0.49 um^2
        derived_layer=DerivedLayer(
            base=_ESD,
            sized_by_um=0.0,  # unused by "holes" mode
            mode="holes",
        ),
        # esd.rb "ESD.5b": `esd.holes.with_area(nil, 0.49.um)`
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.5b"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.separation.nplus.1",
        description="minimum ESD implant spacing to Nplus",
        layer=_ESD,
        other_layer=(32, 0),  # Nplus
        check="separation",
        threshold_dbu=300,  # 0.3 um
        # esd.rb "ESD.8": `esd.separation(nplus.or(pplus), 0.3.um)` (Nplus half)
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.8"),
        voltage_independent=True,
    ),
    DrcRule(
        id="esd.separation.pplus.1",
        description="minimum ESD implant spacing to Pplus",
        layer=_ESD,
        other_layer=(31, 0),  # Pplus
        check="separation",
        threshold_dbu=300,  # 0.3 um
        # esd.rb "ESD.8": `esd.separation(nplus.or(pplus), 0.3.um)` (Pplus half)
        scope="ESD",
        provenance=_gf180mcu_klayout_deck_provenance(_ESD_PROVENANCE_FILE, "ESD.8"),
        voltage_independent=True,
    ),
]

DECK += [
    DrcRule(
        id=f"{stem}.separation.guard_ring_mk.1",
        description=f"minimum {name} spacing to GUARD_RING_MK",
        layer=layer,
        other_layer=_GUARD_RING_MK,
        check="separation",
        threshold_dbu=10_000,  # 10 um
        # guard_ring.rb "GR.2": `<layer>.separation(guard_ring_mk, 10.um)`
        scope="GR",
        provenance=_gf180mcu_klayout_deck_provenance(_GR_PROVENANCE_FILE, "GR.2"),
        voltage_independent=True,
    )
    for stem, name, layer in (
        ("comp", "Comp", (22, 0)),
        ("nwell", "Nwell", (21, 0)),
        ("poly2", "Poly2", (30, 0)),
        ("metal1", "Metal1", (34, 0)),
        ("metal2", "Metal2", (36, 0)),
        ("metal3", "Metal3", (42, 0)),
        ("metal4", "Metal4", (46, 0)),
        ("metal5", "Metal5", (81, 0)),
        ("metaltop", "MetalTop", (53, 0)),
    )
]

# ---------------------------------------------------------------------------
# eFuse (`EF.*`) rules -- `rule_decks/efuse.rb` (issue #4316)
# ---------------------------------------------------------------------------
#
# `efuse.rb` defines 27 rule ids over EFUSE_MK (80/5), PLFUSE (125/5) and the
# derived `cathode`/`anode` poly regions. Until issue #4316 this deck carried
# both marker layers as layer names / off-grid checks only, so drawn eFuse
# geometry was unevaluated and a `clean` verdict had an undisclosed hole.
#
# Transcribed (the rules that need no derived cathode/anode layer):
#
# - `EF.14`: `lvs_source.not_outside(efuse_mk).not(efuse_mk)`, i.e. an
#   LVS_SOURCE (100/8) shape touching EFUSE_MK must lie inside it -- an
#   `"enclosing"` rule at margin 0 (interaction-scoped, like the runset).
# - `EF.17`: EFUSE_MK to EFUSE_MK space, 0.26um.
# - `EF.20`: `plfuse.separation(comp + nplus + esd + sab + resistor, 2.73,
#   euclidian)`, one rule per layer (a union of five layers has no
#   single-layer spelling; the violating distances are the same).
#
# Everything else is in `_EFUSE_UNMODELED_RULES` below, with the reason.
_EFUSE_MK = (80, 5)  # EFUSE_MK
_PLFUSE = (125, 5)  # PLFUSE
_LVS_SOURCE = (100, 8)  # LVS_SOURCE
_EF_PROVENANCE_FILE = "efuse"

DECK += [
    DrcRule(
        id="efuse_mk.enclosing.lvs_source.1",
        description="EFUSE_MK must enclose LVS_SOURCE",
        layer=_EFUSE_MK,
        other_layer=_LVS_SOURCE,
        check="enclosing",
        threshold_dbu=0,
        # efuse.rb "EF.14": `lvs_source.not_outside(efuse_mk).not(efuse_mk)`
        scope="EF",
        provenance=_gf180mcu_klayout_deck_provenance(_EF_PROVENANCE_FILE, "EF.14"),
        voltage_independent=True,
    ),
    DrcRule(
        id="efuse_mk.space.1",
        description="minimum EFUSE_MK to EFUSE_MK space",
        layer=_EFUSE_MK,
        check="space",
        threshold_dbu=260,  # 0.26 um
        # efuse.rb "EF.17": `efuse_mk.space(0.26.um, euclidian)`
        scope="EF",
        provenance=_gf180mcu_klayout_deck_provenance(_EF_PROVENANCE_FILE, "EF.17"),
        voltage_independent=True,
    ),
]

DECK += [
    DrcRule(
        id=f"plfuse.separation.{stem}.1",
        description=f"minimum PLFUSE space to {name}",
        layer=_PLFUSE,
        other_layer=other,
        check="separation",
        threshold_dbu=2730,  # 2.73 um
        # efuse.rb "EF.20": `plfuse.separation(comp + nplus + esd + sab +
        # resistor, 2.73.um, euclidian)`
        scope="EF",
        provenance=_gf180mcu_klayout_deck_provenance(_EF_PROVENANCE_FILE, "EF.20"),
        voltage_independent=True,
    )
    for stem, name, other in (
        ("comp", "Comp", (22, 0)),
        ("nplus", "Nplus", (32, 0)),
        ("esd", "ESD", _ESD),
        ("sab", "SAB", (49, 0)),
        ("resistor", "Resistor", (62, 0)),
    )
]

#: `efuse.rb` rule ids this deck knowingly does not transcribe (issue #4316),
#: with the reason each is out of reach of the existing check kinds.
#: `UNMODELED_RULES` surfaces every key as `coverage.unmodeled_rules`. Remove
#: an id when its rule is added.
_EFUSE_UNMODELED_RULES: dict[str, str] = {
    "EF.01": "needs `efuse_mk_pplus = efuse_mk & pplus`, a derived layer",
    "EF.02": (
        "`plfuse.width != 0.18um`: a min/max width on the short side; the "
        "bbox-based max half would flag the 1.26um length"
    ),
    "EF.03": "PLFUSE length via `centers`/edge-length selection",
    "EF.04a": "`interacting(poly2 - plfuse, 2, 2)` count selector on PLFUSE",
    "EF.04b": "`non_rectangles`: no check kind",
    "EF.04c": "`non_rectangles` on the derived `cathode` poly region",
    "EF.04d": "`non_rectangles` on the derived `anode` poly region",
    "EF.05": "needs the derived `poly2 & efuse_mk` region and LVS_SOURCE boolean",
    "EF.06": "edge-length test on the derived `cathode` region",
    "EF.07": "edge-length test on the derived `cathode` region",
    "EF.08": "edge-length test on the derived `anode` region",
    "EF.09": "edge-length test on the derived `anode` region",
    "EF.10": "needs the derived `cathode` region",
    "EF.11": "needs the derived `anode` region",
    "EF.12": "needs `contact & cathode` and a polygon-output separation",
    "EF.13": "needs `contact & anode` and a polygon-output separation",
    "EF.15": "`plfuse.interacting(contact)`: no polygon-selector check kind",
    "EF.16a": "`cathode.not_covering(contact, 4, 4)`: derived region + count",
    "EF.16b": "`anode.not_covering(contact, 4, 4)`: derived region + count",
    "EF.18": "`plfuse.outside(...)` polygon selector over a union incl. Metal1/2",
    "EF.19": "`plfuse.outside(metal1).outside(metal2)` polygon selector",
    "EF.21": "`extents.edges` / `centers` edge-length test on derived regions",
    "EF.22a": "edge-length test on the derived `cathode` region",
    "EF.22b": "edge-length test on the derived `anode` region",
}

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
}


#: Official gf180mcu DRM rule ids this curated deck knowingly does *not*
#: transcribe (issue #3796): the thick-MetalTop (30 kA / "MT30") option, the
#: wide-metal/die-level MetalTop rules, and the metal-slotting rules
#: (DRM sections 7.15, 7.16, 14.6.3; the PDK's native
#: `metaltop_30k.rb`/`mslot.rb`/`density.rb` decks encode them). This deck
#: models only standard-thickness MetalTop (`metaltop.width.1` = MT.1,
#: `metaltop.space.1` = MT.2a) and has no PDK-variant selection, so a layout
#: that is `klt drc --deck gf180mcu` clean can still fail these on a 30K
#: variant such as `gf180mcuA`. `coverage.unmodeled_rules` (and `klt deck
#: info`/`klt deck rules`) list them rather than let a clean verdict read as
#: full coverage. Remove an id here when its rule is added.
_MT_MSLOT_UNMODELED_RULES: tuple[str, ...] = (
    "MSLOT.1",
    "MSLOT.2",
    "MSLOT.3",
    "MSLOT.4",
    "MSLOT.5",
    "MSLOT.6",
    "MSLOT.7",
    "MSLOT.8",
    "MT.2b",
    "MT.3",
    "MT.4",
    "MT30.1a",
    "MT30.1b",
    "MT30.2",
    "MT30.3",
    "MT30.4",
    "MT30.5",
    "MT30.6",
    "MT30.7",
    "MT30.8",
)

#: Metal1-5 wide-metal spacing and the narrow-line end-of-line companions this
#: deck does not transcribe (issue #3968). `Mn.2b` ("space to wide Metaln",
#: length and width > 10um) needs a wide-metal predicate and `CO.6a`/`CO.6b`,
#: `Vn.3c`/`Vn.3d`, `Vn.4b`/`Vn.4c` an end-of-line (narrow metal < 0.34um)
#: predicate, neither of which `DrcRule`'s vocabulary can express. Ids are the
#: native runset's (`M1.2b`...`M5.2b`; via ids are stack-keyed, so no `V5.*`
#: for this fixed 5LM stack). Remove an id here when its rule is added.
_METAL_EOL_UNMODELED_RULES: tuple[str, ...] = (
    "CO.6a",
    "CO.6b",
    "M1.2b",
    "M2.2b",
    "M3.2b",
    "M4.2b",
    "M5.2b",
    *(f"V{n}.{rule}" for n in (1, 2, 3, 4) for rule in ("3c", "3d", "4b", "4c")),
)

# (layer, datatype) -> upstream layer name, from gf180mcu.lyp's layer-map
# (google/globalfoundries-pdk-libs-gf180mcu_fd_pv), used only to render
# violations[].layer as e.g. "Poly2" instead of the bare "30/0" fallback --
# and (issue #2365) `klt deck devices`' per-device-class drawn-layer
# predicate sets, which is why the device-recognition layers this deck's
# `resistors`/`diodes` entries key off -- the markers (`RES_MK` 110/5,
# `diode_mk` 115/5), the poly-resistor `requires` pair (`SAB` 49/0,
# `Resistor` 62/0) and the deep-well layer they exclude (`DNWELL` 12/0) --
# are named here too: a predicate set that renders as a bare `115/5` is
# materially harder to act on than one that renders as `diode_mk (115/5)`.
# None of the five carries a DRC rule in `DECK` above, so naming them
# changes no `violations[].layer` rendering.
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
    (35, 0): "Via1",
    (36, 0): "Metal2",
    (38, 0): "Via2",
    (42, 0): "Metal3",
    (40, 0): "Via3",
    (46, 0): "Metal4",
    (41, 0): "Via4",
    (81, 0): "Metal5",
    (53, 0): "MetalTop",
    (37, 0): "Pad",
    (75, 0): "FuseTop",
    (55, 0): "Dualgate",
    (12, 0): "DNWELL",
    (49, 0): "SAB",
    (62, 0): "Resistor",
    (110, 5): "RES_MK",
    (115, 5): "diode_mk",
    (127, 5): "DRC_BJT",
    (117, 5): "CAP_MK",
    (117, 10): "MIM_L_MK",
    (166, 5): "MOS_CAP_MK",
    (80, 5): "efuse_mk",
    (125, 5): "plfuse",
    (241, 0): "Schottky_diode",
    (24, 0): "ESD",
    (167, 5): "GUARD_RING_MK",
    (100, 8): "LVS_SOURCE",
}

#: Official gf180mcu rule ids this curated deck knowingly does *not*
#: transcribe (issue #3791): the Dualgate (`dualgate.drc`) rules that need
#: compound/cross-layer expressions `DrcRule` cannot evaluate (DV.1, DV.3,
#: DV.6-DV.9) and the 5V/6V `_MV` columns of the Comp/Poly2/Nwell/LPW
#: families, of which only DF.1a/DF.3a/DF.13/DF.14 are transcribed. Listed as
#: `coverage.unmodeled_rules` so a `clean` verdict on a Dualgate layout is not
#: read as full 5V/6V coverage. Remove an id here when its rule is added.
_UNMODELED_DUALGATE_RULES: tuple[str, ...] = (
    "DF.15a_MV",
    "DF.15b_MV",
    "DF.16_MV",
    "DF.17_MV",
    "DF.19_MV",
    "DF.1b_MV",
    "DF.2a_MV",
    "DF.3c_MV",
    "DF.4a_MV",
    "DF.4b_MV",
    "DF.4c_MV",
    "DF.4d_MV",
    "DF.4e_MV",
    "DF.5_MV",
    "DF.6_MV",
    "DF.7_MV",
    "DF.8_MV",
    "DV.1",
    "DV.3",
    "DV.6",
    "DV.7",
    "DV.8",
    "DV.9",
    "LPW.1_MV",
    "LPW.2a_MV",
    "LPW.2b_MV",
    "NW.1a_MV",
    "NW.1b_MV",
    "NW.2a_MV",
    "NW.2b_MV",
    "NW.5_MV",
    "NW.6_MV",
    "NW.7_MV",
    "PL.1_MV",
    "PL.1a_MV",
    "PL.2_MV",
    "PL.3b_MV",
    "PL.4_MV",
    "PL.5a_MV",
    "PL.5b_MV",
    "PL.7_MV",
)

#: Everything this deck knowingly does not transcribe, surfaced by
#: `decks.get_unmodeled_rules` as `coverage.unmodeled_rules`: the untranscribed
#: ESD/guard-ring ids (issue #3792), the thick-MetalTop and metal-slotting ids
#: (issue #3796), the Metal1-5 wide-metal/end-of-line ids (issue #3968) and
#: the Dualgate ids (issue #3791).
UNMODELED_RULES: tuple[str, ...] = tuple(
    sorted(
        (
            *UNTRANSCRIBED_RULES,
            *_EFUSE_UNMODELED_RULES,
            *_MT_MSLOT_UNMODELED_RULES,
            *_METAL_EOL_UNMODELED_RULES,
            *_UNMODELED_DUALGATE_RULES,
        )
    )
)

# Voltage-domain marker layers this deck draws but does not *fully* model the
# DRC/extraction scoping of (issue #552): `Dualgate` (55/0) selects gf180mcu's
# 5V/6V thick-oxide domain, whose DRM publishes a second, 30-60% larger column
# of thresholds and a distinct set of MOS models.
#
# What is modelled as of issue #1110: the `DF.1a`/`DF.3a` `Comp` width/space
# rules ship as `_LV`/`_MV` rule *pairs* (`comp.width.1`/`comp.width.mv.1`,
# `comp.space.1`/`comp.space.mv.1`), each half scoped to the right side of the
# marker via `DerivedLayer`'s `"not_interacting"`/`"overlapping"` modes -- so
# a `Comp` shape drawn inside `Dualgate` is now checked against 0.30/0.36 um,
# not 0.22/0.28 um, and `drc.py` no longer counts geometry those four rules
# checked toward this warning's gate.
#
# What is NOT modelled, and is what this entry now warns about: every other
# DRC rule above still encodes the 3.3V column only and never reads
# `Dualgate` -- including DRM rules with an `_MV` column this deck has not
# transcribed at all yet (`DF.6` COMP extend beyond gate 0.24 vs. 0.40,
# `PL.5a`/`PL.5b` field poly to COMP 0.10 vs. 0.30).
#
# What issue #1111 (#552's option 2) additionally closed: `EXTRACTION_DECK`
# below now declares one `mos_flavours` entry keyed on this same `Dualgate`
# marker (see `MOSFlavour`'s own docstring in `decks/__init__.py`), so a
# transistor drawn entirely inside `Dualgate` extracts bound to the real
# `nfet_06v0`/`pfet_06v0` models (`--pdk`-resolved SPICE output) instead of
# the default 3.3V ones -- `extract.py`'s own MOS `voltage_domain_warnings`
# no longer fires for this marker (it only ever warned about the *MOS*
# binding gap; the DRC-rule gap above is unaffected and still fires via
# `drc.py`'s own per-rule `coverage.voltage_domain_warnings` gate). This
# registry entry stays -- and its description below is updated to drop the
# now-closed MOS claim -- because the DRC-rule residue above remains real.
#
# `Dualgate` is *also* read correctly today (not part of either gap above) by
# the `DiodeDevice` entries further below, each via its own
# `requires=(..., Dualgate)` field (issue #542).
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {
    (55, 0): (
        "Dualgate (55/0) marks gf180mcu's 5V/6V thick-oxide voltage domain. "
        "This curated deck models it for the DF.1a/DF.3a COMP width/space "
        "rules, the Dualgate DV.2/DV.5 width/space rules (issue #3791) and "
        "(issue #1111) MOS device recognition/model binding "
        "(nfet_06v0/pfet_06v0 under --pdk) only; the other Dualgate DV rules "
        "and _MV columns are listed in coverage.unmodeled_rules, and every "
        "other DRC rule still "
        "applies the 3.3V/_LV thresholds regardless of Dualgate's presence "
        "(e.g. DF.6 COMP extend beyond gate 0.24 vs. 0.40, PL.5a/PL.5b field "
        "poly to COMP 0.10 vs. 0.30 -- all in um, and neither transcribed in "
        "this deck yet). The DF.13/DF.14 tap-distance rules read it as the "
        "MV (15 um) selector (comp.tap_distance.{nwell,psub}.mv.1)."
    ),
}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #
#
# Metal1's pin/label purpose (34/10) is not in LAYER_NAMES above (that table
# only names *drawn* layers); verified against this repo's own gf180mcu
# corpus fixtures (`tests/corpus/gf180mcu/gf180mcu_fd_sc_mcu9t5v0__*.gds`),
# which label every signal and power pin directly on 34/10 (e.g. "I", "ZN",
# "VDD", "VSS").
#
# `poly_label` (30/10) applies that same pin/label purpose (datatype 10) to
# the Poly2 layer, mirroring `well_label`'s pattern on sky130: a text on poly
# names the poly net directly, so a device gate `klt gen` draws as bare poly
# (no contact/metal landing pad -- see `gen.py`'s `_mos_unit_layout`) can be
# promoted to a named `.SUBCKT` pin by `klt gen-compose`'s `pins[]` (#210).
# Like sky130's 66/5, this has no corpus precedent (corpus cells route gates
# in metal) -- a curated choice consistent with this deck's datatype-10 label
# convention.
#
# Unlike sky130, gf180mcu's curated layer set has no distinct substrate/well
# tie layer -- a well/substrate tap is drawn on the *same* `Comp` layer as
# transistor active, so `tap` itself is left `None` below. Issue #1084:
# rather than leaving well/substrate ties structurally unrecognisable
# (`tap=None` with no fallback -- this deck's own pre-#1084 documented
# approximation, and the friction that issue reports), this deck instead
# declares `tap_nplus`/`tap_pplus` -- the same `Nplus` (32/0) / `Pplus`
# (31/0) implant layers already named/used above for bipolar/resistor/diode
# recognition -- so `extract.py` can *derive* an equivalent tap region: an
# n+ (`Nplus`)-covered Comp shape *inside* `Nwell` is a well tie (ties the
# PMOS body to the well's real net), a p+ (`Pplus`)-covered Comp shape
# *outside* every `Nwell` is a substrate tie (ties the NMOS body to the
# substrate's real net) -- exactly how the PDK's own official LVS deck
# recognises a tie with no dedicated tap mask (see the issue's own "What
# would close this" section). Neither can collide with an ordinary
# transistor's own source/drain diffusion: gf180mcu's PMOS source/drain is
# `Pplus`-covered Comp *inside* the well (opposite doping from a well tie
# there) and its NMOS source/drain is `Nplus`-covered Comp *outside* the
# well (opposite doping from a substrate tie there) -- see
# `ExtractionDeck.tap_nplus`/`tap_pplus`'s own docstring for the exact
# derivation `extract.py` performs.
#
# A layout that draws no such tie extracts exactly as it always did (both
# `tap_nplus`/`tap_pplus` are additive-only derivation inputs, not device-
# recognition layers -- there is no drawn geometry to derive a tap region
# from, so `extract.py`'s `tap` region for it is empty, same as before this
# field existed): the PMOS body terminal picks up whatever net (if any) a
# Comp/contact/metal shape *inside* the Nwell polygon happens to be tied to
# via ordinary connectivity (see `ExtractionDeck`'s docstring on why `nwell`
# is never connected to `contact` directly), and is otherwise a floating,
# anonymous net in the extracted netlist.
#
# NMOS body: as with sky130, no separate substrate layer is drawn in this
# curated deck, so absent a drawn substrate tie, the NMOS body terminal is
# tied to the deck's `substrate_net` global (`ExtractionDeck.substrate_net`,
# default `"vsubs"`) -- exactly as before #1084.
#
# Bipolar (BJT) device recognition (issue #223): base = Nwell (the physical
# n-type body a vertical bipolar's emitter diffusion sits in -- the same
# layer this deck's `nwell` MOS-recognition role already uses), emitter =
# Comp (the same diffusion mask an ordinary PMOS source/drain uses; this
# curated deck does not model Nplus/Pplus implants, so Comp alone cannot
# distinguish device polarity -- see `class_name` below), marker = DRC_BJT
# (127/5, `bjt.separation.comp.1` above) -- `extract.py` intersects
# base/emitter with the marker so only genuine BJT device-cell instances are
# recognised, not every Nwell drawn for an ordinary PMOS. No drawn collector
# layer -- the DRM's vertical bipolar's collector is the substrate, tied to
# `substrate_net` like the NMOS body above (see `BipolarDevice`'s
# docstring). `class_name="bjt"` (not `"npn"`/`"pnp"`): the DRM's own 10.7
# section title ("vertical NPN/PNP bipolar rule category") names both
# polarities under one mark layer, and unlike sky130's `pnp_05v5` this repo
# has no positively-identified single device-cell name to attribute a
# specific polarity to -- a generic class name is used rather than guessing.
#
# MiM capacitor device recognition (issue #225), transcribed from the
# official KLayout LVS deck's own derivation and extraction calls (see the
# module docstring's provenance note above for the exact source files):
#
#     mimtm_virtual = fusetop.sized(1.06.um)
#                            .and(topmin1_metal.interacting(fusetop))
#     fuse_cap = fusetop.interacting(cap_mk).interacting(mim_l_mk)
#                       .not(mimcap_exclude)   # mimcap_exclude = efuse_mk.join(plfuse)
#     extract_devices(capacitor('cap_mim_2f0_m4m5_noshield', 2.0e-15, MIMCap),
#                     { 'P1' => mimtm_virtual, 'P2' => fuse_cap })
#
# `area_cap_f_um2`/`perim_cap_f_um` below use the more precise two-term
# `sm141064.ngspice` simulation-model coefficients rather than the LVS
# script's own rounded `2.0e-15` single-term call shown above -- see the
# module docstring's provenance note (issue #512) for the exact source and
# why both terms come from that file instead.
#
# for `METAL_LEVEL = '5LM'` (this curated deck's own Metal1-Metal5 stack --
# see the DRC deck's own "10.4 MIM Capacitor" note above on why only Option B
# applies here) and the LVS deck's own default `MIM_CAP = '2'` (2.0fF/um^2 --
# see the provenance note above on why this deck models only the default of
# the PDK's three selectable densities, not all three: which one a given
# fab run actually ships is a foundry-side option selected at LVS-runset
# invocation time, not something a drawn layout's own geometry can
# distinguish -- there is no per-instance layer marking "this cap uses the
# 1.5fF option" the way `requires`/`excludes` narrows other device classes
# below). `topmin1_metal` for `5LM` is `Metal4` (46/0, `metal4.not(metal4_res)`
# in the official deck; this curated deck does not model `metal4_res`, the
# separately-marked-resistor exclusion -- an unmodelled, narrow gap, mirrors
# the "curated starter subset" scope guard elsewhere in this file).
#
# `top_plate`/`bottom_plate` are `FuseTop`/`Metal4`. `Metal4` is part of this
# deck's `metals` connectivity stack (#220) -- `extract.py`'s bottom-plate
# connectivity wiring (issue #314) ties the recognised (virtual-plate-clipped)
# bottom region into `metals[3]` because `bottom_plate` matches that entry,
# so ordinary routing to `Metal4` reaches this terminal. `top_plate_via`/
# `top_plate_via_metal` (`Via4` 41/0 / `Metal5` 81/0, #314) wire the top plate
# the same way: the official deck's `main.drc` connectivity confirms
# `top_via = via4` lands on `top_metal = metal5` for this stack (see the
# module docstring's "10.4 MIM Capacitor" note), the same `Via4`/`Metal5`
# entries already in this deck's own `vias`/`metals` tuples above.
# `bottom_plate_oversize_um=1.06` reproduces the official "virtual bottom
# plate" derivation exactly (`extract.py`'s capacitor-resolution block
# performs the same `interacting`-then-`sized`-`and` two-step). `CAP_MK`
# (117/5) and `MIM_L_MK` (117/10) are both required on the top plate,
# matching `fuse_cap`'s double `.interacting(...)` above; `efuse_mk` (80/5)
# and `plfuse` (125/5) are excluded, matching `mimcap_exclude`.
#
# Drawn resistors (#222). Additional layer numbers, from the same
# `google/globalfoundries-pdk-libs-gf180mcu_fd_pv` source cited at the top of
# this module -- specifically its KLayout **LVS** deck's own layer table,
# `klayout/lvs/rule_decks/layers_definitions.lvs` (as installed by volare
# under `libs.tech/klayout/lvs/`):
#
#     DNWELL      12/0    (`dnwell   = get_polygons(12, 0)`)
#     Pplus       31/0    (`pplus    = get_polygons(31, 0)`)
#     Nplus       32/0    (`nplus    = get_polygons(32, 0)`)
#     SAB         49/0    (`sab      = get_polygons(49, 0)`  -- salicide block)
#     RES_MK     110/5    (`res_mk   = get_polygons(110, 5)` -- resistor mark)
#     Resistor    62/0    (`resistor = get_polygons(62, 0)`  -- the high-sheet-rho
#                          poly mark; this is the layer the DRC deck's own
#                          `hres.drc` rule file keys off)
#
# The one resistor device modelled below is `ppolyf_u` -- the DRM's "P+ poly,
# unsalicided" precision resistor (DRM chapter "10.0 Analog Device Related
# Rules", and `magic/gf180mcuD.tech`'s device list comment
# "#  ppolyf_u  resistor (P+ poly, unsalicided)"). The official KLayout LVS
# deck derives and extracts it (`lvs/rule_decks/res_derivations.lvs` +
# `res_extraction.lvs`) as:
#
#     ppoly_exclude   = comp.join(res_exclude).join(nplus)
#     ppolyf_u_layer  = pplus.and(poly2).and(sab).and(res_mk)
#                            .not_interacting(resistor).not(dnwell)
#                            .not(ppoly_exclude)
#     poly2_con       = poly2.not(res_mk).not(plfuse)
#     extract_devices(resistor_with_bulk('ppolyf_u', 350, BResistor),
#                     { 'R' => ppolyf_u_layer, 'C' => poly2_con, 'W' => sub })
#
# i.e. sheet resistance **350 ohm/square**, with a bulk terminal tied to the
# substrate (hence `bulk_to_substrate=True` below, which carries this deck's
# same documented substrate-global approximation). Cross-checked against a
# second, independent source in the same PDK install: open_pdks' magic
# technology file `libs.tech/magic/gf180mcuD.tech`, whose extract section
# reads `resist (rpp)/active 350000` (milliohms per square = 350 ohm/sq) for
# the same `rpp` device type (`device rsubcircuit ppolyf_u rpp *poly ...`).
#
# Approximations relative to that official derivation, all conservative:
#
# - `SAB` (salicide block) is a *required* marker, not an optional one. It is
#   exactly what separates this 350 ohm/sq unsalicided device from the
#   salicided `ppolyf_s` (7.3 ohm/sq, `resist (allpolynonres)/active 7300` in
#   the same magic tech file) -- a ~48x difference. A salicided p+ poly
#   resistor therefore stays ordinary connected poly here (today's behaviour
#   -- a short) rather than being reported with a 48x-too-large resistance.
# - `Resistor` (62/0) is an exclusion for `ppolyf_u`: it marks the
#   high-sheet-rho `ppolyf_u_1k`/`_2k`/`_3k` flavours (1000/2000/3000 ohm/sq;
#   see the `ppolyf_u_1k` entry immediately below, which now claims that
#   marked geometry instead of leaving it an unmodelled short). Upstream
#   drops the whole shape (`not_interacting`); this deck subtracts it
#   (`not`), which differs only for a marked segment the `Resistor` layer
#   partially covers.
# - `res_exclude` (esd, fusewindow_d, polyfuse, diode_mk, drc_bjt, nat,
#   mos_cap_mk, esd_mk, lvs_source, efuse_mk, plfuse, mvsd, mvpsd,
#   ldmos_xtor, schottky_diode) is **not** modelled -- none of those layers
#   are in this curated starter subset. A resistor-marked poly segment also
#   carrying one of them extracts as `ppolyf_u` rather than being excluded.
# - The terminal ("C") region is `Poly2` minus the recognised body rather
#   than upstream's `poly2.not(res_mk).not(plfuse)`; the two agree except on
#   marked poly this deck does not recognise as a resistor, which stays
#   ordinary connected poly here rather than being cut out of connectivity.
#
# `ppolyf_u_1k` (issue #299): the high-sheet-rho flavour `ppolyf_u`'s own
# `excludes` above rules out. The official LVS deck's own `res_derivations.lvs`
# derives it as one single region regardless of which of the three named
# flavours a given fab run selects:
#
#     ppoly_exclude = poly_exclude.join(nplus)   # poly_exclude = comp.join(res_exclude)
#     ppolyf_u_h    = poly2.and(sab).and(res_mk).and(resistor)
#                          .not(dnwell).not(v5_xtor).not(dualgate).not(ppoly_exclude)
#
# and `res_extraction.lvs` then extracts *that same* `ppolyf_u_h` region three
# separate ways, selected by a build-time `POLY_RES` deck option this curated
# deck models no equivalent of -- **not** by any additional drawn geometry:
#
#     case POLY_RES
#     when '1k'  extract_devices(resistor_with_bulk('ppolyf_u_1k', 1000, BResistor), ..)
#     when '2k'  extract_devices(resistor_with_bulk('ppolyf_u_2k', 2000, BResistor), ..)
#     when '3k'  extract_devices(resistor_with_bulk('ppolyf_u_3k', 3000, BResistor), ..)
#     end
#
# i.e. a drawn `Resistor`-marked poly segment is geometrically identical
# regardless of which of the three sheet-rho values it is meant to model --
# the same "no per-instance layer marking distinguishes the option" situation
# `EXTRACTION_DECK.capacitors`' MiM density note above already documents for
# `POLY_RES`'s sibling `MIM_CAP` option. Wiring all three as separate
# `ResistorDevice` entries would therefore have every one of them recognise
# the *same* drawn shape and each cut it out of `poly` in turn -- three
# duplicate `R` devices across one physical resistor, not three selectable
# flavours a layout can actually distinguish between. So exactly **one**
# `ResistorDevice` entry is wired below, mirroring the upstream deck's own
# structure (one region, one `case POLY_RES` selection at build time, never
# three simultaneous devices) -- but, unlike before issue #595, that one
# entry now carries all three named flavours via `flavour_option="poly_res"`/
# `flavours=(...)`, so a caller who knows their design was built against the
# `2k`/`3k` interpretation can select it explicitly
# (`klt extract --deck-option poly_res=2k`) instead of being stuck with
# whichever flavour the deck happens to default to. The **default** stays the
# PDK's own default absent an override: `gf180mcu.lvs` sets
# `POLY_RES = $poly_res || '1k'` when the invoking flow does not override it,
# and (like the MiM density default cited above) this is independently
# confirmed as the default *this specific PDK build* ships by open_pdks'
# `gf180mcu.json` variant-description string, `"...2fF MiM + 1k high sheet
# rho poly"`. Selecting `2k`/`3k` only ever changes this entry's *reported*
# name/sheet-rho -- it is still exactly one recognised device per drawn
# segment, never three, and a caller who never passes `--deck-option
# poly_res=...` sees the identical `ppolyf_u_1k`-at-1000-ohm/sq extraction
# this deck reported before #595, byte-for-byte.
#
# Unlike the base `ppolyf_u` entry, `ppolyf_u_h` above does **not** `.and()`
# `Pplus`: the official H-POLY-RES derivation recognises this flavour off
# `SAB`/`RES_MK`/`Resistor` alone (narrowed only by `not(comp)`/`not(nplus)`/
# `not(dnwell)`), so `ppolyf_u_1k` below mirrors that by requiring `SAB` +
# `Resistor` (its own high-sheet-rho marker) without also requiring `Pplus`,
# deliberately not adding a stricter condition the real deck does not apply.
#
# Drawn metal resistors `rm1`/`rm2`/`rm3` and the `tm6k`/`tm9k`/`tm11k`/
# `tm30k` top-metal flavour set (issue #1640, the gf180mcu counterpart of
# sky130's `res_generic_m1`..`res_generic_m5`, issue #1621). Verified against
# the same real fetched `gf180mcuB` install this module's own provenance
# constants pin (`google/globalfoundries-pdk-libs-gf180mcu_fd_pv` @
# `c6d73a35f524070e85faff4a6a9eef49553ebc2b`). `res_derivations.lvs`'s own
# "---- METAL ----" section:
#
#     rm1_res = metal1.and(metal1_res)
#     rm2_res = metal2.and(metal2_res)
#     rm3_res = metal3.and(metal3_res)
#     tm_res  = top_metal.and(top_metal_res)
#
# and `res_extraction.lvs`'s own "---- METAL ----" section extracts each via
# KLayout's native 2-terminal `resistor(name, rsh, NResistor)` builtin (not
# `resistor_with_bulk`), so -- like sky130's `res_generic_mN` and sg13g2's
# `res_metal1`/`res_metal2` -- none of these five classes carry a bulk/
# substrate terminal (`bulk_to_substrate` stays the default `False` below):
#
#     extract_devices(resistor('rm1', 0.09, NResistor),
#                     { 'R' => rm1_res, 'C' => metal1_con })
#     unless METAL_LEVEL == '2LM'
#       extract_devices(resistor('rm2', 0.09, NResistor),
#                       { 'R' => rm2_res, 'C' => metal2_con })
#     end
#     unless METAL_LEVEL == '3LM'
#       extract_devices(resistor('rm3', 0.09, NResistor),
#                       { 'R' => rm3_res, 'C' => metal3_con })
#     end
#     case METAL_TOP
#     when '6K'
#       extract_devices(resistor('tm6k', 0.06, NResistor),
#                       { 'R' => tm_res, 'C' => top_metal_con })
#     when '9K'
#       extract_devices(resistor('tm9k', 0.04, NResistor),
#                       { 'R' => tm_res, 'C' => top_metal_con })
#     when '11K'
#       extract_devices(resistor('tm11k', 0.04, NResistor),
#                       { 'R' => tm_res, 'C' => top_metal_con })
#     when '30K'
#       extract_devices(resistor('tm30k', 0.0095, NResistor),
#                       { 'R' => tm_res, 'C' => top_metal_con })
#     end
#
# `rsh` units are already ohms/sq here (unlike sky130's milliohm/sq LVS
# literal) -- independently cross-checked against `sm141064.ngspice`'s own
# `+rsh_rm1=0.09` / `+rsh_tm6k=60e-3` etc. process-corner parameters (see
# below), which agree bit-for-bit.
#
# Marker layers, from `layers_definitions.lvs`:
#
#     metal1_res  110/11  (`metal1_res = get_polygons(110, 11)`)
#     metal2_res  110/12  (`metal2_res = get_polygons(110, 12)`)
#     metal3_res  110/13  (`metal3_res = get_polygons(110, 13)`)
#     metal5_res  110/15  (`metal5_res = get_polygons(110, 15)`)
#
# `top_metal`/`top_metal_res` are themselves a `case METAL_LEVEL` selection
# (`layers_definitions.lvs`) -- upstream's own default is `METAL_LEVEL =
# '6LM'` (`top_metal = metaltop`, 53/0), but this curated deck's own
# connectivity model caps the routing stack at Metal5 (`metals`/`vias` above
# stop at Metal5 -- see the module docstring's own `MetalTop`/6LM note, and
# `gen.py`'s identical "gf180mcu output assumes the 5LM metal stack (top_metal
# = Metal5)" precedent), matching upstream's own `'5LM'` case instead:
# `top_metal = metal5`, `top_metal_res = metal5_res`, `top_metal_con =
# metal5_con.not(fusetop)`. The `tm_res`/`tm*` flavour set below is therefore
# bound to Metal5/`metal5_res`, not MetalTop -- a documented approximation
# (a 6LM fab run's real top-metal resistor is drawn on MetalTop, invisible to
# this curated deck's connectivity model either way). One further, smaller
# approximation, in the same "cite the most specific source, keep the
# conservative direction" spirit as `ppolyf_u`'s own terminal note above:
# `top_metal_con` additionally excludes `FuseTop` (`.not(fusetop)`, since
# Metal5 also participates in this deck's MiM-cap stack); this deck's
# `ResistorDevice` terminal is always plain `body`-minus-recognised-segment
# with no further per-layer subtraction, so a `tm*` resistor terminal drawn
# under a `FuseTop`-shielded MiM cap on the same Metal5 shape stays connected
# here where upstream would cut it -- a narrow, undrawn-in-practice corner
# (a resistor terminal and a MiM cap plate sharing the same Metal5 polygon).
#
# `rm1`/`rm2`/`rm3`/`tm6k`/`tm9k`/`tm11k`/`tm30k` open research item
# (resolves this issue's own open item 1, mirroring #1621's identical gate
# for sky130): **unlike** sky130's `res_generic_mN` bare-`.model`-only
# carve-out, gf180mcu's own vendored ngspice model tree
# (`libs.tech/ngspice/sm141064.ngspice`, the same file
# `pdk_models.py`'s module docstring already cites for `ppolyf_u`) declares a
# real two-terminal `.subckt` for **every one** of these seven device names,
# confirmed by direct inspection of a real fetched `gf180mcuB` install:
#
#     .subckt rm1 1 2 r_length=l r_width=w dtemp=0 par=1 s=1
#       ...
#       rb 1 2 r='r_temp*r_n*(r_rsh0+...)'
#     .ends rm1
#
# (and identically-shaped `.subckt rm2`/`rm3`/`tm6k`/`tm9k`/`tm11k`/`tm30k`
# blocks), each keyed by the same `r_length`/`r_width` call-site parameter
# convention `ppolyf_u`/`ppolyf_u_1k` already use (`_RESISTOR_PARAM_STYLE`).
# Further cross-checked against the same install's reference CDL testcases
# (`libs.tech/klayout/lvs/testing/testcases/unit/res_devices/netlist/
# rm1.cdl`/`tm6k.cdl`), which instantiate `rm1`/`tm6k` as a primitive `R...`
# element carrying that subcircuit name as its model token, and whose
# `+rsh_rm1=0.09` / `+rsh_tm6k=60e-3` process-corner parameters (in
# `sm141064.ngspice`, same file) agree with `res_extraction.lvs`'s own
# `0.09`/`0.06` literals above. So -- unlike sky130's five metal-resistor
# classes -- all seven of these gf180mcu classes **do** get a
# `_RESISTOR_MODEL_TABLE` binding entry (see `pdk_models.py`).
#
# `tm6k`/`tm9k`/`tm11k`/`tm30k` share *identical* recognition geometry
# (`tm_res` = `top_metal.and(top_metal_res)`, disambiguated only by the
# build-time `METAL_TOP` deck option, not by any drawn layer) -- the exact
# "shared-geometry flavour" shape `ppolyf_u_1k`'s own `flavour_option=
# "poly_res"` above already solves for `POLY_RES` (issue #595). So, mirroring
# that structure, exactly **one** `ResistorDevice` entry is wired below for
# this family, carrying all four named flavours via
# `flavour_option="metal_top"`/`flavours=(...)` rather than four separate
# always-on entries that would each recognise the same drawn Metal5 segment.
# The default flavour is `gf180mcu.lvs`'s own upstream default absent a
# caller override -- `METAL_TOP = $metal_top || '9K'` -- so `tm9k` (0.04
# ohm/sq) is this entry's own `name`/`sheet_rho_ohm_sq`, matching
# `ppolyf_u_1k`'s identical "default flavour is the entry's own name" choice
# for `POLY_RES`'s `'1k'` default. Flavour `value` tokens are spelled
# `"6K"`/`"9K"`/`"11K"`/`"30K"`, matching the PDK's own upstream `METAL_TOP`
# deck-variable spelling (`case METAL_TOP; when '6K' ...`) exactly, the same
# "value token cites the real deck-variable spelling" discipline
# `ppolyf_u_1k`'s own lowercase `"1k"`/`"2k"`/`"3k"` values follow for
# `POLY_RES`'s lowercase case-statement spelling.
#
# Junction-diode device recognition (issue #542). Additional layer numbers
# from the same `klayout/lvs/rule_decks/layers_definitions.lvs` table cited
# for the resistors above:
#
#     Dualgate    55/0    (`dualgate = get_polygons(55, 0)`)
#     diode_mk    115/5   (`diode_mk = get_polygons(115, 5)`)
#
# Transcribed from the official KLayout LVS deck's own
# `diode_derivations.lvs` / `diode_extraction.lvs` (same installed
# `libs.tech/klayout/lvs/` source as the resistor/MiM provenance above). The
# two 6V (`Dualgate`-marked, medium-voltage) flavours are the ones the PDK's
# own I/O library uses for its ESD clamps -- e.g.
# `gf180mcu_fd_io__asig_5p0`'s CDL is a plain dual-diode clamp built from
# `diode_nd2ps_06v0` and `diode_pd2nw_06v0` -- so they are the two wired
# here:
#
#     ncomp   = comp.and(nplus)          # pcomp = comp.and(pplus)
#     ncomp_mv = ncomp.and(dualgate)     # pcomp_mv = pcomp.and(dualgate)
#     nwell_mv = nwell.overlapping(dualgate)
#     diode_nd2ps_exclude = nwell.join(diode_exclude)
#     diode_pd2nw_exclude = lvpwell.join(diode_exclude)
#
#     diode_nd2ps_06v0_terminal_n = ncomp_mv.not(dnwell).and(diode_mk)
#                                           .not(diode_nd2ps_exclude)
#     diode_pd2nw_06v0_terminal_p = pcomp_mv.not(dnwell).and(diode_mk)
#                                           .not(diode_pd2nw_exclude)
#     diode_pd2nw_06v0_terminal_n = nwell_mv.not(dnwell).interacting(ncomp)
#                                           .not(diode_pd2nw_exclude)
#
#     extract_devices(diode('diode_nd2ps_06v0'),
#                     { 'N' => diode_nd2ps_06v0_terminal_n, 'P' => lvpwell_con })
#     extract_devices(diode('diode_pd2nw_06v0'),
#                     { 'N' => diode_pd2nw_06v0_terminal_n,
#                       'P' => diode_pd2nw_06v0_terminal_p })
#
# Mapping onto this deck's `DiodeDevice` fields:
#
# - `diode_nd2ps_06v0` is an n+ diffusion in the p-substrate. Its cathode is
#   `Comp` scoped to `diode_mk` and narrowed by `requires=(Nplus, Dualgate)`
#   (`ncomp_mv`) / `excludes=(Nwell, DNWELL)` -- `Nwell` is upstream's own
#   `diode_nd2ps_exclude` head term (an n+ diffusion *inside* Nwell is a well
#   tap, not this diode) and `DNWELL` routes the deep-nwell `_dn` variant
#   away rather than mislabelling it as this one. Its **anode is the
#   p-substrate**, for which gf180mcu draws no mask (upstream substitutes the
#   drawn `lvpwell_con`, which only exists inside DNWELL): declared `None`,
#   so `extract.py` uses the device's own `diode_mk` footprint -- minus
#   `Nwell`/`DNWELL`, keeping it genuinely outside every well -- and ties it
#   to this deck's `substrate_net` global, exactly as the collector-less
#   `bipolars` entry above and the NMOS body already do.
# - `diode_pd2nw_06v0` is a p+ diffusion in Nwell. Anode = `Comp` scoped to
#   `diode_mk`, narrowed by `requires=(Pplus, Dualgate)` (`pcomp_mv`) /
#   `excludes=(DNWELL)`; cathode = this deck's own `Nwell` layer, scoped to
#   the same `diode_mk` and excluding `DNWELL`. Scoping the well side to the
#   marker replaces upstream's `nwell_mv...interacting(ncomp)` well-selection
#   idiom: the mark is drawn over the p+ anode, which lies inside the well,
#   so `Nwell & diode_mk` selects exactly the well the marked anode sits in
#   and (because it is a subset of `nwell`) shares that well's net identity
#   through `extract.py`'s well wiring.
#
# Deliberately **not** modelled, each an explicit gap rather than a silent
# one -- geometry these entries do not claim stays unrecognised (no device
# emitted, a loud LVS mismatch) rather than being extracted as the wrong
# device:
#
# - The 3.3V (`_03v3`) flavours. Upstream tells them apart by the *absence*
#   of `Dualgate` plus `.not(v5_xtor)` (112/1, not in this curated layer
#   set); wiring them off "no Dualgate" alone would also claim every
#   `v5_xtor`-marked shape. Both `_dn` (deep-nwell) variants are likewise
#   excluded via `DNWELL` above rather than guessed at.
# - `diode_nw2ps_*`, `diode_pw2dw_*`, `diode_dw2ps_*` and `sc_diode`: all key
#   off `well_diode_mk` (153/51), `lvpwell`, `dnwell` or `schottky_diode`,
#   none of which this curated deck models.
# - `diode_exclude` (poly2, resistor, esd, sab, fusewindow_d, polyfuse,
#   res_mk, drc_bjt, nat, mos_cap_mk, efuse_mk, plfuse, mvsd, mvpsd,
#   ldmos_xtor) is only partially modelled: the three members this deck
#   already carries as *device* markers and that could genuinely collide on
#   the same diffusion -- `Poly2` (a marked MOS gate), `RES_MK` (a marked
#   resistor body) and `DRC_BJT` (a marked bipolar) -- are applied to each
#   diode's diffusion terminal. They are deliberately *not* applied to the
#   `Nwell` cathode: subtracting `Poly2` from a well region would punch
#   holes in the well terminal wherever poly crosses it, for no
#   disambiguation benefit (the marked diffusion side already decides
#   whether the device exists).

# `RuleProvenance` for `EXTRACTION_DECK`'s device-recognition entries (issue
# #904, Epic #711 Phase 3a -- the gf180mcu counterpart of sky130's own
# `_sky130_lvs_provenance`/issue #868). `source_repo`/`commit` are the same
# companion KLayout LVS deck repo/pin this module's own resistor/capacitor/
# diode derivation comments above already cite ("a real fetched PDK install:
# volare enable gf180mcu c6d73a35f524070e85faff4a6a9eef49553ebc2b") --
# distinct from `_GF180MCU_PDK_COMMIT` (`de3240d`), which pins the *DRM*
# repo the DRC-side rules above transcribe from, and from
# `_GF180MCU_KLAYOUT_DRC_DECK_COMMIT` (different repo *and*, since issue
# #3429, a different pin: the DRC-side re-derivation cites `f6eeac7` against
# the *DRC* rule decks, `klayout/tech/drc/...`, not the LVS ones below).
#
# **Re-pinned from `c6d73a3` to `f6eeac7` (issue #3540, 2026-10-16).** The LVS
# rule decks did not survive the #3429 re-pin unchanged (`libs.tech/klayout/lvs/
# rule_decks/` moved to `libs.tech/klayout/tech/lvs/rule_decks/`, poly resistors
# split into `*_sub`/`*_nwell` variants, the `metal1`/`metaltop` terminals became
# `*_con`), so every entry below was re-read against `f6eeac7`.
_GF180MCU_LVS_REPO = "google/globalfoundries-pdk-libs-gf180mcu_fd_pv"
# Re-pinned to open_pdks `f6eeac7` (issue #3540, child B of #3420): the LVS rule
# decks moved to `libs.tech/klayout/tech/lvs/rule_decks/` and were re-read there
# (entry-by-entry table: PR for #3540). Unlike the derivation comments above,
# which describe the `c6d73a3` tree they were originally transcribed from, every
# `source_path`/`rule_id` handed to `_gf180mcu_lvs_provenance` is verified
# against `f6eeac7`.
_GF180MCU_LVS_COMMIT = "f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7"


def _gf180mcu_lvs_provenance(source_path: str, rule_id: str) -> RuleProvenance:
    """Build a :class:`RuleProvenance` for a gf180mcu LVS device-recognition
    entry (issue #904) -- `source_repo`/`commit` shared across every
    `EXTRACTION_DECK` device entry (see the module-level constants above),
    `source_path`/`rule_id` (the official upstream device-class name, e.g.
    `"gf180mcu_fd_pr__ppolyf_u"`) per call."""
    return RuleProvenance(
        source_repo=_GF180MCU_LVS_REPO,
        source_path=source_path,
        rule_id=rule_id,
        commit=_GF180MCU_LVS_COMMIT,
    )


# The official KLayout LVS deck's own per-device-family file, following the
# naming convention of the sibling `res_`/`diode_`/`mimcap_` families, all under
# `libs.tech/klayout/tech/lvs/rule_decks/`. Verified against the real
# `mos_extraction.lvs` at `f6eeac7` (issue #3540): it holds the
# `extract_devices(mos4('nfet_03v3'|'pfet_03v3'|'nfet_06v0'|'pfet_06v0', ...))`
# calls these entries cite (previously inferred, lower-confidence).
_GF180MCU_MOS_LVS_SOURCE_PATH = (
    "libs.tech/klayout/tech/lvs/rule_decks/mos_extraction.lvs"
)


# MOS capacitors (issue #2919): `cap_nmos_*`/`cap_pmos_*`, a poly-over-COMP
# gate whose COMP is covered by `MOS_CAP_MK` (166/5). Transcribed from
# `rule_decks/moscap_derivations.lvs` / `moscap_extraction.lvs` at the pinned
# `_GF180MCU_LVS_COMMIT` (read from a real fetched f6eeac7 install):
#
# - plain `cap_nmos_<v>` = an NMOS-style gate (Nplus COMP) outside Nwell and
#   outside DNWELL; `cap_pmos_<v>` = a PMOS-style gate (Pplus COMP) inside
#   Nwell and outside DNWELL. `_dn` = the same inside DNWELL. (The runset's
#   `CONSIDER_DN_DW_FEATURES` option, default off, folds a `_dn` gate onto the
#   plain model name; this deck always reports the `_dn` name -- the model that
#   geometry really is -- so a `klt lvs` reference using `_dn` matches.)
# - `_b` (`cap_nmos_<v>_b` = `ngate_*_base.and(nwell)`, `cap_pmos_<v>_b` =
#   `pgate_*_base.and(ptap)`) is the "same-polarity well" form: an Nplus gate
#   inside Nwell / a Pplus gate outside it. `_b` is not DNWELL-split.
# - `<v>`: `03v3` outside `Dualgate` (55/0), `06v0` inside it. The two
#   `capacitor(...)` area coefficients are 4.4e-15 F/um^2 (03v3) and 2.3e-15
#   F/um^2 (06v0).
# - `moscap_exclude` marker set: transcribed below as `_MOS_CAP_EXCLUDES`,
#   minus `lvs_source` (100/8): this curated deck deliberately reads nothing
#   else on layer number 100 (its `dummy` convention marker is 100/50, see
#   `test_gf180mcu_dummy_marker_does_not_collide_with_any_read_layer`), and an
#   LVS-source annotation over a MOS capacitor is not modelled.
#
# Deliberate approximations: the runset subtracts the excluded markers
# geometrically from the gate; here an island overlapping any of them at all is
# not a capacitor (the same island-level policy `MOSFlavour` documents). The
# well/body terminal the runset names (`P2`) takes no part in the device -- the
# runset's terminals are the poly (`tA`) and the diffusion (`tB`).
_MOS_CAP_EXCLUDES: tuple[tuple[int, int], ...] = (
    (62, 0),  # resistor
    (24, 0),  # esd
    (49, 0),  # sab
    (96, 1),  # fusewindow_d
    (220, 0),  # polyfuse
    (110, 5),  # res_mk
    (115, 5),  # diode_mk
    (127, 5),  # drc_bjt
    (5, 0),  # nat
    (24, 5),  # esd_mk
    (80, 5),  # efuse_mk
    (125, 5),  # plfuse
    (210, 0),  # mvsd
    (11, 39),  # mvpsd
    (226, 0),  # ldmos_xtor
    (241, 0),  # schottky_diode
)

_NPLUS = (32, 0)
_PPLUS = (31, 0)
_DNWELL = (12, 0)
_DUALGATE = (55, 0)


def _mos_cap(
    name: str, implant: tuple[int, int], in_well: bool, voltage: str, deep: bool | None
) -> MosCapacitorDevice:
    """One `MosCapacitorDevice`; `deep` is `True`/`False` for a DNWELL
    requirement/exclusion, `None` for the `_b` forms that do not split on it."""
    thick = voltage == "06v0"
    other = _PPLUS if implant == _NPLUS else _NPLUS
    requires: tuple[tuple[int, int], ...] = ((_DUALGATE,) if thick else ()) + (
        (_DNWELL,) if deep else ()
    )
    excludes = (
        (other,)
        + (() if thick else (_DUALGATE,))
        + ((_DNWELL,) if deep is False else ())
        + _MOS_CAP_EXCLUDES
    )
    return MosCapacitorDevice(
        name=name,
        marker=(166, 5),  # MOS_CAP_MK
        implant=implant,
        in_well=in_well,
        area_cap_f_um2=2.3e-15 if thick else 4.4e-15,
        requires=requires,
        excludes=excludes,
        provenance=_gf180mcu_lvs_provenance(
            "libs.tech/klayout/tech/lvs/rule_decks/moscap_extraction.lvs", name
        ),
    )


_MOS_CAPACITORS: tuple[MosCapacitorDevice, ...] = tuple(
    _mos_cap(f"cap_{kind}_{volt}{suffix}", implant, in_well, volt, deep)
    for volt in ("03v3", "06v0")
    for kind, implant, in_well in (("nmos", _NPLUS, False), ("pmos", _PPLUS, True))
    for suffix, deep in (("", False), ("_dn", True))
) + tuple(
    _mos_cap(f"cap_{kind}_{volt}_b", implant, in_well, volt, None)
    for volt in ("03v3", "06v0")
    for kind, implant, in_well in (("nmos", _NPLUS, True), ("pmos", _PPLUS, False))
)


EXTRACTION_DECK = ExtractionDeck(
    active=(22, 0),  # Comp
    poly=(30, 0),  # Poly2
    nwell=(21, 0),  # Nwell
    contact=(33, 0),  # Contact
    # No distinct drawn tap layer (`tap` stays `None`) -- but `tap_nplus`/
    # `tap_pplus` let `extract.py` derive an equivalent well-/substrate-tie
    # region from these same implant layers (issue #1084, see the module
    # docstring note above this deck's definition for the full derivation).
    tap_nplus=(32, 0),  # Nplus -- well-tie implant (n+ diffusion inside Nwell)
    tap_pplus=(31, 0),  # Pplus -- substrate-tie implant (p+ diffusion outside Nwell)
    # DNWELL (issue #1128): NMOS active/substrate-tie geometry inside a
    # connected DNWELL island resolves to that island's own synthesized
    # substrate identity instead of the single deck-wide `vsubs` global --
    # see `ExtractionDeck.substrate_isolation`'s docstring for the full
    # derivation. Geometry outside every DNWELL island is unaffected.
    substrate_isolation=(12, 0),  # DNWELL
    poly_label=(30, 10),  # Poly2 pin/label purpose -- names a bare-poly gate (#210)
    # Dummy-device marker (issue #2599, the gf180mcu counterpart of sky130's
    # own issue #491 declaration -- see `decks/sky130.py`'s `dummy=(83, 20)`
    # comment and `ExtractionDeck.dummy`'s docstring in `decks/extraction.py`
    # for the extractor-side machinery this field drives, which shipped in
    # #295/#462 and needs no change here). Without it, `klt gen mos_array`/
    # `res_array`/`bjt_array`'s own `"dummy": N` param drew edge-fill devices
    # on this family that `klt lvs` could then only report as
    # `device.unmatched`, with knock-on `topology` conflicts on whichever
    # supply rail the dummy units tie to.
    #
    # gf180mcu has no *native* per-device dummy-recognition mark either. The
    # only dummy-adjacent purposes in its own layer table are (a) the
    # per-conductor density-fill purposes `<Layer>_Dummy` (datatype 4 on
    # `COMP`/`Poly2`/`Metal1`..`MetalTop`), which is where generated *fill
    # shapes themselves* are drawn rather than a marker laid over a real
    # device, and (b) the fill-*exclusion* region markers `NDMY` (111/5) /
    # `PMNDMY` (152/5), whose DRM section 10.8 `DE.*` rules ("design rules
    # for dummy exclude layers") make them a density/DRC concept, not a
    # device-recognition mark the way `DRC_BJT` (127/5) or `RES_MK` (110/5)
    # are. That is the identical "no native mark, curated substitute"
    # situation sky130's own `(83, 20)` comment documents.
    #
    # `(100, 50)` therefore reuses gf180mcu's own *LVS-only annotation*
    # layer number -- 100, the `LVS_*` family (`LVS_RF` 100/5, `LVS_Drain`
    # 100/7, `LVS_Source` 100/8): layers that exist purely to annotate
    # extraction/LVS and that no per-layer design rule keys off, the
    # structural analogue of the `marker.*` layer number sky130's `(83, 20)`
    # reuses. Datatype 50 is assigned no purpose at all. Verified against
    # the same real fetched install this deck's MiM/resistor/diode
    # provenance already cites (`volare enable gf180mcu
    # c6d73a35f524070e85faff4a6a9eef49553ebc2b`): across
    # `libs.tech/klayout/tech/gf180mcu.lyp`,
    # `libs.tech/klayout/drc/rule_decks/layers_def.drc` and
    # `libs.tech/klayout/lvs/rule_decks/layers_definitions.lvs`, layer 100
    # carries exactly three assigned datatypes -- 5, 7 and 8 -- and all four
    # shipped variants (`gf180mcuA`/`B`/`C`/`D`) agree on that set. Datatype
    # 50 leaves generous headroom above upstream's low, contiguous 5..8
    # block, so an upstream purpose extension does not immediately collide.
    # This deck reserves it as an extraction-only, deck-local marker: no
    # official gf180mcu purpose string, and no rule in this module's own
    # `DECK` references layer 100 at all, so `klt drc --deck gf180mcu` never
    # checks it.
    #
    # Verified non-colliding against every layer this `EXTRACTION_DECK`
    # otherwise reads (`ExtractionDeck.connectivity_layers` -- the
    # `active`/`poly`/`nwell`/`contact` core, the `metals`/`vias`/
    # `metal_labels` stacks, `tap_nplus`/`tap_pplus`/`substrate_isolation`/
    # `poly_label`, and every `bipolars`/`capacitors`/`resistors`/`diodes`/
    # `mos_flavours` recognition layer): none of them is on layer 100 at
    # all. `tests/test_extract.py` asserts that non-collision directly.
    dummy=(100, 50),
    # Full Metal1-Metal5 routing stack (#220). Before this, `metals` stopped
    # at Metal1, so anything drawn above it was invisible to the connectivity
    # graph and a normally-routed block extracted as a pile of disconnected
    # nets (LVS then reported a mismatch on essentially every net). The engine
    # loop in `extract.py`'s `_extract_netlist` is already generic over an
    # arbitrary-depth stack (`vias[i]` connects `metals[i]` to `metals[i+1]`);
    # this is deck data, not new machinery.
    metals=(
        (34, 0),  # Metal1
        (36, 0),  # Metal2
        (42, 0),  # Metal3
        (46, 0),  # Metal4
        (81, 0),  # Metal5
    ),
    # `vias[i]` connects `metals[i]` to `metals[i + 1]`, so this tuple has
    # `len(metals) - 1` entries. Layer numbers from `main.drc`'s via-layer
    # derivations (see the module docstring's layer table): Via1 35/0,
    # Via2 38/0, Via3 40/0, Via4 41/0.
    vias=(
        (35, 0),  # Via1 (Metal1 <-> Metal2)
        (38, 0),  # Via2 (Metal2 <-> Metal3)
        (40, 0),  # Via3 (Metal3 <-> Metal4)
        (41, 0),  # Via4 (Metal4 <-> Metal5)
    ),
    # Each metal's pin/label purpose is datatype 10 (gf180mcu's convention,
    # same as Metal1's already-shipped 34/10). Index-aligned with `metals`.
    metal_labels=(
        (34, 10),  # Metal1 pin/label purpose
        (36, 10),  # Metal2 pin/label purpose
        (42, 10),  # Metal3 pin/label purpose
        (46, 10),  # Metal4 pin/label purpose
        (81, 10),  # Metal5 pin/label purpose
    ),
    bipolars=(
        BipolarDevice(
            base=(21, 0),  # Nwell
            emitter=(22, 0),  # Comp
            marker=(127, 5),  # DRC_BJT
            # The base (Nwell) is contacted by an n+ tie ring drawn on the
            # same Comp diffusion layer as the p+ emitter, inside the same
            # DRC_BJT mark -- without narrowing, that ring extracts as a
            # second, artefact `bjt` sharing the base net (issue #302).
            # Excluding Nplus drops it: a real p+ emitter carries Pplus, not
            # Nplus, so this leaves the emitter untouched while removing the
            # n+ base tie. Mirrors the resistor deck's own `(32, 0)` exclude.
            emitter_excludes=(
                (32, 0),
            ),  # Nplus -- an n+ base tie is not the p+ emitter
            class_name="bjt",
            # Unlike sky130's `pnp_05v5`, this deck has no positively-
            # identified official LVS device-class name for its generic
            # "bjt" recognition (see this section's own docstring note above:
            # "unlike sky130's pnp_05v5 this repo has no positively-
            # identified single device-cell name to attribute a specific
            # polarity to") -- so this cites the DRM rule that defines the
            # `DRC_BJT` marker geometry this entry recognises on
            # (`bjt.separation.comp.1` above cites the same rule for the DRC
            # side) rather than fabricating a device-class name that does
            # not exist upstream.
            provenance=_gf180mcu_provenance("10.7 DRC_BJT Mark Layer", "BJT.3"),
        ),
    ),
    capacitors=(
        CapacitorDevice(
            name="cap_mim_2f0_m4m5_noshield",  # official LVS deck's own device name
            top_plate=(75, 0),  # FuseTop
            top_plate_requires=(
                (117, 5),  # CAP_MK
                (117, 10),  # MIM_L_MK
            ),
            top_plate_excludes=(
                (80, 5),  # efuse_mk
                (125, 5),  # plfuse
            ),
            bottom_plate=(46, 0),  # Metal4 (topmin1_metal for the 5LM stack)
            # DRM 10.4.2 footnote 1 / MIMTM.1's "virtual bottom plate":
            bottom_plate_oversize_um=1.06,
            # sm141064.ngspice's cap_mim_2f0fF: c_cox=1.99e-3 F/m^2 (area) /
            # c_capsw=2.383e-10 F/m (perimeter/fringe), see the module
            # docstring's provenance note (issue #512) above. This is the
            # PDK's own default density absent an override (confirmed by
            # open_pdks' `gf180mcu.json` variant string, "...2fF MiM..." --
            # see the module docstring) and therefore also `flavours`' own
            # `"cap_mim_2f0_m4m5_noshield"` entry below, used whenever a
            # caller passes no `mim_cap` `deck_options` override.
            area_cap_f_um2=1.99e-15,
            perim_cap_f_um=2.383e-16,
            # Top-plate connectivity (issue #314): `main.drc`'s own
            # `top_via = via4` / `top_metal = metal5` confirms Via4 lands
            # directly on FuseTop and connects up to Metal5 for this 5LM
            # stack -- see the module docstring's "10.4 MIM Capacitor" note.
            top_plate_via=(41, 0),  # Via4
            top_plate_via_metal=(81, 0),  # Metal5
            # Caller-selectable MiM density (issue #1151), the capacitor
            # sibling of `ppolyf_u_1k`'s own `flavour_option="poly_res"`
            # below -- see the module docstring's note above this entry for
            # the full derivation of the three `flavours` coefficients and
            # why one drawn geometry has three PDK-offered density
            # interpretations here. `klt extract --deck-option
            # mim_cap=cap_mim_1f0_m4m5_noshield` (or `...1f5...`) selects a
            # sibling density explicitly; omitting `--deck-option` keeps
            # today's `cap_mim_2f0_m4m5_noshield` default, byte-for-byte.
            # `f6eeac7` (issue #3540): `mimcap_extraction.lvs` no longer branches
            # on `MIM_OPTION`/`METAL_LEVEL` and names its devices `cap_mim_1f0fF`/
            # `cap_mim_1f5fF`/`cap_mim_2f0fF` (so `provenance.rule_id` below is
            # `cap_mim_2f0fF`); `MIM_OPTION` now only selects the virtual bottom
            # plate in `mimcap_derivations.lvs` (default `'B'`, `topmin1_metal`,
            # which is what this deck models for its Metal5 top). The
            # `*_m4m5_noshield` names here are this deck's own extracted-netlist
            # device names (the previous deck's names, kept for netlist
            # stability); only the citation moved.
            flavour_option="mim_cap",
            flavours=(
                CapacitorFlavour(
                    value="cap_mim_1f0_m4m5_noshield",
                    name="cap_mim_1f0_m4m5_noshield",
                    area_cap_f_um2=9.87e-16,
                    perim_cap_f_um=3.3e-16,
                ),
                CapacitorFlavour(
                    value="cap_mim_1f5_m4m5_noshield",
                    name="cap_mim_1f5_m4m5_noshield",
                    area_cap_f_um2=1.47e-15,
                    perim_cap_f_um=3.79e-16,
                ),
                CapacitorFlavour(
                    value="cap_mim_2f0_m4m5_noshield",
                    name="cap_mim_2f0_m4m5_noshield",
                    area_cap_f_um2=1.99e-15,
                    perim_cap_f_um=2.383e-16,
                ),
            ),
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/mimcap_extraction.lvs",
                "cap_mim_2f0fF",
            ),
        ),
    ),
    resistors=(
        # `f6eeac7` (issue #3540): `res_derivations.lvs` splits every poly
        # resistor into `*_sub` / `*_nwell` body variants (`W` => `sub` vs.
        # `nwell_con`; `npolyf_u/s`, `ppolyf_u/s`, ...) under the same device
        # name and value. This deck keeps ONE entry per device with
        # `bulk_to_substrate=True`: an acknowledged approximation -- the
        # resistance value and device name are identical either way, only the
        # bulk terminal of a poly resistor drawn over Nwell differs.
        ResistorDevice(
            name="ppolyf_u",  # gf180mcu_fd_pr__ppolyf_u
            body=(30, 0),  # Poly2
            marker=(110, 5),  # RES_MK
            sheet_rho_ohm_sq=350.0,  # res_extraction.lvs / magic tech, see above
            requires=(
                (31, 0),  # Pplus -- p+ doped poly (vs. Nplus's npolyf_*)
                (49, 0),  # SAB   -- unsalicided (vs. ppolyf_s, 7.3 ohm/sq)
            ),
            excludes=(
                (22, 0),  # Comp   -- a marked gate is a transistor
                (32, 0),  # Nplus  -> npolyf_* devices, not this one
                (62, 0),  # Resistor -> ppolyf_u_{1k,2k,3k} high-sheet-rho poly
                (12, 0),  # DNWELL -> the `_dw` (deep-nwell) device variants
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__ppolyf_u",
            ),
        ),
        # High-sheet-rho flavour (issue #299, made caller-selectable by
        # #595), see the module docstring's `ppolyf_u_1k` note above for the
        # full derivation. `sheet_rho_ohm_sq`/`name` below are the PDK's own
        # '1k' default (used whenever a caller passes no `poly_res`
        # `deck_options` override); `flavours` additionally declares the
        # `2k`/`3k` siblings selectable via `flavour_option="poly_res"`
        # (`klt extract --deck-option poly_res=2k`) -- see
        # `ResistorDevice.flavour_option`'s own docstring for why this is a
        # deck-option selection rather than a `requires`/`excludes` split:
        # all three share *identical* drawn geometry, so there is no layer a
        # deck could key off to tell them apart on its own.
        ResistorDevice(
            name="ppolyf_u_1k",  # gf180mcu_fd_pr__ppolyf_u_1k (POLY_RES='1k' default)
            body=(30, 0),  # Poly2
            marker=(110, 5),  # RES_MK
            sheet_rho_ohm_sq=1000.0,  # res_extraction.lvs 'when 1k', see above
            requires=(
                (49, 0),  # SAB      -- unsalicided, same as the base ppolyf_u
                (62, 0),  # Resistor -- the high-sheet-rho marker itself
            ),
            excludes=(
                (22, 0),  # Comp   -- a marked gate is a transistor
                (32, 0),  # Nplus  -> npolyf_* devices, not this one
                (12, 0),  # DNWELL -> the `_dw` (deep-nwell) device variants
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
            flavour_option="poly_res",
            flavours=(
                ResistorFlavour(
                    value="1k", name="ppolyf_u_1k", sheet_rho_ohm_sq=1000.0
                ),
                ResistorFlavour(
                    value="2k", name="ppolyf_u_2k", sheet_rho_ohm_sq=2000.0
                ),
                ResistorFlavour(
                    value="3k", name="ppolyf_u_3k", sheet_rho_ohm_sq=3000.0
                ),
            ),
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__ppolyf_u_1k",
            ),
        ),
        # Drawn metal resistors `rm1`/`rm2`/`rm3` (issue #1640, mirroring
        # sky130's `res_generic_m1`..`res_generic_m5`, issue #1621) -- see the
        # module docstring's derivation note above this section for the full
        # `res_derivations.lvs`/`res_extraction.lvs` transcription. Each is a
        # plain 2-terminal `resistor(...)` (no bulk terminal), so
        # `bulk_to_substrate` stays the default `False`.
        ResistorDevice(
            name="rm1",  # gf180mcu_fd_pr__rm1 (subckt "rm1")
            body=(34, 0),  # Metal1
            marker=(110, 11),  # metal1_res
            sheet_rho_ohm_sq=0.09,  # res_extraction.lvs: resistor('rm1', 0.09, ...)
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__rm1",
            ),
        ),
        ResistorDevice(
            name="rm2",  # gf180mcu_fd_pr__rm2 (subckt "rm2")
            body=(36, 0),  # Metal2
            marker=(110, 12),  # metal2_res
            sheet_rho_ohm_sq=0.09,  # res_extraction.lvs: resistor('rm2', 0.09, ...)
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__rm2",
            ),
        ),
        ResistorDevice(
            name="rm3",  # gf180mcu_fd_pr__rm3 (subckt "rm3")
            body=(42, 0),  # Metal3
            marker=(110, 13),  # metal3_res
            sheet_rho_ohm_sq=0.09,  # res_extraction.lvs: resistor('rm3', 0.09, ...)
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__rm3",
            ),
        ),
        # Top-metal thickness flavour set `tm6k`/`tm9k`/`tm11k`/`tm30k`
        # (issue #1640, mirroring `ppolyf_u_1k`'s own `flavour_option=
        # "poly_res"` above, issue #595) -- see the module docstring's
        # derivation note above this section for why this is one
        # `ResistorDevice` entry with a caller-selectable flavour, not four
        # always-on entries. `body`/`marker` are this curated deck's own
        # already-established "top_metal = Metal5" convention (the module
        # docstring note above explains the 6LM/MetalTop approximation this
        # makes). Default flavour is `tm9k` (0.04 ohm/sq), matching
        # `gf180mcu.lvs`'s own `METAL_TOP = $metal_top || '9K'` default.
        ResistorDevice(
            name="tm9k",  # gf180mcu_fd_pr__tm9k (subckt "tm9k", METAL_TOP='9K' default)
            body=(81, 0),  # Metal5 (this deck's own top_metal, see note above)
            marker=(110, 15),  # metal5_res (this deck's own top_metal_res)
            sheet_rho_ohm_sq=0.04,  # res_extraction.lvs 'when 9K', see above
            flavour_option="metal_top",
            flavours=(
                ResistorFlavour(value="6K", name="tm6k", sheet_rho_ohm_sq=0.06),
                ResistorFlavour(value="9K", name="tm9k", sheet_rho_ohm_sq=0.04),
                ResistorFlavour(value="11K", name="tm11k", sheet_rho_ohm_sq=0.04),
                ResistorFlavour(value="30K", name="tm30k", sheet_rho_ohm_sq=0.0095),
            ),
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/res_extraction.lvs",
                "gf180mcu_fd_pr__tm9k",
            ),
        ),
    ),
    # Junction diodes (issue #542) -- see the provenance note above for the
    # official `diode_derivations.lvs`/`diode_extraction.lvs` derivations
    # these transcribe and for what is deliberately left unmodelled.
    diodes=(
        DiodeDevice(
            name="diode_nd2ps_06v0",  # gf180mcu_fd_pr__diode_nd2ps_06v0
            marker=(115, 5),  # diode_mk
            # Anode = the p-substrate; gf180mcu draws no mask for it, so the
            # device's own mark footprint (kept outside every well) stands in
            # and is tied to `substrate_net`.
            anode=None,
            anode_excludes=(
                (21, 0),  # Nwell  -- upstream's `diode_nd2ps_exclude` head term
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
            ),
            cathode=(22, 0),  # Comp (n+ diffusion)
            cathode_requires=(
                (32, 0),  # Nplus    -- n+ doped (`ncomp`)
                (55, 0),  # Dualgate -- the 6V/MV flavour (`ncomp_mv`)
            ),
            cathode_excludes=(
                (21, 0),  # Nwell  -- n+ inside Nwell is a well tap, not this
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
                (30, 0),  # Poly2   \
                (110, 5),  # RES_MK  | modelled subset of `diode_exclude`
                (127, 5),  # DRC_BJT /
            ),
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/diode_extraction.lvs",
                "gf180mcu_fd_pr__diode_nd2ps_06v0",
            ),
        ),
        DiodeDevice(
            name="diode_pd2nw_06v0",  # gf180mcu_fd_pr__diode_pd2nw_06v0
            marker=(115, 5),  # diode_mk
            anode=(22, 0),  # Comp (p+ diffusion)
            anode_requires=(
                (31, 0),  # Pplus    -- p+ doped (`pcomp`)
                (55, 0),  # Dualgate -- the 6V/MV flavour (`pcomp_mv`)
            ),
            anode_excludes=(
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
                (30, 0),  # Poly2   \
                (110, 5),  # RES_MK  | modelled subset of `diode_exclude`
                (127, 5),  # DRC_BJT /
            ),
            cathode=(21, 0),  # Nwell -- shares this deck's own well node
            cathode_excludes=(
                (12, 0),  # DNWELL -> the `_dn` deep-nwell variant
            ),
            provenance=_gf180mcu_lvs_provenance(
                "libs.tech/klayout/tech/lvs/rule_decks/diode_extraction.lvs",
                "gf180mcu_fd_pr__diode_pd2nw_06v0",
            ),
        ),
    ),
    # Default MOS recognition binds the 3.3V models (issue #904) --
    # `nfet_03v3`/`pfet_03v3` are the official upstream device-class names
    # cited here for every transistor drawn *outside* `Dualgate`.
    nfet_provenance=_gf180mcu_lvs_provenance(
        _GF180MCU_MOS_LVS_SOURCE_PATH, "gf180mcu_fd_pr__nfet_03v3"
    ),
    pfet_provenance=_gf180mcu_lvs_provenance(
        _GF180MCU_MOS_LVS_SOURCE_PATH, "gf180mcu_fd_pr__pfet_03v3"
    ),
    # Per-flavour MOS marker (issue #1111, option 2 of #552): a transistor
    # drawn entirely inside `Dualgate` (55/0) is gf180mcu's 5V/6V thick-oxide
    # flavour, whose real upstream device-class names are `nfet_06v0`/
    # `pfet_06v0` (same `mos_extraction.lvs` source as the default pair
    # above -- see `_GF180MCU_MOS_LVS_SOURCE_PATH`'s verification note).
    # `MOSFlavour.flavour="06v0"` is the key `pdk_models.py`'s
    # `_MOS_MODEL_FLAVOURS[("gf180mcu", "gf180mcu")]` table binds to those
    # two real subcircuit names under `--pdk` -- see that module's docstring
    # for the verified-provenance citation (`nfet_06v0` is independently
    # confirmed in the same real fetched install this deck's other MOS/
    # diode/resistor citations use, e.g. `gf180mcu_fd_io.spice`'s own
    # `X5 n56 IE VSS VSS nfet_06v0 ...` instantiation).
    mos_flavours=(
        MOSFlavour(
            marker=(55, 0),  # Dualgate
            flavour="06v0",
            description="gf180mcu 5V/6V thick-oxide domain (Dualgate 55/0)",
        ),
    ),
    # Issue #2919: MOS capacitors (`cap_nmos_*`/`cap_pmos_*`) are marked by
    # `MOS_CAP_MK` (166/5); see `_MOS_CAPACITORS` above. Marker components the
    # recogniser could not classify (no gate, mixed implant, an excluded
    # marker) stay reported here, so they cannot vanish from the netlist with
    # a clean `extracted` status.
    mos_capacitors=_MOS_CAPACITORS,
    unsupported_device_markers=(
        UnsupportedDeviceMarker(
            marker=(166, 5),  # MOS_CAP_MK
            kind="mos_capacitor",
            description="MOS capacitor (MOS_CAP_MK 166/5)",
        ),
    ),
)


# MIM option A (issue #2920): bottom plate Metal2, top plate FuseTop under
# Metal3, joined to Metal3 by Via2 *inside* FuseTop. Selected with
# `--deck-option mim_option=A`; the deck's own `capacitors` above are option B
# (the default, byte-for-byte unchanged). Recognition mirrors the PDK's
# `mimcap_derivations.lvs` (`mim_virtual = fusetop.sized(1.06um) &
# metal2.interacting(fusetop) - (efuse_mk | plfuse)`, `fuse_cap = fusetop
# interacting cap_mk, mim_l_mk, - (efuse_mk | plfuse)`) and the device names
# and densities come from `mimcap_extraction.lvs`'s `MIM_OPTION 'A'` branch
# (`cap_mim_{1f0,1f5,2f0}_m2m3_noshield`); the area/perimeter coefficients are
# the same `sm141064.ngspice` cards as option B's. The top-plate Via2 is cut
# from the generic Via2 layer where it overlaps Metal2 by the existing
# `top_plate_via` machinery, the equivalent of the runset's `via2_n_cap`.
_MIM_OPTION_A_CAPACITORS = (
    CapacitorDevice(
        name="cap_mim_2f0_m2m3_noshield",
        top_plate=(75, 0),  # FuseTop
        top_plate_requires=(
            (117, 5),  # CAP_MK
            (117, 10),  # MIM_L_MK
        ),
        top_plate_excludes=(
            (80, 5),  # efuse_mk
            (125, 5),  # plfuse
        ),
        bottom_plate=(36, 0),  # Metal2
        bottom_plate_oversize_um=1.06,
        area_cap_f_um2=1.99e-15,
        perim_cap_f_um=2.383e-16,
        top_plate_via=(38, 0),  # Via2
        top_plate_via_metal=(42, 0),  # Metal3
        flavour_option="mim_cap",
        flavours=(
            CapacitorFlavour(
                value="cap_mim_1f0_m2m3_noshield",
                name="cap_mim_1f0_m2m3_noshield",
                area_cap_f_um2=9.87e-16,
                perim_cap_f_um=3.3e-16,
            ),
            CapacitorFlavour(
                value="cap_mim_1f5_m2m3_noshield",
                name="cap_mim_1f5_m2m3_noshield",
                area_cap_f_um2=1.47e-15,
                perim_cap_f_um=3.79e-16,
            ),
            CapacitorFlavour(
                value="cap_mim_2f0_m2m3_noshield",
                name="cap_mim_2f0_m2m3_noshield",
                area_cap_f_um2=1.99e-15,
                perim_cap_f_um=2.383e-16,
            ),
        ),
        provenance=_gf180mcu_lvs_provenance(
            "libs.tech/klayout/lvs/rule_decks/mimcap_extraction.lvs",
            "cap_mim_2f0_m2m3_noshield",
        ),
    ),
)

EXTRACTION_DECK = dataclasses.replace(
    EXTRACTION_DECK,
    capacitor_variants=(
        CapacitorVariant("mim_option", "B", EXTRACTION_DECK.capacitors),
        CapacitorVariant("mim_option", "A", _MIM_OPTION_A_CAPACITORS),
    ),
)

# --------------------------------------------------------------------------- #
# `klt extract --parasitics` first-order lumped-RC coefficients
# --------------------------------------------------------------------------- #
#
# Sourced, citable values transcribed from the *public* gf180mcu magic
# technology file `gf180mcu/magic/gf180mcu.tech` in fossi-foundation/open-pdks
# (GPLv3 -- the same upstream the DRC decks are transcribed from), nominal
# corner (the `variants ()` block). Sheet resistances come from that file's
# `resist <layer> <milliohms per square>` entries; area/fringe capacitances
# from its `defaultareacap` (aF/um^2) and `defaultperimeter` (aF/um) entries.
# The .tech header credits GlobalFoundries' PDS_035_03; nothing NDA'd. Values
# remain order-of-magnitude and uncalibrated-to-silicon: calibrating
# parasitic-extraction accuracy against silicon is an explicit non-goal of
# this first cut (issue #216 "Non-goals"). `metals` is index-aligned with
# EXTRACTION_DECK's `metals` stack: index 0 is Metal1. Values expressed as
# (sheet ohms/square, area cap fF/um^2, fringe cap fF/um).
PARASITICS = ParasiticsDeck(
    # No diffusion role (issue #226): the M cards' AS/AD/PS/PD already feed the
    # device model's junction capacitance, so an area/perimeter cap term on the
    # Comp source/drain diffusion here would double-count it. gf180mcu.tech
    # comments its own active-layer cap out for the same reason.
    diffusion=None,
    # Poly2 gate/interconnect. sheet from `resist (allpolynonres)/active 7300`
    # (7300 milliohm/sq = 7.3 ohm/sq); area from `defaultareacap` poly2
    # (110.67 aF/um^2).
    poly=LayerRC(sheet_res_ohm_sq=7.3, cap_area_ff_um2=0.11067, cap_perim_ff_um=0.05),
    metals=(
        # Metal1. perim from `defaultperimeter` Metal1 (39.431 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.03,
            cap_perim_ff_um=0.039431,
        ),
        # Metal2 (issue #547). sheet from `resist (allm2)/metal2 90`
        # (unconditional -- M2 is never the top/thick metal in any gf180mcu
        # stack option); area/perim from `defaultareacap`/`defaultperimeter`
        # allm2 metal2 (15.016 aF/um^2, 33.298 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.015016,
            cap_perim_ff_um=0.033298,
        ),
        # Metal3 (issue #547). sheet from `resist (allm3)/metal3 90`, the
        # `#ifdef METALS4 || METALS5 || METALS6` row -- this deck's 5-metal
        # stack (`EXTRACTION_DECK.metals` has 5 entries) means M3 is not the
        # top metal, so it takes the fixed 90 milliohm/sq row rather than the
        # THICKMET-dependent one reserved for a 3-metal-only stack. area/perim
        # from `defaultareacap`/`defaultperimeter` allm3 metal3 (10.094
        # aF/um^2, 30.021 aF/um) -- unconditional; thickness options only vary
        # M3's sheet resistance, never its cap coefficients.
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.010094,
            cap_perim_ff_um=0.030021,
        ),
        # Metal4 (issue #547). sheet from `resist (allm4)/metal4 90`, the
        # `#ifdef METALS5 || METALS6` row (same reasoning as Metal3 -- not the
        # top metal in a 5-level stack). area/perim from
        # `defaultareacap`/`defaultperimeter` allm4 metal4 (7.602 aF/um^2,
        # 28.153 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.09,
            sheet_res_high_ohm_sq=0.104,
            sheet_res_low_ohm_sq=0.076,
            cap_area_ff_um2=0.007602,
            cap_perim_ff_um=0.028153,
        ),
        # Metal5 (issue #547, revised by #572 and again by #3540), the top
        # metal of this 5-level stack. sheet from the resolved, pre-built
        # `gf180mcuD.tech` for the pinned PDK commit
        # f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 (nominal `variants ()`
        # corner block, line 3347): `resist (allm5)/metal5    40` (40
        # milliohm/sq = 0.04 ohm/sq). At the earlier pin
        # c6d73a35f524070e85faff4a6a9eef49553ebc2b that same resolved row read
        # 60 (the other two corner blocks 70 -> 49 and 50 -> 31), which #572
        # adopted over #571's 40 taken from the unresolved generic
        # `gf180mcu.tech`; f6eeac7's resolved file now carries 40, matching
        # the value #572 rejected, so 0.04 here is a re-pin delta rather than
        # a return to #571's derivation. No other `resist`/`defaultareacap`/
        # `defaultperimeter`/`defaultoverlap allm[2-5]` row changed between
        # the two pins. area/perim from `defaultareacap`/`defaultperimeter`
        # allm5 metal5 (5.798 aF/um^2, 30.386 aF/um) -- unconditional across
        # every THICKMET option.
        LayerRC(
            sheet_res_ohm_sq=0.04,
            sheet_res_high_ohm_sq=0.049,
            sheet_res_low_ohm_sq=0.031,
            cap_area_ff_um2=0.005798,
            cap_perim_ff_um=0.030386,
        ),
    ),
    # `defaultoverlap` vertical plate-coupling coefficients (issue #760,
    # Extract Stage 2a): entry `i` is the coefficient between `metals[i]`
    # (lower plate) and `metals[i+1]` (upper plate), so a fully-populated
    # table has `len(metals) - 1` = 4 entries for this 5-level stack.
    #
    # Transcribed from the resolved, pre-built `gf180mcuD.tech` for the
    # pinned PDK commit f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 (values
    # re-checked unchanged from c6d73a3, issue #3540), nominal
    # `variants ()` corner block -- the same source and corner Metal5's
    # sheet resistance above is cited from (issue #572), chosen over the
    # generic `gf180mcu.tech` because only the resolved file has the
    # `#ifdef METALS5` Metal4/Metal5 rows already selected for this deck's
    # actual 5-level stack. Only `allmN metalN allmM metalM` rows are used
    # (metal-over-metal); the same blocks' `... nwell well` / `...
    # allpolynonres active` rows describe metal-over-substrate/poly overlap,
    # which the existing `cap_area_ff_um2` ground term already covers.
    #
    # Magic states these in aF/um^2; divided by 1000 to fF/um^2, matching
    # `cap_area_ff_um2`'s convention above.
    metal_overlaps=(
        # Metal1 (idx0) <-> Metal2 (idx1): `defaultoverlap allm2 metal2
        # allm1 metal1 59.027`.
        0.059027,
        # Metal2 (idx1) <-> Metal3 (idx2): `defaultoverlap allm3 metal3
        # allm2 metal2 59.027`.
        0.059027,
        # Metal3 (idx2) <-> Metal4 (idx3): `defaultoverlap allm4 metal4
        # allm3 metal3 59.027`.
        0.059027,
        # Metal4 (idx3) <-> Metal5 (idx4): `defaultoverlap allm5 metal5
        # allm4 metal4 39.351`.
        0.039351,
    ),
    # Per-cut via resistance and the interconnect-resistance corners (issue
    # #3053), from the same resolved `gf180mcuD.tech` / pinned PDK commit
    # f6eeac7dad085ffcc829ccfd721f7b4ce39edcf7 as Metal5 above. Magic states
    # `contact` rows in milliohm per cut; divided by 1000. Nominal: `variants
    # ()` block (L3355-3358); high-R: `variants (hrhc),(hrlc)` (L3404-3407,
    # 15000 = 15 ohm, 3.3x nominal); low-R: `variants (lrhc),(lrlc)`
    # (L3453-3456, 0 -- an ideal short, stored as published, not clamped; see
    # `CutRC`). The high/low *metal* sheet resistances are on `metals` above
    # (L3390-3396 / L3439-3445). `lower_metal_index` is the Metal level the
    # cut lands on: m2c = Metal1->Metal2, m3c = Metal2->Metal3, via3 =
    # Metal3->Metal4, via4 = Metal4->Metal5. `mimcc` (MiM cap contact, also
    # 4.5 ohm) and the diffusion/poly contacts (`ndc` ... `pc`) are not
    # curated: they are not between two metals of this stack.
    cuts=(
        CutRC("m2c", 0, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("m3c", 1, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("via3", 2, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
        CutRC("via4", 3, nominal_ohm=4.5, high_ohm=15.0, low_ohm=0.0),
    ),
)
