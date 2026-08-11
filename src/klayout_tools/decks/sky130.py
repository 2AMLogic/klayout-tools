"""sky130 DRC deck: a curated subset of the official rule set.

Layer/datatype pairs and rule thresholds below are transcribed from the
official, community-maintained sky130 KLayout DRC deck and layer map,
fetched directly from the canonical source repository for this issue:

- ``sky130/klayout/sky130.lydrc`` (the DRC-DSL rule script)
- ``sky130/klayout/sky130.lyt`` (the layer map / layer-properties technology
  file, which defines the ``name.purpose : layer/datatype`` pairs used below,
  e.g. ``poly.drawing : 66/20``)

both from https://github.com/fossi-foundation/open-pdks (the current home of
the repo historically known as ``open_pdks``/``RTimothyEdwards/open_pdks``;
distributed under GPLv3). Each rule's docstring cites the exact source line
(rule id and comment) it was transcribed from, so values can be re-verified
against a fresh checkout of that file at any time.

This is *not* a full transcription of the official deck (hundreds of rules
across dozens of layers) — see the "Scope guard" section of issue #9: a
curated starter subset spanning width/space/enclosure checks across
poly/diff/li1/met1/licon1/mcon, wide enough to prove the deck-adapter shape
(:class:`~klayout_tools.decks.DrcRule`) and produce a non-trivial worked
example. Coverage is expected to grow incrementally in follow-on issues.

Threshold-fidelity exception (issue #551): every rule below transcribes its
source rule's threshold *value* unmodified, with exactly one deliberate
exception -- ``li1.enclosing.licon1.1``, which is transcribed at its source
rule's (``li.5``) unconditional zero-margin floor rather than at its
published 0.08um. ``li.5`` requires that margin only on *two adjacent edges*
of each cut, a per-edge-pair conditional this vocabulary cannot express, and
real sky130 layout takes advantage of it (a minimum-width ``li`` strap sits
flush with the cut on the other two edges), so an unconditional 0.08um check
would flag correct-by-construction geometry throughout every standard cell.
See that rule's own docstring for the measurement and the reasoning.

Negative finding — bipolar (BJT) device-mark rule (issue #183): sky130's
layer map (``sky130.lyt``) defines a ``pnp.drawing`` mark layer (82/44,
alongside an unused ``npn.drawing`` at 82/20) that the vertical-PNP bipolar
device library cells (e.g. ``sky130_fd_pr__pnp_05v5``) draw over themselves
for device recognition — the sky130 counterpart of gf180mcu's ``DRC_BJT``
mark layer (127/5, see ``gf180mcu.py``). But the *only* rule in
``sky130.lydrc`` that references it is ``dnwell.4``::

    dnwell.and(pnp).output("dnwell.4", "dnwell.4 : dnwell must not overlap pnp")

— a compatibility exclusion between two unrelated process layers (``dnwell``,
the deep-nwell triple-well isolation layer at 64/18, is a *different* layer
from ``nwell.drawing`` at 64/20, and is not drawn by this curated deck or by
``bjt_array``'s sky130 output, whose ``well`` role is ``None``). It is not a
separation/spacing rule protecting
the bipolar device from unrelated diffusion/tap the way gf180mcu's
``BJT.3`` (``bjt.separation.comp.1`` below) is, and its "must never overlap
at all" semantics have no representation among the check kinds this
engine's ``Region.*_check()`` dispatch supports (``width``/``space``/
``notch``/``separation``/``enclosing``/``enclosed``/``overlap`` — see
``DrcRule``'s docstring): ``overlap_check`` enforces a *minimum required*
overlap, the opposite of "forbid any overlap," so transcribing ``dnwell.4``
under that check kind would silently pass every layout rather than flag
anything. Conclusion: sky130's official DRC deck has no analogue to
gf180mcu's ``BJT.3`` bipolar device-mark separation rule; sky130's vertical
PNP device relies on ``pnp.drawing`` for LVS/device-recognition purposes
only, not DRC-level mark/separation checking. No rule was added here, and
``_PDK_ROLE_LAYERS["sky130"]["bjt_mark"]`` in ``klayout_tools.gen`` remains
``None`` accordingly.

Follow-up (issue #223): ``pnp.drawing`` (82/44) is now consumed for exactly
that LVS/device-recognition purpose -- ``EXTRACTION_DECK.bipolars`` below
uses it as the ``marker`` layer of a :class:`~klayout_tools.decks.BipolarDevice`
entry, scoping ``klt extract``'s ``DeviceExtractorBJT3Transistor`` wiring to
genuine ``sky130_fd_pr__pnp_05v5``-style device-cell instances rather than
every drawn ``nwell.drawing`` region. ``npn.drawing`` (82/20) is deliberately
*not* wired into a second ``bipolars`` entry: this module's own finding above
(no ``.lydrc`` rule references it beyond the unrelated ``dnwell.4``
compatibility exclusion) is the only evidence available in this repo, and it
does not on its own establish that some sky130 bipolar device cell draws it
for recognition the way ``pnp.drawing`` is drawn by ``..._pnp_05v5`` -- absent
that positive confirmation (this repo vendors no sky130 device-library GDS to
check directly), this deck stays with the one PNP entry it can attribute to a
concretely-named device cell.

``klt extract``'s MiM-capacitor device recognition (``EXTRACTION_DECK.capacitors``
below, issue #225) is transcribed from a *different* source than the DRC
rules above: neither ``sky130.lydrc`` nor ``sky130.lyt`` (both cited above)
carries a MiM-cap device-recognition derivation or capacitance-per-area
coefficient -- that lives in the companion **KLayout LVS** deck,
``sky130.lvs``, which (like ``sky130A.lyp``, the layer-properties file naming
the ``capm``/``capm2`` mark layers below) is fetched by ``open_pdks`` from a
*different* upstream repo than the ``open-pdks`` mono-repo itself:
``efabless/sky130_klayout_pdk`` (its ``klayout-repo`` Makefile target;
confirmed via a real sky130A install, volare,
``libs.tech/klayout/lvs/sky130.lvs`` / ``libs.tech/klayout/tech/sky130A.lyp``).
``sky130.lvs`` derives and extracts sky130's two independent MiM stacks
unconditionally (no foundry-side selectable-density option the way
gf180mcu's does -- see ``gf180mcu.py``'s own provenance note)::

    connect(capm , via3)
    connect(met3_ncap, met3_con)
    extract_devices(capacitor("sky130_fd_pr__model__cap_mim", 2e-15, MIMCap),
                    { "P1" => met3_con, "P2" => capm })

    connect(capm2, via4)
    connect(met4_ncap, met4_con)
    extract_devices(capacitor("sky130_fd_pr__model__cap_mim_m4", 2e-15, MIMCap),
                    { "P1" => met4_con, "P2" => capm2 })

i.e. sheet capacitance **2.0 fF/um^2** for both, a plain two-terminal
``DeviceExtractorCapacitor`` (no bulk terminal), with the bottom plate simply
"whatever conductor the purpose-drawn top-plate mark layer sits over" -- no
"virtual bottom plate" sizing derivation needed the way gf180mcu's MiM stack
requires (sky130's ``capm``/``capm2`` are purpose-built device-recognition
layers, not an ordinary routing-metal layer a real device geometrically
implies). Both curated `capacitors` entries below reuse the deck's own
``met3``/``met4`` (70/20, 71/20) conductor layers as ``bottom_plate`` --
unfiltered, since ``capm``/``capm2`` alone already scope recognition to
genuine MiM-cap instances -- rather than the full official
``met3_con``/``met4_con`` derivations (which additionally exclude
``met3_res``/``met4_res``/``inductor``/``met3_fuse``/``met4_fuse``/``vpp``,
none of which this curated deck otherwise models -- the same "curated
starter subset" scope guard elsewhere in this file).

``area_cap_f_um2``/``perim_cap_f_um`` (issue #512) are refined from the LVS
deck's own rounded ``2e-15`` F/um² above to sky130's own **simulation**
model's two-term law, ``czero = carea + cperim`` with ``carea =
camimc*w*l`` / ``cperim = cpmimc*(w+l)*2``, transcribed from the same
``efabless/sky130_klayout_pdk``-adjacent, official ``sky130_fd_pr`` install
(``volare enable sky130 c6d73a35f524070e85faff4a6a9eef49553ebc2b``):
``libs.ref/sky130_fd_pr/spice/sky130_fd_pr__cap_mim_m3_1.model.spice`` (met3,
``sky130_fd_pr__model__cap_mim``) and ``..._cap_mim_m3_2.model.spice`` (met4,
``sky130_fd_pr__model__cap_mim_m4``) share this same formula; both defer the
``camimc``/``cpmimc`` numeric values to a per-corner include
(``libs.tech/ngspice/r+c/res_typical__cap_typical__lin.spice``, the "tt"
corner this deck otherwise assumes elsewhere), which gives ``camimc =
2.00e-15`` F/um² (matching the LVS deck's own ``2e-15`` area coefficient
exactly -- unlike gf180mcu, no area-term correction is needed here) and
``cpmimc = 0.19e-15`` F/um (the previously-unmodelled perimeter/fringe
term). Both stacks share one corner file, so the same ``perim_cap_f_um``
value applies to both curated entries below.

Two rules below (``poly.width.1`` is not one of them) approximate an
official rule defined on a *compound* layer expression (a union of two mask
layers) as a check against a single drawn layer, because our engine (native
``klayout.db.Region`` check primitives — see ``docs/cli/drc.md``) checks one
layer, or one layer against one other layer, at a time; it does not evaluate
arbitrary boolean layer expressions the way the DRC-DSL script runner does.
Each such approximation is called out explicitly in its rule's docstring.

Layer numbers (verified against ``sky130.lyt``'s ``layer-map`` and the
``sky130.lydrc`` layer variable definitions, e.g. ``poly = polygons(66,
20)``):

    diff.drawing    65/20
    tap.drawing     65/44
    poly.drawing    66/20
    licon1.drawing  66/44
    li1.drawing     67/20
    mcon.drawing    67/44
    met1.drawing    68/20
    via.drawing     68/44
    met2.drawing    69/20

met2/via rule coverage (issue #513, closing the gap #511 opened when it
extended ``EXTRACTION_DECK`` to a third connectivity level -- see that
field's own comment below): ``m2.1``/``m2.2``/``m2.4``/``m2.5`` and
``via.1a``/``via.2``/``via.4a``/``via.5a`` below are transcribed from a real
sky130A PDK install (volare), whose
``libs.tech/klayout/drc/sky130A_mr.drc`` is the sky130A variant's current
name for the same official, community-maintained KLayout DRC deck this
module's other rules already cite as ``sky130.lydrc`` (confirmed by
cross-checking: every rule id/value this module already cites --
``poly.1a``, ``li.1``, ``li.3``, ``m1.1``, ``m1.2``, ``m1.4``,
``licon.5``/``licon.8``, ``ct.2`` -- appears in ``sky130A_mr.drc`` with the
identical id and threshold value). ``m2.6`` (minimum met2 area, 0.0676
um^2) is a real rule in that same source but is **not** transcribed: no
``"area"`` check primitive exists anywhere in this engine's ``DrcRule``
vocabulary (``drc.py``'s ``_SINGLE_LAYER_CHECKS``/``_TWO_LAYER_CHECKS``
support only ``width``/``space``/``notch``/``separation``/``enclosing``/
``enclosed``/``overlap``) -- adding it would require a new check primitive,
out of scope for this "extend the deck with existing-shape rules" issue; see
#513 for a candidate follow-on.

met3/met4/met5 connectivity (issue #619, closing the gap ``place_and_route.py``'s
``_ROUTING_LAYER_RANGE`` exposed: it already told OpenROAD signal routing
could use met1-met5, while ``EXTRACTION_DECK.metals`` stopped at met2, so a
net joined only through met3-or-higher silently split into two nets with no
warning): ``metals``/``metal_labels``/``vias`` below now cover the full
li1-through-met5 stack, following the exact met1->met2 extension pattern
issue #508 established (see that field's own comment below). Layer numbers
verified against the same real sky130A install (volare) cited by #508:
``libs.tech/klayout/tech/sky130A.lyt``'s ``<connectivity>`` block names
``met2,69/44,met3`` / ``met3,70/44,met4`` / ``met4,71/44,met5`` (matching
this deck's existing "via connects layer N's own layer number, datatype 44,
to layer N+1" pattern -- mcon at li1's 67/44, via at met1's 68/44), and its
``<symbols>`` block gives ``met3='70/20+70/5+70/16'`` /
``met4='71/20+71/5+71/16'`` / ``met5='72/20-72/15+72/5+72/16'`` (drawing/
label datatypes 20/5, the same convention every metal level already in this
deck follows -- also matching this repo's own ``gen.py``'s
``"top_metal": (72, 20)`` bond-pad role, cited there as ``met5.drawing``
from the same source). met3/met4 (70/20, 71/20) were already named in this
deck as ``capacitors[].bottom_plate`` (see the module docstring's provenance
note above) -- a layer can legitimately serve both roles at once, and
``ExtractionDeck.device_recognition_only_layers`` (issue #619) is exactly
the mechanism that would have flagged this "read but not merged" ambiguity
had it existed before this extension closed the gap.
"""

from __future__ import annotations

from . import (
    BipolarDevice,
    CapacitorDevice,
    DrcRule,
    ExtractionDeck,
    LayerRC,
    ParasiticsDeck,
    ResistorDevice,
    RuleProvenance,
)

# `RuleProvenance.source_repo`/`.commit` shared by every sky130 width/space
# rule's provenance below (issue #747, piloted on this deck's 11 width/space
# rules only -- see the module docstring's own citation of this repo/commit
# for the resistor/capacitor coefficients above, the same `volare enable
# sky130 c6d73a35f524070e85faff4a6a9eef49553ebc2b` fetch this deck's DRC
# rule values were themselves verified against, per the "met2/via rule
# coverage" note above).
_OPEN_PDKS_REPO = "fossi-foundation/open-pdks"
_OPEN_PDKS_COMMIT = "c6d73a35f524070e85faff4a6a9eef49553ebc2b"

# This deck's rule thresholds below are authored assuming database units are
# nanometres (dbu_um = 0.001), so a threshold in micrometres times 1000 gives
# threshold_dbu. `run_drc()` rescales threshold_dbu by NOMINAL_DBU_UM /
# layout.dbu at run time, so the deck still gives correct results against a
# layout written at a different dbu (see DrcRule's docstring).
NOMINAL_DBU_UM = 0.001

DECK: list[DrcRule] = [
    DrcRule(
        id="poly.width.1",
        description="minimum poly width",
        layer=(66, 20),  # poly.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # sky130.lydrc rule "poly.1a": poly.width(0.15, euclidian)
        # -> "poly.1a : min. poly width : 0.15um"
        scope="poly",  # sky130.lydrc "poly.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="poly.1a",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="diff.width.1",
        description="minimum diff width (approximates the official difftap.1 rule)",
        layer=(65, 20),  # diff.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # sky130.lydrc rule "difftap.1": difftap.width(0.15, euclidian), where
        # difftap = diff.or(tap) -- a compound layer our engine does not
        # evaluate; approximated here by checking diff.drawing alone. The
        # threshold value (0.15um) is the real, unmodified source value.
        # -> "difftap.1 : min. difftap width : 0.15um"
        scope="difftap",  # sky130.lydrc "difftap.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="difftap.1",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="li1.width.1",
        description="minimum li1 (local interconnect) width",
        layer=(67, 20),  # li1.drawing
        check="width",
        threshold_dbu=170,  # 0.17 um
        # sky130.lydrc rule "li.1": not_in_cell5_li.width(0.17, euclidian)
        # -> "li.1 : min. li width : 0.17um"
        # (the "not_in_cell5_li" exclusion covers a handful of named analog
        # macro cells not modelled here; general-case threshold is used.)
        scope="li",  # sky130.lydrc "li.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="li.1",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="li1.space.1",
        description="minimum li1 (local interconnect) spacing",
        layer=(67, 20),  # li1.drawing
        check="space",
        threshold_dbu=170,  # 0.17 um
        # sky130.lydrc rule "li.3": not_in_cell5_li.space(0.17, euclidian)
        # -> "li.3 : min. li spacing : 0.17um"
        scope="li",  # sky130.lydrc "li.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="li.3",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="met1.width.1",
        description="minimum met1 width",
        layer=(68, 20),  # met1.drawing
        check="width",
        threshold_dbu=140,  # 0.14 um
        # sky130.lydrc rule "m1.1": m1.width(0.14, euclidian)
        # -> "m1.1 : min. m1 width : 0.14um"
        scope="m1",  # sky130.lydrc "m1.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="m1.1",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="met1.space.1",
        description="minimum met1 spacing",
        layer=(68, 20),  # met1.drawing
        check="space",
        threshold_dbu=140,  # 0.14 um
        # sky130.lydrc rule "m1.2": non_huge_m1.space(0.14, euclidian)
        # -> "m1.2 : min. m1 spacing : 0.14um"
        # (the wide-metal 0.28um exception, "m1.3ab", is not modelled here.)
        scope="m1",  # sky130.lydrc "m1.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="m1.2",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="met1.enclosing.mcon.1",
        description="minimum met1 enclosure of mcon",
        layer=(68, 20),  # met1.drawing
        other_layer=(67, 44),  # mcon.drawing
        check="enclosing",
        threshold_dbu=30,  # 0.03 um
        # sky130.lydrc rule "m1.4": not_in_cell6_m1.enclosing(mcon, 0.03, euclidian)
        # -> "m1.4 : min. m1 enclosure of mcon : 0.03um"
        scope="m1",  # sky130.lydrc "m1.*" rule-id family (#566)
    ),
    DrcRule(
        id="diff.enclosing.licon.1",
        description="minimum diff enclosure of licon1",
        layer=(65, 20),  # diff.drawing
        other_layer=(66, 44),  # licon1.drawing
        check="enclosing",
        threshold_dbu=40,  # 0.04 um
        # sky130.lydrc rule "licon.5": diff.enclosing(licon, 0.04, euclidian)
        # -> "licon.5 : min. diff enclosure of licon : 0.04um"
        scope="licon",  # sky130.lydrc "licon.*" rule-id family (#566)
    ),
    DrcRule(
        id="poly.enclosing.licon.1",
        description="minimum poly enclosure of licon1",
        layer=(66, 20),  # poly.drawing
        other_layer=(66, 44),  # licon1.drawing
        check="enclosing",
        threshold_dbu=50,  # 0.05 um
        # sky130.lydrc rule "licon.8": poly.enclosing(licon, 0.05, euclidian)
        # -> "licon.8 : min. poly enclosure of licon : 0.05um"
        scope="licon",  # sky130.lydrc "licon.*" rule-id family (#566)
    ),
    DrcRule(
        id="li1.enclosing.licon1.1",
        description=(
            "li1 (local interconnect) must cover licon1 (approximates the "
            "official two-adjacent-edges enclosure rule as its zero-margin "
            "floor)"
        ),
        layer=(67, 20),  # li1.drawing -- the conductor *above* the cut
        other_layer=(66, 44),  # licon1.drawing
        check="enclosing",
        threshold_dbu=0,  # 0.0 um -- see the approximation note below
        # sky130A_mr.drc rule "li.5":
        #   li.enclosing(licon_peri, 0.08, projection).second_edges
        #     -> .width(angle_limit(100.0), 1.dbu) -> licon.interacting(...)
        # -> "li.5 : min. li enclosure of licon of 2 adjacent edges : 0.08um"
        #
        # Approximation (threshold *is* modified here, unlike every other
        # rule in this deck -- see the module docstring's own li.5 note):
        # li.5 does not require 0.08um of li enclosure on every side of a
        # licon, only on *two adjacent* edges, which lets real layout land a
        # minimum-width li strap flush with the cut on the other two. Our
        # vocabulary has no way to express that per-edge-pair conditional, so
        # transcribing 0.08um as a plain, unconditional enclosure check would
        # be actively wrong rather than merely conservative: measured against
        # this repo's own sky130 corpus it flags 6-56 violations per
        # standard cell and 6566 across the OpenROAD-produced GCD macro
        # (`tests/corpus/place_and_route/gcd.gds.gz`) -- all correct-by-
        # construction geometry. What li.5 *unconditionally* implies, and
        # what our vocabulary can express exactly, is the zero-margin floor:
        # the li1 conductor must actually cover the licon1 cut it lands on.
        # A zero threshold makes `enclosing_check` itself report nothing and
        # carries the whole rule in `_run_check`'s `outside_region` escape
        # term (`drc.py`) -- the same shape as gf180mcu's own zero-threshold
        # `metal1.enclosing.via1.1` ("V1.3a"), which the gf180mcu DRM
        # publishes as literally 0.0um. This closes the asymmetry issue #551
        # reports (`diff`/`poly` -- the layers *below* licon1 -- were checked,
        # the conductor *above* it was not) without the false positives.
    ),
    DrcRule(
        id="mcon.space.1",
        description="minimum mcon spacing",
        layer=(67, 44),  # mcon.drawing
        check="space",
        threshold_dbu=190,  # 0.19 um
        # sky130.lydrc rule "ct.2": mcon.space(0.19, euclidian)
        # -> "ct.2 : min. mcon spacing : 0.19um"
        scope="ct",  # sky130.lydrc "ct.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130.lydrc",
            rule_id="ct.2",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    # met2/via rule coverage (issue #513), mirroring the met1/mcon rule
    # shapes just above -- see the module docstring's own #513 note for
    # source/provenance and the m2.6 (area) scope-out.
    DrcRule(
        id="met2.width.1",
        description="minimum met2 width",
        layer=(69, 20),  # met2.drawing
        check="width",
        threshold_dbu=140,  # 0.14 um
        # sky130A_mr.drc rule "m2.1": m2.width(0.14, euclidian)
        # -> "m2.1 : min. m2 width : 0.14um"
        scope="m2",  # sky130A_mr.drc "m2.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130A_mr.drc",
            rule_id="m2.1",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="met2.space.1",
        description="minimum met2 spacing",
        layer=(69, 20),  # met2.drawing
        check="space",
        threshold_dbu=140,  # 0.14 um
        # sky130A_mr.drc rule "m2.2": non_huge_m2.space(0.14, euclidian)
        # -> "m2.2 : min. m2 spacing : 0.14um"
        # (the wide-metal 0.28um exception, "m2.3ab", is not modelled here --
        # the same approximation met1.space.1 above already makes for its
        # own wide-metal exception, "m1.3ab".)
        scope="m2",  # sky130A_mr.drc "m2.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130A_mr.drc",
            rule_id="m2.2",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="via.width.1",
        description=(
            "minimum via1 (met1<->met2 via) size (approximates the "
            "official rectangularity + min/max-length rule as a "
            "minimum-width check)"
        ),
        layer=(68, 44),  # via.drawing
        check="width",
        threshold_dbu=150,  # 0.15 um
        # sky130A_mr.drc rule "via.1a_a" (part of the "via.1a" family):
        # via_not_mt.width(0.15, euclidian)
        # -> "via.1a_a : min. width of via outside of moduleCut : 0.15um"
        # (mirrors gf180mcu.py's "contact.width.1" approximation: the
        # official rule also requires the via be rectangular ("via.1a") and
        # capped at the same 0.15um length ("via.1a_b") -- our width_check
        # primitive only supports a minimum-width lower bound, so only the
        # min-size half of the rule is enforced here.)
        scope="via",  # sky130A_mr.drc "via.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130A_mr.drc",
            rule_id="via.1a_a",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="via.space.1",
        description="minimum via1 (met1<->met2 via) spacing",
        layer=(68, 44),  # via.drawing
        check="space",
        threshold_dbu=170,  # 0.17 um
        # sky130A_mr.drc rule "via.2": via.space(0.17, euclidian)
        # -> "via.2 : min. via spacing : 0.17um"
        scope="via",  # sky130A_mr.drc "via.*" rule-id family (#566)
        provenance=RuleProvenance(
            source_repo=_OPEN_PDKS_REPO,
            source_path="sky130/klayout/sky130A_mr.drc",
            rule_id="via.2",
            commit=_OPEN_PDKS_COMMIT,
        ),
    ),
    DrcRule(
        id="met1.enclosing.via.1",
        description="minimum met1 enclosure of via1 (met1<->met2 via)",
        layer=(68, 20),  # met1.drawing
        other_layer=(68, 44),  # via.drawing
        check="enclosing",
        threshold_dbu=55,  # 0.055 um
        # sky130A_mr.drc rule "via.4a": m1.edges.enclosing(via, 0.055,
        # euclidian) -> "via.4a : min. m1 enclosure of 0.15um via : 0.055um"
        # (a second, 2-adjacent-edges-relaxed refinement, "via.5a" (0.085um),
        # is not modelled here -- the same "primary rule only" approximation
        # met1.enclosing.mcon.1 above already makes for its own sibling
        # refinement, "m1.5".)
        scope="via",  # sky130A_mr.drc "via.*" rule-id family (#566)
    ),
    DrcRule(
        id="met2.enclosing.via.1",
        description="minimum met2 enclosure of via1 (met1<->met2 via)",
        layer=(69, 20),  # met2.drawing
        other_layer=(68, 44),  # via.drawing
        check="enclosing",
        threshold_dbu=55,  # 0.055 um
        # sky130A_mr.drc rule "m2.4": m2.enclosing(via_outside_periphery,
        # 0.055, euclidian) -> "m2.4 : min. m2 enclosure of via : 0.055um"
        # (the official rule's periphery-scoped variant, "m2.4_b" (0.045um,
        # areaid:core-only), and its 2-adjacent-edges-relaxed refinement,
        # "m2.5" (0.085um), are not modelled -- this curated deck does not
        # model areaid.* layers elsewhere either; same "primary rule only"
        # approximation as met1.enclosing.via.1 above.)
        scope="m2",  # sky130A_mr.drc "m2.*" rule-id family (#566)
    ),
]

# (layer, datatype) -> "name.purpose" string, from sky130.lyt's layer-map,
# used only to render violations[].layer as e.g. "poly.drawing" instead of
# the bare "66/20" fallback.
LAYER_NAMES: dict[tuple[int, int], str] = {
    (65, 20): "diff.drawing",
    (65, 44): "tap.drawing",
    (66, 20): "poly.drawing",
    (66, 44): "licon1.drawing",
    (67, 20): "li1.drawing",
    (67, 44): "mcon.drawing",
    (68, 20): "met1.drawing",
    (68, 44): "via.drawing",
    (82, 44): "pnp.drawing",
    (69, 20): "met2.drawing",
    (70, 20): "met3.drawing",
    (71, 20): "met4.drawing",
    # met3/met4/met5 connectivity levels + their landing vias (issue #619),
    # see the module docstring's provenance note for this extension.
    (69, 44): "via2.drawing",
    (70, 44): "via3.drawing",
    (71, 44): "via4.drawing",
    (72, 20): "met5.drawing",
    (89, 44): "capm.drawing",
    (97, 44): "capm2.drawing",
}

# Voltage-domain marker layers this deck draws but does not model the DRC/
# extraction scoping of (issue #552's `decks.get_unmodeled_voltage_markers`,
# the gf180mcu `Dualgate` sibling of this table). sky130's real PDK has an
# `hvi` (high-voltage identification) layer with its own DRC column, but
# this curated deck's `LAYER_NAMES` above does not name it as a recognised
# layer at all yet (unlike gf180mcu's `Dualgate`, which is drawn/named here
# just not scoped) -- so there is nothing to register in this table until a
# follow-on issue adds `hvi` as a named layer first. Empty, not omitted, so
# `get_unmodeled_voltage_markers("sky130")` returns `{}` explicitly rather
# than relying on the registry's unregistered-deck fallback.
UNMODELED_VOLTAGE_MARKERS: dict[tuple[int, int], str] = {}

# --------------------------------------------------------------------------- #
# `klt extract` connectivity + device-extraction deck
# --------------------------------------------------------------------------- #
#
# Layer numbers not already covered above, from the same source cited at the
# top of this module (sky130.lyt's layer-map / open_pdks):
#
#     nwell.drawing   64/20
#     nwell.pin       64/5   (a text/label layer -- carries the body-tie pin
#                             name, e.g. "VPB", directly on the well shape)
#     poly.pin        66/5   (a text/label layer on the poly shape itself --
#                             the datatype-5 ".pin" convention every other
#                             label layer here follows; used to name a gate
#                             node that has no metal landing pad, see below)
#     li1.pin         67/5
#     met1.pin        68/5
#
# Verified against this repo's own sky130 corpus fixtures
# (`tests/corpus/sky130/sky130_fd_sc_hd__*.gds`): every cell's nwell body pin
# is labelled directly on layer 64/5 (e.g. "VPB"); li1/met1 signal and power
# pins are labelled on 67/5 / 68/5 respectively (e.g. "A", "Y", "VPWR",
# "VGND"). `tap.drawing` (65/44) is a *distinct* drawn layer from
# `diff.drawing` (65/20) -- sky130 never draws a substrate/well tie on the
# transistor active layer -- so it is safe to connect `tap` to the well and
# to `contact` directly (see `ExtractionDeck`'s docstring for why the
# opposite (connecting the *well* to *every* contact inside it) is wrong).
#
# NMOS body (issue #490): sky130 draws no separate substrate/pwell layer, so
# `tap.drawing` does double duty -- a shape drawn *inside* an `nwell` is a
# PMOS well tie (unchanged, described above), a shape drawn *outside* every
# `nwell` sits on native P-substrate and is a genuine, drawable substrate
# tie. `extract.py` splits `tap` by `nwell` containment and wires the
# outside-the-well slice through `contact`/the metal stack the same way the
# well-tie slice already is, so the NMOS body terminal (and the identically-
# modelled `bulk_to_substrate` resistor bulk and collector-less bipolar
# collector terminals) resolve to that ring's own real, labelled net when a
# substrate tap ring is drawn and contacted up to a named net. This repo's
# own `sky130_fd_sc_hd__*` corpus cells draw no such ring at the single-cell
# level (only VPB/nwell is a genuine standalone pin there), so single-cell
# extraction still falls back to the deck's `substrate_net` global
# (`ExtractionDeck.substrate_net`, default `"vsubs"`) exactly as before --
# the real-tap case matters for analog macros/guard rings that do draw one.
#
# `poly_label` (66/5) mirrors `well_label`: a text on the poly layer names the
# poly net directly, so a device gate `klt gen` draws as bare poly -- with no
# contact/metal landing pad (see `gen.py`'s `_mos_unit_layout`, which gives
# S/D segments a contact+metal pad but leaves gate fingers as bare poly) --
# can still be promoted to a named `.SUBCKT` pin by `klt gen-compose`'s
# `pins[]` (#210). Unlike the metal/well pins above, sky130's own corpus
# cells route gates in li1 rather than labelling poly directly, so 66/5 has no
# corpus precedent here; it is a curated choice consistent with the
# datatype-5 ".pin" convention every other label layer in this deck follows.
#
# Drawn resistors (#222, extended by #299). Additional layer numbers, same
# sources as above:
#
#     poly.res    66/13  (the poly resistor-ID mark; named "poly.res - 66/13"
#                         in sky130A.lyp's layer table, and defined as
#                         `poly_res = polygons(66, 13)` in sky130.lvs)
#     rpm         86/20  (300 ohm/sq precision-resistor implant mask)
#     urpm        79/20  (2 kohm/sq precision-resistor implant mask)
#     psdm        94/20  (P+ implant -- `polygons(94, 20)` in sky130.lvs;
#                         gates the two higher-sheet-rho flavours below,
#                         not otherwise modelled elsewhere in this deck)
#
# Provenance for the resistor devices modelled below is the *official*
# sky130 KLayout LVS deck, `sky130/klayout/lvs/sky130.lvs` from the same
# open_pdks source cited at the top of this module (as installed by volare
# under `libs.tech/klayout/lvs/sky130.lvs`), which derives and extracts the
# generic (`res_generic_po`) flavour as:
#
#     poly_res_exclude = diff.or(diff_res).or(diff_cut).or(tap).or(pwbm)
#                            .or(hvtr).or(hvtp).or(lvtn).or(li_cut)
#                            .or(vpp).or(npnid)
#     poly_res_init    = poly.and(poly_res).not(poly_res_exclude)
#     poly_res_generic = poly_res_init.not(rpm).not(urpm)
#     poly_con         = poly.not(poly_res).not(vpp).not(npnid)
#     extract_devices(resistor("sky130_fd_pr__res_generic_po", 48.2, NResistor),
#                     { "R" => poly_res_generic, "C" => poly_con })
#
# i.e. sheet resistance **48.2 ohm/square**, a plain two-terminal
# `DeviceExtractorResistor` (no bulk terminal). Cross-checked against a
# second, independent source in the same PDK install: open_pdks' magic
# technology file `libs.tech/magic/sky130A.tech`, whose extract section
# reads `resist rmp/active 48200` and `resist mrp1/active 48200 0.5`
# (milliohms per square = 48.2 ohm/sq) for the same `sky130_fd_pr__res_generic_po`
# device (`device resistor sky130_fd_pr__res_generic_po rmp *poly`).
#
# Approximations relative to that official derivation, all conservative:
#
# - `excludes` models the four exclusion layers this curated deck actually
#   knows about (diff, tap, rpm, urpm). The remaining upstream exclusions
#   (diff_res, diff_cut, pwbm, hvtr, hvtp, lvtn, li_cut, vpp, npnid) are
#   *not* modelled -- the curated-starter-subset scope guard this module
#   documents for DRC applies to extraction too. A poly-res segment marked
#   with one of those unmodelled layers extracts as this generic 48.2 ohm/sq
#   device rather than being excluded.
# - `rpm`/`urpm` are each *also* now their own wired `ResistorDevice` entry
#   below (issue #299 -- previously they were pure exclusions on
#   `res_generic_po`, leaving the shapes they mark an unmodelled short; see
#   the `res_high_po`/`res_xhigh_po` note further below), so `res_generic_po`
#   itself still excludes both -- `rpm`/`urpm`-marked poly is a *different*
#   device class, never this 48.2 ohm/sq generic one.
# - The terminal ("C") region is `poly` minus the recognised body rather than
#   upstream's `poly.not(poly_res)...`; the two agree except on marked poly
#   that this deck does *not* recognise as a resistor, which stays ordinary
#   connected poly here rather than being cut out of connectivity entirely.
#
# `res_high_po`/`res_xhigh_po` (issue #299): sky130's *other* two poly
# resistor flavours, each keyed off the same `poly.res` marker as
# `res_generic_po` plus one of the two precision-implant masks above. The
# official `sky130.lvs` derives both off one shared implant-gated base
# (`poly_res_init = poly.and(poly_res).not(poly_res_exclude)`, the same
# exclusion set `res_generic_po` above already transcribes) and its own P+
# implant mask, `psdm` (94/20, `polygons(94, 20)` in the same layer table --
# not otherwise modelled by this curated deck, added here only as an
# additional `requires` layer):
#
#     poly_res_300 = poly_res_init.and(psdm).and(rpm).not(urpm)
#     poly_res_2k  = poly_res_init.and(psdm).not(rpm).and(urpm)
#
# `rpm`/`urpm` are mutually exclusive by construction here (each requires the
# other's absence), so the two device classes below recognise disjoint
# geometry -- unlike gf180mcu's `POLY_RES`-selected flavours (see
# `gf180mcu.py`'s `ppolyf_u_1k` note), a real drawn `sky130` resistor's
# `rpm` vs. `urpm` choice *is* distinguishable from the layout alone.
# `sky130.lvs` then further splits each by one of five fixed lengths
# (0.35/0.69/1.41/2.85/5.73 um, `poly_high_0p35`, etc.) into per-length
# device classes -- but every one of those five extracts with the *same*
# sheet-rho value (319.8 or 2000 ohm/sq respectively; confirmed directly
# against `sky130.lvs`'s own `extract_devices` calls, e.g.
# `resistor_with_bulk("sky130_fd_pr__res_high_po_0p35", 319.8, BResistor)`
# through `..._5p73`, all 319.8), so the length split changes only the
# extracted *device-class name* (presumably for downstream SPICE-model
# binding to a fixed-length parasitic-end-effect correction), not the sheet
# resistance `R = L / W * sheet_rho` computes from the segment's own drawn
# geometry. This curated deck's `ResistorDevice` has no fixed-length-variant
# concept (the same "per-width device classes and a length tolerance this
# curated deck does not model" limitation already flagged above), so both
# entries below are wired as one flat device class each, independent of the
# segment's drawn length -- giving the correct resistance for any length,
# just not upstream's own per-length class name/model-binding split.
# Both entries also extract `resistor_with_bulk` (bulk tied to `sub`, the
# native P-substrate) in the official deck, hence `bulk_to_substrate=True`
# below -- the same substrate-global approximation the NMOS body terminal and
# gf180mcu's `ppolyf_u` already carry.
#
# `res_high_po`'s `fixed_offset_ohm` (issue #518): the `319.8` ohm/sq
# `sheet_rho_ohm_sq` cited above is `sky130.lvs`'s own single-term
# restatement of a real *two*-term device -- `sky130_fd_pr__res_high_po`'s
# SPICE `.subckt` (`libs.ref/sky130_fd_pr/spice/sky130_fd_pr__res_high_po
# .model.spice`) composes a length-scaling `rbody` sub-resistor in series
# with an `rhead` sub-resistor whose own length is a fixed model constant
# (`l=1.0`), not the caller's drawn length -- i.e. a per-instance *fixed*
# resistance term `R = L / W * sheet_rho_ohm_sq` alone cannot express. Both
# `sheet_rho_ohm_sq` and `fixed_offset_ohm` below are *measured* (not
# transcribed), the same way this issue's own filing measured the gap --
# via ngspice against the real PDK install (the same `volare enable sky130
# c6d73a35f524070e85faff4a6a9eef49553ebc2b` fetch cited in the MiM
# capacitor provenance note above), at the nominal `tt` corner
# (`libs.tech/combined/sky130.lib.spice`'s `.lib tt` section), `w=1um`,
# sweeping `l` across {1, 5, 10, 35}um: total two-terminal resistance
# 704.532385 / 2003.84137 / 3627.97760 / 11748.6587 ohm respectively
# (matching this issue's own citation table to the digit it quotes).
# `libs.tech/combined/sky130.lib.spice` is the scalable, single-`.subckt`
# resistor model `libs.tech/combined/continuous/models_resistors.spice`
# actually simulates -- its own header calls it "New scalable 300 Ohm/sq
# Poly resistor model ... Converted from discrete model", and it evaluates
# to `rhead`'s `rhead_ps * sw_poly_head_res` / `rbody`'s
# `sw_sky130_fd_pr__res_high_po_rs` sheet-rho values (325.0/345.8312 ohm/sq
# at the `tt` corner's `libs.tech/combined/continuous/parameters_res_nom
# .spice`, `corner_factor=0`) rather than the discrete per-length
# `.model.spice` file's own `p2`/`q2`/`p3`/`q3` shape-correction terms --
# ngspice does not implement those parameters for its built-in semiconductor
# resistor device (confirmed directly: instantiating a resistor against a
# `.model` declaring them logs `unrecognized parameter (p2) - ignored` for
# every one), so they are a no-op in any ngspice simulation of either the
# discrete or scalable model -- consistent with the `l`-independence of the
# measured `rhead` contribution.
#
# Both coefficients are exact fits (curve-fit against the 4 lengths above
# plus an independent, held-out `l=6um` cross-check point --
# `2328.66861` ohm measured vs. `2328.668611` ohm predicted, a `4e-10`
# relative residual) to the total-resistance-is-affine-in-`l` relationship
# this measurement shows at fixed `w=1um`:
# `R_total(l) = sheet_rho_ohm_sq * l / w + fixed_offset_ohm`. All five
# measured points (including the held-out one) fit this affine law to a
# relative residual under `1e-8` -- comfortably inside this deck's own
# `rel=1e-6` verification bar (see `tests/test_extract.py`). No claim is
# made here that this affine law holds at a `w` other than the `1um` this
# issue measures (the real PDK model's `rhead` term does have its own,
# undetermined `w`-dependence) -- the same single-coefficient,
# curated-approximation limitation `sheet_rho_ohm_sq` alone already carries
# for every other resistor entry in this deck, one coefficient over.
EXTRACTION_DECK = ExtractionDeck(
    active=(65, 20),  # diff.drawing
    poly=(66, 20),  # poly.drawing
    nwell=(64, 20),  # nwell.drawing
    tap=(65, 44),  # tap.drawing -- distinct from diff.drawing, see above
    well_label=(64, 5),  # nwell.pin
    poly_label=(66, 5),  # poly.pin -- names a bare-poly gate node (#210)
    # Dummy-device marker (issue #491, extends the `dummy` field's #295/#462
    # extractor-side machinery -- see `ExtractionDeck.dummy`'s docstring in
    # `decks/__init__.py`). sky130 has no *native* per-device dummy-marker GDS
    # layer of its own: dummy fill in the real PDK is a density-rule/DRC
    # concept (metal-fill "waffle" generation, e.g. `sky130.lyt`'s
    # `cfom.*`/`fom.dummy` (22/23) family), not a device-recognition mark the
    # way `pnp.drawing` is for bipolars -- the same "no native mark, curated
    # substitute" situation this module's docstring already documents for
    # bipolars (see the "Negative finding" note above) before #223's
    # `pnp.drawing` follow-up gave that one a real mark to key off.
    # `(83, 20)` reuses sky130.lyt's own `marker.*` layer *number* (83 --
    # `marker.error : 83/6`, `marker.warning : 83/7`, `text.drawing : 83/44`,
    # confirmed against a real `sky130.lyt` checkout, the same source cited at
    # the top of this module) with datatype 20, the ".drawing"-purpose
    # datatype convention every other curated layer here follows -- but
    # `sky130.lyt` assigns no purpose to layer 83 datatype 20 at all, so this
    # deck reserves it as an extraction-only, deck-local marker with no
    # official sky130 purpose string and no DRC rule referencing it (`klt drc`
    # never checks this layer). Verified non-colliding against every shape
    # this repo's own `tests/corpus/sky130/*.gds` fixtures actually draw
    # (their only layer-83 shapes are on datatype 44, i.e. real
    # `text.drawing` instance/cell labels -- never datatype 20).
    dummy=(83, 20),
    contact=(66, 44),  # licon1.drawing
    # Third connectivity level, met2.drawing (issue #508, follow-up to
    # #454/#468's `"metal2"`/`"via1"` roles): before this, `metals` stopped
    # at met1, so met1 (this same deck's own second level, exposed as the
    # `"metal2"` routing role) was the *only* plane above the device-pad
    # layer a caller could route on -- a router already using met1 for its
    # own intra-block bussing had no second, independent plane left for
    # inter-block nets, on a PDK whose curated layer-number table already
    # names met2/met3/met4 elsewhere (MiM-cap bottom-plate layers only,
    # never a `metals`/`vias` connectivity level -- see the module
    # docstring's provenance note for `capacitors` below). Layer numbers
    # verified against a real sky130A install (volare):
    # `libs.tech/klayout/tech/sky130A.lyt`'s own `<connectivity>` block
    # names the met1<->met2 via `via1` (its own connectivity-script variable
    # name -- *not* related to this repo's own, purely positional `"via1"`/
    # `"via2"` role-name scheme below, which already used `"via1"` for mcon;
    # a pure naming coincidence, not a shared identity) at `68/44`, matching
    # `libs.tech/klayout/tech/sky130A.lyp`'s display name `via.drawing -
    # 68/44` this module's docstring already cites; met2.drawing itself is
    # `69/20` (`sky130A.lyt`'s `met2='69/20+69/5-69/14-69/15'` symbol,
    # matching `LAYER_NAMES`'s existing `(69, 20): "met2.drawing"` entry
    # above).
    #
    # met3/met4/met5 (issue #619, see the module docstring's own provenance
    # note for this extension): before this, `metals`/`vias` stopped at
    # met2, one full level short of `place_and_route.py`'s
    # `_ROUTING_LAYER_RANGE["sky130_fd_sc_hd"]` (`"met1-met5"`) -- a net
    # OpenROAD was told it could route through met3-or-higher would silently
    # split into two disconnected nets on extraction, with nothing in the
    # response flagging it (`ignored_layers` couldn't either: met3/met4 were
    # already read as `capacitors[].bottom_plate`, so they weren't
    # "ignored," just never merged -- see `ExtractionDeck.
    # device_recognition_only_layers`). met3.drawing/met4.drawing/
    # met5.drawing are `70/20`/`71/20`/`72/20`; the connecting vias are
    # `69/44`/`70/44`/`71/44` (each named after the *lower* metal's own layer
    # number, datatype 44 -- the same "via lands on the layer number of the
    # metal below it" convention mcon (`67/44`, li1's number) and via
    # (`68/44`, met1's number) already establish above). Both facts verified
    # against the same real sky130A install (volare) cited above: its
    # `<connectivity>` block names `met2,69/44,met3` / `met3,70/44,met4` /
    # `met4,71/44,met5`, and its `<symbols>` block gives
    # `met3='70/20+70/5+70/16'` / `met4='71/20+71/5+71/16'` /
    # `met5='72/20-72/15+72/5+72/16'`.
    metals=(
        (67, 20),  # li1.drawing
        (68, 20),  # met1.drawing
        (69, 20),  # met2.drawing
        (70, 20),  # met3.drawing
        (71, 20),  # met4.drawing
        (72, 20),  # met5.drawing
    ),
    # Pin/label-carrying datatype for every metal level (datatype 5), the
    # li1.pin/met1.pin/met2.pin convention issue #508 already established,
    # extended to met3.pin/met4.pin/met5.pin (`70/5`/`71/5`/`72/5`, issue
    # #619) by the same `sky130A.lyt` `<symbols>` block cited above (each
    # metal symbol includes its own `+N/5` label component).
    metal_labels=(
        (67, 5),  # li1.pin
        (68, 5),  # met1.pin
        (69, 5),  # met2.pin
        (70, 5),  # met3.pin
        (71, 5),  # met4.pin
        (72, 5),  # met5.pin
    ),
    vias=(
        (67, 44),  # mcon.drawing (li1<->met1)
        (68, 44),  # via.drawing (met1<->met2)
        (69, 44),  # via2.drawing (met2<->met3, issue #619)
        (70, 44),  # via3.drawing (met3<->met4, issue #619)
        (71, 44),  # via4.drawing (met4<->met5, issue #619)
    ),
    # Vertical PNP (issue #223, see the module docstring's "Follow-up" note
    # above): base = nwell.drawing (the physical n-type body the device's
    # p+ emitter sits in), emitter = diff.drawing (the same p+ diffusion
    # mask an ordinary PMOS source/drain uses), marker = pnp.drawing --
    # `extract.py` intersects base/emitter with the marker so only genuine
    # `sky130_fd_pr__pnp_05v5`-style device-cell instances are recognised,
    # not every PMOS's nwell. No drawn collector layer -- the collector is
    # the native P-substrate, tied to `substrate_net` like the NMOS body
    # above (see `BipolarDevice`'s docstring).
    #
    # No `emitter_excludes`/`emitter_requires` narrowing (issue #302):
    # unlike gf180mcu (whose p+ emitter carries `Pplus` and whose n+ base
    # tie carries `Nplus`, so an `emitter_excludes=(Nplus,)` cleanly drops
    # the base-contact ring), this curated deck models **no implant layers**
    # (see this module's docstring, the #183/#223 negative finding). The real
    # `sky130_fd_pr__pnp` draws its base tie on the *distinct* `tap.drawing`
    # (65/44) layer, not the `diff.drawing` (65/20) emitter layer, so no
    # same-layer collision occurs in practice -- a base tie on `tap.drawing`
    # is already outside the `emitter & base` region. Residual limitation:
    # were a base tie drawn on the *literal* `diff.drawing` emitter layer,
    # no already-modelled layer could distinguish it from the emitter, so it
    # would still double-count. Resolving that would require modelling
    # sky130's implant (`nsdm`/`psdm`) masks, which this starter deck does
    # not -- documented rather than partially/incorrectly worked around.
    bipolars=(
        BipolarDevice(
            base=(64, 20),  # nwell.drawing
            emitter=(65, 20),  # diff.drawing
            marker=(82, 44),  # pnp.drawing
            class_name="pnp",
        ),
    ),
    # MiM capacitors (issue #225, see the module docstring's provenance note
    # above): two independent stacks, one per metal level sky130's official
    # LVS deck draws a purpose-built top-plate mark layer on. Both
    # `bottom_plate` layers (met3/met4, `70/20`/`71/20`) are now *also* two of
    # this deck's own `metals` connectivity levels (extended met2->met5 by
    # issue #619 -- previously `metals` stopped at met2, one level short of
    # met3, so `extract.py`'s bottom-plate connectivity wiring (issue #314)
    # had nothing to match against and both plates stayed isolated
    # connectivity nodes). A layer serving both roles at once -- a `metals`
    # connectivity level *and* a device-recognition role -- is not a
    # conflict (see `ExtractionDeck.device_recognition_only_layers`'s
    # docstring): the bottom plate now genuinely merges with the surrounding
    # met3/met4 routing net the way real silicon does, on top of still being
    # recognised as a MiM-cap terminal. No `top_plate_via`/
    # `top_plate_via_metal`: sky130's real MiM stacks do have a landing via
    # for each (`sky130.lvs`'s `connect(capm, via3)` / `connect(capm2,
    # via4)`, see the module docstring's provenance note) -- `via3`/`via4`
    # are now tracked `vias` entries too (issue #619), but wiring the
    # top-plate mark layer itself to them is a separate, not-yet-modelled
    # feature this issue does not add; see `CapacitorDevice`'s "Known
    # limitation". No `bottom_plate_oversize_um` -- unlike gf180mcu's MiM
    # stack, sky130's bottom plate is simply the conductor the purpose-drawn
    # top-plate layer sits over, no "virtual bottom plate" derivation needed.
    capacitors=(
        CapacitorDevice(
            name="sky130_fd_pr__model__cap_mim",
            top_plate=(89, 44),  # capm.drawing
            bottom_plate=(70, 20),  # met3.drawing
            # tt-corner camimc/cpmimc, see provenance note above (issue #512):
            area_cap_f_um2=2.0e-15,  # 2.0 fF/um^2 (camimc, unchanged from LVS)
            perim_cap_f_um=1.9e-16,  # 0.19 fF/um (cpmimc, previously unmodelled)
        ),
        CapacitorDevice(
            name="sky130_fd_pr__model__cap_mim_m4",
            top_plate=(97, 44),  # capm2.drawing
            bottom_plate=(71, 20),  # met4.drawing
            # tt-corner camimc/cpmimc, see provenance note above (issue #512):
            area_cap_f_um2=2.0e-15,  # 2.0 fF/um^2 (camimc, unchanged from LVS)
            perim_cap_f_um=1.9e-16,  # 0.19 fF/um (cpmimc, previously unmodelled)
        ),
    ),
    # Drawn poly precision resistors (issue #222, extended by #299): the
    # generic `sky130_fd_pr__res_generic_po` device -- a segment of
    # poly.drawing covered by the poly.res resistor-ID marker -- plus the two
    # higher-sheet-rho flavours `rpm`/`urpm` additionally select. See the
    # module comment above for the full derivation/provenance of all three.
    resistors=(
        ResistorDevice(
            name="res_generic_po",  # sky130_fd_pr__res_generic_po
            body=(66, 20),  # poly.drawing
            marker=(66, 13),  # poly.res
            sheet_rho_ohm_sq=48.2,  # sky130.lvs / magic sky130A.tech, see above
            excludes=(
                (65, 20),  # diff.drawing -- a marked gate is a transistor
                (65, 44),  # tap.drawing
                (86, 20),  # rpm  -> sky130_fd_pr__res_high_po_*  (see below, #518)
                (79, 20),  # urpm -> sky130_fd_pr__res_xhigh_po_* (2 kohm/sq)
            ),
        ),
        ResistorDevice(
            name="res_high_po",  # sky130_fd_pr__res_high_po_* (lengths merged)
            body=(66, 20),  # poly.drawing
            marker=(66, 13),  # poly.res
            # Measured via ngspice against the real two-term PDK model at the
            # `tt` corner (issue #518), refined from `sky130.lvs`'s own
            # rounded, area-only-equivalent `319.8` -- see the provenance
            # note above for the exact methodology/source files.
            sheet_rho_ohm_sq=324.827244,
            fixed_offset_ohm=379.705147,
            requires=(
                (94, 20),  # psdm -- P+ implant, see the module comment above
                (86, 20),  # rpm  -- 300 ohm precision-resistor implant mask
            ),
            excludes=(
                (65, 20),  # diff.drawing -- a marked gate is a transistor
                (65, 44),  # tap.drawing
                (79, 20),  # urpm -- mutually exclusive with rpm, see above
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
        ),
        ResistorDevice(
            name="res_xhigh_po",  # sky130_fd_pr__res_xhigh_po_* (lengths merged)
            body=(66, 20),  # poly.drawing
            marker=(66, 13),  # poly.res
            sheet_rho_ohm_sq=2000.0,  # sky130.lvs res_xhigh_po_* extract_devices calls
            requires=(
                (94, 20),  # psdm -- P+ implant, see the module comment above
                (79, 20),  # urpm -- 2 kohm precision-resistor implant mask
            ),
            excludes=(
                (65, 20),  # diff.drawing -- a marked gate is a transistor
                (65, 44),  # tap.drawing
                (86, 20),  # rpm -- mutually exclusive with urpm, see above
            ),
            bulk_to_substrate=True,  # upstream extracts it with 'W' => sub
        ),
    ),
)

# --------------------------------------------------------------------------- #
# `klt extract --parasitics` first-order lumped-RC coefficients
# --------------------------------------------------------------------------- #
#
# Sourced, citable values transcribed from the *public* sky130 magic
# technology file `sky130/magic/sky130.tech` in fossi-foundation/open-pdks
# (GPLv3 -- the same upstream the DRC decks are transcribed from), nominal
# corner (the `variants (),(orig),(si)` block). Sheet resistances come from
# that file's `resist <layer> <milliohms per square>` entries; area/fringe
# capacitances from its `defaultareacap` (aF/um^2) and `defaultperimeter`
# (aF/um) entries -- the parallel-plate and fringe capacitance to the plane
# below. The .tech header credits SkyWater's public PEX/xRC/cap_models;
# nothing NDA'd. Values remain order-of-magnitude and uncalibrated-to-silicon:
# calibrating parasitic-extraction accuracy against silicon is an explicit
# non-goal of this first cut (issue #216 "Non-goals"). `metals` is
# index-aligned with EXTRACTION_DECK's `metals` stack: index 0 is li1 (local
# interconnect -- relatively high sheet R), index 1 is met1 (low sheet R),
# index 2 is met2 (issue #508's third connectivity level), indices 3-5 are
# met3/met4/met5 (issue #619's extension to the full li1-through-met5 stack
# -- without these three entries, extending `EXTRACTION_DECK.metals` alone
# would have regressed sky130 from a fully-populated `PARASITICS.metals`
# table back into the same "metal level with no R/C coefficient" gap #547
# fixed for gf180mcu, silently zeroing out `--parasitics`'s R/C for any net
# routed on met3 or higher).
# Values expressed as (sheet ohms/square, area cap fF/um^2, fringe cap fF/um).
PARASITICS = ParasiticsDeck(
    # No diffusion role (issue #226): `klt extract`'s M cards already carry
    # AS/AD/PS/PD, and the SPICE device model turns those into junction
    # capacitance, so an area/perimeter cap term on the source/drain diffusion
    # here would double-count it. sky130.tech comments its own active-layer
    # cap out for the same reason (`# Rely on device models to capture *ndiff
    # area cap`).
    diffusion=None,
    # poly gate/interconnect. sheet: `resist (allpolynonres)`; perim from
    # sky130.tech `defaultperimeter` for poly (55.27 aF/um).
    poly=LayerRC(sheet_res_ohm_sq=48.2, cap_area_ff_um2=0.107, cap_perim_ff_um=0.05527),
    metals=(
        # li1 local interconnect. perim from `defaultperimeter` li1 (40.7 aF/um).
        LayerRC(sheet_res_ohm_sq=12.8, cap_area_ff_um2=0.037, cap_perim_ff_um=0.0407),
        # met1. area/perim from `defaultareacap`/`defaultperimeter` met1
        # (25.78 aF/um^2, 40.57 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.125, cap_area_ff_um2=0.02578, cap_perim_ff_um=0.04057
        ),
        # met2 (issue #508). sheet: `resist (allm2)/metal2` in the same
        # nominal `variants (),(orig),(si)` block as met1 above (125
        # milliohms/sq = 0.125 ohm/sq -- identical to met1's own value in
        # that block); area/perim from the matching nominal
        # `defaultareacap`/`defaultperimeter allm2 metal2` entries (17.5
        # aF/um^2, 37.76 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.125, cap_area_ff_um2=0.0175, cap_perim_ff_um=0.03776
        ),
        # met3 (issue #619). sheet: `resist (allm3)/metal3` in the same
        # nominal `variants (),(orig),(si)` block as met1/met2 above (47
        # milliohms/sq = 0.047 ohm/sq); area/perim from the matching nominal,
        # non-RERAM `defaultareacap`/`defaultperimeter allm3 metal3` entries
        # (12.37 aF/um^2, 40.99 aF/um -- the `#else (!RERAM)` branch, the
        # same branch met1/met2's own values above were transcribed from).
        LayerRC(
            sheet_res_ohm_sq=0.047, cap_area_ff_um2=0.01237, cap_perim_ff_um=0.04099
        ),
        # met4 (issue #619). sheet: `resist (allm4)/metal4`, same nominal
        # block and `#ifdef METAL5` guard sky130.tech wraps met4/met5 in (47
        # milliohms/sq = 0.047 ohm/sq -- identical to met3's own value in
        # that block); area/perim from the matching nominal, non-RERAM
        # `defaultareacap`/`defaultperimeter allm4 metal4` entries (8.42
        # aF/um^2, 36.68 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.047, cap_area_ff_um2=0.00842, cap_perim_ff_um=0.03668
        ),
        # met5 (issue #619). sheet: `resist (allm5)/metal5`, same nominal
        # block (29 milliohms/sq = 0.029 ohm/sq); area/perim from the
        # matching nominal, non-RERAM `defaultareacap`/`defaultperimeter
        # allm5 metal5` entries (6.32 aF/um^2, 38.85 aF/um).
        LayerRC(
            sheet_res_ohm_sq=0.029, cap_area_ff_um2=0.00632, cap_perim_ff_um=0.03885
        ),
    ),
)
